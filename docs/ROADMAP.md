# DV — Roadmap baseado em evidência

**Status:** PROPOSTO (aguardando aprovação do mantenedor para os itens marcados como `DECISÃO`)
**Escopo:** transforma o estado documentado do repositório em itens executáveis, com critério de aceite verificável.
**Base:** leitura integral de `AGENTS.md`, `README.md`, `docs/` (governança, pesquisa WP-03 a WP-06, Luna, reuso upstream, auditoria), `claims/claims-v1.json`, `policy/project-state.json` e a saída do próprio harness (`scripts/harness.py`) em `main` (`77fa943`).

---

## 1. Diagnóstico: o que está provado, o que está só documentado

| Área | Estado verificável hoje | Nível de evidência |
|---|---|---|
| Governança e critérios | `EVIDENCE-POLICY`, `ARCHITECTURE-GOVERNANCE`, `PROJECT-OBJECTIVE` definidos | Documentação normativa |
| Reuso upstream | Estado atual em `docs/research/UPSTREAM-REUSE-PLAN.md` (seção *Current audit state*) | Ver fonte |
| WP-03 (harness determinístico) | `PASS` (CI verde na `main` e execução de 2026-09-16) | Verificado |
| WP-04 (executor real) | Stage 1 executado uma vez (`SMOKE_VALID_FAILED`). Stage 2 **bloqueado**: bytes originais do binding e da qualificação não estão no repositório. Campanha **não fechada**. | Parcial |
| WP-05 (executores locais gratuitos) | Endpoint Ollama qualificado. Binding v2 **pendente** de qualificação do scaffold. | Parcial |
| WP-06 (admissão de corpus) | **0 de 6** candidatos da Wave-01 admitidos. Nenhuma base audit Docker executada. | Ferramental pronto; evidência empírica **zero** |
| Luna-only (composição) | Protocolo, schema e compilador de prompt existem. Qualificador do planner executado **uma vez** (`S0_PLAN_INVALID`). **Runner de D0/D1/D2 não existe.** Nenhum tratamento executado. | Contratos e 1 execução de qualificação |
| Claims (`claims/claims-v1.json`) | 9 claims, **todas `UNTESTED` / E0** | Hipóteses |
| Harness OSPS (`scripts/harness.py`) | 3 `GAP` locais: licença não commitada; documento de uso é placeholder; `docs/RELEASE.md` incompleto. Os demais controles são `UNKNOWN_EXTERNAL` (dependem da plataforma). | Verificado pelo harness |
| Testes | `tools/`: 313 testes na `main`, todos passando. `tests/`: passando. Workflows determinísticos: verdes no Linux. | Verificado (compilação e testes) |

**Conclusão objetiva:** o projeto tem muita **governança e pesquisa documentada**, mas **quase nenhuma execução experimental completa**. Não existe, até agora, nenhuma comparação de tratamentos com custo total contabilizado. Nenhum claim foi testado.

### 1.1 Achado que muda o plano

**O conteúdo das PRs #75 e #77 não está na `main`.** Ambas foram mescladas nos branches-base (`chore/apply-template-latest` e `research/external-agent-engineering-claims`), não na `main`. Consequência: `docs/research/EXTERNAL-FINDINGS-2026.md`, `tools/dv_claim_experiment_gate.py`, `experiments/p1/claim-experiment-registry-v1.json` e `.github/workflows/run-claim-experiment.yml` **não estão na `main`**. A auditoria de documentação acima considerou o que está na `main`, mais o branch `research/external-agent-engineering-claims`.

---

## 2. Restrições de governança (obrigatórias para este roadmap)

O roadmap precisa respeitar estas regras, que já estão no repositório. Elas definem o que pode e o que não pode virar código agora.

1. **Reuso upstream:** a regra vigente está em `docs/research/UPSTREAM-REUSE-PLAN.md`. Este roadmap só inclui o que ela permite.
2. **Código permitido agora** (lista de `ARCHITECTURE-GOVERNANCE.md`, "Artefatos mínimos específicos do DV candidatos"):
   - schema do Evidence Ledger;
   - schema do Execution Ledger;
   - contrato mínimo `VerificationResult`;
   - contrato mínimo `CapabilityProfile`;
   - interface de política de decisão, somente se P1/P4 exigirem.
