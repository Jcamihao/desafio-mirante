import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from mirante.api.deps import EvalRepoDep, LimiterDep, ServiceDep, TracerDep
from mirante.domain.evaluation import EvaluationRun, Metrics
from mirante.evals.runner import run_evaluation, select_cases

router = APIRouter(prefix="/evaluate", tags=["avaliacao"])


class EvaluateRequest(BaseModel):
    cases: list[str] | None = Field(default=None, description="Anexos a avaliar (padrao: B a F)")
    max_attempts: int | None = Field(default=None, ge=1, le=5)


class EvaluationSummary(BaseModel):
    id: uuid.UUID
    created_at: datetime | None
    model: str
    metrics: Metrics


@router.post(
    "",
    response_model=EvaluationRun,
    summary="Roda o pipeline sobre os Anexos B-F e calcula as metricas de qualidade",
)
async def evaluate(
    request: EvaluateRequest,
    service: ServiceDep,
    repository: EvalRepoDep,
    tracer: TracerDep,
    limiter: LimiterDep,
) -> EvaluationRun:
    """Sincrono e LENTO (1 chamada de LLM por caso, em sequencia): espere minutos."""
    try:
        cases = select_cases(request.cases)
    except KeyError as exc:
        raise HTTPException(status_code=422, detail=exc.args[0]) from exc
    async with limiter:
        return await run_evaluation(
            service, repository, tracer, cases, max_attempts=request.max_attempts
        )


@router.get("", response_model=list[EvaluationSummary], summary="Avaliacoes anteriores")
def list_evaluations(
    repository: EvalRepoDep, limit: Annotated[int, Query(ge=1, le=100)] = 20
) -> list[EvaluationSummary]:
    return [EvaluationSummary(**r.model_dump()) for r in repository.list(limit)]


@router.get("/{run_id}", response_model=EvaluationRun, summary="Avaliacao completa por caso")
def get_evaluation(run_id: uuid.UUID, repository: EvalRepoDep) -> EvaluationRun:
    run = repository.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Avaliacao nao encontrada")
    return run
