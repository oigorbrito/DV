# Trilha de pesquisa de composição Luna-only

## Status

Trilha de pesquisa prospectiva. Nenhum tratamento de tarefa de desenvolvimento foi executado e nenhum holdout foi acessado.

O executor/modelo ativo é fixo na superfície já qualificada Codex + `gpt-5.6-luna` v4. Outras faixas de modelo estão fora do caminho crítico ativo.

Isso não apaga nem reinterpreta os artefatos anteriores da P1 com múltiplos modelos/roteamento. Eles permanecem como trabalho prospectivo histórico e podem ser revisitados depois, mas não são necessários para a pergunta atual.

## Pergunta

A decomposição/composição explícita melhora os resultados verificados de engenharia de software o suficiente para pagar o próprio custo adicional no sistema total, quando o modelo subjacente é mantido constante?

Isso isola a composição da escolha do modelo. Um resultado positivo não pode ser atribuído ao uso de um modelo mais forte.

## Comparação inicial

- **D0 — LUNA_DIRECT_VERIFY**: uma execução do Luna e, em seguida, verificação independente.
- **D1 — LUNA_PLAN_EXECUTE_VERIFY**: uma chamada de planejamento do Luna, sem mutação, emite um plano estruturado restrito; uma nova execução do Luna começa da base exata com a tarefa + o plano; depois, verificação.
- **D2 — LUNA_PLAN_STEPWISE_VERIFY**: o mesmo formato de plano é executado passo a passo por invocações novas do Luna, em um único workspace de candidato que evolui; depois, verificação.

Retentativas, ciclo de reparo, escalonamento de modelo, roteamento dinâmico, agentes paralelos e controlador aprendido não fazem parte da comparação inicial.

## Por que estes tratamentos

D0 estabelece a fronteira custo/qualidade do sistema mais simples útil.

D1 testa se um artefato externo explícito de decomposição é útil.

D2 testa se a fronteira de controle adicional da execução passo a passo agrega valor além de simplesmente dar um plano ao Luna.

Se D0 domina, a camada de decomposição é rejeitada. Se D1 ajuda e D2 não, um compilador de plano enxuto pode se justificar, mas um controlador passo a passo não. Se D2 ajuda apenas em algumas famílias de tarefas, isso cria uma hipótese posterior de composição adaptativa; não autoriza, por si só, um controlador adaptativo.

## Plano estruturado, não cadeia de raciocínio

O artefato de planejamento é uma especificação operacional, não raciocínio oculto. Ele contém objetivo, restrições, passos ordenados, dependências, artefatos esperados e instruções de verificação. Ele exclui explicitamente cadeia de raciocínio, raciocínio oculto, patches de referência, patches de teste, dados de holdout e dicas pós-hoc sobre o tratamento.

Schema: `experiments/luna-only/luna-decomposition-plan-schema-v1.json`.

Protocolo: `experiments/luna-only/luna-composition-protocol-v1.json`.

## Contabilidade

A métrica primária continua sendo:

`TOTAL_SYSTEM_TOKENS / VERIFIED_SOLVED_TASK`

Todo o custo do planejador, do executor, das invocações que falharam e do verificador permanece no total do tratamento. A decomposição não recebe computação gratuita. Latência e número de invocações são registrados separadamente.

O estudo inicial não força orçamentos de tokens iguais entre os tratamentos, porque isso ocultaria o custo real da decomposição. Em vez disso, cada invocação tem um teto de recursos congelado, e o custo completo do tratamento é medido.

## Requisitos de liberação

Antes que qualquer tarefa de desenvolvimento seja exposta:

1. o pacote exato de qualificação Luna v4 deve ser preservado e válido por hash;
2. o corpus de desenvolvimento deve satisfazer as regras existentes de admissão/prontidão;
3. os templates de prompt do planejador/executor devem estar congelados;
4. os tetos de invocação e de tratamento devem estar congelados;
5. o verificador deve estar congelado e ser independente do executor;
6. nenhuma tarefa de holdout pode ser acessada.

## Consequências para a arquitetura

Esta trilha pode justificar apenas a capacidade mínima sustentada pela evidência.

- D0 domina -> `NO_BUILD_DECOMPOSITION`.
- D1 conquista a fronteira -> investigar um compilador enxuto de contexto/plano.
- D2 também conquista a fronteira -> investigar a mecânica de composição sequencial.
- resultado instável ou com poder insuficiente -> `INCONCLUSIVE`.

Nenhum resultado desta trilha de desenvolvimento, sozinho, justifica um orquestrador genérico, um framework multiagente, um roteador aprendido, um sistema de memória persistente ou um runtime completo do DV.
