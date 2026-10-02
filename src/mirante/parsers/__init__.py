from mirante.domain.errors import UnsupportedDialectError
from mirante.parsers.base import SqlParser
from mirante.parsers.postgres import PostgresParser

_REGISTRY: dict[str, type[SqlParser]] = {
    "postgres": PostgresParser,
    "postgresql": PostgresParser,
    "plpgsql": PostgresParser,
}


def get_parser(dialect: str) -> SqlParser:
    try:
        return _REGISTRY[dialect.lower()]()
    except KeyError:
        supported = ", ".join(sorted({p.dialect for p in _REGISTRY.values()}))
        raise UnsupportedDialectError(
            f"Dialeto '{dialect}' nao suportado. Disponiveis: {supported}"
        ) from None


__all__ = ["SqlParser", "get_parser"]
