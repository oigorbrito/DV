"""Validador de integridade de tarefas, oráculos e higiene de ambiente (``src/dv_harness/task_integrity.py``).

Uso:
    python scripts/dv_validate_tasks.py --tasks-dir <dir-com-manifestos> [--output-json <arquivo>] [--strict|--no-strict]

Cada ``*.json`` em ``--tasks-dir`` é um manifesto ``dv-batch-manifest-v1``. O validador roda tudo em
diretórios temporários e não altera os manifestos.

Saída: 0 quando o lote passa, ou quando ``--no-strict`` mesmo com falhas; 1 quando ``--strict`` (padrão) e
alguma tarefa falha; 2 para uso inválido (sem manifestos, diretório inexistente, manifesto malformado).
A tabela de verificação e o ``dataset_sha256`` são impressos sempre.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tasks-dir", type=Path, required=True, help="diretório com manifestos *.json")
    parser.add_argument("--output-json", type=Path, default=None, help="grava o relatório completo em JSON")
    parser.add_argument("--strict", action=argparse.BooleanOptionalAction, default=True,
                        help="padrão: sai com 1 se qualquer tarefa falhar no gate")
    return parser


def main(argv: list[str] | None = None) -> int:
    from dv_harness import batch
    from dv_harness.task_integrity import integrity_gate, integrity_table, report_to_dict

    args = _parser().parse_args(argv)
    if not args.tasks_dir.is_dir():
        print(f"blocked: --tasks-dir is not a directory: {args.tasks_dir}")
        return 2
    manifests = sorted(args.tasks_dir.glob("*.json"))
    if not manifests:
        print(f"blocked: --tasks-dir has no *.json manifests: {args.tasks_dir}")
        return 2
    tasks = []
    try:
        for manifest in manifests:
            _suite, loaded, _sha = batch.load_manifest(manifest)
            tasks.extend(loaded)
    except (batch.ManifestError, ValueError, OSError) as exc:
        print(f"blocked: malformed manifest: {exc}")
        return 2

    report = integrity_gate(tasks)
    print(integrity_table(report))
    if args.output_json is not None:
        # bytes com LF explícito: o relatório pode ser pinado e tem de ser idêntico no Windows e no WSL
        text = json.dumps(report_to_dict(report), indent=2, ensure_ascii=False) + "\n"
        args.output_json.write_bytes(text.encode("utf-8"))
    if args.strict and not report.passed:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
