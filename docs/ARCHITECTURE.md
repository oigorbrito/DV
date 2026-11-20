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
| `src/dv_harness/tool_executor.py` | Módulo 2 do harness modular: `ToolExecutor` com 4 primitivas (`read_file`, `write_file`, `edit_file` search/replace exato, `run_bash` stateless com timeout, `PAGER=cat` e truncamento), retorno `ToolResult` e `TOOL_SCHEMAS` (formato OpenAI). Trava de workspace: `path` e `cwd` são resolvidos com `.resolve()` e precisam ficar dentro de `root`, senão a primitiva retorna erro. Essa trava não cobre caminhos acessados dentro do texto de um comando `run_bash`. `run_bash` roda em grupo de processos próprio e mata o grupo no timeout. Não é sandbox. Testes em `tests/test_tool_executor.py`. |
| `src/dv_harness/agent_loop.py` | Módulo 4 do harness modular: `AgentLoop`, loop mínimo sobre `LLMClient`, `ToolExecutor` e `ContextManager`. Ordem por turno: contexto, chamada ao LLM, contabilização, `_check_budgets` (antes de executar ferramentas), depois execução. Status: `completed`, `max_steps_reached`, `budget_exceeded`, `budget_unverifiable` ou `provider_error` (com totais preservados). `AgentResult` traz `calls` (telemetria por chamada com `wall_clock_ms`, provedor, cache, raciocínio, preço), `unmeasured` (métricas fora do núcleo declaradas explicitamente) e `final_answer` nulo quando não há texto. Divergência de defaults em relação ao mini-swe-agent documentada no docstring. Testes em `tests/test_agent_loop.py`. |
| `src/dv_harness/llm_client.py` | Módulo 1 do harness modular: `LLMClient`, adaptador fino sobre `litellm`. Retorna tokens, `cost_usd` (`None` só para preço desconhecido, via `BadRequestError`), `wall_clock_ms`, `provider`, `api_id`, `cached_input_tokens`, `reasoning_tokens` e `pricing_source`. Campos não expostos valem `None`. Dependências fixadas em `requirements-dv-harness.txt`. Testes em `tests/test_llm_client.py`. |
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
