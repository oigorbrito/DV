"""ToolExecutor: 4 primitivas de ferramenta com retorno estruturado (Módulo 2).

Primitivas: ``read_file``, ``write_file``, ``edit_file`` (search/replace exato) e ``run_bash``.
Cada uma retorna ``ToolResult`` e nunca levanta exceção para falhas esperadas
(arquivo ausente, binário, ``old_text`` ausente ou ambíguo, timeout, exit code != 0).
Erros de programação, como tipos inválidos, ainda levantam exceção.

Este módulo não contém loop de agente, política de decisão nem gerenciamento de
contexto (Módulos 3 e 4, ainda não implementados).

Trava de workspace: os parâmetros ``path`` das primitivas de arquivo e ``cwd`` do
``run_bash`` são resolvidos com ``.resolve()`` (seguindo links simbólicos) e precisam ficar
dentro de ``root``. Caso contrário, a primitiva devolve ``ToolResult(ok=False)``. Essa trava
não é um sandbox. O texto de um comando ``run_bash`` pode acessar qualquer caminho do
sistema, por exemplo com ``cat /etc/passwd``. Por isso o benchmark deve rodar em workspace
isolado (container ou worktree descartável). Restringir o que o agente pode fazer continua
sendo responsabilidade do ambiente que o executa.

Atribuições empíricas
---------------------
- Conjunto de 4 primitivas (``read``, ``write``, ``edit``, ``bash``): baseado no
  ``earendil-works/pi`` (https://github.com/earendil-works/pi), cujo conjunto padrão de
  ferramentas é exatamente esse. Fonte: resumo de busca, não leitura direta do repositório.
- ``run_bash`` stateless via ``subprocess`` (``Popen`` em grupo próprio, com ``killpg``
  no timeout), cada ação independente, sem shell persistente: baseado no
  ``SWE-agent/mini-swe-agent``
  (https://github.com/SWE-agent/mini-swe-agent). Verificado no README do repositório.
  Variáveis anti-travamento (``PAGER=cat``, ``MANPAGER=cat``, ``LESS=-R``) e timeout
  padrão de 30 s verificados em ``src/minisweagent/config/default.yaml`` e
  ``src/minisweagent/environments/local.py``. A FAQ oficial não foi lida.
- Edição por ``search/replace`` exato em vez de reescrita integral: alinhado às notas do
  Aider (https://aider.chat/docs/leaderboards/notes.html), que descrevem o formato
  "whole" como fácil, mas caro em tokens. O benchmark Diff-XYZ
  (https://arxiv.org/abs/2510.12487) indica que o desempenho de search/replace depende do
  tamanho do modelo. O artigo não foi lido diretamente (acesso bloqueado nesta sessão). A
  escolha aqui se apoia em segurança (falha com ambiguidade), não em superioridade
  demonstrada.
- Feedback estruturado, conciso e com truncamento de saída (ACI): princípio do paper
  SWE-agent (NeurIPS 2024, https://arxiv.org/abs/2405.15793). O paper não foi lido
  diretamente. O limite de 10.000 caracteres é escolha deste módulo, não valor citado.
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

# Variáveis que impedem paginadores e ferramentas interativas de travar a execução.
_NON_INTERACTIVE_ENV = {
    "PAGER": "cat",
    "MANPAGER": "cat",
    "GIT_PAGER": "cat",
    "LESS": "-R",
}


class WorkspaceEscapeError(ValueError):
    """Caminho ou ``cwd`` fora do ``root`` do executor."""


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    output: str = ""
    error: str | None = None
    exit_code: int | None = None
    truncated: bool = False


def truncate_output(text: str, limit: int) -> tuple[str, bool]:
    """Limita ``text`` a ``limit`` caracteres, mantendo início e fim.

    Retorna ``(texto, truncado)``. O marcador conta dentro do limite.
    """
    if limit < 0:
        raise ValueError("limit must be >= 0")
    if len(text) <= limit:
        return text, False
    marker_template = "\n[... {omitted} caracteres omitidos ...]\n"
    # Espaço reservado para o marcador com número de dígitos de pior caso.
    reserve = len(marker_template.format(omitted=len(text)))
    if limit <= reserve:
        return text[:limit], True
    budget = limit - reserve
    head = budget // 2
    tail = budget - head
    omitted = len(text) - head - tail
    marker = marker_template.format(omitted=omitted)
    return text[:head] + marker + (text[-tail:] if tail else ""), True


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
        """Resolve ``path`` contra ``root``, seguindo links simbólicos.

        Levanta ``WorkspaceEscapeError`` se o alvo ficar fora de ``root``. Os métodos
        públicos convertem isso em ``ToolResult(ok=False)`` para o modelo se recuperar.
        """
        p = Path(path)
        candidate = (p if p.is_absolute() else self.root / p).resolve()
        if candidate != self.root and not candidate.is_relative_to(self.root):
            raise WorkspaceEscapeError(f"path escapes workspace root: {path}")
        return candidate

    # --- read_file -----------------------------------------------------------

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> ToolResult:
        """Lê arquivo texto. Linhas são 1-based e inclusivas, com numeração ``N\\tlinha``."""
        if start_line is not None and start_line < 1:
            return ToolResult(ok=False, error="start_line must be >= 1")
        if end_line is not None and start_line is not None and end_line < start_line:
            return ToolResult(ok=False, error="end_line must be >= start_line")

        try:
            target = self._resolve(path)
        except WorkspaceEscapeError as exc:
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

        lines = text.splitlines()
        first = (start_line or 1) - 1
        last = len(lines) if end_line is None else min(end_line, len(lines))
        if first > len(lines) and lines:
            return ToolResult(ok=False, error=f"start_line {start_line} beyond end of file ({len(lines)} lines)")
        numbered = "\n".join(f"{i + 1}\t{lines[i]}" for i in range(first, last))
        out, truncated = truncate_output(numbered, self.max_output_chars)
        return ToolResult(ok=True, output=out, truncated=truncated)

    # --- write_file ----------------------------------------------------------

    def write_file(self, path: str, content: str) -> ToolResult:
        """Cria ou sobrescreve arquivo texto UTF-8, criando diretórios pai se necessário."""
        try:
            target = self._resolve(path)
        except WorkspaceEscapeError as exc:
            return ToolResult(ok=False, error=str(exc))
        if target.is_dir():
            return ToolResult(ok=False, error=f"is a directory: {path}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult(ok=False, error=f"write failed: {exc.strerror or exc}")
        return ToolResult(ok=True, output=f"wrote {len(content.encode('utf-8'))} bytes to {path}")

    # --- edit_file -----------------------------------------------------------

    def edit_file(self, path: str, old_text: str, new_text: str) -> ToolResult:
        """Substitui ``old_text`` por ``new_text``. Falha se não existir ou aparecer mais de uma vez."""
        if not old_text:
            return ToolResult(ok=False, error="old_text must not be empty")
        try:
            target = self._resolve(path)
        except WorkspaceEscapeError as exc:
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

        count = text.count(old_text)
        if count == 0:
            return ToolResult(ok=False, error=f"old_text not found in {path}")
        if count > 1:
            return ToolResult(
                ok=False,
                error=f"old_text is ambiguous: {count} occurrences in {path}; include more context",
            )
        try:
            target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
        except OSError as exc:
            return ToolResult(ok=False, error=f"write failed: {exc.strerror or exc}")
        return ToolResult(ok=True, output=f"edited {path}")

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
            except WorkspaceEscapeError as exc:
                return ToolResult(ok=False, error=str(exc))
        else:
            workdir = self.root
        env = {**os.environ, **_NON_INTERACTIVE_ENV}
        # Popen em sessão própria para poder matar o grupo inteiro no timeout.
        # Com subprocess.run, filhos em background (ex.: "sleep 30 & sleep 30") sobreviviam
        # ao timeout como processos órfãos. Medido nesta sessão antes da mudança.
        try:
            proc = subprocess.Popen(
                command,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(workdir),
                env=env,
                start_new_session=True,
            )
        except OSError as exc:
            return ToolResult(ok=False, error=f"could not start command: {exc.strerror or exc}")

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            stdout, stderr = proc.communicate()
            partial = _decode(stdout) + _decode(stderr)
            out, truncated = truncate_output(partial, self.max_output_chars)
            return ToolResult(
                ok=False,
                output=out,
                error=f"timeout after {timeout}s",
                exit_code=None,
                truncated=truncated,
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
        )


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
