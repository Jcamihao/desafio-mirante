"""Registro persistido de cada execucao do pipeline e a regra que decide o seu desfecho."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from mirante.domain.state import PipelineState
from mirante.domain.status import Status


class HistoryRecord(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    source_code: str
    generated_code: str | None = None
    report: dict[str, Any] = Field(default_factory=dict)
    status: Status
    procedure_name: str | None = None
    dialect: str = "postgres"
    duration_ms: int | None = None
    created_at: datetime | None = None  # preenchido pelo banco


def compute_status(state: PipelineState) -> Status:
    if state.get("errors") or not state.get("generated_code"):
        return Status.FAILURE
    validation = state.get("validation")
    return Status.SUCCESS if validation and validation.passed else Status.PARTIAL


def total_duration_ms(report: dict[str, Any]) -> int:
    """Soma o tempo dos estagios (a geracao tem uma entrada por tentativa)."""
    total = sum(
        report.get(stage, {}).get("duration_ms", 0)
        for stage in ("parsing", "analysis", "validation")
    )
    total += sum(a.get("duration_ms", 0) for a in report.get("generation", {}).get("attempts", []))
    return int(round(total))