3. **Código de experimento** é permitido quando serve a uma claim e a um protocolo congelado (ferramental de reprodutibilidade, sem arquitetura aprovada).
4. **Não é permitido** começar pelo código: a ordem é `claim → experimento → evidência → decisão → arquitetura` (`EVIDENCE-POLICY.md`).
5. **Dependência externa:** o SMAG não é dependência deste projeto. O README o descreve como ferramenta operacional separada. Este roadmap não depende dele.
6. **Código copiado de fora:** a ordem de decisão e a exigência de procedência estão em `AGENTS.md` (Arquitetura padrão) e em `UPSTREAM-REUSE-PLAN.md` (procedência).

---

## 3. Roadmap

Cada item tem: **pré-requisito**, **entrega**, **critério de aceite** (evidência, não opinião) e **bloqueio atual**.

### Fase R0 — Higiene do repositório (sem dependência de pesquisa)

| ID | Item | Entrega | Critério de aceite | Bloqueio |
|---|---|---|---|---|
| R0.1 | **`DECISÃO`:** licença | Escolher e commitar a licença em local reconhecido (OSPS-LE-03.01) | `scripts/harness.py` deixa de reportar `GAP` em OSPS-LE-03.01 | Escolha sua |
| R0.2 | Documento de uso | Substituir o placeholder por instruções reais de uso | Sem placeholder; harness sem `GAP` em OSPS-DO-01.01 | Nenhum |
| R0.3 | `docs/RELEASE.md` completo | Preencher os controles condicionados ao primeiro release | Sem `GAP` em RELEASE-DOC-ADAPTER | Só faz sentido após R0.1 |
| R0.4 | Unificar nomenclatura | Confirmar DV como nome canônico; manter prefixos `ndv_` como legado (já descrito no `AGENTS.md`) | `grep` sem variantes conflitantes em documentos novos | Nenhum |
| R0.5 | Publicar pesquisa externa | Mesclar o conteúdo de #75 e #77 na `main` | Arquivos listados em §1.1 presentes em `main`, com suíte verde | **`DECISÃO`:** autorizar a PR da `research/external-agent-engineering-claims` para `main` |

### Fase R1 — Contratos mínimos de evidência (permitidos pelo `ARCHITECTURE-GOVERNANCE`)

Esta fase transforma a política de evidência em código verificável, sem criar runtime.

| ID | Item | Entrega | Critério de aceite |
|---|---|---|---|
| R1.1 | Evidence Ledger (schema + validador) | `experiments/schemas/evidence-ledger-v1.json` e validador Python | Valida os artefatos já existentes (WP-03, WP-04 Stage 1, planner Luna) **sem reescrevê-los**. Rejeita registro sem classe de evidência (`LITERATURE`, `NDV_EXPERIMENTAL`, `ENGINEERING_OBSERVATION`) ou sem nível E0–E4 |
| R1.2 | Execution Ledger (schema + validador) | Schema de custo total por invocação: tokens de planejamento, execução, verificação, falhas, retries, latência | Soma de custos reproduzível a partir de `usage.json`; falha de invocação conta no total |
| R1.3 | `VerificationResult` | Contrato com verificador, versão, hash e veredito | Veredito `PASS`/`FAIL`/`INCONCLUSIVE` vinculado a verificador independente do executor (regra "executor self-report ≠ verified") |
| R1.4 | `CapabilityProfile` | Contrato que descreve o que um executor/composição pode mudar e quando | Preenchido para Luna direto e para o D1 do protocolo; validado por teste |

**Teste de aceite da fase:** `tools/` e `tests/` passam, e um validador roda sobre todas as evidências existentes em `pilot-runs/` e `experiments/p1/` sem erro, sem alterar nenhum arquivo histórico.

