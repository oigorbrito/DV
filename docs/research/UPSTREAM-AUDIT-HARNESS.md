# Harness de auditoria upstream do DV

**Status:** ATIVO  
**Versão:** 1  
**Escopo:** auditoria de projetos doadores durante `REUSE_ONLY`  
**Autoridade:** companheiro operacional de `docs/research/UPSTREAM-REUSE-PLAN.md`  
**Natureza:** harness de evidência e decisão; não é um framework de execução nem a implementação de um benchmark

## Finalidade

Oferecer uma forma repetível de auditar projetos doadores upstream, sem transformar a auditoria em um esforço de reparo, portabilidade ou reimplementação.

Este harness existe para responder a uma pergunta de forma eficiente:

> Um doador upstream já satisfaz o contrato de capacidade do DV que ainda não foi resolvido, e em qual granularidade temporal/de runtime?

O harness é intencionalmente procedimental. Ele não introduz um runtime, benchmark, roteador, controlador, adaptador ou laço de agente do DV.

## Base empírica e de engenharia de software

O harness segue estes princípios:

1. **Identidade imutável do objeto.** Registrar o repositório canônico e o commit/release exato antes de interpretar o comportamento.
2. **Separar afirmações de evidência.** Afirmações do README/artigo, observações de código-fonte, qualificação de import/ponto de entrada e observações de runtime são registradas separadamente.
3. **Automatizar fases repetíveis.** Configuração, execução, verificação e limpeza devem ser explícitas e scriptáveis quando o upstream já fornece esses mecanismos.
4. **Registrar o ambiente.** Versão de Python/runtime, estado das dependências, restrições da plataforma, ativos necessários e comandos exatos são evidência.
5. **Falhar de forma fechada na ausência de proveniência.** Um comando posterior bem-sucedido não apaga uma falha anterior de identidade, dependência, ativo ou integridade do harness.
6. **Nenhum reparo silencioso.** Datasets ausentes, checkpoints desatualizados, versões incompatíveis fixadas, imports antecipados, deriva de provedor e exemplos quebrados são observações de reprodutibilidade do upstream, e não convites para corrigir o código do doador.
7. **Intervenção mínima.** Preferir configuração upstream e pontos de extensão documentados. Não editar a implementação do doador apenas para que a auditoria passe.
8. **Incerteza explícita.** Se a confirmação em runtime estiver bloqueada, registrar `CODE_CONFIRMED_EXECUTION_BLOCKED` ou `INCONCLUSIVE`; não promover uma inferência de código-fonte a afirmação de execução.
9. **Parar quando a pergunta estiver respondida.** Não reproduzir um artigo ou benchmark completo se o contrato do DV em aberto já foi respondido com evidência suficiente.
10. **Preservar evidência negativa.** Atritos operacionais e tentativas de reprodução que falharam continuam fazendo parte do registro da auditoria.

Essas regras são consistentes com a orientação de reprodutibilidade da ACM, que enfatiza fases explícitas de configuração/execução/limpeza, experimentos automatizados, registro de parâmetros de execução e versões de software, e artefatos suficientes para avaliação independente.

Referências:

- ACM SIGMOD Availability & Reproducibility Initiative: https://reproducibility.sigmodconf.hosting.acm.org/
- ACM SIGPLAN Empirical Evaluation Guidelines: https://www.sigplan.org/Resources/EmpiricalEvaluation/
- ACM Artifact Review and Badging: https://www.acm.org/publications/policies/artifact-review-and-badging-current

## Classes de afirmação

Toda conclusão deve identificar a classe de evidência mais forte que a sustenta.

| Classe | Significado | Redação permitida |
| --- | --- | --- |
| `DOC_CLAIM` | apenas README, artigo, documentação ou comentários | "O upstream afirma..." |
| `CODE_CONFIRMED` | comportamento localizado em código-fonte fixado | "O código-fonte implementa..." |
| `ENTRYPOINT_QUALIFIED` | ponto de entrada documentado/importado carrega em um ambiente suportado | "O ponto de entrada é operacionalmente importável..." |
| `EXECUTION_CONFIRMED` | caminho upstream relevante executado com comportamento de runtime observável | "A execução demonstrou..." |
| `CODE_CONFIRMED_EXECUTION_BLOCKED` | o código sustenta a conclusão, mas a qualificação em runtime está bloqueada por restrições de reprodutibilidade do upstream | "O código confirma X; a confirmação em runtime está bloqueada por Y." |
| `INCONCLUSIVE` | a evidência não resolve o contrato | nenhuma conclusão mais forte é permitida |

