"""Contratos HTTP. Separados do dominio: o JSON publico pode evoluir sem mexer no pipeline."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, Field, StringConstraints

from mirante.domain.status import Status

SqlText = Annotated[str, StringConstraints(max_length=200_000)]


class ModernizeRequest(BaseModel):
    source_code: SqlText = Field(min_length=1, description="CREATE FUNCTION/PROCEDURE a modernizar")
    dialect: str = Field(default="postgres", description="Dialeto de origem (hoje: postgres)")
    schema_sql: SqlText | None = Field(
        default=None, description="DDL das tabelas referenciadas (Anexo A): enriquece geracao"
    )
    dependencies: list[SqlText] = Field(
        default_factory=list,
        max_length=10,
        description="Outras procedures chamadas por esta (ex.: Anexo B para o F)",
    )
    max_attempts: int | None = Field(
        default=None, ge=1, le=5, description="Geracao + reparos (padrao: Settings)"
    )


class StageErrorOut(BaseModel):
    stage: str
    message: str


class ModernizeResponse(BaseModel):
    id: uuid.UUID | None = Field(description="Linha em modernization_history (None se nao gravou)")
    status: Status
    procedure_name: str | None = None
    generated_code: str | None = None
    report: dict[str, Any]
    errors: list[StageErrorOut] = Field(default_factory=list)


class HistorySummary(BaseModel):
    id: uuid.UUID
    status: Status
    procedure_name: str | None
    dialect: str
    duration_ms: int | None
    created_at: datetime | None


class ComponentHealth(BaseModel):
    database: str
    llm_provider: str
    llm_model: str
    llm_configured: bool


class HealthResponse(BaseModel):
    status: str  # ok | degraded
    version: str
    components: ComponentHealth
