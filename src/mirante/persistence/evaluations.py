import uuid

from sqlalchemy import Column, DateTime, Engine, MetaData, Table, Text, func, insert, select
from sqlalchemy.dialects.postgresql import JSONB, UUID

from mirante.domain.evaluation import EvaluationRun

metadata = MetaData()
evaluation_runs = Table(
    "evaluation_runs",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("model", Text, nullable=False),
    Column("metrics", JSONB, nullable=False),
    Column("results", JSONB, nullable=False),
)


class EvaluationRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(self, run: EvaluationRun) -> uuid.UUID:
        values = {
            "id": run.id,
            "model": run.model,
            "metrics": run.metrics.model_dump(),
            "results": [r.model_dump(mode="json") for r in run.results],
        }
        with self._engine.begin() as conn:
            conn.execute(insert(evaluation_runs).values(**values))
        return run.id

    def get(self, run_id: uuid.UUID) -> EvaluationRun | None:
        with self._engine.connect() as conn:
            row = (
                conn.execute(select(evaluation_runs).where(evaluation_runs.c.id == run_id))
                .mappings()
                .first()
            )
        return EvaluationRun(**row) if row else None

    def list(self, limit: int = 20) -> list[EvaluationRun]:
        query = select(evaluation_runs).order_by(evaluation_runs.c.created_at.desc()).limit(limit)
        with self._engine.connect() as conn:
            return [EvaluationRun(**row) for row in conn.execute(query).mappings()]


def default_evaluation_repository() -> EvaluationRepository:
    from mirante.persistence.db import get_engine

    return EvaluationRepository(get_engine())
