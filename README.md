# DV

**Programa de pesquisa sobre composição mínima suficiente de capacidades de engenharia de software.**

> Nome canônico: **DV**. Os prefixos `ndv_` / `NDV` em ferramentas, workflows, experimentos, claims e evidência já existentes são legado: são mantidos sem renomeação por compatibilidade e rastreabilidade. Novos artefatos usam `dv`. Regra completa em [`AGENTS.md`](AGENTS.md) (seção "Nome canônico").

O DV investiga se a seleção adaptativa da **combinação mínima suficiente de capacidades** pode melhorar a fronteira sucesso verificado / custo total / latência em relação a estratégias de execução fixas.

O DV **não é uma arquitetura de software aprovada**.

Ele é orientado a pesquisa e a refutação, e admite explicitamente estes resultados válidos:

```text
NO_BUILD
REUSE_ONLY
INCONCLUSIVE
```

---

## Pergunta de pesquisa

A pergunta central não é:

> Como construímos mais um orquestrador?

Ela é:

> **Quais capacidades são realmente necessárias para uma classe de tarefas de engenharia de software, e quando o benefício delas supera o custo total do sistema?**

A unidade de análise é a **capacidade**, não a identidade do repositório.

Modelos, agentes, runtimes, verificadores, orquestradores, sistemas de memória, roteadores e ferramentas futuras são tratados como mecanismos candidatos substituíveis.

---

## Relação com os outros projetos

O DV é intencionalmente separado dos repositórios operacionais e de produto.

### CodePro

CodePro é um chassi executável de engenharia de software, com contratos operacionais, ferramentas de qualificação, telemetria, verificação e limites de evidência em tarefas reais.

O DV é mais amplo e orientado a pesquisa:

```text
DV
  asks which capability composition should exist

CodePro
  implements and measures a bounded software-engineering chassis
```

Resultados do DV podem justificar reuso, remoção ou experimentação posterior em outro sistema. Eles não promovem automaticamente um mecanismo do CodePro.

### MetaO

O MetaO pergunta como governar runtimes e orquestradores heterogêneos sob um plano de controle externo.

O DV pergunta, antes disso, se uma camada de composição ou de controle se justifica economicamente.

Um resultado válido do DV pode, portanto, ser:

```text
NO_ADDITIONAL_CONTROL_LAYER_REQUIRED
```

### SMAG / SMAG-ReX

Esses projetos têm responsabilidades operacionais e de governança.

O DV pode estudar mecanismos de execução, roteamento, transferência, decomposição ou composição, mas a evidência de pesquisa permanece separada da promoção a produto.

---

## Princípio central

Use o ecossistema a favor do DV, em vez de reconstruí-lo.

Ordem padrão de engenharia:

```text
REUSE
  ↓
ADAPT
  ↓
WRAP
  ↓
FORK
  ↓
BUILD CUSTOM
```

Uma nova camada precisa se pagar com o próprio custo adicional.

Conceito padrão de execução:

```text
minimum sufficient composition
        ↓
external execution
        ↓
independent evidence
        ↓
continue / verify / retry / handoff / escalate / stop
```

---

## Objetivo econômico principal

Minimizar o custo total do sistema sujeito a uma probabilidade exigida de sucesso verificado.

Métrica primária:

```text
TOTAL_SYSTEM_TOKENS
────────────────────
VERIFIED_SOLVED_TASK
```

A contagem de tokens, sozinha, não é o modelo completo de custo.

A contabilidade preserva:

- custo monetário;
- latência;
- novas tentativas;
- chamadas;
- contexto;
- verificação;
- roteamento;
- transferência;
- coordenação;
- computação;
- custo de falhas;
- complexidade operacional.

```text
FREE != ZERO_COST
```

---

## Postura científica

O DV parte da premissa de que suas hipóteses podem estar erradas.

```text
research
  ↓
hypothesis
  ↓
controlled test
  ↓
evidence
  ↓
decision
  ↓
implementation only if justified
```

Regras importantes:

```text
EXECUTOR_DONE != VERIFIED_SOLVED
SYNTHETIC_PASS != PRODUCT_PROMOTION
UPSTREAM_FEATURE_EXISTS != LOCAL_NEED
MECHANISM_AVAILABLE != MECHANISM_NECESSARY
```

Capacidades arquiteturais obrigatórias normalmente exigem evidência confirmada em holdout antes de se tornarem centrais.

---

## Modo de engenharia atual

A fase atual de reuso upstream é explicitamente:

```text
REUSE_ONLY
```

Antes que uma implementação customizada seja justificada, o DV verifica se o comportamento exigido já existe em projetos upstream e pode ser reutilizado por configuração, pontos de extensão existentes, adaptadores ou encapsulamentos limitados.

