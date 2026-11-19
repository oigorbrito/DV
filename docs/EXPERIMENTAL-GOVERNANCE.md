# Governança experimental do DV

## Finalidade

Experimentos existem para refutar ou restringir afirmações, não para justificar uma arquitetura predeterminada.

## Regras centrais

- Congelar tarefa, tratamento, vínculo do executor, moldagem, orçamentos, verificador, política de repetição e plano de análise antes da execução comparativa.
- Testes de fumaça validam a instrumentação; não autorizam afirmações científicas de desempenho.
- Tarefas de holdout devem permanecer lacradas até que o protocolo autorize seu uso.
- Nenhum resultado de tratamento pode influenciar se uma tarefa é admitida.
- Falhas e execuções inconclusivas retêm todo o custo de recursos consumido.
- Não interromper cedo porque um tratamento favorecido está na frente ou atrás.
- Não descartar silenciosamente falhas de harness, falhas de provedor, desvio de ambiente ou defeitos de oráculo; atribuí-los explicitamente.
- `UNMEASURED != ZERO`.

## Qualificação do pipeline vs. evidência comparativa

O DV distingue:

- `PIPELINE_SMOKE_READY`: suficiente para validar materialização, invocação, captura de candidatos, verificação, contabilidade e serialização de resultados.
- `COMPARATIVE_READY`: suficiente para tratamentos qualificados e tarefas admitidas que sustentem a comparação científica pretendida.

Um teste de fumaça do pipeline pode passar mesmo quando nenhuma tarefa é resolvida, desde que o pipeline seja reproduzível e a evidência esteja completa.

## Regras de parada

- Se `best fixed ~= oracle`, o potencial de roteamento não está estabelecido.
- Se a sobrecarga de coordenação/controle for maior ou igual à economia, rejeitar a camada adicionada.
- Se um trabalho anterior existente fornece a capacidade exigida a um custo esperado menor, preferir o reuso.
- Se os resultados forem instáveis sob a política de repetição congelada, retornar `INCONCLUSIVE`.
- Nunca ajustar até que um tratamento preferido vença.

## Portões de fase

- P1 estuda a economia de executor e de roteamento.
- P2 só se abre quando o custo de continuidade/recuperação é materialmente relevante.
- P3 só se abre quando a heterogeneidade de composição tem potencial mensurável.
- P4 só se abre quando decisões sequenciais importam e políticas mais simples deixam arrependimento mensurável.
- P5 integra apenas as capacidades que sobreviveram aos portões anteriores.

## Disciplina de documentação

Crie documentação quando ela controlar uma decisão, experimento, requisito de proveniência, resultado ou portão. Evite documentação especulativa sem consequência operacional.
