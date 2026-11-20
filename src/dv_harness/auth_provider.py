"""dv_harness.auth_provider: Provedores plugáveis de credenciais e sessões de autenticação.

Implementação equivalente à arquitetura de autenticação do ``@earendil-works/pi-coding-agent``:
- Armazenamento seguro de credenciais com permissões restritas (0o700 / 0o600) e travamento
  atômico concorrente (double-checked locking).
- Fluxo híbrido OAuth 2.0 PKCE (RFC 7636 S256) via loopback HTTP local (127.0.0.1:53692) e
  entrada manual no terminal (para sessões SSH / headless / container).
- Rotação preventiva e renovação automática de access token via refresh token (buffer de 5 minutos).
- Injeção de cabeçalhos de identidade e flags de compatibilidade (Anthropic Claude Pro/Max e OpenAI Codex).
- Conector de leitura de sessões locais de CLI existentes (Antigravity, Claude Code, ~/.pi, ADC gcloud).
- Sanitização estrita contra vazamento de tokens em mensagens de erro ou ledgers (Regra 9 e B6).
"""

from __future__ import annotations

import abc
import base64
import contextlib
import hashlib
import http.server
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dv_harness.protocol import COST_BASIS_PRICED, COST_BASIS_SUBSCRIPTION, COST_BASIS_UNKNOWN

# Configurações e diretórios padrão (fora do workspace do experimento - Regra B6)
DEFAULT_AUTH_DIR = Path.home() / ".dv_harness"
DEFAULT_AUTH_FILE = DEFAULT_AUTH_DIR / "auth.json"

# Buffer de expiração preventiva (5 minutos, igual ao pi DEFAULT_OAUTH_MINIMUM_VALIDITY_MS)
DEFAULT_MIN_VALIDITY_S = 300  # 5 minutos em segundos

# Identificadores Anthropic (alinhados ao pi-ai/src/auth/oauth/anthropic.ts)
ANTHROPIC_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
ANTHROPIC_AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
ANTHROPIC_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
ANTHROPIC_CALLBACK_PORT = 53692
ANTHROPIC_CALLBACK_PATH = "/callback"
ANTHROPIC_SCOPES = (
    "org:create_api_key user:profile user:inference user:sessions:claude_code "
    "user:mcp_servers user:file_upload"
)
CLAUDE_CODE_VERSION = "2.1.322"

# Padrões regex para ofuscação de segredos em logs e ledgers (Regra 9)
SECRET_PATTERNS = [
    re.compile(r"sk-ant-oat[A-Za-z0-9_-]+"),
    re.compile(r"sk-ant-ort[A-Za-z0-9_-]+"),
    re.compile(r"sk-ant-api[A-Za-z0-9_-]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/-]+=*", re.IGNORECASE),
]


def sanitize_auth_secrets(text: str) -> str:
    """Ofusca tokens e credenciais conhecidos antes de registro no ledger ou exibição."""
    if not text:
        return ""
    sanitized = text
    for pattern in SECRET_PATTERNS:
        sanitized = pattern.sub("[REDACTED_AUTH_TOKEN]", sanitized)
    return sanitized


def generate_pkce() -> tuple[str, str]:
    """Gera par PKCE (code_verifier, code_challenge S256) segundo a RFC 7636."""
    verifier = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    return verifier, challenge


@dataclass(frozen=True)
class AuthCredentials:
    """Credenciais resolvidas para injeção na requisição ao modelo."""

    headers: dict[str, str] = field(default_factory=dict)
    api_key: str | None = None
    cost_basis: str = COST_BASIS_UNKNOWN
    pricing_source: str | None = None
    session_metadata: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Armazenamento Seguro e Lock Concorrente (auth.json com 0o600 / 0o700)
# ---------------------------------------------------------------------------


