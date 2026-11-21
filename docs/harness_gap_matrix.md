# Matriz de lacunas do harness DV — B0 para B0+

**Escopo e evidência.** Revisão somente leitura de `src/dv_harness/{agent_loop,tool_executor,context_manager}.py`, ledger/contrato locais e fontes presentes no workspace. Referências Pi: checkout `D:\projetos\finecode` (inclui `packages/durable` e `packages/coding-agent`). OpenCode: `D:\projetos\smag-rex\packages\opencode`. Não foi localizado checkout Codex CLI no workspace; `D:\projetos\kevin\experiments\pi-vs-sweaf\isolation\opencode-tee\opencode` é shim Python de telemetria, não código upstream OpenCode. Logo, afirmações específicas do Codex CLI não são verificáveis aqui. Linhas abaixo são relativas aos arquivos apontados; snapshots locais não fixam SHA upstream, então não constituem proveniência imutável.

## 0. Proveniência verificada (medida no commit de implementação de `b0_plus`)

| Referência | Checkout local | `git rev-parse HEAD` | Data do commit | Estado do worktree | Status |
|---|---|---|---|---|---|
| Pi (`packages/durable`, `packages/coding-agent`) | `D:\projetos\finecode` | `ad41b5c344d5399982718720db975b1e020e2c40` | 2026-10-19 | 1 entrada não rastreada (`nul`) | `LOCAL_CHECKOUT_DIRTY` |
| OpenCode (`packages/opencode`) | `D:\projetos\smag-rex\packages\opencode` (repositório `D:\projetos\smag-rex`) | `5b73e034668904d3cc5871e9940e4bdbdc95b4f7` | 2026-09-18 | 8 modificadas, 1 não rastreada (`.vs/`) | `LOCAL_CHECKOUT_DIRTY` |
| Codex CLI | não localizado no workspace | — | — | — | `INCONCLUSIVE_NOT_LOCATED` |

**Consequências.** (1) O hash identifica o commit, mas não o conteúdo lido: os dois checkouts têm alterações não commitadas, e as referências de linha das seções 1–4 foram lidas nesse estado. Antes de citar uma linha como upstream, reverificar com `git show <hash>:<caminho>`. (2) A política `REUSE_ONLY` impede tratar essas capacidades como necessárias. Elas são referências de design para a ablation `b0_plus`, não evidência de melhoria. (3) Os valores em `src/dv_harness/variants.py` (`REFERENCE_PROVENANCE`) são gravados em `run_started.config` e em `batch-summary.json`, para que cada run registre o estado de referência usado.

## 1. Resiliência de `edit_file` & `read_file`

**Pi.** O caminho do harness durável implementa fallback progressivo em `packages/durable/src/tools/edit-diff.ts`: primeiro substring literal (`203–214`), depois `normalizeForFuzzyMatch` (`30–51`, `216–240`): Unicode NFKC, remoção de whitespace à direita por linha, normalização de aspas curvas, dashes e espaços Unicode especiais. A busca fuzzy opera sobre o texto normalizado e aplica substituições nesse espaço (`301–362`), verificando ocorrência única (`248–251`, `329–332`) e rejeitando sobreposição (`342–350`). Preserva linhas inalteradas no caminho fuzzy (`353–356`). Isso não colapsa geral espaços interiores ou indentação arbitrária. O pacote `coding-agent` contém a mesma implementação em `packages/coding-agent/src/core/tools/edit-diff.ts:27–55`.

O retorno do Pi não é uma listagem completa do arquivo: `edit.ts:127–137` entrega a frase `Successfully replaced N block(s) in PATH.` e detalhes contendo diff e patch unificado. O patch usa 4 linhas de contexto (`edit-diff.ts:365–370`); o diff de exibição também usa 4 por padrão, numeração 1-based alinhada à direita, prefixos `+`, `-` ou espaço e `...` para contexto suprimido (`377–380`, `385–418`, `423–457`). A linha da resposta de sucesso, isoladamente, não tem numeração de arquivo. Não há trava read-before-edit: `edit.ts:101–119` lê o arquivo internamente antes de substituir; não exige chamada anterior da ferramenta `read`.

**OpenCode.** `packages/opencode/src/tool/edit.ts` documenta fallback por cadeia: `SimpleReplacer`, `LineTrimmedReplacer`, `BlockAnchorReplacer`, `WhitespaceNormalizedReplacer`, `IndentationFlexibleReplacer`, `EscapeNormalizedReplacer`, `TrimmedBoundaryReplacer`, `ContextAwareReplacer`, `MultiOccurrenceReplacer` (`694–704`). A normalização whitespace colapsa `\s+` em espaço e compara linha/substrings (`427–455`), bem como blocos multilinha (`459–465`). A escolha de ocorrência exige unicidade, salvo `replaceAll`; falha ambígua solicita contexto adicional (`682–729`). Não vi gate read-before-edit: `edit.ts:123–133` faz stat, lê o arquivo e chama `replace` no próprio edit. `read.ts:13–17, 28–35` define limites e offset/limit, mas não grava autorização de leitura.

