"""Executa UMA chamada do Python gerado, isolado em subprocesso, e imprime o resultado em JSON.

Roda com `python -I` e ambiente minimo (sem chaves de API). NAO e um sandbox de seguranca (rede e
disco continuam acessiveis): em producao, rodar em container sem rede e com usuario sem privilegios.
Este arquivo nao importa nada de `mirante`: e carregado como script e tambem importado pelo harness
(para compartilhar `canonical_rows`, garantindo que os dois lados sejam normalizados igual).
"""

import dataclasses
import datetime
import decimal
import importlib
import json
import sys
from typing import Any


def canon(value: Any) -> Any:
    """Valor -> JSON comparavel: Decimal 10.00 == 10, datas em ISO, dataclass vira linha."""
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int | float | decimal.Decimal):
        number = decimal.Decimal(str(value)).normalize()
        return "0" if number == 0 else format(number, "f")
    if isinstance(value, datetime.date | datetime.datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): canon(v) for k, v in sorted(value.items())}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return [canon(getattr(value, f.name)) for f in dataclasses.fields(value)]
    if isinstance(value, list | tuple):
        return [canon(v) for v in value]
    return repr(value)


def _is_row(item: Any) -> bool:
    return dataclasses.is_dataclass(item) or isinstance(item, list | tuple)


def canonical_rows(result: Any) -> list[Any]:
    """Resultado de uma procedure -> lista de linhas canonicas (escalar = 1 linha de 1 coluna)."""
    if result is None:
        return []
    if isinstance(result, list) and all(_is_row(i) for i in result):
        return [canon(i) if _is_row(i) else [canon(i)] for i in result]
    return [canon(result) if _is_row(result) else [canon(result)]]


def _decode(obj: Any) -> Any:
    if isinstance(obj, dict) and "$decimal" in obj:
        return decimal.Decimal(obj["$decimal"])
    if isinstance(obj, dict) and "$date" in obj:
        return datetime.date.fromisoformat(obj["$date"])
    return obj


def run(payload: dict[str, Any]) -> dict[str, Any]:
    from sqlalchemy import create_engine

    sys.path.insert(0, payload["module_dir"])
    function = getattr(importlib.import_module(payload["module"]), payload["function"])
    engine = create_engine(
        payload["url"], connect_args={"options": f"-c search_path={payload['schema']}"}
    )
    args = [_decode(a) for a in payload["args"]]
    try:
        with engine.begin() as conn:  # commit se a funcao retornar; rollback se levantar
            result = function(conn, *args)
    except Exception as exc:  # noqa: BLE001 - qualquer erro do codigo gerado e um resultado
        orig = getattr(exc, "orig", None)
        return {
            "ok": False,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
                "sqlstate": getattr(orig, "sqlstate", None),
            },
        }
    return {"ok": True, "rows": canonical_rows(result)}


if __name__ == "__main__":
    print(json.dumps(run(json.load(sys.stdin))))