### Fase R2 — Experimento Luna-only (a única linha de pesquisa com protocolo congelado pronto)

Protocolo: `experiments/luna-only/luna-composition-protocol-v1.json`. Tratamentos: **D0** (Luna direto + verificação), **D1** (plano estruturado + execução + verificação), **D2** (execução passo a passo).

| ID | Item | Entrega | Critério de aceite | Bloqueio |
|---|---|---|---|---|
| R2.1 | Runner D0 (dry-run e contabilidade) | Ferramenta que monta o tratamento D0, sem executar | Dry-run gera manifesto com ceilings congelados; testes cobrem limite de invocação | Nenhum |
| R2.2 | Runner D0 (execução) | Execução real sobre corpus admitido | Só roda com `ADMITTED_FROZEN` por tarefa; registra custo total | **R3** (corpus) |
| R2.3 | Runner D1 | Planejamento não mutável + execução com plano | Idem R2.2; plano validado por `validate_plan` | **R2.2** e **R3** |
| R2.4 | Runner D2 | Execução passo a passo | Idem | **R2.3** |
| R2.5 | Análise da fronteira | Custo total / tarefa verificada, por tratamento | Relatório com falhas, latência e resultado nulo se houver | **R2.2–R2.4** |

**Regra de decisão (já congelada no protocolo):** se D0 domina, `NO_BUILD_DECOMPOSITION`. Se D1 ganha, investigar um compilador de plano fino. Se D2 também ganha, investigar mecânica sequencial. Resultado instável → `INCONCLUSIVE`.

### Fase R3 — Corpus admitido (pré-requisito real de qualquer comparação)

| ID | Item | Entrega | Critério de aceite | Bloqueio |
|---|---|---|---|---|
| R3.1 | Extração pinada do Parquet | Extração com identidade fixada | Hash e versões registrados | Acesso ao dado e ambiente (máquina local) |
| R3.2 | Quarentena e base audit | Para cada um dos 6 candidatos: base run em Docker | Relatório de base audit por candidato | **Máquina local com Docker** |
| R3.3 | Oráculo e admissão | `ndv_freeze_s2_admission.py` gera `ADMITTED_FROZEN` por candidato que passa | Registro imutável; candidatos reprovados também registrados | R3.2 |

**Meta de saída da fase:** pelo menos o número de tarefas exigido por `P1_COMPARATIVE_READY` (definido em `EXPERIMENTAL-PROGRAM.md`). Hoje o número é **zero**.

### Fase R4 — Fechar pendências de WP-04 e WP-05

| ID | Item | Entrega | Critério de aceite | Bloqueio |
|---|---|---|---|---|
| R4.1 | Binding original do WP-04 | Importar os bytes originais, com SHA-256 e localização imutável | `ndv_import_wp04_binding.py` aceita o arquivo | **Issue #2:** arquivos na sua máquina |
| R4.2 | Stage 2 (D-F6-01) | Executar após R4.1 | Evidência v2 com hash válido | R4.1 |
| R4.3 | Fechamento da campanha WP-04 | `ndv_close_wp04_campaign.py` aprova ambos os estágios | `WP04_PIPELINE_SMOKE_COMPLETE` (não autoriza comparação) | R4.2 |
| R4.4 | Scaffold Aider+Qwen | Qualificar o scaffold para liberar o binding v2 do WP-05 | `S0_READY` com hash | Máquina local com Ollama |

### Fase R5 — Claims e reuso (pesquisa antes de código)

| ID | Item | Entrega | Critério de aceite |
|---|---|---|---|
| R5.1 | Especificar testes para C-T1, C-T2, C-P1, C-S1 | Cada hipótese vira uma especificação de experimento (já mapeadas em `EXTERNAL-FINDINGS-2026.md`) | Cada uma tem tarefa, verificador, orçamento e critério de falsificação. **Não executar ainda.** |
| R5.2 | Continuar a auditoria upstream | AgentSquare, depois SkillOrchestra e ClawArena, seguindo `UPSTREAM-AUDIT-HARNESS.md` | Registro `REUSE_AS_IS` / `REUSE_WITH_CONFIGURATION` / `CODE_CONFIRMED_EXECUTION_BLOCKED` / `NO_BUILD` com evidência de código |
| R5.3 | Decisão de saída do modo `REUSE_ONLY` | Só se R5.2 deixar gap concreto que configuração não resolve | Decisão registrada em documento próprio |
| R5.4 | Reavaliar claims | Atualizar status de cada claim com evidência E1+ | Claims passam de `UNTESTED` só com experimento registrado |

