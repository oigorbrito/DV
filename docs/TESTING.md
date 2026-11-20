# Testes

## Verificação local canônica

```bash
python scripts/harness.py
python -m unittest discover -s tests -v
```

## Escopo atual dos testes

- `tests/test_harness.py`: testes do harness de maturidade (`scripts/harness.py`).
- `tests/test_context_manager.py`: testes do Módulo 3 (`src/dv_harness/context_manager.py`). Cobrem preservação de `system`/`user`/`assistant`, janela de observações, `tool_call_id`, não-mutação do histórico e contagem de caracteres. Executar: `python -m pytest tests/test_context_manager.py -v`.
- `tests/test_tool_executor.py`: testes do Módulo 2 (`src/dv_harness/tool_executor.py`) com `tmp_path`. Inclui medição de que o timeout não deixa processos órfãos. Testes de bash são pulados fora de POSIX. Executar: `python -m pytest tests/test_tool_executor.py -v`.
- `tests/test_llm_client.py`: testes do Módulo 1 do harness modular (`src/dv_harness/llm_client.py`). Usa pytest e mocks, sem chamada real de API. Requer `pip install -r requirements-dv-harness.txt` e executa a partir da raiz: `python -m pytest tests/test_llm_client.py -v`.
- `tools/test_*.py`: testes das ferramentas de pesquisa DV. Executar a partir de `tools/` (`cd tools && python -m unittest discover -p "test_*.py" -v`), porque alguns casos usam caminhos relativos (`../experiments/...`). O workflow `.github/workflows/verify.yml` executa ambos os conjuntos.
- Workflows `wp03`–`wp07` continuam executando subconjuntos específicos por etapa.

## Gatilho de maturidade

- OSPS Level 2: testes automatizados devem ser executados pelo CI antes que uma alteração seja aceita quando o controle aplicável assim exigir.
- OSPS Level 3: a documentação deve explicar quando/como os testes são executados, e alterações importantes devem adicionar ou atualizar testes de acordo com a política documentada.

O harness não afirma que um repositório está no Level 2/3 apenas porque este arquivo existe.
