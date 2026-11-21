"""Variantes opt-in do harness e proveniência das referências (fonte única).

``b0`` é o padrão e não muda comportamento. ``b0_plus`` é uma ablação experimental: liga
``edit_file`` com fallback de 3 estágios e diff unificado, truncamento de cauda para ``run_bash``,
placeholders informativos no ``ContextManager`` e o aviso de repetição (doom loop) no ``AgentLoop``.
Nenhuma dessas regras é promovida por evidência aqui; cada uma precisa de um experimento congelado.

Proveniência: os hashes abaixo são os ``HEAD`` dos checkouts locais no momento da medição. Os
checkouts tinham alterações não commitadas (contagem em ``dirty_entries``), então o hash sozinho
não reproduz o conteúdo lido. Ver ``docs/harness_gap_matrix.md``.
"""

from __future__ import annotations

VARIANT_B0 = "b0"
VARIANT_B0_PLUS = "b0_plus"
HARNESS_VARIANTS: tuple[str, ...] = (VARIANT_B0, VARIANT_B0_PLUS)
DEFAULT_VARIANT = VARIANT_B0

TAIL_HEAD_RATIO = 0.2  # b0_plus: 20% início, 80% fim (o traceback fica no fim)
DOOM_LOOP_THRESHOLD = 3  # b0_plus: a 3ª chamada idêntica consecutiva recebe o aviso

REFERENCE_PROVENANCE: dict[str, dict[str, object]] = {
    "pi_finecode": {
        "checkout": "D:/projetos/finecode",
        "head": "ad41b5c344d5399982718720db975b1e020e2c40",
        "dirty_entries": 1,
        "status": "LOCAL_CHECKOUT_DIRTY",
    },
    "opencode_smag_rex": {
        "checkout": "D:/projetos/smag-rex/packages/opencode",
        "head": "5b73e034668904d3cc5871e9940e4bdbdc95b4f7",
        "dirty_entries": 9,
        "status": "LOCAL_CHECKOUT_DIRTY",
    },
    "codex_cli": {
        "checkout": None,
        "head": None,
        "status": "INCONCLUSIVE_NOT_LOCATED",
    },
}


B0_PLUS_GUIDELINES: tuple[str, ...] = (
    "Do not copy the line-number prefix (N<TAB>) from read_file output into edit_file old_text.",
    "Prefer edit_file over write_file to change an existing file; use write_file only for new files or complete rewrites.",
    "Use read_file and edit_file instead of cat, sed or heredocs in run_bash.",
    "When read_file reports truncation, continue with start_line set to the line number it names.",
    "Keep edit_file old_text concise: only the lines that change plus the minimum surrounding context that "
    "identifies a single occurrence.",
)


def build_system_prompt(base: str, variant: str) -> str:
    """Prompt de sistema da variante. b0: o texto base, sem alteração. b0_plus: acrescenta diretrizes de uso."""
    check_variant(variant)
    if variant != VARIANT_B0_PLUS:
        return base
    return base + "\n\nTool guidelines:\n" + "\n".join(f"- {line}" for line in B0_PLUS_GUIDELINES)


def check_variant(variant: str) -> str:
    """Valida o nome da variante. Levanta ``ValueError`` para nomes desconhecidos."""
    if variant not in HARNESS_VARIANTS:
        raise ValueError(f"harness_variant must be one of {', '.join(HARNESS_VARIANTS)}, got {variant!r}")
    return variant