class FileLock:
    """Bloqueio de arquivo atômico com stale-recovery e retry no Windows contra EPERM / ELOCKED."""

    def __init__(
        self,
        lock_path: Path,
        timeout_s: float = 30.0,
        retry_delay_s: float = 0.05,
        stale_s: float = 30.0,
    ) -> None:
        self.lock_path = lock_path
        self.timeout_s = timeout_s
        self.retry_delay_s = retry_delay_s
        self.stale_s = stale_s
        self._fd: int | None = None

    def acquire(self) -> None:
        deadline = time.time() + self.timeout_s
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                flags = os.O_CREAT | os.O_EXCL | os.O_RDWR
                if hasattr(os, "O_BINARY"):
                    flags |= os.O_BINARY
                self._fd = os.open(str(self.lock_path), flags, 0o600)
                with contextlib.suppress(OSError):
                    os.write(self._fd, str(time.time()).encode("ascii"))
                return
            except (FileExistsError, PermissionError):
                # Stale lock recovery (espelhando staleMs de proper-lockfile do pi)
                try:
                    mtime = self.lock_path.stat().st_mtime
                    if time.time() - mtime > self.stale_s:
                        with contextlib.suppress(OSError):
                            self.lock_path.unlink()
                except OSError:
                    pass

                if time.time() >= deadline:
                    raise TimeoutError(f"Timeout aguardando lock: {self.lock_path}") from None
                time.sleep(self.retry_delay_s)

    def release(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:
                pass
            self._fd = None
            try:
                if self.lock_path.exists():
                    self.lock_path.unlink()
            except OSError:
                pass

    def __enter__(self) -> FileLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class AuthStorage:
    """Gerenciador de credenciais persistidas em auth.json, espelhando AuthStorage do pi."""

    def __init__(self, auth_path: Path | None = None) -> None:
        self.auth_path = auth_path or DEFAULT_AUTH_FILE
        self.lock_path = self.auth_path.with_suffix(".lock")

    def _ensure_dir_and_perms(self) -> None:
        parent = self.auth_path.parent
        parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            parent.chmod(0o700)

    def read_all(self) -> dict[str, Any]:
        if not self.auth_path.exists():
            return {}
        try:
            with open(self.auth_path, "r", encoding="utf-8") as f:
                content = f.read().strip()
                return json.loads(content) if content else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def get_credential(self, provider_id: str) -> dict[str, Any] | None:
        return self.read_all().get(provider_id)

    def modify(
        self,
        provider_id: str,
        update_fn: Callable[[dict[str, Any] | None], dict[str, Any] | None],
    ) -> dict[str, Any] | None:
        """Modificação atômica sob lock exclusivo (double-checked locking)."""
        self._ensure_dir_and_perms()
        with FileLock(self.lock_path):
            current_all = self.read_all()
            current_cred = current_all.get(provider_id)
            updated_cred = update_fn(current_cred)
            if updated_cred is not None:
                current_all[provider_id] = updated_cred
            elif provider_id in current_all:
                del current_all[provider_id]

            # Escrita atômica em arquivo temporário com perms 0o600 diretas em os.open
            temp_file = self.auth_path.with_suffix(".tmp")
            flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
            if hasattr(os, "O_BINARY"):
                flags |= os.O_BINARY
            fd = os.open(str(temp_file), flags, 0o600)
            with open(fd, "w", encoding="utf-8") as f:
                json.dump(current_all, f, indent=2, ensure_ascii=False)
            with contextlib.suppress(OSError):
                temp_file.chmod(0o600)
            os.replace(temp_file, self.auth_path)
            with contextlib.suppress(OSError):
                self.auth_path.chmod(0o600)
            return updated_cred


# ---------------------------------------------------------------------------
# Servidor HTTP Loopback de Callback OAuth + Fallback Terminal
# ---------------------------------------------------------------------------


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server: OAuthCallbackServer

    def do_GET(self) -> None:
        parsed_url = urllib.parse.urlparse(self.path)
        if parsed_url.path != self.server.callback_path:
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h3>Callback path not found</h3></body></html>")
            return

        params = urllib.parse.parse_qs(parsed_url.query)
        error = params.get("error", [None])[0]
        if error:
            desc = params.get("error_description", [error])[0]
            self.server.error_message = desc
            self.server.event_done.set()
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                f"<html><body style='font-family:sans-serif;'><h2>Erro na autorizacao</h2><p>{desc}</p></body></html>".encode(
                    "utf-8"
                )
            )
            return

        code = params.get("code", [None])[0]
        state = params.get("state", [None])[0]
        if not code:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h3>Missing authorization code</h3></body></html>")
            return

        if self.server.expected_state and state != self.server.expected_state:
            self.send_response(400)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html><body><h3>State mismatch</h3></body></html>")
            return

        self.server.received_code = code
        self.server.event_done.set()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(
            f"<html><body style='font-family:sans-serif; text-align:center; padding:40px;'><h2>Autenticado com sucesso!</h2><p>Voce ja pode fechar esta aba e retornar ao terminal.</p></body></html>".encode(
                "utf-8"
            )
        )

    def log_message(self, format: str, *args: Any) -> None:
        pass  # Silencia logs HTTP no console para não sujar a saída


