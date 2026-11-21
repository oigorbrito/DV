"""ToolExecutor: 4 primitivas de ferramenta com retorno estruturado (Módulo 2).

Primitivas: ``read_file``, ``write_file``, ``edit_file`` (search/replace exato) e ``run_bash``.
Cada uma retorna ``ToolResult``. Falhas esperadas (arquivo ausente, binário, ``old_text``
ausente ou ambíguo, timeout, exit code != 0, caminho inválido ou fora do workspace) viram
``ToolResult(ok=False)``. Erros de programação ainda levantam exceção.

Dois textos por resultado (protocolo p1 v2, A6):
- ``output``: versão truncada, que vai para o modelo.
- ``raw_output``: saída completa, sem truncamento, que vai para o registro de auditoria.

Ambiente do subprocesso (B6): allowlist estrita. Nenhuma variável do host fora da allowlist
chega ao comando, inclusive chaves de API. Ver ``ENV_ALLOWLIST``.

Trava de workspace: ``path`` e ``cwd`` são resolvidos com ``.resolve()`` (seguindo links
simbólicos) e precisam ficar dentro de ``root``. Byte nulo e caminhos que não resolvem
também viram ``ToolResult(ok=False)``.

Não é sandbox. O texto de um ``run_bash`` pode acessar qualquer caminho do sistema
(``cat /etc/passwd``). O benchmark deve rodar em workspace isolado (container ou worktree).

Atribuições empíricas
---------------------
- Conjunto de 4 primitivas: ``earendil-works/pi`` (https://github.com/earendil-works/pi),
  ``dist/core/tools/index.js:17`` (conjunto completo de 7 nomes). Este módulo expõe
  ``read``, ``bash``, ``edit`` e ``write``.
- ``run_bash`` stateless com ``Popen`` em grupo próprio e ``killpg`` no timeout:
  ``SWE-agent/mini-swe-agent`` (https://github.com/SWE-agent/mini-swe-agent),
  ``src/minisweagent/environments/local.py:74-91``. Timeout padrão de 30 s em ``local.py:16``.
- Variáveis não interativas (``PAGER``, ``MANPAGER``, ``LESS``, ``PIP_PROGRESS_BAR``,
  ``TQDM_DISABLE``): ``config/default.yaml:107-112`` do mini.
- Saída completa preservada: ``pi`` grava em arquivo temporário
  (``dist/core/tools/output-accumulator.js:87-93``) e o mini em ``extra.raw_output``
  (``models/utils/actions_toolcall.py:98``).
- Truncamento de saída: limite por caracteres, cabeça e cauda. Referência: mini
  ``default.yaml:119-140`` e pi ``truncate.js:10-11``. Ver divergências em ``docs/ARCHITECTURE.md``.
- Substituição exata com contagem única: alinhada ao ``pi`` (``edit.js:12-14``), sem busca fuzzy
  (fuzzy e multi-edit ficam para ablação futura).
- Normalização de fim de linha: ``pi`` ``edit-diff.js:8-22``. Este módulo normaliza só
  arquivos inteiramente CRLF, para não alterar linhas que o edit não toca.
- SWE-agent (NeurIPS 2024, https://arxiv.org/abs/2405.15793): feedback conciso e truncamento.
"""

from __future__ import annotations

import contextlib
import copy
import difflib
import hashlib
import os
import re
import signal
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dv_harness.variants import TAIL_HEAD_RATIO, VARIANT_B0_PLUS, check_variant

DEFAULT_MAX_OUTPUT_CHARS = 10_000
DEFAULT_TIMEOUT_S = 30
_BINARY_PROBE_BYTES = 8192

# Única fonte de variáveis herdadas do host pelo subprocesso (B6). Sem chaves de API.
ENV_ALLOWLIST: tuple[str, ...] = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TERM", "USER")

# Variáveis não interativas (default.yaml:107-112 do mini, mais GIT_PAGER).
NON_INTERACTIVE_ENV: dict[str, str] = {
    "PAGER": "cat",
    "MANPAGER": "cat",
    "GIT_PAGER": "cat",
    "LESS": "-R",
    "PIP_PROGRESS_BAR": "off",
    "TQDM_DISABLE": "1",
}


class PathError(ValueError):
    """Caminho ou ``cwd`` inválido para o workspace (byte nulo, não resolve, fora da raiz)."""


class WorkspaceEscapeError(PathError):
    """Caminho ou ``cwd`` resolvido fora de ``root``."""


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str = ""
    error: str | None = None
    exit_code: int | None = None
    truncated: bool = False
    raw_output: str = ""
    timed_out: bool = False


