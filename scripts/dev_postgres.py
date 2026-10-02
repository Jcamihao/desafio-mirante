"""PostgreSQL embutido (sem Docker) para desenvolvimento e testes.

O pacote `pgserver` nao tem wheel para Python 3.14, mas o servidor e um processo separado: este
script roda num ambiente 3.12 efemero e o app (3.14) conecta por socket Unix.

    uv run --no-project --python 3.12 --with pgserver python scripts/dev_postgres.py start
    uv run --no-project --python 3.12 --with pgserver python scripts/dev_postgres.py stop

`start` imprime a DATABASE_URL (formato SQLAlchemy/psycopg) para colocar no .env.
"""

import sys
from pathlib import Path

import pgserver

PGDATA = Path(__file__).resolve().parent.parent / ".pgdata"
DATABASE = "mirante"


def main(action: str) -> None:
    server = pgserver.get_server(PGDATA, cleanup_mode=None)  # None: segue rodando apos o script
    if action == "stop":
        server.cleanup()
        print("postgres parado")
        return
    if DATABASE not in server.psql("SELECT datname FROM pg_database;"):
        server.psql(f"CREATE DATABASE {DATABASE};")
    print(server.get_uri(DATABASE).replace("postgresql://", "postgresql+psycopg://", 1))


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "start")
