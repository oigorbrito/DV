# Imagem mínima para executar o harness em container descartável (ajuste 3, contrato v3).
#
# Dependências: somente as fixadas em requirements-dv-harness.txt (litellm, usado pelo pacote
# dv_harness; pytest e hypothesis para os testes). O código do repositório NÃO entra na imagem:
# ele é montado em /app como bind mount somente leitura por scripts/dv_container_run.py.
#
# Build (no host com Docker):  docker build -t dv-harness:local .
FROM python:3.12-slim

RUN useradd --uid 10001 --create-home runner

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    LITELLM_LOCAL_MODEL_COST_MAP=True
# LITELLM_LOCAL_MODEL_COST_MAP: usa o mapa de preços local da versão fixada, sem buscar na rede
# (reprodutível e compatível com --network none).

COPY requirements-dv-harness.txt /tmp/requirements-dv-harness.txt
RUN pip install --no-cache-dir -r /tmp/requirements-dv-harness.txt \
    && rm /tmp/requirements-dv-harness.txt

USER runner
WORKDIR /work

CMD ["python", "--version"]