def truncate_output(text: str, limit: int) -> tuple[str, bool]:
    """Limita ``text`` a ``limit`` caracteres, mantendo início e fim. O marcador conta no limite."""
    if limit < 0:
        raise ValueError("limit must be >= 0")
    if len(text) <= limit:
        return text, False
    marker_template = "\n[... {omitted} caracteres omitidos ...]\n"
    reserve = len(marker_template.format(omitted=len(text)))
    if limit <= reserve:
        return text[:limit], True
    budget = limit - reserve
    head = budget // 2
    tail = budget - head
    omitted = len(text) - head - tail
    marker = marker_template.format(omitted=omitted)
    return text[:head] + marker + (text[-tail:] if tail else ""), True


def truncate_tail_priority(text: str, limit: int) -> tuple[str, bool]:
    """Truncamento de cauda (b0_plus): 20% do orçamento no início, 80% no fim.

    O fim concentra erros e tracebacks; o início preserva o comando e o cabeçalho da saída.
    O marcador conta no limite, como em ``truncate_output``.
    """
    if limit < 0:
        raise ValueError("limit must be >= 0")
    if len(text) <= limit:
        return text, False
    marker_template = "\n[... {omitted} caracteres omitidos (tail_priority) ...]\n"
    reserve = len(marker_template.format(omitted=len(text)))
    if limit <= reserve:
        return (text[-limit:] if limit else ""), True
    budget = limit - reserve
    head = int(budget * TAIL_HEAD_RATIO)
    tail = budget - head
    omitted = len(text) - head - tail
    marker = marker_template.format(omitted=omitted)
    return text[:head] + marker + (text[-tail:] if tail else ""), True


_ELEMENT_RE = re.compile(r"[^\n]*\n|[^\n]+")


def _elements(text: str) -> list[str]:
    """Linhas com o terminador de linha incluído (o último pode não ter)."""
    return _ELEMENT_RE.findall(text)


def _strip_terminator(element: str) -> str:
    return element[:-1] if element.endswith("\n") else element


def _fit_terminator(new_text: str, ends_nl: bool) -> str:
    """Ajusta a quebra final de ``new_text`` ao bloco original. Substituir por vazio remove o bloco."""
    if new_text == "":
        return ""
    body = new_text[:-1] if new_text.endswith("\n") else new_text
    return body + ("\n" if ends_nl else "")


def _line_windows(body: str, old: str, norm: Any) -> list[tuple[int, int]]:
    """Janelas ``(início, tamanho)`` de linhas inteiras cujo texto normalizado casa com ``old``."""
    old_elems = _elements(old)
    body_elems = _elements(body)
    k = len(old_elems)
    if k == 0 or k > len(body_elems):
        return []
    old_norm = [norm(_strip_terminator(e)) for e in old_elems]
    return [(i, k) for i in range(len(body_elems) - k + 1)
            if all(norm(_strip_terminator(body_elems[i + j])) == old_norm[j] for j in range(k))]


def _crlf_normalised(text: str, old_text: str, new_text: str) -> tuple[bool, str, str, str]:
    """Arquivo inteiramente CRLF: busca e substituição em LF. Devolve ``(puro_crlf, corpo, antigo, novo)``."""
    crlf = text.count("\r\n")
    pure_crlf = crlf > 0 and text.count("\n") == crlf
    if pure_crlf:
        return True, text.replace("\r\n", "\n"), old_text.replace("\r\n", "\n"), new_text.replace("\r\n", "\n")
    return False, text, old_text, new_text


def _min_indent(bodies: list[str]) -> int | None:
    """Menor recuo, em espaços, das linhas não vazias. ``None`` se todas estão vazias."""
    indents = [len(b) - len(b.lstrip(" ")) for b in bodies if b.strip()]
    return min(indents) if indents else None


def _reindent_block(new: str, target: int) -> str:
    """Desloca ``new`` para que seu menor recuo seja ``target``, preservando a estrutura interna.

    O deslocamento é ``target - recuo_de_new``. Quando ``new`` já está no recuo do arquivo, nada muda (sem
    deslocamento duplo). Para deslocamento negativo, só se remove espaço à esquerda, nunca código. Linhas
    vazias ficam como estão. Vazio fica vazio (remoção limpa). Terminadores de linha são preservados.
    """
    elements = _elements(new)
    bodies = [_strip_terminator(e) for e in elements]
    base = _min_indent(bodies)
    if base is None or base == target:
        return new
    shift = target - base
    out = []
    for element, body in zip(elements, bodies, strict=True):
        if body.strip():
            if shift > 0:
                body = " " * shift + body
            else:
                body = body[min(-shift, len(body) - len(body.lstrip(" "))):]
        out.append(body + ("\n" if element.endswith("\n") else ""))
    return "".join(out)


# --- edição nivelada (b0_plus): cadeia de 7 estágios, cada um exigindo match único ----------
# Referências: Pi edit-diff.ts (normalizeForFuzzyMatch, preservação de linhas não tocadas);
# OpenCode edit.ts (EscapeNormalized, TrimmedBoundary, BlockAnchor com limiar 0.65 e guarda de desproporção).