`DOC_CLAIM` sozinho nunca basta para fechar um portão de capacidade.

## Máquina de estados da auditoria

Cada doador passa pelos estados abaixo:

```text
ACQUIRED
  -> IDENTITY_PINNED
  -> STATIC_MAPPED
  -> ENTRYPOINT_PRECHECK
  -> MINIMAL_UPSTREAM_EXECUTION
  -> CONTRACT_DECISION
  -> STOP
```

Um doador pode terminar antes como:

```text
UPSTREAM_ENVIRONMENT_BLOCKED
UPSTREAM_DEPENDENCY_DRIFT
UPSTREAM_ASSET_MISSING
UPSTREAM_CHECKPOINT_INCOMPATIBLE
UPSTREAM_ENTRYPOINT_BLOCKED
CODE_CONFIRMED_EXECUTION_BLOCKED
INCONCLUSIVE
```

Esses são desfechos de auditoria, não motivos para construir código do DV.

## Fase 1 — aquisição e identidade

Registrar antes de qualquer instalação ou modificação:

```text
canonical_repository
local_path
commit_sha_or_release
license
acquisition_date
working_tree_status
declared_runtime_versions
declared_dependency_files
```

Verificações obrigatórias:

```text
git status --short
git rev-parse HEAD
```

Se a árvore local já estiver modificada, distinguir as modificações pré-existentes das configurações ou caches gerados pela auditoria antes de usá-la como evidência.

## Fase 2 — mapeamento arquitetural estático

Inspecionar apenas o código necessário para responder ao contrato em aberto.

Registrar:

```text
entry_points
controller_or_orchestrator
execution_loop
composition_units
state_handoff
selection_inputs
selection_outputs
feedback_path
cost_or_quality_objective
```

Para sistemas adaptativos, classificar sempre **quando** uma decisão muda:

```text
offline
training-time
per-dataset
per-query
per-episode
per-rollout
per-subtask
between-delegations
per-step
per-tool-call
during-active-executor
```

E classificar **o que** pode mudar:

```text
model
reasoning/prompt
tools
memory/state
context
instructions
skills
subagents
topology/workflow
verifier
retry policy
handoff
```

A distinção entre "pode produzir arquiteturas diferentes" e "pode recompor durante uma trajetória ativa" é obrigatória.

## Fase 3 — pré-checagem do ponto de entrada

Usar a versão de runtime suportada pelo upstream.

Tentar primeiro o ponto de entrada documentado mais barato, normalmente:

```text
--help
import
dry-run
published minimal example
```

Registrar cada falha pela causa, e não repetir que se trata de um defeito do produto.

Exemplos:

```text
DEPENDENCY_MISSING
DEPENDENCY_VERSION_DRIFT
RUNTIME_VERSION_UNSUPPORTED
CONFIG_REQUIRED_AT_IMPORT
EAGER_OPTIONAL_PROVIDER_IMPORT
ASSET_MISSING
CHECKPOINT_MISMATCH
```

Uma verificação de consistência do gerenciador de pacotes, como `pip check`, prova apenas que os pacotes instalados são mutuamente satisfeitos. Ela não prova que todos os requisitos do upstream estão instalados nem que o ambiente upstream é reproduzível.

## Fase 4 — execução mínima do upstream

A execução só deve ser tentada quando todas as condições abaixo forem verdadeiras:

1. o caminho upstream é relevante para o contrato do DV em aberto;
2. os ativos/checkpoints necessários estão disponíveis ou documentados;
3. a execução pode ser limitada sem editar a implementação do doador;
4. o custo e o uso de APIs externas são conhecidos;
5. a execução não exige reconstruir um benchmark nem inventar um harness do DV.

Usar, nesta ordem:

```text
upstream test
upstream example
upstream smoke/dry-run
upstream benchmark with upstream-exposed subset
existing external benchmark
```

Não modificar um doador apenas para expor um subconjunto menor ou para fazer carregar um checkpoint obsoleto.

## Regra operacional de parada

Parar a tentativa de reprodução em runtime quando o próximo passo testar ou reparar principalmente o empacotamento do upstream, e não o contrato de capacidade do DV.

Gatilhos típicos de parada:

- a semântica de runtime relevante já foi localizada no código, e a instalação adicional diz respeito a imports antecipados não relacionados;
- o benchmark documentado exige ativos externos que não vêm com o repositório;
- o carregador atual espera checkpoints que não estão na revisão fixada;
- o framework tem um mecanismo interno de subconjunto, mas não o expõe pelo ponto de entrada do upstream;
- a execução limitada exigiria editar o código do doador, o dataset, o carregador, o benchmark ou o checkpoint;
- a reprodução completa seria materialmente mais cara do que o ganho de informação para o portão atual.

