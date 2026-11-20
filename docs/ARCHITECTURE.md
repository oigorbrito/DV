# Arquitetura

**STATUS: OBSERVADA**

Este documento descreve o que existe no repositório, verificado no código. Não descreve arquitetura prevista. Nenhuma arquitetura de runtime do DV foi aprovada (ver `README.md` e `docs/ARCHITECTURE-GOVERNANCE.md`).

## Finalidade

Programa de pesquisa DV sobre composição mínima suficiente de capacidades de engenharia de software. O repositório contém ferramentas de pesquisa, contratos experimentais congelados e registros de execução. Não contém um produto.

## Limite do sistema

Dentro do limite: `tools/`, `experiments/`, `claims/`, `provenance/`, `pilot-runs/`, `policy/`, `scripts/` e `.github/workflows/`.

Fora do limite: os executores externos (Codex, Aider, modelos locais via Ollama, Docker) e o dado público de origem. O repositório apenas os invoca.

## Componentes observados

| Componente | Conteúdo verificado |
|---|---|
| `tools/` | 122 arquivos Python, sendo 60 de teste (`test_*.py`). Os nomes seguem o prefixo legado `ndv_*` e os novos seguem `dv_*`. |
| `experiments/` | 39 arquivos JSON de contratos congelados, protocolos e modelos de execução. |
| `claims/` | Registro de afirmações com estado e nível de evidência (`claims-v1.json`). |
| `provenance/` | Inventário do histórico DV (`dv-legacy-manifest.json`). |
| `pilot-runs/` | Registros de execuções piloto. |
| `policy/` | Estado do projeto e seu schema, lidos pelo harness. |
| `scripts/harness.py` | Harness de evidência e maturidade. Imprime `PASS`, `GAP`, `UNKNOWN_EXTERNAL` e `NOT_APPLICABLE`. |
| `src/dv_harness/context_manager.py` | Módulo 3 do harness modular: `ContextManager` com Observation Masking. Mantém `system`, `user` e `assistant` intactos. Mascara observações (`tool`) fora das `keep_recent_observations` mais recentes (padrão 10), preservando `tool_call_id`, e só quando a troca economiza caracteres. Não usa sumarização por LLM. Retorna cópia profunda e estatísticas (`CompactionStats`). Testes em `tests/test_context_manager.py`. |
| `src/dv_harness/tool_executor.py` | Módulo 2: `ToolExecutor` com 4 primitivas (`read_file`, `write_file`, `edit_file`, `run_bash`), retorno `ToolResult` com `output` (truncado, para o modelo) e `raw_output` (completo, para auditoria). Ambiente do subprocesso por allowlist estrita (`ENV_ALLOWLIST`), sem chaves de API do host. Linhas divididas só em `\n`. `edit_file` normaliza CRLF apenas em arquivos inteiramente CRLF. Caminhos com byte nulo, ENAMETOOLONG, loops de link e fora do workspace viram `ToolResult(ok=False)`. Testes: `tests/test_tool_executor.py`. |
| `src/dv_harness/agent_loop.py` | Módulo 4: `AgentLoop`, loop mínimo sobre `LLMClient`, `ToolExecutor` e `ContextManager`. Precedência de estados (D1): provider_error, budget_exceeded (tokens, depois custo conhecido, por cotas inferiores), budget_unverifiable, completed (submissão explícita via ferramenta `submit`), error_limit_reached (A2, padrão 3), max_steps_reached. Texto sem ferramenta é erro de formato, não conclusão. `AgentResult` traz `calls` (um `CallRecord` por chamada lógica, com todas as tentativas), `tool_events` (saída bruta, SHA-256 e versão truncada), `partitions` (8 explícitas), `unmeasured` (motivo para cada null) e `outcome=None` (atribuído pelo avaliador). Testes: `tests/test_agent_loop.py`, `tests/test_properties.py`. |
| `src/dv_harness/llm_client.py` | Módulo 1: `LLMClient`, adaptador fino sobre `litellm`. Política de retentativa (`RetryPolicy`, backoff exponencial sem jitter, lista de abortos de `litellm_model.py:50-57`). Cada tentativa vira `AttemptRecord`, inclusive as que falharam; esgotadas, levanta `LLMCallFailed` com todas. Usage ausente é `None`. `cost_basis` (`priced_by_table`, `priced_zero_by_table`, `unknown`). `model_returned` é o `raw.model` da API. Dependências fixadas em `requirements-dv-harness.txt`. Testes: `tests/test_llm_client.py`. |
| `.github/workflows/` | 13 workflows de validação, de análise de segurança e de rotulagem. |

## Interfaces

Não há API de serviço. As interfaces são linhas de comando (`argparse`) das ferramentas e os arquivos JSON que elas leem e escrevem. 44 arquivos de ferramentas recusam sobrescrever um diretório ou arquivo de saída já existente.

## Fluxo de dados

Não existe um fluxo único. Cada pipeline é uma sequência de ferramentas, e a ordem está nos nomes e nas entradas de cada uma. Um fluxo só é considerado estabelecido quando um experimento congelado o especifica.

## Limites de confiança observados

- **Executor:** antes de cada invocação, as variáveis de ambiente listadas no contrato da superfície são removidas (`environment_variables_removed`).
- **Verificação de base e de verificador:** os contêineres Docker são criados com `--network none`.
- **Execução de tarefas:** os runners exigem um token explícito de execução e recusam tarefas não admitidas. Esses controles estão no código, mas a execução real ainda não foi observada neste repositório.

## Invariantes

- A documentação não afirma a existência de componentes ou controles que o código não tem.
- Mudanças em interfaces, limites de confiança, comportamento de execução ou dependências devem atualizar este documento.

## Registro de decisões

Decisões de arquitetura estão em `docs/ARCHITECTURE-GOVERNANCE.md`. Decisões de escopo do programa estão em `docs/PROJECT-OBJECTIVE.md`.

| `src/dv_harness/protocol.py` | Protocolo p1 v2: enums de estado (camada 1, `status`), outcome (camada 2: `SOLVED`/`FAILURE`/`INCONCLUSIVE`), `outcome_for` (tabela testada exaustivamente), `cost_basis_for` e `run_cost_basis`, lista de partições e motivos padronizados de não medido. Contrato em `experiments/p1/run-accounting-contract-v2.json`. O v1 é mantido sem alteração e verificado por sha256. Testes: `tests/test_protocol.py`. |
| `src/dv_harness/ledger.py` | Ledger incremental (I1/D7): cada evento é gravado por linha com `fsync` antes de a execução prosseguir. `reconcile` detecta chamadas em voo após interrupção. Eventos: `run_started`, `call_started`, `call_finished`, `tool_finished` (com saída bruta), `run_finished`. Testes: `tests/test_ledger.py`. |
| `scripts/dv_mutation_check.py` | Verificação por mutação reprodutível: 27 mutantes sobre o código de `src/`, aplicados numa cópia descartável. Mutante morto = algum teste falha. Executar: `python scripts/dv_mutation_check.py --workdir <dir> --json <arquivo>`. |