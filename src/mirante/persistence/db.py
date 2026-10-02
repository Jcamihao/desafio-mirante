import json
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text

from mirante.config import get_settings


def make_engine(url: str) -> Engine:
    return create_engine(
        url,
        pool_pre_ping=True,  # descarta conexoes mortas (ex.: banco reiniciado)
        json_serializer=lambda obj: json.dumps(obj, ensure_ascii=False, default=str),
    )


@lru_cache
def get_engine() -> Engine:
    return make_engine(get_settings().database_url)


def ping(engine: Engine) -> bool:
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:  # noqa: BLE001 - qualquer falha de conexao significa "indisponivel"
        return False
    return True
