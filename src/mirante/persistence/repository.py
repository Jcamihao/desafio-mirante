import uuid
from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Engine,
    Integer,
    MetaData,
    Table,
    Text,
    func,
    insert,
    select,
)
from sqlalchemy.dialects.postgresql import ENUM, JSONB, UUID

from mirante.domain.history import HistoryRecord
from mirante.domain.status import Status

metadata = MetaData()
# Espelha db/init/001_*.sql (o SQL e a fonte da verdade; um teste de integracao garante o espelho).
history = Table(
    "modernization_history",
    metadata,
    Column("id", UUID(as_uuid=True), primary_key=True),
    Column("source_code", Text, nullable=False),
    Column("generated_code", Text),
    Column("report", JSONB, nullable=False),
    Column("status", ENUM(Status, name="modernization_status", create_type=False), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("procedure_name", Text),
    Column("dialect", Text, nullable=False),
    Column("duration_ms", Integer),
)


class HistoryRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(self, record: HistoryRecord) -> uuid.UUID:
        values: dict[str, Any] = record.model_dump(exclude={"created_at"})
        with self._engine.begin() as conn:
            conn.execute(insert(history).values(**values))
        return record.id

    def get(self, record_id: uuid.UUID) -> HistoryRecord | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(history).where(history.c.id == record_id)).mappings().first()
        return HistoryRecord(**row) if row else None

    def list(self, limit: int = 50, status: Status | None = None) -> list[HistoryRecord]:
        query = select(history).order_by(history.c.created_at.desc()).limit(limit)
        if status:
            query = query.where(history.c.status == status)
        with self._engine.connect() as conn:
            return [HistoryRecord(**row) for row in conn.execute(query).mappings()]


def default_repository() -> HistoryRepository:
    from mirante.persistence.db import get_engine

    return HistoryRepository(get_engine())
