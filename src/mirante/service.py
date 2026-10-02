"""Caso de uso "modernizar": executa o grafo e garante que a execucao fique registrada.

Fica entre a API (HTTP) e o grafo: as rotas ficam finas e esta regra e testavel sem HTTP.
"""

import logging
import uuid
from typing import Any

from langgraph.graph.state import CompiledStateGraph
from sqlalchemy.exc import SQLAlchemyError

from mirante.api.schemas import ModernizeRequest, ModernizeResponse, StageErrorOut
from mirante.domain.history import HistoryRecord
from mirante.domain.status import Status
from mirante.observability import NoopTracer, Tracer
from mirante.persistence.repository import HistoryRepository

logger = logging.getLogger(__name__)


def http_status_for(response: ModernizeResponse) -> int:
    """O codigo HTTP diz DE QUEM foi a falha; o corpo e sempre o mesmo, com o relatorio."""
    if response.status != Status.FAILURE:
        return 200  # SUCCESS e PARTIAL: ha codigo utilizavel (PARTIAL vem sinalizado no corpo)
    stages = {e.stage for e in response.errors}
    if "parsing" in stages:
        return 422  # entrada do cliente invalida
    if "generation" in stages:
        return 502  # LLM (provedor externo) falhou
    return 500


class ModernizationService:
    def __init__(
        self,
        graph: CompiledStateGraph,
        repository: HistoryRepository,
        tracer: Tracer | None = None,
    ) -> None:
        self._graph = graph
        self._repository = repository
        self._tracer = tracer or NoopTracer()

    async def run(
        self, request: ModernizeRequest, session_id: str | None = None
    ) -> ModernizeResponse:
        state: dict[str, Any] = {
            "source_code": request.source_code,
            "dialect": request.dialect,
            "schema_sql": request.schema_sql,
            "dependencies": request.dependencies,
        }
        if request.max_attempts:
            state["max_attempts"] = request.max_attempts
        trace = self._tracer.start(request, session_id)
        state["trace_id"] = trace.trace_id
        try:
            out = await self._graph.ainvoke(state, config=trace.config or None)
        except Exception as exc:  # bug num no: o grafo nao chegou ao `persist`
            logger.exception("Falha inesperada no pipeline")
            response = self._record_crash(request, exc)
            trace.finish(response)
            return response

        parsed = out.get("parsed")
        response = ModernizeResponse(
            id=out.get("history_id"),
            status=out["status"],
            procedure_name=parsed.name if parsed else None,
            generated_code=out.get("generated_code"),
            report=out.get("final_report", {}),
            errors=[StageErrorOut(stage=e.stage, message=e.message) for e in out.get("errors", [])],
        )
        trace.finish(response)
        return response

    def _record_crash(self, request: ModernizeRequest, exc: Exception) -> ModernizeResponse:
        """Garante o requisito "toda execucao e persistida" tambem quando o grafo explode."""
        message = f"{type(exc).__name__}: {exc}"
        error = StageErrorOut(stage="pipeline", message=message)
        record = HistoryRecord(
            id=uuid.uuid4(),
            source_code=request.source_code,
            report={"errors": [error.model_dump()], "status": Status.FAILURE.value},
            status=Status.FAILURE,
            dialect=request.dialect,
        )
        saved: uuid.UUID | None = None
        try:
            saved = self._repository.save(record)
        except SQLAlchemyError:
            logger.exception("Falha ao gravar a execucao com crash %s", record.id)
        return ModernizeResponse(
            id=saved, status=Status.FAILURE, report=record.report, errors=[error]
        )
