"""Decisoes de fluxo do grafo (arestas condicionais), separadas da montagem."""

from mirante.config import get_settings
from mirante.domain.state import PipelineState


def after_parsing(state: PipelineState) -> str:
    """Sem IR valida nao ha o que analisar/gerar: encerra (a persistencia registra a falha)."""
    return "failed" if state.get("errors") else "analysis"


def after_analysis(state: PipelineState) -> str:
    return "failed" if state.get("errors") else "generation"


def after_generation(state: PipelineState) -> str:
    return "failed" if state.get("errors") else "validation"


def after_validation(state: PipelineState) -> str:
    """Laco de reparo: so volta ao gerador se ainda houver tentativas; senao encerra (parcial)."""
    if state["validation"].passed:
        return "done"
    limit = state.get("max_attempts") or get_settings().max_generation_attempts
    return "repair" if len(state.get("generation_log", [])) < limit else "done"
