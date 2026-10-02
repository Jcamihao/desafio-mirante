"""No final — Persistencia: grava a execucao em modernization_history, qualquer que seja o desfecho.

Todos os caminhos do grafo (sucesso, parcial, falha de parsing, falha do LLM) terminam aqui.
"""

import logging
import uuid
from collections.abc import Callable
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from mirante.domain.history import HistoryRecord, compute_status, total_duration_ms
from mirante.domain.state import NodeFunction, PipelineState, StageError
from mirante.persistence.repository import HistoryRepository, default_repository

logger = logging.getLogger(__name__)


def build_persistence_node(
    repository_factory: Callable[[], HistoryRepository] = default_repository,
) -> NodeFunction:
    def persistence_node(state: PipelineState) -> dict[str, Any]:
        status = compute_status(state)
        record_id = uuid.uuid4()
        stages = state.get("report", {})
        parsed = state.get("parsed")
        report = {
            **stages,
            "status": status.value,
            "errors": [e.model_dump() for e in state.get("errors", [])],
            "meta": {
                "history_id": str(record_id),
                "dialect": state.get("dialect", "postgres"),
                "schema_provided": bool(state.get("schema_sql")),
                "dependencies": len(state.get("dependencies", [])),
                "attempts": len(state.get("generation_log", [])),
                "langfuse_trace_id": state.get("trace_id"),
            },
        }
        record = HistoryRecord(
            id=record_id,
            source_code=state.get("source_code", ""),
            generated_code=state.get("generated_code"),
            report=report,
            status=status,
            procedure_name=parsed.name if parsed else None,
            dialect=state.get("dialect", "postgres"),
            duration_ms=total_duration_ms(stages),
        )
        try:
            repository_factory().save(record)
        except SQLAlchemyError as exc:
            # O banco fora do ar nao pode esconder o resultado que o usuario ja pagou para gerar.
            logger.exception("Falha ao persistir a execucao %s", record_id)
            message = f"{type(exc).__name__}: {exc.__class__.__doc__ or exc}".strip()
            return {
                "status": status,
                "final_report": report,
                "errors": [StageError(stage="persistence", message=message)],
                "report": {"persistence": {"status": "failed", "error": message}},
            }
        return {
            "status": status,
            "final_report": report,
            "history_id": str(record_id),
            "report": {"persistence": {"status": "ok", "id": str(record_id)}},
        }

    return persistence_node
