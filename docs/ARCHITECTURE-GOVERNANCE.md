# Governança de arquitetura do DV

## Postura padrão

Nenhum componente de arquitetura do DV é aprovado apenas por parecer útil conceitualmente.

A ordem padrão de implementação é:

`REUSE -> ADAPT -> WRAP -> FORK -> BUILD CUSTOM`

Uma capacidade customizada proposta deve passar pelos dois portões:

1. portão científico: a capacidade cria valor mensurável;
2. portão de engenharia: o software existente não atende ao contrato exigido de forma mais econômica.

## Bloqueado por padrão

Até que a evidência altere esse status, não implementar:

- orquestrador do DV
- framework genérico de agentes do DV
- sistema de memória do DV
- roteador genérico do DV
- motor de workflow do DV
- gateway de modelos/provedores do DV
- plataforma de sandbox/workspace do DV
- motor de busca de arquitetura do DV
- controlador aprendido/adaptativo

## Artefatos mínimos específicos do DV candidatos

Estes podem se justificar mais cedo porque codificam a própria pesquisa, em vez de duplicar infraestrutura do ecossistema:

- schema do Evidence Ledger
- schema do Execution Ledger
- contrato mínimo `VerificationResult`
- contrato mínimo `CapabilityProfile`
- interface mínima de política de decisão, somente se P1/P4 exigirem

## Regras de eliminação de capacidades

- Se `workspace-only ~= structured-state`, rejeitar uma camada customizada de estado/memória.
- Se um método de contexto existente é equivalente ou melhor, reutilizá-lo.
- Se `best-fixed ~= oracle`, rejeitar uma camada de roteamento.
- Se `rules ~= learned/LLM controller`, rejeitar a sofisticação do controlador.
- Se uma orquestração externa é equivalente a menor custo total, não construir orquestração.
- Se um trabalho anterior satisfaz o contrato, a implementação pode ser eliminada antes de um protótipo customizado ser escrito.

## Autoridade de arquitetura

A arquitetura é um resultado da evidência, não um pré-requisito do programa de pesquisa. Nenhuma "arquitetura DV v1" normativa deve ser congelada antes que a evidência integrada a justifique.
