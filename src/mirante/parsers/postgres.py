"""Parser PL/pgSQL baseado em pglast (libpg_query = o proprio parser do PostgreSQL).

Duas passadas:
  1. `parse_sql`      -> assinatura (nome, parametros IN/OUT, retorno, linguagem, corpo em texto).
  2. `parse_plpgsql` -> AST do corpo (IF/LOOP/FETCH/RAISE/...), convertida para a IR (`Stmt`).
Cada SQL embutido e re-parseado com `parse_sql` para extrair tabelas, CTEs, locks e funcoes.
"""

import re
from typing import Any

from pglast import ast, parse_plpgsql, parse_sql
from pglast.parser import ParseError
from pglast.stream import RawStream
from pglast.visitors import Visitor

from mirante.domain.errors import SqlParseError
from mirante.domain.ir import (
    Column,
    Param,
    ParamMode,
    ParsedProcedure,
    ReturnSpec,
    SqlInfo,
    Stmt,
    Table,
    Variable,
)

_PARAM_MODES: dict[str, ParamMode] = {
    "d": "IN",  # sem modo explicito (FUNC_PARAM_DEFAULT) == IN
    "i": "IN",
    "o": "OUT",
    "b": "INOUT",
    "v": "VARIADIC",
    "t": "TABLE",
}
_ELOG_LEVELS = {
    10: "DEBUG",
    15: "LOG",
    17: "INFO",
    18: "NOTICE",
    19: "WARNING",
    21: "EXCEPTION",
}
_LOCK_NAMES = {
    "FORUPDATE": "FOR UPDATE",
    "FORNOKEYUPDATE": "FOR NO KEY UPDATE",
    "FORSHARE": "FOR SHARE",
    "FORKEYSHARE": "FOR KEY SHARE",
}
_IMPLICIT_VARS = {"found", "sqlstate", "sqlerrm"}
_EXPR_MODE = 2  # PLPGSQL_RAWPARSE_PLPGSQL_EXPR
_ASSIGN_MODES = {3, 4, 5}  # PLPGSQL_RAWPARSE_ASSIGN1..3


def _type_name(tn: ast.TypeName) -> str:
    base = [str(n.sval) for n in tn.names or () if n.sval != "pg_catalog"][-1]
    if tn.typmods:
        mods = [str(m.val.ival) for m in tn.typmods]
        base += f"({','.join(mods)})"
    return base + ("[]" if tn.arrayBounds else "")


_DECLARED_TYPE = re.compile(
    r"\s*{name}\s+(?:CONSTANT\s+)?([^;:=]+?)\s*(?:NOT\s+NULL\s*)?(?::=|=|DEFAULT|;)", re.I
)


def _declared_type(body_lines: list[str], lineno: int | None, name: str, fallback: str) -> str:
    """O datum do PL/pgSQL so traz o tipo-base (`numeric`); a precisao `(18,2)` so existe no texto.

    Recupera o tipo da linha do DECLARE (via `lineno`) e o canoniza com o proprio parser.
    Precisao importa: atribuir a NUMERIC(18,2) arredonda — a traducao precisa reproduzir isso.
    """
    if not lineno or not 0 < lineno <= len(body_lines):
        return fallback
    match = re.match(
        _DECLARED_TYPE.pattern.format(name=re.escape(name)), body_lines[lineno - 1], re.I
    )
    if not match:
        return fallback
    try:
        stmt: Any = parse_sql(f"SELECT NULL::{match.group(1)}")[0].stmt  # sondagem dinamica
        return _type_name(stmt.targetList[0].val.typeName)
    except ParseError, AttributeError:
        return fallback


def rhs_of_assignment(text: str) -> str:
    for sep in (":=", "="):
        if sep in text:
            return text.split(sep, 1)[1].strip()
    return text


class _SqlCollector(Visitor):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[str] = []
        self.ctes: list[str] = []
        self.functions: list[str] = []
        self.recursive = False
        self.locking: str | None = None

    def visit_RangeVar(self, ancestors: Any, node: ast.RangeVar) -> None:
        if node.relname:
            self.tables.append(node.relname)

    def visit_CommonTableExpr(self, ancestors: Any, node: ast.CommonTableExpr) -> None:
        if node.ctename:
            self.ctes.append(node.ctename)

    def visit_WithClause(self, ancestors: Any, node: ast.WithClause) -> None:
        self.recursive = self.recursive or bool(node.recursive)

    def visit_FuncCall(self, ancestors: Any, node: ast.FuncCall) -> None:
        self.functions.append(".".join(str(n.sval) for n in node.funcname or ()))

    def visit_LockingClause(self, ancestors: Any, node: ast.LockingClause) -> None:
        if node.strength is None:
            return
        key = node.strength.name.removeprefix("LCS_")
        self.locking = _LOCK_NAMES.get(key, key)


