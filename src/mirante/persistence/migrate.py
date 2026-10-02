"""Migrador minimo: aplica db/init/*.sql em ordem e registra em schema_migrations."""

from pathlib import Path

from sqlalchemy import Engine, text

MIGRATIONS_DIR = Path(__file__).resolve().parents[3] / "db" / "init"


def apply_migrations(engine: Engine, directory: Path = MIGRATIONS_DIR) -> list[str]:
    """Devolve os arquivos aplicados nesta chamada (vazio se ja estava tudo em dia)."""
    applied: list[str] = []
    with engine.begin() as conn:
        # Serializa instancias subindo juntas: o lock cai sozinho no fim da transacao.
        conn.execute(text("SELECT pg_advisory_xact_lock(727001)"))
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        )
        done: set[str] = set(conn.execute(text("SELECT filename FROM schema_migrations")).scalars())
        for path in sorted(directory.glob("*.sql")):
            if path.name in done:
                continue
            conn.exec_driver_sql(path.read_text())
            conn.execute(
                text("INSERT INTO schema_migrations (filename) VALUES (:f)"), {"f": path.name}
            )
            applied.append(path.name)
    return applied


if __name__ == "__main__":  # uv run python -m mirante.persistence.migrate
    from mirante.persistence.db import get_engine

    done = apply_migrations(get_engine())
    print("aplicadas:", done or "nenhuma (banco em dia)")
