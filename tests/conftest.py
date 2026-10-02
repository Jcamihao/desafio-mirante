import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from mirante.config import get_settings
from mirante.persistence.db import make_engine
from mirante.persistence.migrate import apply_migrations
from mirante.persistence.repository import HistoryRepository

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


@pytest.fixture(scope="session")
def sample():
    def load(prefix: str) -> str:
        return next(SAMPLES.glob(f"{prefix}_*.sql")).read_text()

    return load


@pytest.fixture(scope="session")
def schema_sql() -> str:
    return (SAMPLES / "schema.sql").read_text()


@pytest.fixture(scope="session")
def pg_engine():
    """Banco TEMPORARIO por sessao (nunca toca no banco de desenvolvimento). Pula sem Postgres."""
    url = make_url(os.environ.get("TEST_DATABASE_URL") or get_settings().database_url)
    name = f"mirante_test_{uuid.uuid4().hex[:8]}"
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f"CREATE DATABASE {name}"))
    except OperationalError as exc:
        pytest.skip(
            f"PostgreSQL indisponivel ({type(exc).__name__}); suba com scripts/dev_postgres.py"
        )
    engine = make_engine(url.set(database=name).render_as_string(hide_password=False))
    yield engine
    engine.dispose()
    with admin.connect() as conn:
        conn.execute(text(f"DROP DATABASE {name} WITH (FORCE)"))
    admin.dispose()


@pytest.fixture
def repo(pg_engine) -> HistoryRepository:
    """Repositorio sobre o schema migrado, com a tabela limpa a cada teste."""
    apply_migrations(pg_engine)
    with pg_engine.begin() as conn:
        conn.execute(text("TRUNCATE modernization_history"))
    return HistoryRepository(pg_engine)
