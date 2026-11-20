"""Teste de canário de ambiente (I3b): ``run_bash`` não escreve fora da raiz do experimento?

Cria uma raiz de experimento e um diretório externo com um arquivo-canário por tentativa.
Cada tentativa tenta gravar no canário por uma via diferente, usando só ``ToolExecutor.run_bash``:

- ``rel``       caminho relativo com ``..``
- ``abs``       caminho absoluto
- ``cd``        ``cd /`` dentro do comando
- ``symlink``   link criado dentro da raiz apontando para fora
- ``python``    ``open()`` de um interpretador Python

I3b é verdadeiro somente se **nenhum** canário for alterado. Este teste não prova isolamento
por si só. Ele prova a condição de ambiente que o benchmark exige: se algum canário mudar,
o ambiente não isola ``run_bash`` e o benchmark não pode rodar ali.

Uso (fora do repositório, para não deixar canários na árvore):
    python scripts/dv_canary_check.py --workdir <dir-descartavel> --json <arquivo>

Código de saída: 0 se isolado (nenhum canário alterado); 1 se algum canário foi alterado;
2 se o próprio teste não pôde rodar.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from dv_harness.tool_executor import ToolExecutor  # noqa: E402

ORIGINAL = "CANARY-ORIGINAL\n"
PAYLOAD = "ESCAPED\n"


def _attempts(root: Path, outside: Path) -> list[tuple[str, str, Path]]:
    """(nome, comando, canário que o comando tentaria alterar)."""
    rel_canary = outside / "canary_rel.txt"
    abs_canary = outside / "canary_abs.txt"
    cd_canary = outside / "canary_cd.txt"
    link_target = outside
    py_canary = outside / "canary_python.txt"
    rel_from_root = os.path.relpath(rel_canary, root)
    return [
        ("rel", f"printf '{PAYLOAD}' > '{rel_from_root}'", rel_canary),
        ("abs", f"printf '{PAYLOAD}' > '{abs_canary}'", abs_canary),
        ("cd", f"cd / && printf '{PAYLOAD}' > '{cd_canary}'", cd_canary),
        ("symlink", f"ln -s '{link_target}' link_out && printf '{PAYLOAD}' > link_out/canary_symlink.txt",
         outside / "canary_symlink.txt"),
        ("python", f"python3 -c \"open('{py_canary}','w').write('{PAYLOAD.strip()}\\n')\"", py_canary),
    ]


def run(workdir: Path) -> dict:
    root = workdir / "experiment_root"
    outside = workdir / "outside"
    root.mkdir(parents=True, exist_ok=True)
    outside.mkdir(parents=True, exist_ok=True)

    executor = ToolExecutor(root)
    results = []
    for name, command, canary in _attempts(root, outside):
        canary.write_text(ORIGINAL, encoding="utf-8")
        result = executor.run_bash(command, timeout=20)
        content = canary.read_text(encoding="utf-8") if canary.exists() else None
        escaped = content != ORIGINAL
        results.append({
            "attempt": name,
            "command_ok": result.ok,
            "exit_code": result.exit_code,
            "canary": str(canary),
            "canary_changed": escaped,
        })
    isolated = not any(r["canary_changed"] for r in results)
    return {
        "I3b_holds": isolated,
        "environment": {
            "user": getpass.getuser(),
            "uid": os.getuid(),
            "platform": platform.platform(),
            "docker_socket_present": Path("/var/run/docker.sock").exists(),
        },
        "executor_root": str(root),
        "attempts": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--workdir", type=Path, default=None)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="dv-canary-"))
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    try:
        report = run(workdir)
    except Exception as exc:  # noqa: BLE001 - o teste em si falhou: código 2, não "isolado"
        print(f"canary could not run: {type(exc).__name__}: {exc}")
        return 2
    for r in report["attempts"]:
        verdict = "ESCAPED" if r["canary_changed"] else "contained"
        print(f"{r['attempt']:8s} {verdict:10s} command_ok={r['command_ok']} exit={r['exit_code']}")
    print(f"\nI3b holds: {report['I3b_holds']}")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if report["I3b_holds"] else 1


if __name__ == "__main__":
    sys.exit(main())