---

## 4. O que **não** entra neste roadmap (por governança)

- Orquestrador, roteador, controlador adaptativo, memória persistente, gateway de modelos ou framework de agentes do DV. Estão `NOT_APPROVED` no README e bloqueados por padrão no `ARCHITECTURE-GOVERNANCE.md`.
- Reescrever ou "consertar" AOrchestra ou MaAS para ganhar evidência (vetado no `UPSTREAM-REUSE-PLAN`).
- Qualquer dependência do SMAG.
- Executar tratamentos antes de R3 (sem corpus admitido, nenhum tratamento pode ser executado).
- Alterar evidência congelada ou reinterpretar resultados históricos.

---

## 5. Decisões que dependem de você

1. **R0.1:** qual licença (ou se ainda não licenciar).
2. **R0.5:** autorizar a PR de `research/external-agent-engineering-claims` para `main`.
3. **R4.1:** localizar os bytes originais do binding e da qualificação do WP-04 (issue #2).
4. **R3.2:** rodar a base audit na sua máquina com Docker. Eu não tenho acesso a essa máquina nesta sessão.
5. **R5.2:** confirmar que a auditoria upstream continua antes de qualquer código de runtime (é a regra vigente).

## 6. Ordem recomendada

`R0.5 → R0.1 → R0.2 → R1 (em paralelo com R3) → R4 → R2.1 → R2.2–R2.5 (após R3) → R5`

R1 e R2.1 são o trabalho de código que **pode começar agora** sem violar a governança. R2.2 em diante depende de corpus admitido, que hoje é o gargalo real.

---

## 7. Status de execução (R1 e R2.1)

| ID | Entregue | Verificação | Limitação registrada |
|---|---|---|---|
| R1.1 | `experiments/schemas/dv-evidence-contracts-v1.json` + regras em `tools/dv_ledgers.py` (evidence record) | `tools/test_dv_ledgers.py` | Nenhuma evidência existente no repositório está nesse formato. O validador **não** foi aplicado retroativamente, para não reescrever histórico. |
| R1.2 | Invocation record e `summarize_treatment` (componentes de tokens, falhas contadas) | Testes de falha contada e de tratamento incorreto | **Total de tokens não definido.** Não se sabe se `input_tokens` inclui `cached_input_tokens`. Por isso o total fica `UNRESOLVED`. |
| R1.3 | `VerificationResult` com independência do verificador e regra "PASS exige três condições" | `is_verified_solved` ignora o relato do executor | Nenhum verificador real foi vinculado ainda. |
| R1.4 | `CapabilityProfile` com vocabulário de `UPSTREAM-REUSE-PLAN` | Teste de dimensão fixa vs. mutável | Não preenchido para nenhum executor real. |
| R2.1 | `tools/dv_d0_runner.py`, modo dry-run | Recusa com orçamento não congelado; nunca inicia processo (testado com `subprocess` substituído por erro) | `experiments/luna-only/luna-d0-budget-v1.json` está `UNFROZEN_TEMPLATE` com valores `null`. **Você precisa escolher e congelar os limites** antes que o runner planeje qualquer tarefa. |

Estado no repositório: o runner recusa com `D0 budget is 'UNFROZEN_TEMPLATE'`, e não há nenhum registro `ADMITTED_FROZEN`, então nenhuma tarefa seria planejada mesmo com orçamento congelado.

Próximo passo: **R2.2** (execução) só pode começar depois de três coisas: orçamento congelado, pelo menos uma admissão `ADMITTED_FROZEN` e a definição do total de tokens (R1.2).
