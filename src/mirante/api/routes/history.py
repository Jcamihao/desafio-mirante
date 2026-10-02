import uuid
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from mirante.api.deps import RepositoryDep
from mirante.api.schemas import HistorySummary
from mirante.domain.history import HistoryRecord
from mirante.domain.status import Status

router = APIRouter(prefix="/history", tags=["historico"])


@router.get("", response_model=list[HistorySummary], summary="Execucoes recentes")
def list_history(
    repository: RepositoryDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    status: Status | None = None,
) -> list[HistorySummary]:
    return [HistorySummary(**r.model_dump()) for r in repository.list(limit, status)]


@router.get("/{record_id}", response_model=HistoryRecord, summary="Execucao completa")
def get_history(record_id: uuid.UUID, repository: RepositoryDep) -> HistoryRecord:
    record = repository.get(record_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Execucao nao encontrada")
    return record
