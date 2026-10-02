from collections import Counter

from mirante.analysis.rules import BUILTINS, DML, RULES, Ctx
from mirante.analysis.tree import walk
from mirante.domain.analysis import Analysis, Constructs, Risk
from mirante.domain.ir import ParsedProcedure, Table

_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


def _constructs(ctx: Ctx) -> Constructs:
    proc, nodes = ctx.proc, ctx.nodes
    stmts = [s for s, _ in nodes]
    infos = ctx.sql_infos()
    known = {d.name.lower() for d in ctx.dependencies} | {proc.name.lower()}
    functions = sorted({f.lower() for _, i in infos for f in i.functions})
    written: dict[str, set[str]] = {}
    read: set[str] = set()
    for s in stmts:
        if s.kind == "execsql" and s.sql:
            if s.sql.statement_type in DML:
                written.setdefault(s.sql.tables[0], set()).add(s.sql.statement_type.upper())
            else:
                read.update(s.sql.tables)
    return Constructs(
        in_params=[p.name for p in proc.params if p.mode in {"IN", "INOUT", "VARIADIC"}],
        out_params=[p.name for p in proc.params if p.mode in {"OUT", "INOUT"}],
        returns_set=bool(proc.returns and proc.returns.setof),
        variables=len(proc.variables),
        cursors=[v.name for v in proc.variables if v.kind == "cursor"],
        locks=[f"{t} {i.locking}" for _, i in infos if i.locking for t in i.tables],
        exception_handlers=sorted(
            {c for s in stmts for c in s.detail.get("exception_conditions", [])}
        ),
        raises=dict(Counter(s.detail["level"] for s in stmts if s.kind == "raise")),
        ctes=sorted({c for _, i in infos for c in i.cte_names}),
        recursive_cte=any(i.recursive for _, i in infos),
        calls_resolved=[f for f in functions if f in known],
        calls_builtin=[f for f in functions if f in BUILTINS],
        calls_unresolved=[f for f in functions if f not in known and f not in BUILTINS],
        tables_read=sorted(read),
        tables_written={t: sorted(ops) for t, ops in sorted(written.items())},
        uses_json=any(f.startswith(("jsonb_", "json_")) for f in functions),
        statement_counts=dict(Counter(s.kind for s in stmts)),
    )


def analyze(
    proc: ParsedProcedure,
    dependencies: list[ParsedProcedure] | None = None,
    schema: list[Table] | None = None,
) -> Analysis:
    ctx = Ctx(
        proc=proc,
        dependencies=dependencies or [],
        schema={t.name: t for t in schema or []},
        nodes=list(walk(proc.body)),
    )
    risks: list[Risk] = []
    skipped: list[str] = []
    for fn, needs_schema in RULES:
        if needs_schema and not ctx.schema:
            skipped.append(fn.__name__)
            continue
        risks.extend(fn(ctx))
    risks.sort(key=lambda r: (_SEVERITY_ORDER[r.severity], r.lines[:1] or [0]))
    return Analysis(constructs=_constructs(ctx), risks=risks, skipped_rules=skipped)
