# Objetivo do projeto DV

## Pergunta de pesquisa

Uma camada adaptativa de meta-controle consegue escolher a combinação mínima suficiente de capacidades heterogêneas de engenharia de software para que o sucesso verificado seja alcançado com custo total do sistema menor e latência aceitável do que estratégias de execução fixas?

## Escopo

O DV estuda a tomada de decisão acima de um ecossistema de modelos, agentes de codificação, executores, ferramentas, verificadores, runtimes e orquestradores. Não assume que nenhuma dessas capacidades deva ser reimplementada.

A interação de trabalho é:

`User -> NDV decision process -> selected external capability/composition -> evidence -> next decision`

Ações candidatas incluem continuar, verificar, tentar de novo, trocar, transferir, escalar e parar.

## Objetivo de otimização

Minimizar o custo total do sistema sujeito a um nível exigido de sucesso verificado.

O custo total inclui execução, roteamento, moldagem da tarefa, planejamento, contexto, coordenação, verificação, novas tentativas/replanejamento, transferência/escalonamento, latência e custos de infraestrutura local e remota aplicáveis.

Métrica primária:

`TOTAL_SYSTEM_TOKENS / VERIFIED_SOLVED_TASK`

Nenhuma pontuação composta única deve ocultar a taxa de resolução, o custo monetário, a latência ou a distribuição de falhas.

## Princípios centrais

- A tarefa primeiro.
- Composição mínima suficiente.
- Cada camada adicionada precisa se pagar.
- Evidência verificada supera o relato do executor.
- A continuidade pertence à tarefa, não ao executor.
- Transferir estado somente quando isso melhora a economia ou a confiabilidade.
- Capacidades existentes são preferidas a implementações próprias quando atendem ao contrato exigido.
- Se agentes de fronteira resolvem a distribuição-alvo mais barato sem o DV, o DV deve encolher ou desaparecer.

## Não objetivos

Por padrão, o DV não é um novo orquestrador, framework multiagente, plataforma de memória, motor de workflow, gateway de modelos, sandbox, banco vetorial ou SDK de provedor.

## Resultados científicos de decisão

Resultados válidos incluem:

- `NO_BUILD`
- `REUSE_ONLY`
- `THIN_NDV`
- `FULL_NDV_JUSTIFIED`
- `INCONCLUSIVE`

O projeto tem sucesso se descobrir que algumas ou todas as capacidades propostas do DV são desnecessárias.

## Relação histórica

Este repositório é o sucessor prospectivo da pesquisa mantida historicamente em `oigorbrito/dv`. Os artefatos experimentais históricos conservam sua identidade e proveniência originais.