BLOCK_ANCHOR_THRESHOLD = 0.65  # OpenCode edit.ts:220
_NUMBERED_PREFIX_RE = re.compile(r"^\d+\t")  # prefixo "N<TAB>" que read_file devolve
_ESCAPE_RE = re.compile(r"\\([ntr\"'\\])")
_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "'": "'", "\\": "\\"}
_UNICODE_MAP = {
    **{ord(c): "'" for c in "\u2018\u2019\u201a\u201b"},
    **{ord(c): '"' for c in "\u201c\u201d\u201e\u201f"},
    **{ord(c): "-" for c in "\u2010\u2011\u2012\u2013\u2014\u2015\u2212"},
    **{ord(c): " " for c in "\u00a0\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u202f\u205f\u3000"},
}


class _AmbiguousMatch(Exception):
    """Mais de um match em um estágio: erro imediato, sem avançar para o próximo estágio."""

    def __init__(self, count: int) -> None:
        super().__init__(count)
        self.count = count


def _unescape(text: str) -> str:
    return _ESCAPE_RE.sub(lambda m: _ESCAPES[m.group(1)], text)


def _norm_unicode(line: str) -> str:
    """NFKC, aspas curvas, traços Unicode e espaços especiais -> ASCII; sem espaço final (Pi)."""
    return unicodedata.normalize("NFKC", line).translate(_UNICODE_MAP).rstrip()


