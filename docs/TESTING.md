# Testes

## Verificação local canônica

Dependências fixadas (inclui `pytest`): `python -m pip install -r requirements-dv-harness.txt`.

```bash
python scripts/harness.py
python -m pytest tests -v
cd tools && python -m unittest discover -p "test_*.py" -v
```

`python -m unittest discover -s tests` **não** é a suíte completa: ele só coleta `TestCase` (apenas `test_harness.py`). Os módulos pytest de `tests/` só rodam com `pytest`.

## Plataforma

| Item | Linux/CI | Windows | WSL |
|---|---|---|---|
| `pytest tests` | executa tudo | executa tudo (22 testes POSIX pulados) | executa tudo |
| `run_bash`, `killpg` (timeout sem órfãos) | sim | não (`shell=True` → `cmd.exe`; só mata o shell) | sim |
| Canário I3b (`scripts/dv_canary_check.py`) | sim | não aplicável | **obrigatório** |
| Mutação (`scripts/dv_mutation_check.py`) | sim | não aplicável | **obrigatório** |

- Fins de linha: `.gitattributes` fixa `eol=lf` e `experiments/**` com `-text`, para que o sha256 do contrato v1 seja o mesmo em qualquer checkout.

## Escopo atual dos testes

- `tests/test_harness.py`: testes do harness de maturidade (`scripts/harness.py`). Coletado por `pytest tests` e por `unittest`.
- Revisão de auditoria: `tests/test_agent_loop.py` cobre a ordem de orçamento (ferramentas não executadas após estouro), `provider_error` com totais preservados, telemetria por chamada, declaração de `unmeasured`, cache e raciocínio somados ou não medidos, e fuga de workspace pelo loop. `tests/test_tool_executor.py` cobre fuga por caminho absoluto, `..`, link simbólico e `cwd`. `tests/test_llm_client.py` cobre propagação de erro inesperado de custo e leitura de cache e latência.
- `tests/test_agent_loop.py`: testes do Módulo 4 (`src/dv_harness/agent_loop.py`). Integra LLM mockado com `ToolExecutor` e `ContextManager` reais em `tmp_path`. Cobrem conclusão direta, ciclo multi-turno em arquivos reais, recuperação de erros, parada exata por limites, `cost_usd=None` e masking acumulado. Executar: `python -m pytest tests/test_agent_loop.py -v`.
- `tests/test_context_manager.py`: testes do Módulo 3 (`src/dv_harness/context_manager.py`). Cobrem preservação de `system`/`user`/`assistant`, janela de observações, `tool_call_id`, não-mutação do histórico e contagem de caracteres. Executar: `python -m pytest tests/test_context_manager.py -v`.
- Protocolo v2 (`tests/test_protocol.py`, `tests/test_ledger.py`): tabela de outcome exaustiva, bases de custo, conformidade entre contrato JSON v2 e código, e hash do v1. Ledger: durabilidade, reconciliação de chamadas em voo, fsync.
- Testes de propriedade (`tests/test_properties.py`, hypothesis): I1 conservação da contabilidade, I2 imutabilidade do log frente ao masking, I3 confinamento sem exceção e sem efeito externo, I4 totalidade dos estados, I5 nenhum efeito colateral após quebra de orçamento, I6 ausência de sentinelas.
- Verificação por mutação (POSIX/WSL): `python scripts/dv_mutation_check.py --workdir <dir> --json <arquivo>`. Roda 49 mutantes (M01–M49) em cópia descartável e exige linha de base verde. Mutante morto significa que algum teste falhou. Resultado neste branch (WSL, `feat/baseline-calibration-harness`): 47 mortos, 2 sobreviventes pré-existentes (já presentes em `8cc338d`, não corrigidos aqui): M07 (`max_steps` conta chamadas falhas) e M21 (ENAMETOOLONG escapando como exceção). São lacunas de teste conhecidas.
- Protocolo v3 (`tests/test_protocol.py`): conformidade do contrato v3 com o código, sha256 fixo do v2 e do v1, quatro baldes de tokens, classificação de falhas e `false_finish`/`false_finish_rate`. Propriedades (`tests/test_properties.py`): só `PROTOCOL_ERROR` consecutivo atinge `error_limit`; baldes são soma só se todas as chamadas medem.
- Wrapper de container (`tests/test_container_run.py`): argv com quotas fixas, `--network none`, timeout com `docker kill`, OOM e falha de daemon como flags de infraestrutura, ledger `infra_finished`. Usa `docker` simulado; não exige daemon.
- Canário de ambiente (I3b): `python scripts/dv_canary_check.py --workdir <dir>`. Exige POSIX (WSL/Linux); no Windows o script não é válido. Falso no ambiente de medição (ver ARCHITECTURE). `tests/test_canary.py` prova que o detector acusa escape e aceita um executor contido.
- Smoke test com modelo real: `python scripts/dv_smoke_run.py --outdir <dir>`. Requer `ANTHROPIC_API_KEY`. Sem a variável, recusa rodar.
- Cobertura com branch: `python -m pytest tests --cov=src/dv_harness --cov-branch --cov-report=term-missing` (com `COVERAGE_FILE` fora do repositório).
- `tests/test_tool_executor.py`: testes do Módulo 2 (`src/dv_harness/tool_executor.py`) com `tmp_path`. Inclui medição de que o timeout não deixa processos órfãos. Testes de bash são pulados fora de POSIX. Executar: `python -m pytest tests/test_tool_executor.py -v`.
- `tests/test_llm_client.py`: testes do Módulo 1 do harness modular (`src/dv_harness/llm_client.py`). Usa pytest e mocks, sem chamada real de API. Requer `pip install -r requirements-dv-harness.txt` e executa a partir da raiz: `python -m pytest tests/test_llm_client.py -v`.
- `tools/test_*.py`: testes das ferramentas de pesquisa DV. Executar a partir de `tools/` (`cd tools && python -m unittest discover -p "test_*.py" -v`), porque alguns casos usam caminhos relativos (`../experiments/...`). O workflow `.github/workflows/verify.yml` executa ambos os conjuntos.
- Workflows `wp03`–`wp07` continuam executando subconjuntos específicos por etapa.