O `read_file` DV retorna linhas 1-based no formato `N<TAB>texto` (`src/dv_harness/tool_executor.py:164–170,197–204`). O edit DV substitui somente texto literal único, com exceção de CRLF puro (`226–231,326–350`); retorna `edited PATH`, sem diff/contexto (`251–259`). Não há trava read-before-edit nem estado de arquivos lidos.

## 2. Truncamento inteligente (`run_bash`)

**Codex CLI:** sem checkout local identificável, não é possível afirmar proporção, marcador, estratégia para stderr ou preservação de traceback a partir de evidência de arquivo/linha. A exigência da pergunta não deve ser preenchida por memória ou inferência.

**Pi:** a ferramenta bash upstream atual no checkout observado não usa head+tail: declara saída retida como tail (`packages/durable/src/tools/bash.ts:20–25`), limita 2.000 linhas/50 KiB (`src/truncate.ts:12–13`) e pede `outputLimits: { retain: "tail" }` (`bash.ts:138–152`). O buffer/ambiente retém cauda; a saída completa é desviada para arquivo e o resultado fornece caminho e marcador `...N bytes|lines truncated...` (`bash.ts:97–107,115–135`; `truncate.ts:124–140`). Assim tracebacks/erros no fim ficam no preview, sem percentual head/tail; não há divisão 30/70 no código examinado.

**DV atual:** `truncate_output` divide o orçamento disponível metade início/metade fim e usa `\n[... N caracteres omitidos ...]\n` (`src/dv_harness/tool_executor.py:90–105`). Como stdout é concatenado a stderr depois da execução (`309–321`), preservar o fim tende a manter diagnósticos finais, porém uma traceback longa pode ser cortada dentro do bloco: não há detecção/limite por linha ou integridade de traceback. O ledger mantém `raw_output` completo (`agent_loop.py:142–155,401–418`; schema v3, `experiments/p1/run-accounting-contract-v3.json:156`).

**OpenCode (referência adicional observada):** `tool/shell.ts:568–580` aplica `tail(raw, maxLines, maxBytes)`, salva texto inteiro quando cortado e prefixa `...output truncated...` com caminho do arquivo. Também é tail, não head+tail. O workspace observado não comprova política Codex.

## 3. Higiene de Contexto & KV-Cache

**Pi.** `packages/durable/src/harness/compaction.ts:107–145,274–321` seleciona prefixo antigo até preservar `keepRecentTokens`, resume esse prefixo, e grava um marcador/head no primeiro item mantido. Resultados de ferramenta na fonte do resumo são limitados a 2.000 caracteres (`63–64,379–415`). `packages/durable/src/harness/context.ts:203–219` mantém system prompt no início: a implementação registra que mover/reinserir ferramentas no meio invalida prompt cache; por isso mantém o baseline estável e só move o primeiro system message à frente quando semântica permite. A compactação preserva o histórico recente literal e troca prefixo por resumo: evita reler o transcript inteiro, mas não estabelece memória indexada de arquivos inspecionados nem uma trava por caminho.

**DV atual.** `ContextManager.prepare` mascara observações `tool` antigas, preserva mensagens system/user/assistant, IDs e histórico original, e só troca conteúdo quando o placeholder reduz caracteres (`src/dv_harness/context_manager.py:81–130`). É masking, não compaction token-aware nem resumo; não persiste inventário de arquivos lidos. Não altera o prefixo estático, mas o módulo não garante estabilidade de cache do provider por si mesmo. A mensagem do modelo continua contendo placeholders para resultados mascarados.

## 4. Detecção determinística de Doom Loops

**OpenCode.** `packages/opencode/src/session/processor.ts:29,353–379` examina as últimas 3 partes do assistant; se todas forem tool calls finalizadas com mesmo nome e `JSON.stringify(input)` igual, pede permissão `doom_loop`. Não é hash nem similaridade de erro. Esse caminho abre uma barreira de permissão; não injeta mensagem de texto de mudança de estratégia no transcript, então não há mensagem exata a citar. A saída/erro não participa da igualdade.

**Codex CLI:** não há fonte local verificada; detector, limiar e mensagem são desconhecidos neste levantamento. Não atribuir ao Codex a detecção OpenCode.

**DV atual.** `AgentLoop` limita erros de protocolo consecutivos (padrão 3), não repetições semânticas: após `PROTOCOL_ERROR` consecutivo encerra com `error_limit_reached` (`src/dv_harness/agent_loop.py:339–352`). `ToolEvent` já registra `name`, args via chamada/ledger, saída e SHA-256 bruto (`agent_loop.py:142–155,401–418`), mas não há hash de `(tool_name,args)`, detecção de mesmo erro ou prompt de mudança de estratégia.

