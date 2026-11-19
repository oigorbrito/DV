# AGENTS.md

These rules apply to humans and automated coding/research agents working in this repository.

Este arquivo combina duas camadas que se aplicam simultaneamente; nenhuma substitui a outra:

- **Parte A** — governança experimental do programa DV, preservada integralmente.
- **Parte B** — adaptador de evidência/OSPS do template, incorporado integralmente.

## Operating posture

1. NDV is research-first. Do not implement a runtime, router, orchestrator, memory system, workflow engine, model gateway, generic agent framework, or adaptive controller unless an approved experiment and architecture gate explicitly require it.
2. Existing ecosystem capabilities must be evaluated for reuse before custom implementation.
3. Experimental contracts are immutable after freeze. If semantics change, create a new version; do not silently edit frozen evidence.
4. Historical artifacts in `oigorbrito/dv` must not be rewritten or rebranded as if they were originally NDV artifacts.
5. Executor-reported success is not verified success. Independent verification governs acceptance.
6. Account for complete system cost, including failures, routing, shaping, context, planning, execution, handoff, verification, retries, latency, local hardware/energy where reliable, and coordination overhead.
7. `NO_BUILD`, `REUSE_ONLY`, `REFUTED_IN_SCOPE`, and `INCONCLUSIVE` are valid outcomes.
8. Never tune task selection, stopping criteria, repetitions, or analysis because a favored treatment is leading or losing.
9. Do not expose secrets in files, logs, issues, commits, prompts, or result artifacts.
10. Prefer the smallest change that preserves scientific traceability and reproducibility.

## Decision order

For every proposed capability:

`claim -> controlled experiment -> evidence level -> decision -> reuse/adapt/build choice`

Never use:

`idea -> implementation`.

## Architecture default

Use this order unless evidence justifies otherwise:

`REUSE -> ADAPT -> WRAP -> FORK -> BUILD CUSTOM`

A custom capability is blocked when existing prior art satisfies the required contract at lower expected integration and maintenance cost.

## Verification rule

`EXECUTOR_DONE != VERIFIED_SOLVED`

Verification must be attributable, versioned, reproducible, and independent of the treatment output when the experiment requires an independent oracle.

## Provenance

New NDV work belongs in this repository. Historical DV evidence remains authoritative at its original source repository/commit. Reference legacy artifacts by repository, path, commit and hash when needed.

## Nome canônico

1. O nome canônico do programa e do repositório é **DV**. Títulos, documentação e identificadores novos usam `DV` / `dv`. Novas ferramentas usam o prefixo `dv_` (por exemplo, `tools/dv_*.py`).
2. `NDV` / `ndv` não é mais nome canônico. Nomes legados (`tools/ndv_*.py`, workflows `ndv`, experimentos, claims, schemas) são mantidos sem renomeação, porque workflows e evidência congelada os referenciam.
3. Renomear arquivos, identificadores ou conteúdo congelado exige nova versão do contrato (regra 3 de Postura operacional) e a atualização de todas as referências no mesmo change, com verificação posterior.
4. Não reescreva `ndv` em artefatos históricos ou evidência congelada. Ao citá-los, use o nome original e a referência de commit/hash.

## Invariantes de governança experimental

Estas regras são herdadas do README do programa e valem para todo o trabalho DV:

```text
EXECUTOR_DONE != VERIFIED_SOLVED
SYNTHETIC_PASS != PRODUCT_PROMOTION
UPSTREAM_FEATURE_EXISTS != LOCAL_NEED
MECHANISM_AVAILABLE != MECHANISM_NECESSARY
```

Modo de engenharia atual: `REUSE_ONLY` (ver README). Fases experimentais P1–P5 não começam só porque uma fase anterior existe.

Pontos de partida de governança: `docs/EXPERIMENTAL-GOVERNANCE.md`, `docs/EVIDENCE-POLICY.md`, `docs/research/EXPERIMENTAL-PROGRAM.md`, `docs/research/UPSTREAM-REUSE-PLAN.md`.

## Inventário de proveniência DV

O histórico DV (repositório de origem, commits e hashes) está em `provenance/dv-legacy-manifest.json`. Evidência legada não é reclassificada como evidência DV.

## Parte B — Evidência e maturidade (template)

Estas instruções são um adaptador do repositório construído sobre controles definidos externamente. Elas **não são, por si mesmas, um requisito do OpenSSF, NIST ou DORA**.

> **Regra Fundamental de Evidência:**
> **IMPLEMENTED != EXECUTED, EXECUTED != VERIFIED**
> Nenhum agente deve assumir que algo funciona ou está em conformidade apenas porque o código foi escrito. A evidência observável e executável é o único critério aceito.

Antes de alterar código ou documentação:

1. Leia `policy/project-state.json`, `docs/MATURITY.md`, `docs/ARCHITECTURE.md` e `docs/TESTING.md`.
2. Execute `python scripts/harness.py` antes de afirmar que o repositório está pronto.
3. Nunca converta evidência ausente, desatualizada, inacessível ou não executada em `PASS`.
4. Use exatamente estes significados para os resultados:
   - `PASS`: a evidência foi encontrada ou executada e satisfaz a verificação local.
   - `GAP`: um requisito local aplicável não é satisfeito.
   - `UNKNOWN_EXTERNAL`: o requisito depende de um estado do repositório/plataforma que este harness não consegue comprovar localmente.
   - `NOT_APPLICABLE`: o gatilho documentado não é atualmente verdadeiro.
5. Rótulos do produto como `prototype`, `MVP` ou `production` são apenas descritivos. Eles não promovem a maturidade OSPS.
6. Se um release for observado (arquivo de estado ou tag Git), execute as verificações condicionadas ao release e relate cada nova lacuna aplicável.
7. Se o número de mantenedores ou a população de usuários mudar materialmente, sinalize `MATURITY_REASSESSMENT_REQUIRED`; não promova silenciosamente o nível-alvo.
8. Não adicione uma afirmação de segurança, release, arquitetura ou conformidade sem que a evidência de suporte seja versionada ou verificável externamente.
9. Prefira a menor alteração que feche uma lacuna documentada. Não adicione controles apenas por aparência.
10. Após alterações materiais no projeto, execute novamente o harness e os testes.
