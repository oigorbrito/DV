"""Testes unitários e de integração do AuthProvider e sua integração com LLMClient.

Executar: pytest tests/test_auth_provider.py -v
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
from support import ROOT  # noqa: F401

from dv_harness.auth_provider import (
    ANTHROPIC_CALLBACK_PATH,
    AuthCredentials,
    AuthProvider,
    AuthStorage,
    BrowserOAuthAuthProvider,
    CLAUDE_CODE_VERSION,
    EnvAPIKeyAuthProvider,
    FileLock,
    LocalCLISessionAuthProvider,
    OAuthCallbackServer,
    generate_pkce,
    sanitize_auth_secrets,
)
from dv_harness.llm_client import LLMCallFailed, LLMClient
from dv_harness.protocol import COST_BASIS_PRICED, COST_BASIS_SUBSCRIPTION, COST_BASIS_UNKNOWN


# ---------------------------------------------------------------------------
# PKCE e Sanitização
# ---------------------------------------------------------------------------


def test_generate_pkce_conforms_to_rfc7636():
    verifier, challenge = generate_pkce()
    assert len(verifier) >= 43
    # Verifier não deve ter padding '='
    assert "=" not in verifier
    # Challenge deve ser base64url(sha256(verifier))
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    expected_challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")
    assert challenge == expected_challenge


def test_sanitize_auth_secrets():
    text = (
        "Error calling https://api.anthropic.com with Bearer sk-ant-oat01-mySecretAccessToken1234567 "
        "and refresh sk-ant-ort01-mySecretRefreshToken1234567 and apiKey sk-ant-api03-abcdefg987654321"
    )
    sanitized = sanitize_auth_secrets(text)
    assert "sk-ant-oat01" not in sanitized
    assert "sk-ant-ort01" not in sanitized
    assert "sk-ant-api03" not in sanitized
    assert "[REDACTED_AUTH_TOKEN]" in sanitized


# ---------------------------------------------------------------------------
# AuthStorage & FileLock
# ---------------------------------------------------------------------------


def test_auth_storage_creates_file_and_permissions(tmp_path: Path):
    auth_file = tmp_path / "subdir" / "auth.json"
    storage = AuthStorage(auth_file)

    assert storage.read_all() == {}

    cred = {
        "type": "oauth",
        "access": "sk-ant-oat01-token",
        "refresh": "sk-ant-ort01-refresh",
        "expires": int(time.time() * 1000) + 3600000,
    }
    storage.modify("anthropic", lambda _: cred)

    assert auth_file.exists()
    saved = storage.get_credential("anthropic")
    assert saved == cred


def test_auth_storage_atomic_modify_double_checked(tmp_path: Path):
    auth_file = tmp_path / "auth.json"
    storage = AuthStorage(auth_file)

    storage.modify("test", lambda _: {"count": 1})
    storage.modify("test", lambda current: {"count": (current["count"] + 1) if current else 1})

    assert storage.get_credential("test") == {"count": 2}


# ---------------------------------------------------------------------------
# Loopback HTTP Server
# ---------------------------------------------------------------------------


def test_oauth_callback_server_flow():
    state = "test-state-12345"
    server = OAuthCallbackServer("127.0.0.1", 0, ANTHROPIC_CALLBACK_PATH, state)
    port = server.server_address[1]

    import threading
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()

    # Simula browser enviando callback correto
    url = f"http://127.0.0.1:{port}{ANTHROPIC_CALLBACK_PATH}?code=auth_code_xyz&state={state}"
    with urllib.request.urlopen(url, timeout=5) as resp:
        assert resp.status == 200
        body = resp.read().decode("utf-8")
        assert "Autenticado com sucesso" in body

    server.event_done.wait(timeout=2)
    assert server.received_code == "auth_code_xyz"
    server.server_close()


def test_oauth_callback_server_state_mismatch():
    server = OAuthCallbackServer("127.0.0.1", 0, ANTHROPIC_CALLBACK_PATH, "expected-state")
    port = server.server_address[1]

    import threading
    t = threading.Thread(target=server.handle_request, daemon=True)
    t.start()

    # Simula estado incorreto
    url = f"http://127.0.0.1:{port}{ANTHROPIC_CALLBACK_PATH}?code=auth_code_xyz&state=wrong-state"
    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(url, timeout=5)
    assert exc_info.value.code == 400
    server.server_close()


# ---------------------------------------------------------------------------
# BrowserOAuthAuthProvider
# ---------------------------------------------------------------------------


def test_browser_oauth_provider_expired_token_triggers_refresh(tmp_path: Path):
    auth_file = tmp_path / "auth.json"
    storage = AuthStorage(auth_file)

    # Token expirado (expirou há 10 segundos)
    expired_ts = int((time.time() - 10) * 1000)
    initial_cred = {
        "type": "oauth",
        "access": "old-access-token",
        "refresh": "valid-refresh-token",
        "expires": expired_ts,
    }
    storage.modify("anthropic", lambda _: initial_cred)

    provider = BrowserOAuthAuthProvider(provider_name="anthropic", storage=storage)

    refreshed_cred = {
        "type": "oauth",
        "access": "new-refreshed-access-token",
        "refresh": "new-refresh-token",
        "expires": int((time.time() + 3600) * 1000),
    }

    with mock.patch.object(provider, "_refresh_anthropic", return_value=refreshed_cred) as mock_refresh:
        creds = provider.resolve_auth("claude-3-7-sonnet")
        mock_refresh.assert_called_once_with("valid-refresh-token")
        assert creds.api_key == "new-refreshed-access-token"
        assert creds.headers["Authorization"] == "Bearer new-refreshed-access-token"
        assert creds.headers["user-agent"] == f"claude-cli/{CLAUDE_CODE_VERSION}"
        assert creds.headers["anthropic-beta"] == "claude-code-20250219,oauth-2025-04-20"
        assert creds.cost_basis == COST_BASIS_SUBSCRIPTION


def test_browser_oauth_provider_valid_token_does_not_refresh(tmp_path: Path):
    auth_file = tmp_path / "auth.json"
    storage = AuthStorage(auth_file)

    # Token válido por mais 1 hora
    valid_ts = int((time.time() + 3600) * 1000)
    initial_cred = {
        "type": "oauth",
        "access": "active-access-token",
        "refresh": "valid-refresh-token",
        "expires": valid_ts,
    }
    storage.modify("anthropic", lambda _: initial_cred)

    provider = BrowserOAuthAuthProvider(provider_name="anthropic", storage=storage)

    with mock.patch.object(provider, "_refresh_anthropic") as mock_refresh:
        creds = provider.resolve_auth("claude-3-7-sonnet")
        mock_refresh.assert_not_called()
        assert creds.api_key == "active-access-token"
        assert creds.headers["Authorization"] == "Bearer active-access-token"
        assert creds.cost_basis == COST_BASIS_SUBSCRIPTION


# ---------------------------------------------------------------------------
# LocalCLISessionAuthProvider
# ---------------------------------------------------------------------------


def test_local_cli_session_provider_reads_from_existing_auth_json(tmp_path: Path):
    pi_auth = tmp_path / "pi_agent_auth.json"
    data = {
        "anthropic": {
            "type": "oauth",
            "access": "sk-ant-oat01-host-session",
            "refresh": "sk-ant-ort01-host-session",
            "expires": int((time.time() + 3600) * 1000),
        }
    }
    pi_auth.write_text(json.dumps(data), encoding="utf-8")

    provider = LocalCLISessionAuthProvider(candidate_paths=[pi_auth], provider_name="anthropic")
    creds = provider.resolve_auth("claude-3-5-sonnet")

    assert creds.api_key == "sk-ant-oat01-host-session"
    assert creds.headers["Authorization"] == "Bearer sk-ant-oat01-host-session"
    assert creds.cost_basis == COST_BASIS_SUBSCRIPTION


def test_local_cli_session_provider_fails_when_no_session(tmp_path: Path):
    provider = LocalCLISessionAuthProvider(candidate_paths=[tmp_path / "nonexistent.json"])
    with pytest.raises(RuntimeError) as exc_info:
        provider.resolve_auth("claude-3-5-sonnet")
    assert "Nenhuma sessao CLI local ativa encontrada" in str(exc_info.value)


# ---------------------------------------------------------------------------
# EnvAPIKeyAuthProvider
# ---------------------------------------------------------------------------


def test_env_api_key_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-testkey123")
    provider = EnvAPIKeyAuthProvider()
    creds = provider.resolve_auth("claude-3-5-sonnet")

    assert creds.api_key == "sk-ant-api-testkey123"
    assert creds.cost_basis == COST_BASIS_PRICED
    assert creds.headers["x-api-key"] == "sk-ant-api-testkey123"


# ---------------------------------------------------------------------------
# Integração com LLMClient & Invariante I6 / Regra B6
# ---------------------------------------------------------------------------


class MockAuthProvider(AuthProvider):
    def __init__(self, token: str = "sk-ant-oat01-mocked-token") -> None:
        self.token = token

    def get_cost_basis(self) -> str:
        return COST_BASIS_SUBSCRIPTION

    def resolve_auth(self, model: str, force_refresh: bool = False) -> AuthCredentials:
        return AuthCredentials(
            headers={
                "Authorization": f"Bearer {self.token}",
                "user-agent": f"claude-cli/{CLAUDE_CODE_VERSION}",
                "anthropic-beta": "claude-code-20250219,oauth-2025-04-20",
            },
            api_key=self.token,
            cost_basis=COST_BASIS_SUBSCRIPTION,
            pricing_source="subscription: OAuth session",
        )


def _fake_raw_completion(prompt_tokens: int = 15, completion_tokens: int = 25):
    class Msg:
        content = "hello world"
        tool_calls = None

    class Choice:
        message = Msg()

    class Usage:
        pass

    u = Usage()
    u.prompt_tokens = prompt_tokens
    u.completion_tokens = completion_tokens

    class Raw:
        choices = [Choice()]
        usage = u
        model = "claude-3-5-sonnet-20241022"
        id = "chatcmpl-test-123"

    return Raw()


def test_llm_client_subscription_accounting_invariants():
    """Garante que sob sessão de assinatura, cost_usd é None (não 0.0) e tokens são 100% medidos."""
    received_kwargs = {}

    def fake_completion(**kwargs):
        received_kwargs.update(kwargs)
        return _fake_raw_completion(prompt_tokens=40, completion_tokens=60)

    auth_prov = MockAuthProvider()
    client = LLMClient(
        default_model="claude-3-5-sonnet",
        auth_provider=auth_prov,
        completion_fn=fake_completion,
    )

    resp = client.complete([{"role": "user", "content": "hi"}])

    # 1. Headers injetados na chamada
    assert received_kwargs["extra_headers"]["Authorization"] == "Bearer sk-ant-oat01-mocked-token"
    assert received_kwargs["extra_headers"]["user-agent"] == f"claude-cli/{CLAUDE_CODE_VERSION}"
    assert received_kwargs["api_key"] == "sk-ant-oat01-mocked-token"

    # 2. Contabilidade estrita do Contrato v2 (Invariante I6):
    # cost_usd deve ser None (nunca 0.0), cost_basis é subscription_session
    assert resp.cost_usd is None
    assert resp.cost_basis == COST_BASIS_SUBSCRIPTION
    assert resp.pricing_source == "subscription: OAuth session"

    # 3. Tokens e tempo são 100% medidos
    assert resp.prompt_tokens == 40
    assert resp.completion_tokens == 60
    assert resp.total_tokens == 100
    assert resp.wall_clock_ms is not None and resp.wall_clock_ms > 0


def test_llm_client_sanitizes_tokens_in_errors_and_ledger():
    """Garante que tokens ou cabeçalhos sensíveis em erros do provedor são ofuscados."""
    def failing_completion(**kwargs):
        raise RuntimeError("HTTP 401 Unauthorized for URL https://api.anthropic.com with Bearer sk-ant-oat01-leakMe123456789")

    auth_prov = MockAuthProvider(token="sk-ant-oat01-leakMe123456789")
    client = LLMClient(
        default_model="claude-3-5-sonnet",
        auth_provider=auth_prov,
        completion_fn=failing_completion,
        sleep_fn=lambda _: None,
    )

    with pytest.raises(LLMCallFailed) as exc_info:
        client.complete([{"role": "user", "content": "test"}])

    err_text = str(exc_info.value)
    # Token nunca deve vazar na mensagem da exceção
    assert "sk-ant-oat01-leakMe" not in err_text
    assert "[REDACTED_AUTH_TOKEN]" in err_text

    # E na lista de tentativas (que vai pro ledger) também deve estar sanitizado
    for att in exc_info.value.attempts:
        assert "sk-ant-oat01-leakMe" not in (att.error_message or "")
        assert "[REDACTED_AUTH_TOKEN]" in (att.error_message or "")
