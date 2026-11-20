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

import os
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
    ) -> None:
        self.root = (Path(root) if root is not None else Path.cwd()).resolve()
        self.max_output_chars = max_output_chars

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
            target = self._resolve(path)
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
        out, truncated = truncate_output(numbered, self.max_output_chars)
        return ToolResult(ok=True, output=out, truncated=truncated, raw_output=numbered)

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

        updated, problem = _apply_unique_replacement(text, old_text, new_text, path)
        if problem is not None:
            return problem
        try:
            target.write_text(updated, encoding="utf-8", newline="")
        except OSError as exc:
            return ToolResult(ok=False, error=f"write failed: {exc.strerror or exc}")
        message = f"edited {path}"
        return ToolResult(ok=True, output=message, raw_output=message)

    # --- run_bash ------------------------------------------------------------

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
            stdout, stderr = proc.communicate()
            full = _decode(stdout) + _decode(stderr)
            out, truncated = truncate_output(full, self.max_output_chars)
            return ToolResult(
                ok=False,
                output=out,
                error=f"timeout after {timeout}s",
                exit_code=None,
                truncated=truncated,
                raw_output=full,
            )

        combined = _decode(stdout)
        err_text = _decode(stderr)
        if err_text:
            combined = f"{combined}\n[stderr]\n{err_text}" if combined else f"[stderr]\n{err_text}"
        out, truncated = truncate_output(combined, self.max_output_chars)
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
    crlf = text.count("\r\n")
    pure_crlf = crlf > 0 and text.count("\n") == crlf
    if pure_crlf:
        body = text.replace("\r\n", "\n")
        needle = old_text.replace("\r\n", "\n")
        replacement = new_text.replace("\r\n", "\n")
    else:
        body, needle, replacement = text, old_text, new_text
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

def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """Mata o grupo de processos do comando (POSIX). Ignora se já terminou."""
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()


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
