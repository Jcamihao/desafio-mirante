# Imagem do servidor: LangGraph (langgraph dev) + a API FastAPI montada via langgraph.json (http.app).
FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

# Camada de dependencias separada do codigo: rebuilds rapidos quando so o codigo muda.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src ./src
COPY db ./db
COPY samples ./samples
COPY langgraph.json ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
EXPOSE 2024
HEALTHCHECK --interval=10s --timeout=3s --retries=10 \
  CMD python -c "import urllib.request as u; u.urlopen('http://localhost:2024/health', timeout=2)"

# `langgraph dev` roda o servidor em memoria (sem licenca). Para persistencia do estado do LangGraph e
# escala horizontal, usar `langgraph up`/LangGraph Platform (ver README, secao de escalabilidade).
CMD ["langgraph", "dev", "--host", "0.0.0.0", "--port", "2024", "--no-browser", "--no-reload"]
