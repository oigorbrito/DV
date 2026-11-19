# Testes

## Verificação local canônica

```bash
python scripts/harness.py
python -m unittest discover -s tests -v
```

## Escopo atual dos testes

- `tests/test_harness.py`: testes do harness de maturidade (`scripts/harness.py`).
- `tools/test_*.py`: testes das ferramentas de pesquisa DV. Executar a partir de `tools/` (`cd tools && python -m unittest discover -p "test_*.py" -v`), porque alguns casos usam caminhos relativos (`../experiments/...`). O workflow `.github/workflows/verify.yml` executa ambos os conjuntos.
- Workflows `wp03`–`wp07` continuam executando subconjuntos específicos por etapa.

## Gatilho de maturidade

- OSPS Level 2: testes automatizados devem ser executados pelo CI antes que uma alteração seja aceita quando o controle aplicável assim exigir.
- OSPS Level 3: a documentação deve explicar quando/como os testes são executados, e alterações importantes devem adicionar ou atualizar testes de acordo com a política documentada.

O harness não afirma que um repositório está no Level 2/3 apenas porque este arquivo existe.