## Diff arquitetural mínimo proposto — B0 → B0+

Proposta de documentação e adaptadores opt-in, não implementação autorizada por evidência experimental:

1. Manter `ToolExecutor` literal e modo atual como baseline B0. Especificar B0+ como configuração/versionamento separado, sem mudar `CONTRACT_ID=p1-run-accounting-v3`, `LEDGER_SCHEMA=dv-run-ledger-v2`, eventos existentes, significado de estados, partições, contabilidade, campos ou bytes de artefatos congelados. A referência normativa v3 declara auditoria da saída bruta completa e hash (`experiments/p1/run-accounting-contract-v3.json:156`); o ledger é append-only/fsync (`src/dv_harness/ledger.py:29–45`).
2. Adicionar apenas configuração/código de tratamento de observações no caminho do modelo (aditivo): diff curto após `edit_file`, fallback fuzzy explicitamente opt-in, e rastreador de arquivos lidos por caminho+hash para advisory/read-before-edit. Não mudar a primitiva literal nem bloquear/alterar resultados do tratamento por padrão. Se uma chamada/telemetria for adicionada ao ledger, criar nova versão de schema/contrato e avaliador compatível; não reescrever v3.
3. Para saída, manter o `raw_output`/SHA atuais intocados. B0+ pode fornecer preview tail ou head+tail em configuração distinta; incluir marcador e limite em snapshot da configuração. Não mudar métrica, custo, ferramenta executada ou artefato histórico; comparar como nova variante congelada.
4. Doom loop, se avaliado, deve ser detector advisory determinístico baseado em tool+args normalizados, com threshold versionado; mensagem de estratégia entra apenas no prompt do modelo e sua contagem/custo continua no uso medido existente. Não mudar `PROTOCOL_ERROR`, `DOMAIN_FAILURE`, parada D1 nem converter repetição em outcome.
5. Registrar proveniência dos checkouts (repo canônico, commit, licença, data e hash) antes de alegar reuse. O checkout Codex ausente deixa essa parte **INCONCLUSIVE**; sob `REUSE_ONLY`, não construir fuzzy/doom-loop customizado como substituto de auditoria incompleta.

**Status de implementação.** Implementado como variante opt-in `--harness-variant b0_plus` (padrão continua `b0`, inalterado): `edit_file` com fallback de 3 estágios e diff `n=4`, truncamento 20/80 para `run_bash`, placeholders informativos no `ContextManager` e `DoomLoopTracker` com aviso `SYSTEM_ADVISORY` no `AgentLoop`. Implementado (estado atual): motor de edição de sete estágios com match único (`exact`, `line_trimmed`, `trimmed_boundary`, `indentation_flexible` com re-indentação relativa, `escape_normalized`, `unicode_normalized` e `block_anchor` com limiar 0,65), remoção do prefixo `N<TAB>` e guarda de no-op (seção 1); truncamento de cauda para `run_bash` (seção 2); placeholders informativos (seção 3); doom loop (seção 4); pista de continuação no `read_file` truncado entre linhas; aviso específico quando uma única linha longa é cortada por tamanho; saída completa do `run_bash` gravada fora do workspace; marcador `(no output)`; schemas e diretrizes de uso por variante. **Acesso somente-leitura ao diretório de saída (`full_output_dir`, o `spill_dir` do pedido):** no `b0_plus`, `read_file` aceita caminhos regulares dentro desse diretório, sem symlinks e sem traversal; `write_file` e `edit_file` continuam restritos à raiz do workspace. No `b0` o confinamento é estrito, sem acesso ao diretório de saída. Limitações remanescentes: (1) o aviso de linha única mostra só o início da linha, e o restante exige um filtro via `run_bash`; (2) o teste de symlink não roda no Windows sem privilégio de criar links, e roda no WSL; (3) não há rastreamento de leitura nem exigência de leitura prévia (read-before-edit), e o detector de erro repetido não foi implementado. Nenhuma das regras foi promovida por evidência experimental.

O B0+ acima é um diff mínimo para uma **nova ablação/variante**, não promoção comprovada de arquitetura: evidência de que cada capacidade melhora a métrica primária ainda é necessária. A política local `REUSE_ONLY` e o protocolo v3 impedem inferir que upstream capability implica necessidade local.


## 5. Fechamento dos mecanismos do `aider` (`b0_plus`)

**Proveniência.** `D:\projetos\aider` @ `3520eeb33`, lido com `git show HEAD:<caminho>`. O worktree desse checkout tem alterações não commitadas, que não foram usadas.

