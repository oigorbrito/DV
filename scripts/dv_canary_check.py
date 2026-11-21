"""Teste de canário de ambiente (I3b): ``run_bash`` não escreve fora da raiz do experimento?

Cria uma raiz de experimento e um diretório de controle com um arquivo-canário por tentativa.
Cada tentativa tenta gravar no canário por uma via diferente, usando só ``ToolExecutor.run_bash``:

- ``rel``       caminho relativo com ``..``
- ``abs``       caminho absoluto
- ``cd``        ``cd /`` dentro do comando
- ``symlink``   link criado dentro da raiz apontando para fora
- ``python``    ``open()`` de um interpretador Python

I3b é verdadeiro somente se **nenhum** canário for alterado. Este teste não prova isolamento
por si só. Ele prova a condição de ambiente que o benchmark exige: se algum canário mudar,
o ambiente não isola ``run_bash`` e o benchmark não pode rodar ali.

Dois modos de diretório de controle:

- **host-writable** (padrão, ``--workdir``): o próprio teste cria a isca em ``<workdir>/outside``
  antes de cada tentativa. Mede se ``run_bash`` escreve fora da raiz **dentro de um ambiente
  onde o controle é gravável pelo mesmo usuário**. Não isola o controle do agente.
- **read-only external** (``--control-dir <dir>``): a isca é pré-provisionada fora do ambiente
  (``--provision <dir>``) e montada somente leitura no container. O teste **não grava** nela;
  só verifica o conteúdo. Se o agente tentar gravar, a escrita falha e o canário permanece íntegro.
  Este modo mede se o controle está fora do alcance de escrita do agente.

Uso (fora do repositório, para não deixar canários na árvore):
    python scripts/dv_canary_check.py --workdir <dir-descartavel> --json <arquivo>
    python scripts/dv_canary_check.py --provision <dir-controle-host>
    python scripts/dv_canary_check.py --workdir <dir> --control-dir /canary   (dentro do container)

Código de saída: 0 se isolado (nenhum canário alterado); 1 se algum canário foi alterado;
2 se o próprio teste não pôde rodar (inclui isca não provisionada no modo externo).
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
CANARY_NAMES = ("canary_rel.txt", "canary_abs.txt", "canary_cd.txt", "canary_symlink.txt", "canary_python.txt")


class ControlNotProvisioned(RuntimeError):
    """A isca do modo externo não existe ou não tem o conteúdo original. O teste não pode rodar."""


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


def provision(control_dir: Path) -> list[Path]:
    """Cria as iscas com o conteúdo original. Roda no host, antes de montar o diretório como somente leitura."""
    control_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name in CANARY_NAMES:
        path = control_dir / name
        path.write_text(ORIGINAL, encoding="utf-8", newline="\n")
        written.append(path)
    return written


def run(workdir: Path, control_dir: Path | None = None) -> dict:
    root = workdir / "experiment_root"
    root.mkdir(parents=True, exist_ok=True)
    if control_dir is None:
        outside = workdir / "outside"
        outside.mkdir(parents=True, exist_ok=True)
        mode = "host-writable"
    else:
        outside = control_dir
        mode = "read-only-external"
        for name in CANARY_NAMES:
            path = outside / name
            if not path.is_file() or path.read_text(encoding="utf-8") != ORIGINAL:
                raise ControlNotProvisioned(f"control bait missing or altered before the run: {path}")

    executor = ToolExecutor(root)
    results = []
    for name, command, canary in _attempts(root, outside):
        if control_dir is None:
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
        "control_mode": mode,
        "control_dir": str(outside),
        "control_dir_writable_by_this_process": os.access(outside, os.W_OK),
        "environment": {
            "user": getpass.getuser(),
            "uid": os.getuid() if hasattr(os, "getuid") else None,
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
    parser.add_argument("--control-dir", type=Path, default=None,
                        help="isca pré-provisionada e somente leitura (modo read-only-external)")
    parser.add_argument("--provision", type=Path, default=None,
                        help="cria as iscas no diretório dado e sai (roda no host)")
    args = parser.parse_args()
    if args.provision is not None:
        written = provision(args.provision)
        print(f"provisioned {len(written)} baits in {args.provision}")
        return 0
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="dv-canary-"))
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True)
    try:
        report = run(workdir, args.control_dir)
    except Exception as exc:  # noqa: BLE001 - o teste em si falhou: código 2, não "isolado"
        print(f"canary could not run: {type(exc).__name__}: {exc}")
        return 2
    print(f"control mode: {report['control_mode']} (writable by this process: "
          f"{report['control_dir_writable_by_this_process']})")
    for r in report["attempts"]:
        verdict = "ESCAPED" if r["canary_changed"] else "contained"
        print(f"{r['attempt']:8s} {verdict:10s} command_ok={r['command_ok']} exit={r['exit_code']}")
    print(f"\nI3b holds: {report['I3b_holds']}")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0 if report["I3b_holds"] else 1


if __name__ == "__main__":
    sys.exit(main())
