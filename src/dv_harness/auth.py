"""CLI de conveniência para gerenciamento de autenticação do dv_harness.

Uso:
    python -m dv_harness.auth login [--provider anthropic] [--port 53692]
    python -m dv_harness.auth status
    python -m dv_harness.auth logout [--provider anthropic]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from dv_harness.auth_provider import (
    ANTHROPIC_CALLBACK_PORT,
    DEFAULT_AUTH_FILE,
    AuthStorage,
    BrowserOAuthAuthProvider,
    LocalCLISessionAuthProvider,
)


def _mask_token(tok: str | None) -> str:
    if not tok:
        return "[none]"
    if len(tok) <= 12:
        return "[redacted]"
    return f"{tok[:8]}...{tok[-4:]}"


def cmd_status(args: argparse.Namespace) -> int:
    storage = AuthStorage(DEFAULT_AUTH_FILE)
    creds = storage.read_all()

    print("==================================================")
    print("  dv_harness - Estado de Autenticação de Sessões")
    print("==================================================")
    print(f"Arquivo local: {DEFAULT_AUTH_FILE}")

    if not creds:
        print("\nNenhuma credencial persistida em ~/.dv_harness/auth.json.")
    else:
        print(f"\nCredenciais registradas ({len(creds)}):")
        now = time.time()
        for provider, data in creds.items():
            ctype = data.get("type", "unknown")
            access = _mask_token(data.get("access") or data.get("key"))
            expires_ts = data.get("expires", 0)
            expires_s = expires_ts / 1000.0 if expires_ts > 10_000_000_000 else float(expires_ts)
            
            if expires_s <= 0:
                exp_str = "sem expiração definida"
                status = "ATIVO"
            elif expires_s > now:
                remaining_min = int((expires_s - now) / 60)
                exp_str = f"válido por mais {remaining_min} min"
                status = "ATIVO"
            else:
                exp_str = "EXPIRADO"
                status = "REFRESHÁVEL" if data.get("refresh") else "INATIVO"

            print(f"  • {provider}: tipo={ctype}, status={status} ({exp_str}), token={access}")

    # Checa sessões externas conhecidas (Local CLI)
    print("\nSessões externas detectadas no host:")
    pi_auth = Path.home() / ".pi" / "agent" / "auth.json"
    if pi_auth.exists():
        print(f"  [x] pi-coding-agent: {pi_auth} (disponível via LocalCLISessionAuthProvider)")
    else:
        print("  [ ] pi-coding-agent (~/.pi/agent/auth.json): não encontrado")

    claude_json = Path.home() / ".claude.json"
    if claude_json.exists():
        print(f"  [x] Claude Code: {claude_json} (detectado no host)")
    else:
        print("  [ ] Claude Code (~/.claude.json): não encontrado")

    print("==================================================")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    provider_name = args.provider.lower()
    if provider_name != "anthropic":
        print(f"Erro: Provedor '{args.provider}' não suporta login OAuth via navegador no momento.")
        return 1

    print(f"Iniciando login interativo para '{provider_name}'...")
    storage = AuthStorage(DEFAULT_AUTH_FILE)
    provider = BrowserOAuthAuthProvider(
        provider_name=provider_name,
        storage=storage,
        callback_port=args.port,
    )

    try:
        creds = provider.resolve_auth(model="claude-3-7-sonnet", force_refresh=args.force)
        print("\n[OK] Autenticação concluída com sucesso!")
        print(f"  Provedor: {provider_name}")
        print(f"  Base de contabilidade: {creds.cost_basis}")
        print(f"  Token de acesso salvo em: {DEFAULT_AUTH_FILE}")
        return 0
    except Exception as exc:
        print(f"\n[ERRO] Falha durante autenticação: {exc}")
        return 1


def cmd_logout(args: argparse.Namespace) -> int:
    provider_name = args.provider.lower()
    storage = AuthStorage(DEFAULT_AUTH_FILE)

    if not storage.get_credential(provider_name):
        print(f"Provedor '{provider_name}' não possui credenciais salvas em {DEFAULT_AUTH_FILE}.")
        return 0

    storage.modify(provider_name, lambda _: None)
    print(f"[OK] Credenciais do provedor '{provider_name}' removidas de {DEFAULT_AUTH_FILE}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="dv_harness.auth",
        description="Gerenciamento de credenciais e sessões de autenticação do dv_harness.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcomando status
    p_status = subparsers.add_parser("status", help="Exibe o status das sessões e credenciais.")
    p_status.set_defaults(func=cmd_status)

    # Subcomando login
    p_login = subparsers.add_parser("login", help="Executa login interativo OAuth pelo navegador.")
    p_login.add_argument("--provider", default="anthropic", help="Nome do provedor (padrão: anthropic)")
    p_login.add_argument("--port", type=int, default=ANTHROPIC_CALLBACK_PORT, help=f"Porta loopback (padrão: {ANTHROPIC_CALLBACK_PORT})")
    p_login.add_argument("--force", action="store_true", help="Força novo login mesmo se já existir token válido")
    p_login.set_defaults(func=cmd_login)

    # Subcomando logout
    p_logout = subparsers.add_parser("logout", help="Remove credenciais de um provedor.")
    p_logout.add_argument("--provider", default="anthropic", help="Nome do provedor (padrão: anthropic)")
    p_logout.set_defaults(func=cmd_logout)

    parsed = parser.parse_args(argv)
    return parsed.func(parsed)


if __name__ == "__main__":
    sys.exit(main())