| Mecanismo do `aider` | Referência | Estado no `b0_plus` |
|---|---|---|
| Trecho mais parecido na falha de edição, com ±5 linhas e limiar 0,6 | `aider/coders/editblock_coder.py:602` (`find_similar_lines`) | **Fechado.** `tool_executor.py`: `_closest_match_window` e `_not_found_message`. A mensagem não traz prefixo `N\t`. |
| Aviso quando o texto novo já está no arquivo | `editblock_coder.py:110` | **Fechado.** `Note: new_text already appears in <path>; verify whether this edit was already applied.` |
| Arquivos em foco sempre atuais, sem observação antiga poluindo o contexto | `aider/coders/base_coder.py:789-815` (re-render do disco a cada turno) | **Fechado de forma determinística.** `context_manager.py`: `_stale_read_indices`. Uma leitura anterior a uma edição ou escrita bem-sucedida do mesmo caminho é substituída por um marcador de obsolescência, mesmo fora da janela recente. O prefixo estático não muda e o `b0` não invalida. |
| `old_text` conciso, só as linhas que mudam | `aider/coders/editblock_prompts.py:139-144` | **Fechado.** Entrou em `B0_PLUS_GUIDELINES` (`variants.py`). |

**Não portado, por decisão.**
- Semântica de primeira ocorrência (`editblock_prompts.py:136`: "only replace the first match occurrence"). Nosso motor recusa match ambíguo. Mantemos a recusa, por segurança.
- Lembrete de regras reinjetado ao final do contexto (`system_reminder`, `editblock_prompts.py:120`). Mexe no prompt, que é tratamento experimental. Fica para o desenho congelado.
- Sumarização do histórico (`base_coder.py:1024`). Não há evidência local de que o histórico seja o gargalo nas tarefas atuais.
- Elisão `...` (`editblock_coder.py`, `try_dotdotdots`). Baixo impacto nos casos observados.
- Fuzzy do `dmp` (`search_replace.py:271-277`). Só existe no caminho `udiff`, e nosso `block_anchor` cobre o mesmo tipo de caso.

**Achado sobre o `aider`.** No `HEAD` `3520eeb`, o bloco de fuzzy de `replace_closest_edit_distance` está atrás de um `return` incondicional (`editblock_coder.py:186`). Esse bloco é inalcançável. A cadeia efetiva do editblock é a exata, com tolerância a indentação.

**Evidência de `finecore`.** Os resultados em `benchmarks/results/harness-comparison/` são **históricos do Orcacode**. O próprio README de `benchmarks/harness-comparison` registra que nenhuma medição live do FineCore foi feita nessa migração. Esses resultados **não são evidência do FineCore** e não devem ser citados como baseline dele.

**Próximo passo, antes de qualquer execução.** O desenho metodológico precisa estar congelado antes de rodar algo:
1. Seleção de tarefas por semente, comitada antes da execução. Modelo: `kevin/benchmarks/swebench/select_verified_subset.py` (semente fixa, sem inspecionar gold, dificuldade ou resultados).
2. Gate de integridade do oráculo. Hoje existe como teste de referência (`tests/test_batch_run.py`); formalizá-lo como pré-condição de execução.
3. Pelo menos 3 repetições, com alternância de ordem entre `b0` e `b0_plus`. Modelo: `finecore/benchmarks/harness-comparison/README.md`.
4. Categorias além de edição, com resposta exata (leitura, agregação, QA). Hoje o suíte só tem edição.

Nenhum experimento foi executado nesta etapa. Nenhum dos mecanismos acima foi promovido por evidência experimental.

**Status final (atualizado em `fd9b96e`; conformidade de telemetria em commit posterior).** O protocolo confirmatório está congelado em `experiments/p1/wp08-b0-plus-laddering-ablation-v1.json` (`FROZEN_PROTOCOL`, `PROSPECTIVE_FROZEN_NOT_EXECUTED`), sob o contrato `experiments/p1/run-accounting-contract-v4.json` (`p1-run-accounting-v4`, `FROZEN_SPECIFICATION`, supersede v3). Os sha256 dos dois artefatos estão fixados em `tests/test_governance_contracts.py`. Fechados em código: GAP-1 (digest dos argumentos no `ToolEvent`), GAP-2 (`telemetry_complete` e `cost_accounting_status`), GAP-3 (`dataset_sha256` nos três resumos), GAP-4 (`ledger_sha256` por run e por degrau) e GAP-5 (schema por tarefa `dv-laddering-summary-v1`; o nome do lote não tem fonte normativa). Abertos, com motivo em `tests/test_v4_telemetry_conformance.py` (`GAP_MAP`): GAP-6 (probe de capacidade exige chamadas reais), GAP-7 (bindings e orçamento global dependem de decisão do usuário) e GAP-8 (código de análise não implementado). Os contratos `v1`, `v2`, `v3` e `v4` e o protocolo `wp08` permanecem byte-a-byte inalterados.
