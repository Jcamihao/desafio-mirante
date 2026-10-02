"""Dependencias injetaveis (FastAPI). Os testes as substituem via `app.dependency_overrides`."""

import asyncio
from collections.abc import Callable
from functools import lru_cache
from typing import Annotated

from fastapi import Depends

from mirante.config import Settings, get_settings
from mirante.graph.builder import build_graph
from mirante.observability import Tracer, get_tracer
from mirante.persistence.db import get_engine, ping
from mirante.persistence.evaluations import EvaluationRepository, default_evaluation_repository
from mirante.persistence.repository import HistoryRepository, default_repository
from mirante.service import ModernizationService


@lru_cache
def get_repository() -> HistoryRepository:
    return default_repository()


@lru_cache
def get_evaluation_repository() -> EvaluationRepository:
    return default_evaluation_repository()


def get_tracer_dep() -> Tracer:
    return get_tracer()


@lru_cache
def get_service() -> ModernizationService:
    return ModernizationService(build_graph(), get_repository(), get_tracer())


@lru_cache
def get_run_limiter() -> asyncio.Semaphore:
    """Limita execucoes simultaneas: cada uma segura um LLM por 20-120 s."""
    return asyncio.Semaphore(get_settings().max_concurrent_runs)


def get_app_settings() -> Settings:
    return get_settings()


def get_database_check() -> Callable[[], bool]:
    """Devolve `() -> bool` (banco acessivel?); nos testes e trocado por um valor fixo."""
    return lambda: ping(get_engine())


# Aliases `Annotated`: o idioma moderno do FastAPI (evita Depends() em valores default).
ServiceDep = Annotated[ModernizationService, Depends(get_service)]
RepositoryDep = Annotated[HistoryRepository, Depends(get_repository)]
LimiterDep = Annotated[asyncio.Semaphore, Depends(get_run_limiter)]
SettingsDep = Annotated[Settings, Depends(get_app_settings)]
DatabaseCheckDep = Annotated[Callable[[], bool], Depends(get_database_check)]
EvalRepoDep = Annotated[EvaluationRepository, Depends(get_evaluation_repository)]
TracerDep = Annotated[Tracer, Depends(get_tracer_dep)]