Ao parar, preservar a conclusão mais forte sustentada e registrar explicitamente o bloqueio de runtime.

## Limite de não reparo

Durante `REUSE_ONLY`, não executar nenhuma das ações abaixo apenas para completar uma auditoria:

```text
patch donor source
rename or convert checkpoints
rewrite dependency declarations
vendor missing algorithms
create a custom benchmark
create a custom execution loop
inject a hidden subset selector
change evaluation semantics
manufacture missing assets
```

Mudanças apenas de configuração são permitidas quando usam uma interface upstream existente. Elas devem ser registradas e revertidas, ou mantidas fora da árvore de trabalho do doador quando isso for viável.

## Registro de evidência

Para cada doador, registrar no mínimo:

```text
donor
repository
commit
license
environment
working_tree_before
working_tree_after
question_under_test
documentation_claims
code_evidence
entrypoint_evidence
execution_evidence
runtime_blockers
temporal_granularity
mutable_dimensions
unresolved_gap
decision
next_gate
```

`working_tree_after` deve separar:

```text
versioned_source_changes
configuration_changes
generated_caches
virtual_environment
logs
downloaded_assets
```

Nunca descrever caches gerados ou um ambiente virtual local como alterações do código-fonte do doador.

## Vocabulário de decisão

A auditoria de um doador se encerra com exatamente uma disposição primária:

```text
REUSE_AS_IS
REUSE_WITH_CONFIGURATION
REUSE_WITH_MINIMAL_ADAPTER
NO_BUILD
CODE_CONFIRMED_EXECUTION_BLOCKED
INCONCLUSIVE
GAP_PERSISTS_NEXT_DONOR
```

`REUSE_WITH_MINIMAL_ADAPTER` só fica disponível se as condições da Fase B de `UPSTREAM-REUSE-PLAN.md` forem atendidas.

`BUILD_CUSTOM_REQUIRED` não é um desfecho do harness de auditoria durante a fase ativa de `REUSE_ONLY`.

## Regra de decisão de portão

Para a fila atual de doadores:

```text
if donor closes unresolved contract:
    record reuse decision
    STOP donor queue for that contract
elif donor contributes a distinct reusable capability but gap remains:
    record capability
    proceed to next donor
elif runtime is blocked but code resolves the temporal/capability question:
    record CODE_CONFIRMED_EXECUTION_BLOCKED
    proceed only if the next donor may close the remaining gap
else:
    record INCONCLUSIVE
```

Nunca converter "o upstream é difícil de reproduzir" em "o DV precisa implementar isso".

## Aplicação às auditorias concluídas

### AOrchestra

Evidência mais forte:

```text
EXECUTION_CONFIRMED
```

Fronteira de adaptação observada:

```text
between delegations / new SubAgent
```

Questão residual:

```text
generic recomposition during an already-active executor
```

### MaAS

Evidência arquitetural mais forte:

```text
CODE_CONFIRMED
```

Fronteira de adaptação observada:

```text
per query, before operator execution
```

As observações operacionais incluem:

```text
Python version constraint (<3.12)
dependency/version drift sensitivity
eager imports of unrelated tools/providers
configuration validation during import
HumanEval dataset absent from clone
current test loader checkpoint absent
versioned .pth artifacts not matching current loader naming
internal subset support not exposed by the CLI path
```

Portanto, a postura correta de auditoria é:

```text
MaAS contributes query-conditioned architecture selection.
The residual active-executor recomposition gap remains.
Do not repair MaAS merely to run a full HumanEval reproduction.
Proceed to AgentSquare after recording the operational blocker state.
```

## Limpeza e transferência

No fechamento de cada doador:

1. capturar o `git status --short` final;
2. não commitar segredos;
3. restaurar a configuração versionada que contenha credenciais;
4. remover apenas caches, logs e ambientes virtuais gerados pela auditoria, quando isso for seguro e útil;
5. preservar comandos, versões, bloqueios e conclusões no registro de transferência;
6. iniciar o próximo doador a partir da sua identidade fixada, e não de premissas herdadas da implementação anterior.

A auditoria está concluída quando outro engenheiro consegue determinar **o que foi provado, o que foi apenas inferido, o que foi bloqueado e por que o trabalho parou**, sem repetir a investigação.
