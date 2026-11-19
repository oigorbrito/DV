# Política de evidência do DV

## Níveis de evidência

- `E0 — EXPLORATORY`: observação isolada, piloto, sinal da literatura ou nota de engenharia. Não autoriza arquitetura.
- `E1 — REPRODUCED`: resultado repetido na mesma configuração. Útil para estabilidade, não para afirmações causais fortes.
- `E2 — CONTROLLED`: tratamento e linha de base congelados, ambiente reproduzível, contabilidade completa e atribuição explícita de falhas.
- `E3 — HOLDOUT_CONFIRMED`: resultado controlado que sobrevive a um holdout selado de forma prospectiva ou a uma confirmação externa equivalente.
- `E4 — INDEPENDENTLY_REPLICATED`: resultado reproduzido de forma independente, fora do caminho experimental original.

Capacidades centrais obrigatórias normalmente exigem `E3`, a menos que uma exceção explícita seja documentada com escopo e justificativa.

## Estados de afirmação

- `UNTESTED`
- `EXPLORATORY_SIGNAL`
- `SUPPORTED_IN_SCOPE`
- `REFUTED_IN_SCOPE`
- `INCONCLUSIVE`
- `STALE`
- `SUPERSEDED`

O apoio é sempre limitado ao escopo. Uma afirmação apoiada para uma família de modelos, distribuição de tarefas, benchmark, regime de custo ou data específica não deve ser generalizada silenciosamente para além disso.

## Classes de evidência

O DV mantém estas classes separadas:

- `LITERATURE_EVIDENCE`
- `NDV_EXPERIMENTAL_EVIDENCE`
- `ENGINEERING_OBSERVATION`

A literatura externa pode motivar ou contestar uma afirmação, mas não conta como confirmação experimental do DV.

## Gatilhos de reteste

Afirmações podem se tornar `STALE` quando forem materialmente afetadas por:

- `NEW_FRONTIER_EXECUTOR`
- `MAJOR_MODEL_VERSION_CHANGE`
- `PRICE_SHIFT`
- `MAJOR_HARNESS_CHANGE`
- `VERIFIER_CHANGE`
- `BENCHMARK_INVALIDATION`
- `TASK_DISTRIBUTION_SHIFT`
- `NEW_PRIOR_ART`
- `REPLICATION_CONTRADICTION`

Tornar-se desatualizada não apaga evidência histórica; apenas limita a aplicabilidade atual.

## Regra de promoção

O fluxo normativo é:

`claim -> experiment -> evidence -> decision -> architecture`

Não:

`idea -> implementation`.

## Resultados negativos e nulos

Resultados negativos, nulos e inconclusivos são desfechos de primeira classe. Não suprimir tratamentos fracassados, execuções instáveis ou evidências que reduzam o escopo do DV.

## Disciplina de parada

Regra fonte: `docs/EXPERIMENTAL-GOVERNANCE.md` (seções *Regras centrais* e *Regras de parada*).
