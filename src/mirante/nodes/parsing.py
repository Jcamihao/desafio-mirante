"""No 1 — Parsing: SQL bruto -> IR estruturada. Sem I/O e sem LLM (deterministico)."""

import time
from collections import Counter
from typing import Any

from mirante.domain.errors import SqlParseError, UnsupportedDialectError
from mirante.domain.ir import Stmt
from mirante.domain.state import PipelineState, StageError
from mirante.parsers import get_parser


def _walk(stmts: list[Stmt]):
    for stmt in stmts:
        yield stmt
        for children in stmt.blocks.values():
            yield from _walk(children)


def parsing_node(state: PipelineState) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        parser = get_parser(state.get("dialect", "postgres"))
        parsed = parser.parse_procedure(state["source_code"])
        dependencies = [parser.parse_procedure(src) for src in state.get("dependencies", [])]
        schema_sql = state.get("schema_sql")
        schema = parser.parse_schema(schema_sql) if schema_sql else []
    except (SqlParseError, UnsupportedDialectError) as exc:
        return {
            "errors": [
                StageError(
                    stage="parsing",
                    message=str(exc),
                    location=getattr(exc, "location", None),
                )
            ],
            "report": {"parsing": {"status": "failed", "error": str(exc)}},
        }

    statements = list(_walk(parsed.body))
    tables = sorted({t for s in statements if s.sql for t in s.sql.tables})
    return {
        "parsed": parsed,
        "parsed_dependencies": dependencies,
        "schema": schema,
        "report": {
            "parsing": {
                "status": "ok",
                "procedure": parsed.name,
                "kind": parsed.kind,
                "params": [f"{p.mode} {p.name} {p.type}" for p in parsed.params],
                "statements": dict(Counter(s.kind for s in statements)),
                "tables": tables,
                "dependencies": [d.name for d in dependencies],
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        },
    }
