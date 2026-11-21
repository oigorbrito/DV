"""Análise pareada e bootstrap do protocolo wp08 (``src/dv_harness/ablation_analysis.py``, fecha GAP-8).

Uso (sem chamadas de modelo; só lê resumos já gravados):
    python scripts/dv_analyze_ablation.py --arms-dir <dir> --output-json <arquivo.json>
    python scripts/dv_analyze_ablation.py --arm-a DIR --arm-b DIR --arm-c DIR --arm-d DIR --output-json <arquivo.json>
    python scripts/dv_analyze_ablation.py --print-code-sha256

``--arms-dir`` contém as pastas ``A_B0``, ``B_B0_PLUS``, ``C_SOLO_R1`` e ``D_DV_LADDERING``. Cada uma contém
``rep-NN/`` com o resumo do lote. ``--print-code-sha256`` imprime o hash que deve ser fixado na pré-condição
``pc6`` do wp08 e encerra sem ler entradas.

Saída: 0 análise concluída (qualquer veredito). 2 entrada inválida ou incompatível; nada é gravado.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))


def _parser() -> argparse.ArgumentParser:
    from dv_harness import ablation_analysis as aa

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--arms-dir", type=Path, default=None, metavar="DIR",
                        help="diretório com as quatro pastas de braço")
    parser.add_argument("--arm-a", type=Path, default=None, metavar="DIR", help="pasta do braço A_B0")
    parser.add_argument("--arm-b", type=Path, default=None, metavar="DIR", help="pasta do braço B_B0_PLUS")
    parser.add_argument("--arm-c", type=Path, default=None, metavar="DIR", help="pasta do braço C_SOLO_R1")
    parser.add_argument("--arm-d", type=Path, default=None, metavar="DIR", help="pasta do braço D_DV_LADDERING")
    parser.add_argument("--seed", type=int, default=aa.DEFAULT_SEED, help=f"semente; padrão: {aa.DEFAULT_SEED}")
    parser.add_argument("--resamples", type=int, default=aa.DEFAULT_RESAMPLES,
                        help=f"reamostragens bootstrap; padrão: {aa.DEFAULT_RESAMPLES}")
    parser.add_argument("--output-json", type=Path, default=None, metavar="FILE", help="arquivo de saída (novo)")
    parser.add_argument("--print-code-sha256", action="store_true",
                        help="imprime o analysis_code_sha256 de ablation_analysis.py e sai")
    return parser


def _arm_dirs(args: argparse.Namespace) -> dict[str, Path]:
    from dv_harness import ablation_analysis as aa

    explicit = {aa.ARM_A: args.arm_a, aa.ARM_B: args.arm_b, aa.ARM_C: args.arm_c, aa.ARM_D: args.arm_d}
    if args.arms_dir is not None and any(v is not None for v in explicit.values()):
        raise aa.AblationInputError("use --arms-dir or the explicit --arm-x flags, not both")
    if args.arms_dir is not None:
        return {arm: Path(args.arms_dir) / arm for arm in aa.ARM_IDS}
    flags = {aa.ARM_A: "--arm-a", aa.ARM_B: "--arm-b", aa.ARM_C: "--arm-c", aa.ARM_D: "--arm-d"}
    missing = [flags[arm] for arm in aa.ARM_IDS if explicit[arm] is None]
    if missing:
        raise aa.AblationInputError(f"missing {missing} (or give --arms-dir)")
    return {arm: Path(path) for arm, path in explicit.items()}


def main(argv: list[str] | None = None) -> int:
    from dv_harness import ablation_analysis as aa

    args = _parser().parse_args(argv)
    if args.print_code_sha256:
        print(aa.analysis_code_sha256())
        return 0
    if args.output_json is None:
        print("blocked: --output-json is required. Nothing was written.")
        return 2
    if args.output_json.exists():
        print(f"blocked: {args.output_json} already exists; use a new file. Nothing was written.")
        return 2
    try:
        dirs = _arm_dirs(args)
        arms = {arm: aa.load_arm(arm, path) for arm, path in dirs.items()}
        result: dict[str, Any] = aa.analyze(arms, seed=args.seed, resamples=args.resamples)
    except aa.AblationInputError as exc:
        print(f"blocked: {exc}. Nothing was written.")
        return 2
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"dataset_sha256: {result['dataset_sha256']}")
    print(f"analysis_code_sha256: {result['analysis_code_sha256']}")
    print(f"H1 (B0_PLUS vs B0): {result['H1_b0_plus_vs_b0']['verdict']}")
    print(f"H2 (DV_LADDERING vs SOLO_R1): {result['H2_dv_laddering_vs_solo_r1']['verdict']}")
    print(f"summary: {args.output_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
