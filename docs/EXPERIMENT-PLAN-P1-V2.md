# Desenho experimental P1, versão 2 (proposta de emenda)

**Status: `DRAFT_AMENDMENT`. Não é normativo até ser ratificado por um contrato versionado.**

Este documento deriva de, e não edita, o plano mestre congelado:

- Plano: `docs/research/MASTER-TEST-PLAN-P1.md` e `experiments/p1/master-test-plan-v1.json`
  (`plan_id: dv-p1-master-test-plan-v1`, `status: MASTER_TEST_PLAN_READY`), commit **`771bfda`**
  ("docs: freeze P1 master test plan"). Esse commit está na branch local `feat/baseline-calibration`
  e não foi publicado.
- Análise estatística: `experiments/p1/statistical-analysis-plan-v1.json` (no mesmo commit).

Nada aqui altera o `771bfda`. Se uma regra deste documento contradiz o plano congelado, a regra
congelada prevalece até a ratificação. O conflito conhecido está na seção 3.

Base empírica do que é afirmado: o harness mede tokens por chamada, os quatro baldes de KV-Cache
(com `None` quando não expostos), `cost_basis`, `false_finish` e `infra_finished` (ver
`docs/TESTING.md`, seção de evidência de container). Tudo que não tem essa base está marcado como
**pendente**.

## 1. Unidade experimental

A unidade de avaliação é a tupla **(Modelo, Harness, Ambiente, Orçamento)**.

- Um resultado vale só para a tupla em que foi medido. Uma comparação de harness exige fixar
  Modelo, Ambiente e Orçamento. O mesmo vale para as outras três dimensões.
- Ambiente inclui a imagem de container, as quotas (`--cpus`, `--memory`, `--pids-limit`,
  `--timeout-s`), `--network none` e a versão fixada das dependências.
- Orçamento inclui `max_steps`, `max_tokens`, `max_cost_usd` e `error_limit`, todos declarados
  no run e gravados no ledger.

## 2. Fases de evidência

| Fase | N | O que pode concluir | O que é vedado |
|---|---|---|---|
| **Screening de pipeline** | **10** | Que o pipeline roda de ponta a ponta e produz as métricas. Resultado binário: `PIPELINE_VALID` ou `PIPELINE_INVALID`. | **Concluir superioridade de qualquer braço.** Reportar taxa de resolução como efeito. |
| Ablação científica | **N ≥ 30 (alvo 50)** | Diferença pareada entre braços de uma única alavanca, com IC 95% e a tríade da seção 4. | Conclusão universal. Generalização além dos estratos observados. |

Regras do N=10:

- Serve apenas para verificar pipeline, custo e ruído de medição. Não entra em nenhuma tabela de
  eficácia.
- Um screening com N=10 que mostre diferença entre braços não é evidência. Mostra apenas que a
  medição tem sinal.

## 3. Ablações: amostra e estratificação

**Conflito conhecido.** O plano congelado diz: *"No fixed sample size is asserted before variance
and resource review"* (`MASTER-TEST-PLAN-P1.md`, §3, linhas 63–64). N≥30 é, portanto, uma
emenda. Esta versão o propõe como **piso de desenho**, não como cálculo de potência:

- N ≥ 30 tarefas admitidas, com alvo 50, para qualquer afirmação de ablação.
- Estratificação por horizonte: **curto** versus **médio/longo**, com no mínimo 10 tarefas por
  estrato.
- Análise **pareada**: mesma tarefa, mesmo ambiente e mesmo orçamento em todos os braços.
- Se a variância observada no screening exigir mais tarefas, o N aumenta antes de qualquer
  conclusão. Se o piso não for atingido, o resultado é `INCONCLUSIVE`.

**Pendência bloqueante:** a definição operacional de horizonte. Ela precisa ser fixada antes da
observação, por exemplo pela mediana de chamadas de ferramenta de um run de referência congelado.
Não foi escolhida aqui, porque não há base empírica para escolher um critério.

## 4. Relatório obrigatório: tríade de Pareto

Todo relatório de ablação traz as três partes. Não há métrica composta.

1. **Verified Solve Rate**, com IC 95%.
   - Numerador: tarefas com `outcome = SOLVED` (veredito de verificador independente).
   - Denominador: todas as tarefas admitidas do braço. `INCONCLUSIVE` conta como não resolvido e é
     reportado à parte.
   - Intervalo de Wilson para a proporção. Para diferenças pareadas, bootstrap percentil.
     Nenhum p-value é usado como limiar, conforme o plano congelado.

