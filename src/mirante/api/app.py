"""Aplicacao FastAPI montada pelo servidor LangGraph (langgraph.json -> http.app).

O `langgraph dev/up` expoe suas rotas (/runs, /threads, ...) e monta esta app ao lado, entao o grafo
continua acessivel pelo protocolo do LangGraph E pelos endpoints /modernize e /health.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool

from mirante.api.routes import evaluate, health, history, modernize
from mirante.config import get_settings
from mirante.observability import flush
from mirante.persistence.db import get_engine
from mirante.persistence.migrate import apply_migrations

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI):
    if get_settings().auto_migrate:
        try:
            applied = await run_in_threadpool(apply_migrations, get_engine())
            logger.info("Migracoes aplicadas: %s", applied or "nenhuma (em dia)")
        except Exception:  # noqa: BLE001 - sem banco a API sobe; /health mostra "degraded"
            logger.exception("Nao foi possivel migrar o banco; seguindo sem persistencia")
    yield
    await run_in_threadpool(flush)  # nao perde traces pendentes no desligamento


def create_app() -> FastAPI:
    app = FastAPI(
        title="Mirante SQL Modernizer",
        description="Pipeline hibrido (LLM + regras): PL/pgSQL -> Python 3.14 via LangGraph.",
        lifespan=lifespan,
    )
    app.include_router(modernize.router)
    app.include_router(health.router)
    app.include_router(history.router)
    app.include_router(evaluate.router)
    return app


app = create_app()