class OAuthCallbackServer(http.server.HTTPServer):
    def __init__(
        self,
        host: str,
        port: int,
        callback_path: str,
        expected_state: str,
    ) -> None:
        super().__init__((host, port), _CallbackHandler)
        self.callback_path = callback_path
        self.expected_state = expected_state
        self.received_code: str | None = None
        self.error_message: str | None = None
        self.event_done = threading.Event()


class NonInteractiveAuthError(RuntimeError):
    """Erro quando autenticação interativa é exigida em ambiente não interativo (sem TTY)."""


def wait_for_oauth_code(
    server: OAuthCallbackServer,
    prompt_message: str,
    timeout_s: float = 180.0,
) -> str:
    """Aguarda o código pelo servidor HTTP loopback ou via console (fallback manual)."""
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    is_interactive = (
        sys.stdin is not None
        and hasattr(sys.stdin, "isatty")
        and sys.stdin.isatty()
    )

    print(f"\n{prompt_message}")
    manual_code: list[str | None] = [None]
    input_thread: threading.Thread | None = None

    if is_interactive:
        print("Caso o navegador nao redirecione automaticamente, cole a URL de redirecionamento ou o codigo aqui:")

        def _read_stdin() -> None:
            try:
                line = sys.stdin.readline()
                if line:
                    val = line.strip()
                    if "code=" in val:
                        qs = urllib.parse.parse_qs(urllib.parse.urlparse(val).query)
                        val = qs.get("code", [val])[0]
                    elif "#" in val:
                        val = val.split("#", 1)[0]
                    manual_code[0] = val
                    server.event_done.set()
            except Exception:
                pass

        input_thread = threading.Thread(target=_read_stdin, daemon=True)
        input_thread.start()
    else:
        print("[AUTH] Ambiente nao interativo detectado (sem TTY em stdin). Entrada manual desativada; aguardando callback do navegador.")

    completed = server.event_done.wait(timeout=timeout_s)
    server.shutdown()
    server.server_close()

    if not completed:
        if not is_interactive:
            raise NonInteractiveAuthError(
                "Timeout aguardando callback OAuth em ambiente nao interativo (sem TTY). "
                "Execute 'python -m dv_harness.auth login' em um terminal interativo ou "
                "configure uma sessao previa via LocalCLISessionAuthProvider."
            )
        raise TimeoutError("Tempo esgotado aguardando autenticacao via navegador ou terminal.")
    if server.error_message:
        raise RuntimeError(f"Erro no callback OAuth: {server.error_message}")
    if server.received_code:
        return server.received_code
    if manual_code[0]:
        return manual_code[0]
    raise ValueError("Nenhum codigo de autorizacao foi capturado.")


# ---------------------------------------------------------------------------
# Contrato Base AuthProvider
# ---------------------------------------------------------------------------