def _unique(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def analyze_sql(query: str, parse_mode: int = 0) -> SqlInfo:
    """Extrai tabelas/CTEs/locks/funcoes de um SQL (ou expressao) embutido no PL/pgSQL."""
    text = query.strip()
    candidate = text
    if parse_mode == _EXPR_MODE:
        candidate = f"SELECT {text}"
    elif parse_mode in _ASSIGN_MODES:
        candidate = f"SELECT {rhs_of_assignment(text)}"
    try:
        tree = parse_sql(candidate)
    except ParseError as exc:
        return SqlInfo(text=text, parse_error=str(exc))

    collector = _SqlCollector()
    collector(tree)
    statement_type = None
    if parse_mode == 0 and tree:
        statement_type = type(tree[0].stmt).__name__.removesuffix("Stmt").lower()
    return SqlInfo(
        text=text,
        statement_type=statement_type,
        tables=[t for t in _unique(collector.tables) if t not in collector.ctes],
        cte_names=_unique(collector.ctes),
        recursive=collector.recursive,
        locking=collector.locking,
        functions=_unique(collector.functions),
    )


class _StmtConverter:
    """Converte o JSON do `parse_plpgsql` em `Stmt`, resolvendo `varno` -> nome de variavel."""

    def __init__(self, datums: list[dict[str, Any]]) -> None:
        self._names: list[str] = [str(next(iter(d.values())).get("refname", "?")) for d in datums]

    def name(self, varno: int | None) -> str | None:
        if varno is None or not 0 <= varno < len(self._names):
            return None
        return self._names[varno]

    def expr(self, node: dict[str, Any] | None) -> tuple[str | None, SqlInfo | None]:
        if not node:
            return None, None
        body = node["PLpgSQL_expr"]
        info = analyze_sql(body["query"], body.get("parseMode", 0))
        interesting = info.tables or info.functions or info.cte_names or info.parse_error
        return info.text, info if interesting else None

    def stmts(self, nodes: list[dict[str, Any]] | None) -> list[Stmt]:
        return [self.stmt(n) for n in nodes or []]

    def stmt(self, node: dict[str, Any]) -> Stmt:
        ((key, body),) = node.items()
        kind = key.removeprefix("PLpgSQL_stmt_")
        handler = getattr(self, f"_{kind}", None)
        result = handler(body) if handler else Stmt(kind=kind)
        result.line = body.get("lineno")
        return result

    # --- estruturas de controle -------------------------------------------------
    def _block(self, b: dict[str, Any]) -> Stmt:
        blocks = {"body": self.stmts(b.get("body"))}
        conditions: list[str] = []
        exc = (b.get("exceptions") or {}).get("PLpgSQL_exception_block", {})
        for handler in exc.get("exc_list", []):
            h = handler["PLpgSQL_exception"]
            names = [c["PLpgSQL_condition"]["condname"] for c in h["conditions"]]
            conditions += names
            blocks[f"exception:{'|'.join(names)}"] = self.stmts(h["action"])
        return Stmt(
            kind="block",
            blocks=blocks,
            detail={"exception_conditions": conditions} if conditions else {},
        )

    def _if(self, b: dict[str, Any]) -> Stmt:
        text, sql = self.expr(b["cond"])
        blocks = {"then": self.stmts(b.get("then_body"))}
        for i, elsif in enumerate(b.get("elsif_list") or []):
            e = elsif["PLpgSQL_if_elsif"]
            blocks[f"elsif[{i}]"] = self.stmts(e["stmts"])
        if b.get("else_body"):
            blocks["else"] = self.stmts(b["else_body"])
        return Stmt(kind="if", text=text, sql=sql, blocks=blocks)

    def _case(self, b: dict[str, Any]) -> Stmt:
        text, _ = self.expr(b.get("t_expr"))
        blocks: dict[str, list[Stmt]] = {}
        whens: list[str | None] = []
        for i, w in enumerate(b["case_when_list"]):
            wb = w["PLpgSQL_case_when"]
            when_text = self.expr(wb["expr"])[0]
            # o PG troca o seletor por uma variavel sintetica (__Case__Variable_N__): desfaz
            synthetic = self.name(b.get("t_varno"))
            if when_text and synthetic and text:
                when_text = when_text.replace(f'"{synthetic}"', text)
            whens.append(when_text)
            blocks[f"when[{i}]"] = self.stmts(wb["stmts"])
        if b.get("else_stmts"):
            blocks["else"] = self.stmts(b["else_stmts"])
        return Stmt(kind="case", text=text, detail={"when": whens}, blocks=blocks)

    def _loop(self, b: dict[str, Any]) -> Stmt:
        return Stmt(kind="loop", blocks={"body": self.stmts(b.get("body"))})

    def _while(self, b: dict[str, Any]) -> Stmt:
        text, sql = self.expr(b["cond"])
        return Stmt(kind="while", text=text, sql=sql, blocks={"body": self.stmts(b.get("body"))})

    def _fors(self, b: dict[str, Any]) -> Stmt:
        text, sql = self.expr(b["query"])
        return Stmt(kind="fors", text=text, sql=sql, blocks={"body": self.stmts(b.get("body"))})

    def _fori(self, b: dict[str, Any]) -> Stmt:
        return Stmt(kind="fori", blocks={"body": self.stmts(b.get("body"))})

    def _exit(self, b: dict[str, Any]) -> Stmt:
        text, _ = self.expr(b.get("cond"))
        is_exit = b.get("is_exit", False)
        return Stmt(kind="exit" if is_exit else "continue", text=text)

    # --- SQL / atribuicao ------------------------------------------------------
    def _execsql(self, b: dict[str, Any]) -> Stmt:
        text, sql = self.expr(b["sqlstmt"])
        if sql is None:
            sql = analyze_sql(text or "")
        detail: dict[str, Any] = {}
        if b.get("into"):
            target = next(iter(b["target"].values()))
            detail["into"] = [f["name"] for f in target.get("fields", [])] or [
                target.get("refname")
            ]
            detail["strict"] = bool(b.get("strict"))
        return Stmt(kind="execsql", text=text, sql=sql, detail=detail)

    def _assign(self, b: dict[str, Any]) -> Stmt:
        text, sql = self.expr(b["expr"])
        return Stmt(kind="assign", text=text, sql=sql, detail={"target": self.name(b.get("varno"))})

    def _perform(self, b: dict[str, Any]) -> Stmt:
        text, sql = self.expr(b["expr"])
        return Stmt(kind="perform", text=text, sql=sql)

    def _dynexecute(self, b: dict[str, Any]) -> Stmt:
        text, _ = self.expr(b["query"])
        return Stmt(kind="dynexecute", text=text)

    def _getdiag(self, b: dict[str, Any]) -> Stmt:
        items = [
            {
                "item": d["PLpgSQL_diag_item"]["kind"],
                "target": self.name(d["PLpgSQL_diag_item"]["target"]),
            }
            for d in b["diag_items"]
        ]
        return Stmt(kind="getdiag", detail={"items": items})

    # --- retorno / erros -------------------------------------------------------
    def _return(self, b: dict[str, Any]) -> Stmt:
        text, sql = self.expr(b.get("expr"))
        return Stmt(kind="return", text=text, sql=sql)

    def _return_next(self, b: dict[str, Any]) -> Stmt:
        text, sql = self.expr(b.get("expr"))
        return Stmt(kind="return_next", text=text, sql=sql)

    def _return_query(self, b: dict[str, Any]) -> Stmt:
        text, sql = self.expr(b.get("query"))
        return Stmt(kind="return_query", text=text, sql=sql)

    def _raise(self, b: dict[str, Any]) -> Stmt:
        level = b.get("elog_level")
        params = [self.expr(p)[0] for p in b.get("params") or []]
        level = int(b.get("elog_level", 0))
        message = b.get("message")
        return Stmt(
            kind="raise",
            text=message,
            detail={
                "level": _ELOG_LEVELS.get(level, str(level)),
                "params": params,
                "reraise": message is None and not b.get("condname"),
            },
        )

    # --- cursores --------------------------------------------------------------
    def _open(self, b: dict[str, Any]) -> Stmt:
        return Stmt(kind="open", detail={"cursor": self.name(b.get("curvar"))})

    def _fetch(self, b: dict[str, Any]) -> Stmt:
        target = next(iter(b["target"].values()))
        return Stmt(
            kind="fetch",
            detail={
                "cursor": self.name(b.get("curvar")),
                "into": [f["name"] for f in target.get("fields", [])],
                "is_move": bool(b.get("is_move")),
            },
        )

    def _close(self, b: dict[str, Any]) -> Stmt:
        return Stmt(kind="close", detail={"cursor": self.name(b.get("curvar"))})


class PostgresParser:
    dialect = "postgres"

    def parse_procedure(self, source: str) -> ParsedProcedure:
        try:
            tree = parse_sql(source)
        except ParseError as exc:
            raise SqlParseError(f"SQL invalido: {exc}", getattr(exc, "location", None)) from exc

        creates = [r.stmt for r in tree if isinstance(r.stmt, ast.CreateFunctionStmt)]
        if not creates:
            raise SqlParseError("Nenhum CREATE [OR REPLACE] FUNCTION/PROCEDURE encontrado")
        create = creates[0]

        options = {o.defname: o.arg for o in create.options or []}
        language = options["language"].sval.lower() if "language" in options else "sql"
        if language != "plpgsql":
            raise SqlParseError(f"Linguagem '{language}' nao suportada: apenas plpgsql")

        params = [
            Param(
                name=p.name or f"${i}",
                mode=_PARAM_MODES[p.mode.value],
                type=_type_name(p.argType),
                position=i,
            )
            for i, p in enumerate(create.parameters or [], start=1)
        ]
        returns = self._returns(create, params)
        params = [p for p in params if p.mode != "TABLE"]  # colunas de saida ficam em returns

        try:
            function = parse_plpgsql(source)[0]["PLpgSQL_function"]
        except ParseError as exc:
            raise SqlParseError(f"PL/pgSQL invalido: {exc}") from exc

        converter = _StmtConverter(function["datums"])
        return ParsedProcedure(
            name=".".join(str(n.sval) for n in create.funcname or ()),
            kind="procedure" if create.is_procedure else "function",
            language=language,
            replace=bool(create.replace),
            params=params,
            returns=returns,
            variables=self._variables(
                function["datums"], {p.name for p in params}, options["as"][0].sval.splitlines()
            ),
            body=converter.stmts([function["action"]]),
        )

    @staticmethod
    def _returns(create: ast.CreateFunctionStmt, params: list[Param]) -> ReturnSpec | None:
        columns = [p for p in params if p.mode == "TABLE"]
        if create.returnType is None:
            return None
        return ReturnSpec(
            type=None if columns else _type_name(create.returnType),
            setof=bool(create.returnType.setof) or bool(columns),
            columns=columns,
        )

    @staticmethod
    def _variables(
        datums: list[dict[str, Any]], param_names: set[str], body_lines: list[str]
    ) -> list[Variable]:
        out: list[Variable] = []
        for datum in datums:
            kind, body = next(iter(datum.items()))
            name = body.get("refname", "")
            if kind != "PLpgSQL_var" or name in param_names | _IMPLICIT_VARS:
                continue
            if name.startswith("__Case__Variable"):
                continue
            cursor_expr = body.get("cursor_explicit_expr")
            default = body.get("default_val")
            base_type = body["datatype"]["PLpgSQL_type"]["typname"]
            out.append(
                Variable(
                    name=name,
                    type=_declared_type(body_lines, body.get("lineno"), name, base_type),
                    kind="cursor" if cursor_expr else "variable",
                    default=default["PLpgSQL_expr"]["query"] if default else None,
                    cursor_query=(
                        analyze_sql(cursor_expr["PLpgSQL_expr"]["query"]) if cursor_expr else None
                    ),
                )
            )
        return out

    def parse_schema(self, ddl: str) -> list[Table]:
        try:
            tree = parse_sql(ddl)
        except ParseError as exc:
            raise SqlParseError(f"Schema invalido: {exc}") from exc

        tables: list[Table] = []
        for raw in tree:
            stmt = raw.stmt
            if not isinstance(stmt, ast.CreateStmt):
                continue
            if stmt.relation is None:
                continue
            table = Table(name=str(stmt.relation.relname))
            for elt in stmt.tableElts or []:
                if isinstance(elt, ast.ColumnDef):
                    table.columns.append(self._column(elt, table))
                elif (
                    isinstance(elt, ast.Constraint)
                    and elt.contype
                    and elt.contype.name == "CONSTR_PRIMARY"
                ):
                    table.primary_key = [str(k.sval) for k in elt.keys or ()]
            tables.append(table)
        return tables

    @staticmethod
    def _column(elt: ast.ColumnDef, table: Table) -> Column:
        if elt.typeName is None:
            raise SqlParseError(f"Coluna '{elt.colname}' sem tipo")
        column = Column(name=str(elt.colname), type=_type_name(elt.typeName))
        for c in elt.constraints or []:
            match c.contype.name if c.contype else "":
                case "CONSTR_NOTNULL":
                    column.not_null = True
                case "CONSTR_PRIMARY":
                    table.primary_key = [str(elt.colname)]
                    column.not_null = True
                case "CONSTR_DEFAULT":
                    column.default = RawStream()(c.raw_expr)
                case "CONSTR_CHECK":
                    column.checks.append(RawStream()(c.raw_expr))
        return column
