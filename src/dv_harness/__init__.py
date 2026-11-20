"""Harness modular do DV. Ver ``llm_client`` (Módulo 1)."""

from dv_harness.auth_provider import (
    AuthCredentials,
    AuthProvider,
    AuthStorage,
    BrowserOAuthAuthProvider,
    EnvAPIKeyAuthProvider,
    LocalCLISessionAuthProvider,
    sanitize_auth_secrets,
)
from dv_harness.llm_client import LLMClient, LLMResponse, ToolCall

__all__ = [
    "AuthCredentials",
    "AuthProvider",
    "AuthStorage",
    "BrowserOAuthAuthProvider",
    "EnvAPIKeyAuthProvider",
    "LLMClient",
    "LLMResponse",
    "LocalCLISessionAuthProvider",
    "ToolCall",
    "sanitize_auth_secrets",
]
