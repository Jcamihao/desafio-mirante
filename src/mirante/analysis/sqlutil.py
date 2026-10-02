"""Inspecoes por AST de trechos SQL/expressoes (nada de regex sobre SQL)."""

from dataclasses import dataclass, field
from typing import Any

from pglast import ast, parse_sql
from pglast.parser import ParseError
from pglast.visitors import Visitor

COMPARISON_OPS = {"<>", "!=", "=", "<", ">", "<=", ">="}
SCALE_OPS = {"*", "/", "%", "^"}  # operacoes que aumentam a escala de um numeric


def _ref_name(node: Any) -> str | None:
    if isinstance(node, ast.ColumnRef) and node.fields:
        return getattr(node.fields[-1], "sval", None)
    return None


@dataclass
class ExprFacts:
    compared: set[str] = field(default_factory=set)  # variaveis em comparacao (=, <>, <...)
    null_tested: set[str] = field(default_factory=set)  # variaveis com IS [NOT] NULL
    scaling_ops: bool = False  # tem * / % ou GREATEST/LEAST sobre eles
    funcs_on_columns: set[str] = field(default_factory=set)  # f(coluna) dentro de comparacao


class _Facts(Visitor):
    def __init__(self, plpgsql_names: frozenset[str]) -> None:
        super().__init__()
        self.facts = ExprFacts()
        self._plpgsql_names = plpgsql_names  # variaveis/parametros: nao sao colunas

    def visit_A_Expr(self, ancestors: Any, node: ast.A_Expr) -> None:
        op = str(node.name[0].sval) if node.name else ""
        if op in SCALE_OPS:
            self.facts.scaling_ops = True
        if op in COMPARISON_OPS:
            for side in (node.lexpr, node.rexpr):
                if name := _ref_name(side):
                    self.facts.compared.add(name)
                if (
                    isinstance(side, ast.FuncCall)
                    and side.funcname
                    and any(
                        _ref_name(a) and _ref_name(a) not in self._plpgsql_names
                        for a in side.args or ()
                    )
                ):
                    self.facts.funcs_on_columns.add(str(side.funcname[-1].sval))

    def visit_NullTest(self, ancestors: Any, node: ast.NullTest) -> None:
        if name := _ref_name(node.arg):
            self.facts.null_tested.add(name)


def expr_facts(
    text: str, *, is_expression: bool = True, plpgsql_names: frozenset[str] = frozenset()
) -> ExprFacts:
    """Fatos de uma expressao PL/pgSQL (`is_expression`) ou de um SQL completo."""
    try:
        tree = parse_sql(f"SELECT {text}" if is_expression else text)
    except ParseError:
        return ExprFacts()
    visitor = _Facts(plpgsql_names)
    visitor(tree)
    return visitor.facts


def insert_values(sql: str) -> tuple[str, dict[str, bool]] | None:
    """Para `INSERT ... VALUES (...)`: (tabela, {coluna: valor_e_literal?}); None se nao for."""
    try:
        stmt = parse_sql(sql)[0].stmt
    except ParseError:
        return None
    if not isinstance(stmt, ast.InsertStmt) or stmt.relation is None:
        return None
    select = stmt.selectStmt
    if not isinstance(select, ast.SelectStmt) or not select.valuesLists:
        return None
    columns = [str(c.name) for c in stmt.cols or ()]
    return str(stmt.relation.relname), {
        col: isinstance(val, ast.A_Const)
        for col, val in zip(columns, select.valuesLists[0], strict=False)
    }