A fase só termina quando a evidência sustenta um de dois desfechos:

1. a capacidade upstream é suficiente -> `REUSE_ONLY` / `NO_BUILD`;
2. resta uma lacuna concreta que não pode ser fechada sem nova implementação.

Veja:

- [Plano de reuso upstream](docs/research/UPSTREAM-REUSE-PLAN.md)
- [Harness de auditoria upstream](docs/research/UPSTREAM-AUDIT-HARNESS.md)

---

## Programa experimental

O programa de pesquisa é dividido em etapas:

- **P1** — economia de executor e de roteamento
- **P2** — continuidade e transferência
- **P3** — seleção de composição
- **P4** — controle adaptativo sequencial
- **P5** — validação do sistema integrado

Fases posteriores não começam só porque uma fase anterior existe.

Evidência negativa pode eliminar trabalho posterior por completo.

---

## Limite atual de implementação

Ferramentas e contratos de pesquisa podem existir para tornar os experimentos reproduzíveis.

Isso **não** significa que um runtime do DV tenha sido aprovado.

Estado atual:

```text
DV_RUNTIME            = NOT_APPROVED
ROUTER                 = NOT_APPROVED
ORCHESTRATOR           = NOT_APPROVED
MEMORY_SYSTEM          = NOT_APPROVED
GENERIC_AGENT_FRAMEWORK= NOT_APPROVED
MODEL_GATEWAY          = NOT_APPROVED
CONTROLLER             = NOT_APPROVED
```

Essa distinção é intencional.

Infraestrutura de pesquisa pode ser implementada enquanto a arquitetura estudada continua não selecionada.

---

## Proveniência histórica

O programa era mantido anteriormente em `oigorbrito/dv`.

Os artefatos históricos do DV continuam autoritativos no repositório de origem e nos commits imutáveis originais.

O trabalho prospectivo do DV é canônico neste repositório a partir do corte registrado em:

```text
provenance/dv-legacy-manifest.json
```

Evidência histórica não é reclassificada silenciosamente como nova evidência do DV.

---

## Verificação canônica

Antes de afirmar que o repositório está pronto, execute a partir da raiz:

```bash
python -m pip install -r requirements-dv-harness.txt
python scripts/harness.py
python -m pytest tests -v
```

A suíte do harness (`tests/`) usa pytest. Os testes de `run_bash`, do canário I3b e da verificação por mutação exigem POSIX (Linux ou WSL); fora dele são pulados ou não se aplicam. Ver `docs/TESTING.md`.

Os testes de tooling do programa (`tools/test_*.py`) rodam a partir do diretório `tools/`, porque alguns casos usam caminhos relativos (`../experiments/...`). É o mesmo padrão dos workflows `wp03`–`wp07`:

```bash
cd tools
python -m unittest discover -p "test_*.py" -v
```

`scripts/harness.py` emite apenas estados fechados: `PASS`, `GAP`, `UNKNOWN_EXTERNAL` e `NOT_APPLICABLE`. Enquanto `enforce_local_gaps` for `false`, ele só reporta.

## Estado e postura de maturidade

- Estado legível por máquina: [`policy/project-state.json`](policy/project-state.json) (schema: `policy/project-state.schema.json`).
- `product_stage` é descritivo e não tem efeito normativo. Rótulos como `research`, `MVP` ou `production` **não** promovem maturidade OSPS.
- O alvo normativo é `osps_target_level` (atualmente `1`). Qualquer promoção exige evidência observável e versionada; nunca é automática.
- Regra de evidência: `IMPLEMENTED != EXECUTED`, `EXECUTED != VERIFIED`. Controles de plataforma que o harness não consegue comprovar localmente ficam `UNKNOWN_EXTERNAL`.
- `release.made = false`. Há uma única tag Git (`legacy-dv-cutover-66f3a21`), um marco histórico; release é observado com pelo menos duas tags Git.
- Licença: MIT, em `LICENSE`.

Detalhes: [`docs/MATURITY.md`](docs/MATURITY.md), [`docs/TESTING.md`](docs/TESTING.md), [`AGENTS.md`](AGENTS.md).

## Uso básico

**STATUS: NOT_RELEASED**

Não há release oficial. Este repositório é de pesquisa: use os comandos de verificação acima e consulte `docs/research/` para os protocolos experimentais. Antes de qualquer release, substitua esta seção por instruções reais de uso.

## Comece aqui

Antes de alterar o programa de pesquisa:

- leia [AGENTS.md](AGENTS.md);
- leia os documentos de governança e de pesquisa em [docs/](docs/);
- preserve a identidade exata de fonte, revisão e ambiente de qualquer experimento que sustente uma decisão.

O resultado desejado não é "mais arquitetura".

O resultado desejado é **a menor composição que sobrevive à evidência**.