2. **Custo real efetivo do provedor, incluindo runs falhos.**
   - Soma do `cost_usd` de **todas** as chamadas do run, inclusive tentativas que falharam
     quando o provedor expõe o uso.
   - Dividido pelo número de resoluções verificadas. Reportado com a `cost_basis` de cada chamada.
   - Se qualquer chamada tiver `cost_basis = unknown`, o custo por resolução **não** é reportado
     como número. Um `0` nunca substitui um custo desconhecido.

3. **Mediana e IQR de tokens no subconjunto pareado**, junto com:
   - os quatro baldes de KV-Cache, medidos ou `None`:
     - `uncached_input_tokens`: hoje sempre `None` com litellm 1.110.1; reportado como "não medido";
     - `cache_write_tokens`;
     - `cache_read_tokens`;
     - `output_tokens`, com `reasoning_tokens` como subcampo, nunca somado;
   - latência mediana e IQR, separando `llm_wall_clock_ms` de `tool_wall_clock_ms`;
   - `false_finish_rate` = término declarado (`completed`) e `outcome = FAILURE`, dividido por
     `completed` com outcome verificado. Sem denominador verificado, o valor é `None`.

Um valor `None` em qualquer parte nunca é tratado como zero. Ele aparece com o motivo, como o
harness já registra em `unmeasured`.

## 5. Uma alavanca por braço

Cada braço de ablação muda **exatamente uma** alavanca em relação ao braço de referência
(B0: mesmo modelo, harness, ambiente e orçamento). As alavancas candidatas são estas:

| Alavanca | Braço alternativo | Estado da implementação |
|---|---|---|
| L1: preservação seletiva de erros ativos | preserva observações de erro no `ContextManager` | **não implementada** |
| L2: masking cache-aware | mascara observações respeitando o prefixo em cache | **não implementada** |
| L3: `edit_file` | remove `edit_file` do conjunto de ferramentas (demais iguais) | implementada; a ablação ainda não foi executada |

Regras:

- Um braço que muda duas alavancas é descartado da comparação, não reinterpretado.
- Uma alavanca só entra em análise de ablação depois de ter teste próprio que prove que o braço
  muda apenas aquela variável (diff de configuração gravado no ledger).

## 6. Gate de inversão de dominância (pré-requisito de qualquer Router)

Nenhum Router dinâmico é construído antes que este gate seja **aprovado** em dados pareados.
Estado atual: **fechado**. Não há runs reais.

O gate passa somente se, em pelo menos um estrato de horizonte:

1. uma política dinâmica não é dominada por nenhum braço estático na tríade. Dominância aqui
   significa menor custo por resolução **e** taxa de resolução igual ou maior, com IC que
   sustente a diferença (bootstrap pareado do custo, excluindo zero);
2. o custo de roteamento (tokens e chamadas do próprio roteador) está incluído nos números;
3. a hipótese **H0-C** do plano congelado (*"B3 is not materially worse than a more dynamic
   policy"*) é rejeitada nos dados pareados.

A **inversão de dominância** ocorre quando um braço estático domina o dinâmico em todos os
estratos. Nesse caso o resultado é `NO_BUILD`, conforme a §11 do plano congelado.

## 7. O que já tem base empírica e o que está pendente

| Item | Situação |
|---|---|
| Unidade (Modelo, Harness, Ambiente, Orçamento) | formalizada aqui |
| Screening N=10 como pipeline | formalizado; pipeline ainda não executado com provedor real |
| Ablação N≥30 estratificada | **proposta**; conflito com §3 do plano congelado; horizonte não definido |
| Tríade de Pareto | formalizada; medição dos quatro baldes e de `false_finish` **validada no harness** (testes), não em provedor real |
| Alavancas L1, L2 | **pendentes**: não implementadas |
| Alavanca L3 | implementada, sem ablação |
| Gate de dominância | formalizado; **fechado** (sem dados) |

## 8. Próximo passo

1. Ratificar ou rejeitar a emenda da §3 (N≥30 e horizonte) como contrato versionado
   (`master-test-plan-v2.json`), com justificativa do piso.
2. Executar o screening N=10 com um provedor real, depois da aprovação explícita do uso de API.
3. Só então decidir a ordem de implementação de L1 e L2.
