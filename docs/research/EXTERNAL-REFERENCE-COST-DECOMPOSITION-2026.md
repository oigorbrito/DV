# Referência externa: decomposição de custo e comparação de modelos (2026)

**Status:** LITERATURE_EVIDENCE, nível E0. Não sustenta nenhuma afirmação experimental do DV (`docs/EVIDENCE-POLICY.md`).
**Origem:** texto colado pelo mantenedor, sem URL do documento original. Citações inline a Morph, TestDino, Databricks AI Index, TrueFoundry e Fello AI. Dois links aparecem como `[invalid URL]`.
**Data da avaliação:** 2026-11-19.

## 1. O que o texto propõe

Quatro ideias para o esquema de telemetria do DV:

1. **T0 (overhead de prefixo):** custo fixo de sistema, instruções e schemas, medido com um prompt trivial.
2. **Tokens brutos vs. faturados:** separar input sem cache, escrita de cache, leitura de cache e saída, por turno.
3. **Amplificação de turnos:** número de chamadas até o veredito.
4. **Custo por tarefa correta (C_YES):** custo total de todas as execuções, incluindo falhas, dividido pelos vereditos YES.

Também propõe medir invalidação de cache, custo de superfície de ferramentas (CLI vs. MCP) e percentis (P50, P95, P99), e uma comparação de modelos com preços e benchmarks.

## 2. Verificação

| Afirmação do texto | Status | Motivo |
|---|---|---|
| Os custos por 1.000 turnos ($2,23 off-peak, $4,47 pico, $40,50 GLM-5.3) | **Verificada (aritmética)** | Recalculados com os preços citados, batem. Premissas: 100K de entrada, 95% em cache, 2K de saída. |
| A razão de 9x a 18x entre GLM-5.3 e Flash | **Verificada (aritmética)** | Decorre das contas acima. |
| Janelas de pico em UTC e conversão para UTC-3 | **Verificada (aritmética)** | Horário comercial de Brasília não cai nas janelas de pico declaradas. |
| Pontuações (AA Intelligence Index, DeepSWE, Terminal-Bench, Databricks AI Index) | **Não verificada** | Depende de fontes externas. Nenhuma foi aberta nesta avaliação. |
| Databricks e TrueFoundry "adotaram" a mesma métrica do DV | **Não verificada e sem base** | Afirmação sem fonte. Não deve ser usada como argumento. |
| "281x" de diferença entre mediana e cauda, "3,7x" com troca de MCP por CLI, "140 turnos" no Gemini | **Não verificada** | Atribuídas a TestDino e Databricks, sem acesso à fonte. |
| Links `[invalid URL]` | **Referência quebrada** | Impossível localizar a fonte a partir do texto. |

## 3. Referências internas que o texto usa

| Referência | Status no repositório |
|---|---|
| `dv_pilot_harness.py` | **Arquivo legado, não existe em `tools/`.** O WP-03 verifica seu hash no checkout do repositório antigo (`0f7f936e…`). Alterá-lo quebraria a verificação e violaria a regra 4 do `AGENTS.md`. |
| `oigorbrito/RJ`, tarefa `RJ-04`, `bugfix_dotnet`, `dotnet test` | **Não existem no repositório.** |
| Piloto P0, corpus de 36 tarefas, seis famílias, holdout selado | **Não existem como artefato verificável.** O conceito de holdout existe nos protocolos, mas não este piloto. |
| Prompt "Reply OK" para medir T0 | **Não existe.** |

## 4. O que é útil como hipótese (não como código)

Os conceitos abaixo são medíveis e, em parte, já estão no DV. Podem virar claims **UNTESTED** depois de um desenho experimental, nunca implementação direta.

| Conceito | Já existe no DV? | Observação |
|---|---|---|
| Custo de falhas e retentativas no total | **Sim** | `docs/EXPERIMENTAL-GOVERNANCE.md`: falhas retêm todo o custo. `run-accounting-contract-v1.json`: custo de recursos consumidos é mantido. |
| Ausência explícita de dado | **Sim** | `failure-attribution-v1.json`: telemetria ausente nunca é zero. |
| Custo monetário como métrica | **Sim, como métrica** | `run-accounting-contract-v1.json` lista custo total monetário. Não há campo de custo no `dv_ledgers`. |
| Tokens brutos vs. faturados por turno | **Não** | Pendência R1.2 do roadmap (semântica de cache). |
| T0 (overhead fixo do sistema) | **Não** | Conceito novo para o DV. Precisa de um experimento que o justifique. |
| Amplificação de turnos | **Parcial** | O protocolo Luna conta invocações, não turnos. |
| Invalidação de cache | **Não** | Relevante só se o experimento usar cache. |
| Superfície de ferramentas (CLI vs. MCP) | **Não** | Fora da pergunta atual, a menos que vire fator experimental. |

## 5. Comparação de modelos

Fora da pergunta do DV. A própria `EVIDENCE-POLICY` trata preço como sujeito a `PRICE_SHIFT`: uma comparação de preço fica desatualizada quando o preço muda. A escolha de modelo deve seguir o protocolo experimental, e não um ranking de preço. O Luna é a referência documentada da trilha ativa, e não uma regra.

## 6. Decisão

- **Nenhuma alteração de código** a partir deste texto. O alvo citado (`dv_pilot_harness.py`) é legado e está congelado por hash.
- **Nenhum claim novo** é registrado agora. Se algum conceito da seção 4 virar hipótese, ele entra em `claims/claims-v1.json` como `UNTESTED`, depois de um desenho experimental.
- Para verificar as afirmações de benchmark, é preciso que o mantenedor forneça as URLs das fontes. Sem elas, ficam como "não verificadas".
