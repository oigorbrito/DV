# Arquitetura

**STATUS: PROVISIONAL**

Este arquivo começa intencionalmente pequeno. Ele deve descrever a arquitetura observada, não a arquitetura prevista.

## Finalidade

Programa de pesquisa DV sobre composição mínima suficiente de capacidades de engenharia de software. Ver `README.md`. Nenhuma arquitetura de runtime foi aprovada.

## Limite do sistema

TBD.

## Componentes

Observados no repositório (não são componentes de produto):

- `tools/`: ferramentas de pesquisa e seus testes (`ndv_*.py`, `test_*.py`).
- `experiments/`, `claims/`: contratos e protocolos experimentais congelados.
- `provenance/dv-legacy-manifest.json`: inventário do histórico DV.
- `pilot-runs/`: registros de execuções piloto.
- `scripts/harness.py`: harness de evidência/maturidade do template.

## Interfaces externas

Nenhuma estabelecida ainda.

## Fluxo de dados

Ainda não estabelecido.

## Limites de confiança relevantes para a segurança

Ainda não estabelecidos.

## Invariantes

- A documentação não deve afirmar a existência de componentes ou controles que não existem.
- Alterações de arquitetura que modifiquem interfaces, limites de confiança, comportamento de build/release ou topologia de dependências devem atualizar este documento.

## Registro de decisões

Registre decisões materiais de arquitetura no controle de versão, aqui ou em arquivos ADR dedicados quando o número/a complexidade das decisões justificar isso.