## Gatilho de maturidade

- OSPS Level 2: testes automatizados devem ser executados pelo CI antes que uma alteração seja aceita quando o controle aplicável assim exigir.
- OSPS Level 3: a documentação deve explicar quando/como os testes são executados, e alterações importantes devem adicionar ou atualizar testes de acordo com a política documentada.

O harness não afirma que um repositório está no Level 2/3 apenas porque este arquivo existe.

## Evidência de execução em container (ajuste 3)

Medida com o código desta branch, em Docker Desktop 29.9.3 (Linux), imagem `dv-harness:local`.
Quotas em todas as execuções: `--cpus 1`, `--memory 512m` (sem swap), `--pids-limit 256`,
`--network none`, `--cap-drop ALL`, `--read-only`, usuário não-root. Entrada: `scripts/dv_container_run.py`.

| Caso | Comando (resumo) | Resultado observado | Interpretação |
|---|---|---|---|
| **I3b, isca somente leitura** | `--mount <ctrl>:/canary:ro` + `dv_canary_check.py --control-dir /canary` | exit 0; `I3b holds: True`; `control_dir_writable_by_this_process: False`; 5 tentativas `contained` | Nenhuma via de escape alterou a isca montada como somente leitura. |
| I3b, contraste (isca gravável) | `--mount <ctrl>:/canary` (rw), mesmo comando | exit 1; `I3b holds: False`; 5 tentativas `ESCAPED`; `writable: True` | Prova que o detector acusa escape quando a isca é gravável. |
| Sonda de escrita direta | `sh -c 'echo x > /canary/probe.txt'` com `:ro` | `cannot create /canary/probe.txt: Read-only file system` | A recusa vem do mount, não do comando. |
| **Timeout** | `--timeout-s 3 -- sleep 60` | exit **124**; `timed_out: true`; `cmd_timeouts: 1`; container removido | Telemetria de infraestrutura, não falha do agente. |
| **OOM** | `--memory 64m -- python -c "x = b'a' * 300MiB"` | exit **137**; `oom_killed: true`; container removido | `State.OOMKilled` lido antes da remoção. |
| Docker ausente (`PATH` vazio) | `dv_container_run.py -- echo x` | exit **125**; `daemon_error: true`; sem traceback | Falha de infraestrutura tratada sem exceção não capturada. |

Proteção do caminho de falha (testada em `tests/test_container_run.py`, sem daemon real):

- **`timeout` seguido de falha do `docker kill`** (`TimeoutExpired`, `SubprocessError` ou `FileNotFoundError`): não
  levanta exceção; a falha vai para `cleanup_error`; o `finally` ainda tenta `docker inspect` e `docker rm -f`;
  o exit é **125**, nunca 124 (o timeout não foi garantido).
- **Falha do `docker inspect`** (exceção, `returncode != 0` ou saída inesperada): `oom_killed` fica `None`
  (não medido, nunca `False`), `cleanup_error` registra o motivo e o exit é **125**, nunca 0.
- **Falha do `docker rm -f`**: `cleanup_error` e exit 125.

Em todos os casos, `docker ps -a --filter name=dv-run-` retornou 0 após a execução (limpeza garantida
no `finally`). Os testes automatizados correspondentes estão em `tests/test_container_run.py`
(inclui falha só na limpeza, que vira exit 125) e `tests/test_canary.py` (modo `--control-dir`).

**Limite do I3b com isca somente leitura.** O resultado `True` mede que o controle está fora do
alcance de escrita do agente. Não mede que `run_bash` esteja confinado: dentro de `/work` (rw), o agente
ainda pode escrever à vontade. O benchmark exige os dois fatos separados.

Para reproduzir: `python scripts/dv_canary_check.py --provision <dir-host-fora-do-repo>`, depois o
comando de container da tabela acima.
