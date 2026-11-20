Use `AGENTS.md` como o contrato de evidência de todo o repositório.

Princípio central: **IMPLEMENTED != EXECUTED, EXECUTED != VERIFIED**.

Antes de implementar, leia `policy/project-state.json` e os documentos em `docs/`.
Trate `PASS`, `GAP`, `UNKNOWN_EXTERNAL` e `NOT_APPLICABLE` como estados distintos.
Não afirme que testes, builds, verificações, releases, níveis de maturidade ou controles de segurança passaram, a menos que a evidência correspondente tenha sido de fato executada ou observada.
Quando um primeiro release ou uma mudança relevante para a maturidade for observado, execute `python scripts/harness.py` e informe as novas lacunas aplicáveis antes de declarar a tarefa concluída.
