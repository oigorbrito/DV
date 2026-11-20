# Avaliação de evidência do Real-SWE

Issue: #1  
Data da avaliação: 2026-09-18  
Decisão: **COMPLEMENTARY**

## Escopo

Esta nota avalia o benchmark Real-SWE da Specific Labs, de setembro de 2026, apenas como evidência externa para hipóteses do DV sobre roteamento sensível à tarefa, seleção de modelo+harness, sucesso condicionado a verificador e custo por tarefa verificadamente resolvida.

Não é uma reprodução do benchmark e não importa a ordem de leaderboard como autoridade arquitetural do DV.

## Observações de fonte primária

A Specific Labs afirma que o Real-SWE usa bases de código privadas e licenciadas, de produção, e avalia combinações de modelo+harness, e não modelos isoladamente. A análise pública da amostra cobre dez tarefas, com oito execuções independentes por tarefa para cada combinação de modelo+harness avaliada.

A página do benchmark reporta:

- taxa de resolução como pass@1, média de oito execuções independentes por tarefa;
- execução em sandbox isolado;
- formato de tarefa Harbor;
- verificadores injetados na avaliação;
- verificadores inspirados em testes existentes da base de código ou que os reutilizam;
- resumos de custo médio por rollout, tokens de saída, chamadas de ferramenta e tempo de relógio por modelo/harness;
- forte heterogeneidade entre tarefas e categorias de falha.

A página pública diz explicitamente que o conjunto mostrado é uma amostra pequena e oferece acesso à amostra mediante solicitação, em vez de publicar as bases privadas como um corpus público comum de benchmark.

## Limite de reprodutibilidade

Os itens abaixo não estão disponíveis publicamente em forma suficiente para uma reprodução independente comum:

- os repositórios privados licenciados;
- o corpus completo do benchmark;
- todas as implementações dos verificadores;
- os traces/artefatos brutos por rollout de todas as execuções reportadas;
- um pipeline público reproduzível que um avaliador independente possa executar sobre o mesmo material-fonte privado.

Uma busca na web em 2026-09-18 encontrou análises secundárias, mas nenhuma reprodução independente da execução publicada do Real-SWE.

Portanto:

```text
PUBLISHER_RUN != INDEPENDENT_REPLICATION
PUBLIC_AGGREGATES != RAW_ROLLOUT_DATA
PRIVATE_TASK_ACCESS != PUBLIC_REPRODUCIBILITY
```

## Atribuição entre modelo e harness

O Real-SWE mede intencionalmente combinações completas de modelo+harness. Isso é útil para a hipótese de seleção de executor no nível do sistema do DV, mas significa que os resultados públicos não isolam a qualidade do scaffold da qualidade do modelo.

Uso permitido:

- evidência de que a composição ponta a ponta de modelo+harness importa;
- evidência de que a heterogeneidade específica por tarefa é material;
- evidência de que o sucesso condicionado a verificador deve prevalecer sobre a autodeclaração;
- evidência de que custo e uso de tokens variam materialmente entre composições.

Uso não permitido:

- afirmar que um modelo, sozinho, causou uma pontuação publicada;
- afirmar que um harness, sozinho, causou uma pontuação publicada;
- usar a posição no leaderboard como autoridade direta de executor no DV.

## Adequação a custo e contabilidade

A Specific Labs publica custos médios estimados por rollout e visões agregadas de tokens de saída, chamadas de ferramenta e tempo de relógio por modelo+harness e por tarefa.

Isso é direcionalmente compatível com métricas do DV como:

```text
verified_success_rate
total_system_tokens / verified_solved_task
estimated_cost / verified_solved_task
failure_class / task_family
```

No entanto, o material público não expõe detalhe bruto de contabilidade suficiente para estabelecer equivalência de métrica com o contrato de contabilidade do DV. O DV deve, portanto, tratar os valores de custo/tokens do benchmark como sinais comparativos externos, e não integrá-los diretamente à contabilidade interna.

## Adequação da taxonomia de falhas

A taxonomia pública de falhas inclui:

- premissa não verificada;
- requisito não atendido;
- erro de integração;
- regressão;
- arquivo errado.

Essas categorias são úteis como sinais externos candidatos para a atribuição de falhas e o desenho de escalonamento no DV. Devem ser mapeadas explicitamente, e não copiadas como categorias canônicas do DV.

## Comentário independente

A cobertura secundária recente destaca consistentemente as principais limitações do benchmark:

- desenhado, executado e avaliado pelo próprio publicador;
- amostra pública pequena;
- tarefas e bases privadas limitam a replicação independente;
- a confusão entre modelo e harness é intencional;
- a ordem de leaderboard vizinha não deve ser tratada como autoridade precisa.

Nenhuma replicação independente verdadeira foi identificada durante esta avaliação.

## Decisão

```text
REAL_SWE_EVIDENCE = COMPLEMENTARY
PRIMARY_ARCHITECTURE_AUTHORITY = NO
ROUTING_SIGNAL = YES_WITH_SCOPE
COST_SIGNAL = YES_WITH_SCOPE
FAILURE_TAXONOMY_SIGNAL = YES_WITH_SCOPE
INDEPENDENT_REPLICATION = NOT_FOUND
RAW_PER_ROLLOUT_PUBLIC_DATA = NOT_FOUND
MODEL_ONLY_ATTRIBUTION = NOT_SUPPORTED
HARNESS_ONLY_ATTRIBUTION = NOT_SUPPORTED
```

### Por que não ADMISSIBLE como evidência primária

O benchmark é útil e materialmente relevante, mas a evidência pública atual não satisfaz o padrão mais forte necessário para reprodutibilidade independente ou atribuição causal.

### Por que não INCONCLUSIVE ou REJECT

A metodologia e as medições agregadas publicadas são concretas o suficiente para orientar a formação de hipóteses, verificações de validade externa, experimentos de roteamento sensível à tarefa e desenho de experimentos sensível a custo.

## Regra de uso no DV

O Real-SWE pode orientar o desenho de experimentos e métricas candidatas. Ele não pode, por si só:

- selecionar um executor canônico;
- justificar uma mudança de arquitetura;
- estabelecer superioridade causal de um modelo ou harness;
- substituir os gates próprios de verificador/evidência do DV;
- substituir trials controlados internos.

O DV deve testar qualquer hipótese derivada no próprio corpus controlado e contabilizar todos os resultados sob o próprio contrato de evidência.

## Fontes

Primárias:
- Specific Labs, benchmark Real-SWE: https://realswe.withspecific.com/
- Espelho/página do benchmark da Specific Labs: https://withspecific.com/benchmarks/real-swe

Revisão secundária usada apenas para verificação cruzada de replicação e limitações:
- VarOps: https://varops.com/coding-agents-most-common-failure-is-missed-requirements-not-broken-code-heres-what-to-check-in-a-pilot/
- Creative AI News: https://www.creativeainews.com/articles/real-swe-coding-agents-private-codebases-2026/

## Alerta de nome

O **Real-SWE** da Specific Labs não deve ser confundido com artigos ou pesquisas separadas chamados **RealSWE**, a menos que a proveniência seja verificada explicitamente. Nomes parecidos não são identidade de evidência.