class AuthProvider(abc.ABC):
    """Interface plugável de autenticação para o LLMClient."""

    @abc.abstractmethod
    def resolve_auth(self, model: str, force_refresh: bool = False) -> AuthCredentials:
        """Resolve headers de autenticação e parâmetros para a chamada."""
        raise NotImplementedError

    @abc.abstractmethod
    def get_cost_basis(self) -> str:
        """Informa a base de custo correspondente."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Implementação 1: BrowserOAuthAuthProvider (Estilo Pi)
# ---------------------------------------------------------------------------


class BrowserOAuthAuthProvider(AuthProvider):
    """Autenticação OAuth via navegador com servidor local de callback e refresh atômico."""

    def __init__(
        self,
        provider_name: str = "anthropic",
        storage: AuthStorage | None = None,
        callback_port: int = ANTHROPIC_CALLBACK_PORT,
    ) -> None:
        self.provider_name = provider_name
        self.storage = storage or AuthStorage()
        self.callback_port = callback_port

    def get_cost_basis(self) -> str:
        return COST_BASIS_SUBSCRIPTION

    def _is_expired(self, cred: dict[str, Any]) -> bool:
        expires_ts = cred.get("expires", 0)
        # Se expira em menos de 5 minutos (em ms ou s), consideramos expirado
        expires_s = expires_ts / 1000.0 if expires_ts > 10_000_000_000 else float(expires_ts)
        return (time.time() + DEFAULT_MIN_VALIDITY_S) >= expires_s

    def _refresh_anthropic(self, refresh_token: str) -> dict[str, Any]:
        req_data = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "client_id": ANTHROPIC_CLIENT_ID,
            "refresh_token": refresh_token,
        }).encode("utf-8")
        req = urllib.request.Request(
            ANTHROPIC_TOKEN_URL,
            data=req_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                expires_in = data.get("expires_in", 3600)
                return {
                    "type": "oauth",
                    "access": data["access_token"],
                    "refresh": data.get("refresh_token", refresh_token),
                    "expires": int((time.time() + expires_in - DEFAULT_MIN_VALIDITY_S) * 1000),
                }
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Falha ao renovar token Anthropic: HTTP {exc.code} - {sanitize_auth_secrets(body)}") from exc

    def _login_anthropic(self) -> dict[str, Any]:
        verifier, challenge = generate_pkce()
        try:
            server = OAuthCallbackServer("127.0.0.1", self.callback_port, ANTHROPIC_CALLBACK_PATH, verifier)
            actual_port = self.callback_port
        except OSError:
            server = OAuthCallbackServer("127.0.0.1", 0, ANTHROPIC_CALLBACK_PATH, verifier)
            actual_port = server.server_address[1]

        redirect_uri = f"http://localhost:{actual_port}{ANTHROPIC_CALLBACK_PATH}"
        auth_params = urllib.parse.urlencode({
            "code": "true",
            "client_id": ANTHROPIC_CLIENT_ID,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": ANTHROPIC_SCOPES,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": verifier,
        })
        auth_url = f"{ANTHROPIC_AUTHORIZE_URL}?{auth_params}"

        prompt = (
            f"Por favor abra o seguinte link no seu navegador para autenticar:\n\n"
            f"  {auth_url}\n"
        )
        code = wait_for_oauth_code(server, prompt)

        # Troca code por tokens
        exchange_data = urllib.parse.urlencode({
            "grant_type": "authorization_code",
            "client_id": ANTHROPIC_CLIENT_ID,
            "code": code,
            "state": verifier,
            "redirect_uri": redirect_uri,
            "code_verifier": verifier,
        }).encode("utf-8")
        req = urllib.request.Request(
            ANTHROPIC_TOKEN_URL,
            data=exchange_data,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                expires_in = data.get("expires_in", 3600)
                cred = {
                    "type": "oauth",
                    "access": data["access_token"],
                    "refresh": data.get("refresh_token", ""),
                    "expires": int((time.time() + expires_in - DEFAULT_MIN_VALIDITY_S) * 1000),
                }
                self.storage.modify(self.provider_name, lambda _: cred)
                return cred
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Falha na troca de codigo OAuth: HTTP {exc.code} - {sanitize_auth_secrets(body)}") from exc

    def resolve_auth(self, model: str, force_refresh: bool = False) -> AuthCredentials:
        cred = self.storage.get_credential(self.provider_name)
        if not cred or cred.get("type") != "oauth":
            cred = self._login_anthropic()

        if force_refresh or self._is_expired(cred):
            refresh_tok = cred.get("refresh")
            if not refresh_tok:
                cred = self._login_anthropic()
            else:
                # Double-checked locking atômico
                def _do_refresh(current: dict[str, Any] | None) -> dict[str, Any] | None:
                    if not current or current.get("type") != "oauth":
                        return current
                    if not force_refresh and not self._is_expired(current):
                        return current
                    return self._refresh_anthropic(current["refresh"])

                cred = self.storage.modify(self.provider_name, _do_refresh) or cred

        access_token = cred["access"]
        # Injeção de headers específicos do provedor (espelhando pi-ai/src/api/anthropic-messages.ts)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "user-agent": f"claude-cli/{CLAUDE_CODE_VERSION}",
            "x-app": "cli",
            "anthropic-dangerous-direct-browser-access": "true",
            "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
        }
        return AuthCredentials(
            headers=headers,
            api_key=access_token,
            cost_basis=COST_BASIS_SUBSCRIPTION,
            pricing_source=f"subscription: OAuth session ({self.provider_name})",
            session_metadata={"type": "oauth", "provider": self.provider_name},
        )


# ---------------------------------------------------------------------------
# Implementação 2: LocalCLISessionAuthProvider (Antigravity / Claude Code / ADC)
# ---------------------------------------------------------------------------


class LocalCLISessionAuthProvider(AuthProvider):
    """Conector para ler sessões já ativas no host sem exigir browser ou chaves manuais."""

    def __init__(
        self,
        candidate_paths: list[Path] | None = None,
        provider_name: str = "anthropic",
    ) -> None:
        self.provider_name = provider_name
        self.candidate_paths = candidate_paths or [
            DEFAULT_AUTH_FILE,
            Path.home() / ".pi" / "agent" / "auth.json",
            Path.home() / ".claude.json",
        ]

    def get_cost_basis(self) -> str:
        return COST_BASIS_SUBSCRIPTION

    def resolve_auth(self, model: str, force_refresh: bool = False) -> AuthCredentials:
        # 1. Verifica auth.json existente (~/.dv_harness/auth.json ou ~/.pi/agent/auth.json)
        for path in self.candidate_paths:
            if path.exists() and path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    cred = data.get(self.provider_name)
                    if cred and cred.get("type") == "oauth" and cred.get("access"):
                        access_token = cred["access"]
                        headers = {
                            "Authorization": f"Bearer {access_token}",
                            "user-agent": f"claude-cli/{CLAUDE_CODE_VERSION}",
                            "x-app": "cli",
                            "anthropic-dangerous-direct-browser-access": "true",
                            "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
                        }
                        return AuthCredentials(
                            headers=headers,
                            api_key=access_token,
                            cost_basis=COST_BASIS_SUBSCRIPTION,
                            pricing_source=f"subscription: local session ({path})",
                            session_metadata={"source_file": str(path), "provider": self.provider_name},
                        )
                except Exception:
                    continue

        # 2. Verifica token em ~/.claude.json ou variáveis de ambiente de CLI
        claude_json = Path.home() / ".claude.json"
        if claude_json.exists():
            try:
                data = json.loads(claude_json.read_text(encoding="utf-8"))
                oauth_acc = data.get("oauthAccount", {})
                if oauth_acc:
                    # Token no ambiente ou chave de conta
                    env_token = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("ANTHROPIC_OAUTH_TOKEN")
                    if env_token:
                        headers = {
                            "Authorization": f"Bearer {env_token}",
                            "user-agent": f"claude-cli/{CLAUDE_CODE_VERSION}",
                            "x-app": "cli",
                            "anthropic-dangerous-direct-browser-access": "true",
                            "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
                        }
                        return AuthCredentials(
                            headers=headers,
                            api_key=env_token,
                            cost_basis=COST_BASIS_SUBSCRIPTION,
                            pricing_source="subscription: claude-cli session",
                            session_metadata={"source": "claude_cli_env"},
                        )
            except Exception:
                pass

        raise RuntimeError(
            f"Nenhuma sessao CLI local ativa encontrada para {self.provider_name}. "
            "Execute login previo via BrowserOAuthAuthProvider ou forneca credenciais."
        )


# ---------------------------------------------------------------------------
# Implementação 3: EnvAPIKeyAuthProvider (Compatibilidade Legada)
# ---------------------------------------------------------------------------


class EnvAPIKeyAuthProvider(AuthProvider):
    """Provedor padrão legado baseado em variáveis de ambiente."""

    def __init__(self, env_var: str | None = None) -> None:
        self.env_var = env_var

    def get_cost_basis(self) -> str:
        return COST_BASIS_PRICED

    def resolve_auth(self, model: str, force_refresh: bool = False) -> AuthCredentials:
        var_name = self.env_var or ("ANTHROPIC_API_KEY" if "claude" in model or "anthropic" in model else "OPENAI_API_KEY")
        key = os.environ.get(var_name)
        if not key:
            return AuthCredentials(cost_basis=COST_BASIS_UNKNOWN)
        return AuthCredentials(
            api_key=key,
            headers={"x-api-key": key} if "anthropic" in var_name.lower() else {"Authorization": f"Bearer {key}"},
            cost_basis=COST_BASIS_PRICED,
            pricing_source="environment_variable",
        )