def _levenshtein(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _line_similarity(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return 1.0 - _levenshtein(a, b) / max(len(a), len(b))


def _trim_blank_edges(text: str) -> str:
    elems = _elements(text)
    start, end = 0, len(elems)
    while start < end and not _strip_terminator(elems[start]).strip():
        start += 1
    while end > start and not _strip_terminator(elems[end - 1]).strip():
        end -= 1
    return "".join(elems[start:end])


def _strip_numbered(body: str, old: str, new: str) -> tuple[str, str]:
    """Remove o prefixo ``N<TAB>`` de old (e de new, se também vier numerado). Não mexe se o arquivo tem o prefixo."""
    old_lines = [line for line in old.split("\n") if line.strip()]
    if not old_lines or not all(_NUMBERED_PREFIX_RE.match(line) for line in old_lines):
        return old, new
    if old in body:  # o arquivo contém o texto literal com prefixo: não é acidente
        return old, new

    def strip(text: str) -> str:
        return "\n".join(_NUMBERED_PREFIX_RE.sub("", line) for line in text.split("\n"))

    new_lines = [line for line in new.split("\n") if line.strip()]
    if new_lines and all(_NUMBERED_PREFIX_RE.match(line) for line in new_lines):
        return strip(old), strip(new)
    return strip(old), new


def _lines_stage(body: str, old: str, new: str, norm: Any, *, reindent: bool = False) -> str | None:
    windows = _line_windows(body, old, norm)
    if len(windows) > 1:
        raise _AmbiguousMatch(len(windows))
    if not windows:
        return None
    i, k = windows[0]
    elems = _elements(body)
    block = new
    if reindent:
        target = _min_indent([_strip_terminator(e) for e in elems[i:i + k]])
        if target is not None:
            block = _reindent_block(new, target)
    ends_nl = elems[i + k - 1].endswith("\n")
    return "".join(elems[:i]) + _fit_terminator(block, ends_nl) + "".join(elems[i + k:])


def _exact_stage(body: str, old: str, new: str) -> str | None:
    if not old:
        return None
    count = body.count(old)
    if count > 1:
        raise _AmbiguousMatch(count)
    if count == 0:
        return None
    return body.replace(old, _fit_terminator(new, old.endswith("\n")), 1)


def _line_trimmed_stage(body: str, old: str, new: str) -> str | None:
    return _lines_stage(body, old, new, str.rstrip)


def _trimmed_boundary_stage(body: str, old: str, new: str) -> str | None:
    trimmed = _trim_blank_edges(old)
    if trimmed == old or not trimmed:
        return None
    return _lines_stage(body, trimmed, new, str.rstrip)


def _indentation_stage(body: str, old: str, new: str) -> str | None:
    return _lines_stage(body, old, new, str.strip, reindent=True)


def _escape_stage(body: str, old: str, new: str) -> str | None:
    un_old = _unescape(old)
    if un_old == old:
        return None
    un_new = _unescape(new)  # o modelo escapou o bloco inteiro: new também vem escapado
    for stage in (_exact_stage, _line_trimmed_stage):
        hit = stage(body, un_old, un_new)
        if hit is not None:
            return hit
    return None


def _unicode_stage(body: str, old: str, new: str) -> str | None:
    return _lines_stage(body, old, new, _norm_unicode)


def _block_similarity(body_lines: list[str], old_lines: list[str]) -> float:
    """Média da similaridade das linhas internas, alinhadas por índice (OpenCode BlockAnchor)."""
    middle = min(len(old_lines), len(body_lines)) - 2
    total = sum(_line_similarity(body_lines[j].strip(), old_lines[j].strip()) for j in range(1, middle + 1))
    return total / middle


def _block_anchor_candidates(body: str, old: str) -> list[tuple[int, int]]:
    old_lines = [_strip_terminator(e) for e in _elements(old)]
    k = len(old_lines)
    body_lines = [_strip_terminator(e) for e in _elements(body)]
    first, last = old_lines[0].strip(), old_lines[-1].strip()
    delta = max(1, k // 4)  # floor(0.25 * k), como OpenCode
    found: list[tuple[int, int]] = []
    for i in range(len(body_lines)):
        if body_lines[i].strip() != first:
            continue
        best: tuple[float, int] | None = None
        for size in range(max(3, k - delta), k + delta + 1):
            j = i + size - 1
            if j >= len(body_lines):
                break
            if body_lines[j].strip() != last or size >= max(k + 3, 2 * k):  # guarda de desproporção
                continue
            sim = _block_similarity(body_lines[i:i + size], old_lines)
            if sim >= BLOCK_ANCHOR_THRESHOLD and (best is None or sim > best[0]):
                best = (sim, size)
        if best is not None:
            found.append((i, best[1]))
    return found


def _block_anchor_stage(body: str, old: str, new: str) -> str | None:
    if len(_elements(old)) < 3:
        return None
    found = _block_anchor_candidates(body, old)
    if len(found) > 1:
        raise _AmbiguousMatch(len(found))
    if not found:
        return None
    i, size = found[0]
    elems = _elements(body)
    target = _min_indent([_strip_terminator(e) for e in elems[i:i + size]])
    block = _reindent_block(new, target) if target is not None else new
    ends_nl = elems[i + size - 1].endswith("\n")
    return "".join(elems[:i]) + _fit_terminator(block, ends_nl) + "".join(elems[i + size:])


_STAGES: tuple[tuple[str, Any], ...] = (
    ("exact", _exact_stage),
    ("line_trimmed", _line_trimmed_stage),
    ("trimmed_boundary", _trimmed_boundary_stage),
    ("indentation_flexible", _indentation_stage),
    ("escape_normalized", _escape_stage),
    ("unicode_normalized", _unicode_stage),
    ("block_anchor", _block_anchor_stage),
)


CLOSEST_MATCH_THRESHOLD = 0.6  # aider editblock_coder.py:602 (find_similar_lines)
CLOSEST_MATCH_CONTEXT = 5  # linhas de contexto antes e depois da janela (aider usa N = 5)


def _closest_match_window(body: str, old: str) -> str | None:
    """Janela de linhas do arquivo mais parecida com ``old`` (como aider find_similar_lines), ou ``None``.

    Compara janelas do mesmo tamanho de ``old`` com ``SequenceMatcher``. Só devolve uma janela se a razão
    atingir o limiar. A janela sai com ``CLOSEST_MATCH_CONTEXT`` linhas de contexto antes e depois.
    """
    search = old.splitlines()
    lines = body.splitlines()
    size = len(search)
    if not search or size > len(lines):
        return None
    best_ratio, best_start = 0.0, -1
    for start in range(len(lines) - size + 1):
        ratio = difflib.SequenceMatcher(None, search, lines[start:start + size]).ratio()
        if ratio > best_ratio:
            best_ratio, best_start = ratio, start
    if best_start < 0 or best_ratio < CLOSEST_MATCH_THRESHOLD:
        return None
    first = max(0, best_start - CLOSEST_MATCH_CONTEXT)
    last = min(len(lines), best_start + size + CLOSEST_MATCH_CONTEXT)
    return "\n".join(lines[first:last])


def _not_found_message(body: str, old: str, new: str, path: str) -> str:
    """Mensagem de ``old_text`` não encontrado: pista de trecho parecido e aviso de ``new_text`` já presente."""
    message = f"old_text not found in {path}"
    window = _closest_match_window(body, old)
    if window is not None:
        message += f". Did you mean to match these actual lines in {path}?\n{window}"
    if new.strip() and new in body:
        message += f"\nNote: new_text already appears in {path}; verify whether this edit was already applied."
    return message


def _apply_edit_with_fallback(text: str, old_text: str, new_text: str, path: str
                              ) -> tuple[str | None, str | None, ToolResult | None]:
    """b0_plus: cadeia de 7 estágios. Cada estágio exige exatamente um match.

    Devolve ``(texto_novo, estágio, erro)``. Ambiguidade em um estágio é erro imediato. Levanta
    ``ValueError`` se o resultado for idêntico ao original (no-op). Terminação de linha (CRLF ou LF)
    e a presença ou ausência de quebra final do bloco original são preservadas.
    """
    pure_crlf, body, old, new = _crlf_normalised(text, old_text, new_text)
    old, new = _strip_numbered(body, old, new)
    for name, stage in _STAGES:
        try:
            updated = stage(body, old, new)
        except _AmbiguousMatch as amb:
            return None, name, _ambiguous(amb.count, name, path)
        if updated is not None:
            if pure_crlf:
                updated = updated.replace("\n", "\r\n")
            if updated == text:
                raise ValueError("No changes applied: replacement produces identical content.")
            return updated, name, None
    return None, None, ToolResult(ok=False, error=_not_found_message(body, old, new, path))


def _ambiguous(count: int, stage: str, path: str) -> ToolResult:
    return ToolResult(ok=False, error=f"old_text is ambiguous: {count} matches in {path} ({stage}); "
                                      "include more context")


def _unified_diff(path: str, before: str, after: str) -> str:
    """Diff unificado com 4 linhas de contexto (n=4), para o modelo ver o efeito da edição."""
    a = [line if line.endswith("\n") else line + "\n" for line in _elements(before)]
    b = [line if line.endswith("\n") else line + "\n" for line in _elements(after)]
    return "".join(difflib.unified_diff(a, b, fromfile=f"a/{path}", tofile=f"b/{path}", n=4))


def split_lines(text: str) -> list[str]:
    """Divide estritamente em ``\\n``. Não usa ``splitlines()``, que quebra em \\x0c, \\x0b, \\u2028 etc.

    Um ``\\n`` final não gera linha vazia, como no ``grep -n`` e nos editores.
    """
    if text == "":
        return []
    parts = text.split("\n")
    if text.endswith("\n"):
        parts = parts[:-1]
    return parts


def build_env(source: dict[str, str] | None = None) -> dict[str, str]:
    """Ambiente do subprocesso: só a allowlist do host, mais as variáveis não interativas."""
    host = os.environ if source is None else source
    env = {k: host[k] for k in ENV_ALLOWLIST if k in host}
    env.update(NON_INTERACTIVE_ENV)
    return env


class ToolExecutor:
    """Executa as 4 primitivas. Caminhos relativos são resolvidos a partir de ``root``."""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        max_output_chars: int = DEFAULT_MAX_OUTPUT_CHARS,
        harness_variant: str = "b0",
        full_output_dir: str | os.PathLike[str] | None = None,
    ) -> None:
        self.root = (Path(root) if root is not None else Path.cwd()).resolve()
        if full_output_dir is not None:
            external = Path(full_output_dir).resolve()
            if external == self.root or external.is_relative_to(self.root):
                raise ValueError("full_output_dir must be outside the workspace root")
            self.full_output_dir: Path | None = external
        else:
            self.full_output_dir = None
        self.max_output_chars = max_output_chars
        self.harness_variant = check_variant(harness_variant)

    def _readable_spill(self) -> Path | None:
        """Diretório de saída completa que ``read_file`` pode ler. Só em b0_plus; ``None`` nos demais casos."""
        if self.harness_variant != VARIANT_B0_PLUS or self.full_output_dir is None:
            return None
        return self.full_output_dir.resolve()

    def _resolve_for_read(self, path: str) -> Path:
        """Resolve para LEITURA. Em b0_plus também aceita arquivos dentro de ``full_output_dir`` (só leitura).

        Escrita e edição continuam usando ``_resolve``, que aceita apenas a raiz do workspace. Dentro do
        diretório de saída, cada componente do caminho é checado: symlink é recusado. Caminho que escapa
        do diretório cai no confinamento normal da raiz e é recusado do mesmo jeito.
        """
        spill = self._readable_spill()
        if spill is None:
            return self._resolve(path)
        if "\x00" in path:
            raise PathError("invalid path: contains NUL byte")
        raw = Path(path)
        candidate = Path(os.path.normpath(raw if raw.is_absolute() else self.root / raw))
        try:
            relative = candidate.relative_to(spill)
        except ValueError:
            return self._resolve(path)
        if not relative.parts:
            raise WorkspaceEscapeError(f"path is not a file inside the full output directory: {path}")
        current = spill
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise WorkspaceEscapeError(f"symlinks are not readable in the full output directory: {path}")
        return current

    def _resolve(self, path: str) -> Path:
        """Resolve ``path`` contra ``root``. Levanta ``PathError`` (ou subclasse) se inválido."""
        if "\x00" in path:
            raise PathError("invalid path: contains NUL byte")
        p = Path(path)
        try:
            candidate = (p if p.is_absolute() else self.root / p).resolve()
        except (OSError, RuntimeError) as exc:  # loop de link simbólico, etc.
            raise PathError(f"path cannot be resolved: {path}: {exc}") from exc
        if candidate != self.root and not candidate.is_relative_to(self.root):
            raise WorkspaceEscapeError(f"path escapes workspace root: {path}")
        # Path.exists/is_file levantam OSError para ENAMETOOLONG e ENOTDIR. Sondar aqui torna
        # essas falhas ToolResult, e não exceção. Ausência (FileNotFoundError) é normal.
        try:
            os.stat(candidate)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise PathError(f"path is not usable: {path}: {exc.strerror or exc}") from exc
        return candidate

    # --- read_file -----------------------------------------------------------

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> ToolResult:
        """Lê arquivo texto. Linhas são 1-based e inclusivas, numeradas ``N\\tlinha``."""
        if start_line is not None and start_line < 1:
            return ToolResult(ok=False, error="start_line must be >= 1")
        if end_line is not None and start_line is not None and end_line < start_line:
            return ToolResult(ok=False, error="end_line must be >= start_line")

        try:
            target = self._resolve_for_read(path)
        except PathError as exc:
            return ToolResult(ok=False, error=str(exc))
        if not target.exists():
            return ToolResult(ok=False, error=f"file not found: {path}")
        if target.is_dir():
            return ToolResult(ok=False, error=f"is a directory: {path}")

        try:
            raw = target.read_bytes()
        except OSError as exc:
            return ToolResult(ok=False, error=f"read failed: {exc.strerror or exc}")

        if b"\x00" in raw[:_BINARY_PROBE_BYTES]:
            return ToolResult(ok=False, error=f"binary file, not read as text: {path}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(ok=False, error=f"not valid UTF-8 text: {path}")

        lines = split_lines(text)
        first = (start_line or 1) - 1
        last = len(lines) if end_line is None else min(end_line, len(lines))
        if first > len(lines) and lines:
            return ToolResult(ok=False, error=f"start_line {start_line} beyond end of file ({len(lines)} lines)")
        numbered = "\n".join(f"{i + 1}\t{lines[i]}" for i in range(first, last))
        out, truncated = self._read_output(numbered, first + 1, len(lines))
        return ToolResult(ok=True, output=out, truncated=truncated, raw_output=numbered)

    def _read_output(self, numbered: str, start: int, total: int) -> tuple[str, bool]:
        """b0: cabeça e cauda por caracteres. b0_plus: cabeça com pista de continuação (start_line)."""
        if self.harness_variant == VARIANT_B0_PLUS:
            return _window_with_continuation(numbered, start, total, self.max_output_chars)
        return truncate_output(numbered, self.max_output_chars)

    # --- write_file ----------------------------------------------------------

    def write_file(self, path: str, content: str) -> ToolResult:
        """Cria ou sobrescreve arquivo texto UTF-8, criando diretórios pai se necessário."""
        try:
            target = self._resolve(path)
        except PathError as exc:
            return ToolResult(ok=False, error=str(exc))
        if target.is_dir():
            return ToolResult(ok=False, error=f"is a directory: {path}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8", newline="")
        except OSError as exc:
            return ToolResult(ok=False, error=f"write failed: {exc.strerror or exc}")
        message = f"wrote {len(content.encode('utf-8'))} bytes to {path}"
        return ToolResult(ok=True, output=message, raw_output=message)

    # --- edit_file -----------------------------------------------------------

    def edit_file(self, path: str, old_text: str, new_text: str) -> ToolResult:
        """Substitui ``old_text`` por ``new_text``. Falha se não existir ou aparecer mais de uma vez.

        Arquivo inteiramente CRLF: ``old_text`` e ``new_text`` são normalizados para LF na busca
        e o fim de linha CRLF é restaurado no resultado. Arquivo misto ou LF: busca exata.
        """
        if not old_text:
            return ToolResult(ok=False, error="old_text must not be empty")
        try:
            target = self._resolve(path)
        except PathError as exc:
            return ToolResult(ok=False, error=str(exc))
        if not target.is_file():
            return ToolResult(ok=False, error=f"file not found: {path}")
        try:
            raw = target.read_bytes()
        except OSError as exc:
            return ToolResult(ok=False, error=f"read failed: {exc.strerror or exc}")
        if b"\x00" in raw[:_BINARY_PROBE_BYTES]:
            return ToolResult(ok=False, error=f"binary file, not edited: {path}")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ToolResult(ok=False, error=f"not valid UTF-8 text: {path}")

        updated, problem = self._replace_for_variant(text, old_text, new_text, path)
        if problem is not None:
            return problem
        try:
            target.write_text(updated, encoding="utf-8", newline="")
        except OSError as exc:
            return ToolResult(ok=False, error=f"write failed: {exc.strerror or exc}")
        message = self._edit_message(path, text, updated)
        return ToolResult(ok=True, output=message, raw_output=message)

    # --- run_bash ------------------------------------------------------------

    def _replace_for_variant(self, text: str, old_text: str, new_text: str, path: str) -> tuple[str | None, ToolResult | None]:
        """b0: substituição única literal. b0_plus: fallback de 3 estágios com unicidade estrita."""
        if self.harness_variant == VARIANT_B0_PLUS:
            try:
                updated, _stage, problem = _apply_edit_with_fallback(text, old_text, new_text, path)
            except ValueError as exc:  # no-op: nada a aplicar
                return None, ToolResult(ok=False, error=str(exc))
            return updated, problem
        return _apply_unique_replacement(text, old_text, new_text, path)

    def _edit_message(self, path: str, before: str, after: str) -> str:
        """b0: 'edited <path>'. b0_plus: o mesmo cabeçalho mais o diff unificado (n=4)."""
        if self.harness_variant == VARIANT_B0_PLUS:
            return f"edited {path}\n" + _unified_diff(path, before, after)
        return f"edited {path}"

    def _truncate_command_output(self, text: str) -> tuple[str, bool]:
        """b0: início e fim 50/50. b0_plus: cauda priorizada (20/80). ``raw_output`` nunca é truncado."""
        if self.harness_variant == VARIANT_B0_PLUS:
            out, truncated = truncate_tail_priority(text, self.max_output_chars)
            if truncated and self.full_output_dir is not None:
                out = f"{out}\n[Full output saved to: {self._save_full_output(text)}]"
            return out, truncated
        return truncate_output(text, self.max_output_chars)

    def _save_full_output(self, text: str) -> str:
        """Grava a saída completa FORA do workspace (nunca altera diffs nem oráculos). Devolve o caminho."""
        assert self.full_output_dir is not None
        self.full_output_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        target = self.full_output_dir / f"bash-{digest}.txt"
        target.write_bytes(text.encode("utf-8"))
        return str(target)

    def run_bash(
        self,
        command: str,
        timeout: float = DEFAULT_TIMEOUT_S,
        cwd: str | os.PathLike[str] | None = None,
    ) -> ToolResult:
        """Executa ``command`` em um shell novo, sem estado entre chamadas."""
        if timeout <= 0:
            raise ValueError("timeout must be > 0")
        if cwd is not None:
            try:
                workdir = self._resolve(str(cwd))
            except PathError as exc:
                return ToolResult(ok=False, error=str(exc))
        else:
            workdir = self.root
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(workdir),
                env=build_env(),
                start_new_session=True,
            )
        except OSError as exc:
            return ToolResult(ok=False, error=f"could not start command: {exc.strerror or exc}")

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            stdout, stderr = _communicate_after_kill(proc)
            full = _decode(stdout) + _decode(stderr)
            out, truncated = self._truncate_command_output(full)
            return ToolResult(
                ok=False,
                output=out,
                error=f"timeout after {timeout}s",
                exit_code=None,
                truncated=truncated,
                raw_output=full,
                timed_out=True,
            )

        combined = _decode(stdout)
        err_text = _decode(stderr)
        if err_text:
            combined = f"{combined}\n[stderr]\n{err_text}" if combined else f"[stderr]\n{err_text}"
        out, truncated = self._truncate_command_output(combined)
        ok = proc.returncode == 0
        return ToolResult(
            ok=ok,
            output=out,
            error=None if ok else f"exit code {proc.returncode}",
            exit_code=proc.returncode,
            truncated=truncated,
            raw_output=combined,
        )



def _apply_unique_replacement(text: str, old_text: str, new_text: str, path: str) -> tuple[str | None, ToolResult | None]:
    """Aplica a substituição única. Devolve ``(texto_novo, None)`` ou ``(None, erro)``.

    Arquivo inteiramente CRLF: busca e substituição em LF, com CRLF restaurado no resultado.
    """
    pure_crlf, body, needle, replacement = _crlf_normalised(text, old_text, new_text)
    count = body.count(needle)
    if count == 0:
        return None, ToolResult(ok=False, error=f"old_text not found in {path}")
    if count > 1:
        return None, ToolResult(
            ok=False,
            error=f"old_text is ambiguous: {count} occurrences in {path}; include more context",
        )
    updated = body.replace(needle, replacement, 1)
    if pure_crlf:
        updated = updated.replace("\n", "\r\n")
    return updated, None

POST_KILL_TIMEOUT_S = 5  # limite do communicate depois do kill (um neto pode segurar os pipes)


def _is_posix() -> bool:
    return os.name == "posix"


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Mata a árvore de processos do comando. Ignora se já terminou.

    POSIX: mata o grupo inteiro (``start_new_session``). Windows: ``taskkill /F /T`` mata a árvore;
    sem ele só o shell morre e processos netos podem continuar segurando stdout e stderr.
    """
    if _is_posix():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError):
            pass
    else:
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           check=False, timeout=10)
    with contextlib.suppress(OSError):
        proc.kill()


def _communicate_after_kill(proc: subprocess.Popen[bytes]) -> tuple[bytes, bytes]:
    """Lê o que sobrou depois do kill, com limite. Se um neto ainda segura os pipes, fecha e segue."""
    try:
        return proc.communicate(timeout=POST_KILL_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()
        return b"", b""


def _decode(data: bytes | str | None) -> str:
    if data is None:
        return ""
    if isinstance(data, str):
        return data
    return data.decode("utf-8", errors="replace")


# Esquemas JSON para tool calling (formato OpenAI, aceito pelo LiteLLM).
TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a UTF-8 text file. Output lines are numbered. Use start_line/end_line (1-based, inclusive) to read a range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path."},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a UTF-8 text file. Parent directories are created.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace one exact occurrence of old_text with new_text in a file. Fails if old_text is missing or appears more than once; include more context to make it unique.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_text": {"type": "string", "description": "Exact text to replace, copied from the file."},
                    "new_text": {"type": "string"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Run a shell command in a new, stateless shell. Each call is independent. Output is truncated when long.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "timeout": {"type": "number", "description": f"Seconds. Default {DEFAULT_TIMEOUT_S}."},
                    "cwd": {"type": "string", "description": "Working directory. Default is the executor root."},
                },
                "required": ["command"],
            },
        },
    },
]


# --- variante b0_plus: schemas com regras de uso, leitura com continuação --------------------

B0_PLUS_READ_MAX_LINES = 2000  # mesmo teto de linhas do Pi/OpenCode (read.txt:5)
_CONTINUATION_RESERVE = 160  # caracteres reservados para a pista de continuação

_B0_PLUS_DESCRIPTIONS: dict[str, str] = {
    "read_file": (
        "Read a UTF-8 text file. Output lines are numbered as N<TAB>text; the number is not part of the file, "
        "so never copy it into edit_file old_text. Use start_line/end_line (1-based, inclusive) to read a range. "
        "If the output says it is truncated, continue with start_line set to the line number it names."
    ),
    "write_file": (
        "Create a new UTF-8 text file or completely rewrite one. Parent directories are created. "
        "To change an existing file, prefer edit_file."
    ),
    "edit_file": (
        "Replace one occurrence of old_text with new_text in a file. Copy old_text exactly from the file, "
        "WITHOUT the line-number prefix that read_file prints. It must match exactly one place: include enough "
        "surrounding lines to make it unique. Prefer this over write_file for existing files. Fails if old_text "
        "is missing, ambiguous, or the result would be identical to the original."
    ),
    "run_bash": (
        "Run a shell command in a new, stateless shell. Output is truncated when long; when it is, the full output "
        "is saved to a file outside the workspace and its path is given. To read or edit files, prefer read_file "
        "and edit_file over cat, sed or heredocs."
    ),
}


def get_tool_schemas(variant: str = "b0") -> list[dict[str, Any]]:
    """Schemas enviados ao modelo. b0: os atuais, sem alteração. b0_plus: descrições com regras de uso."""
    check_variant(variant)
    if variant != VARIANT_B0_PLUS:
        return TOOL_SCHEMAS
    schemas = copy.deepcopy(TOOL_SCHEMAS)
    for schema in schemas:
        schema["function"]["description"] = _B0_PLUS_DESCRIPTIONS[schema["function"]["name"]]
    return schemas


_LONG_LINE_RESERVE = 400  # caracteres reservados para o aviso de linha única truncada


def _long_line_warning(line_no: int, length: int, shown: int) -> str:
    return (
        f"[Truncated: line {line_no} has {length} characters and only its first {shown} are shown, "
        f"so the cut is inside this line, not between lines. Do not skip ahead to the next line: "
        f"the rest of line {line_no} is not shown. To inspect it, use run_bash with a filter "
        f"(for example cut, fold, grep -o or jq) that extracts the part you need.]"
    )


def _window_with_continuation(numbered: str, start: int, total: int, limit: int) -> tuple[str, bool]:
    """Janela de leitura (b0_plus): cabeça até o teto de linhas ou de caracteres, com pista de continuação.

    ``start`` é a primeira linha pedida (1-based). A pista nomeia as linhas mostradas e o próximo
    ``start_line``, para o modelo continuar sem adivinhar. Sem truncamento, devolve o texto intacto.
    """
    lines = numbered.split("\n") if numbered else []
    budget = max(0, limit - _CONTINUATION_RESERVE)
    shown: list[str] = []
    used = 0
    for line in lines[:B0_PLUS_READ_MAX_LINES]:
        cost = len(line) + (1 if shown else 0)
        if used + cost > budget:
            break
        shown.append(line)
        used += cost
    if not shown and lines:  # a primeira linha sozinha excede o orçamento: corte por tamanho
        head_budget = max(0, limit - _LONG_LINE_RESERVE)
        return lines[0][:head_budget] + "\n" + _long_line_warning(start, len(lines[0]), head_budget), True
    if len(shown) == len(lines):
        return numbered, False
    end = start + len(shown) - 1
    hint = f"[Truncated: showing lines {start}-{end} of {total}. Use start_line={end + 1} to continue.]"
    return "\n".join(shown) + "\n" + hint, True
