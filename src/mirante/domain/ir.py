"""Representacao intermediaria (IR) produzida pelo parsing e consumida por analise/geracao.

Tudo aqui e Pydantic: serializa para JSONB (relatorio) e para o prompt do LLM sem adaptadores.
"""

from typing import Any, Literal

from pydantic import BaseModel, Field

ParamMode = Literal["IN", "OUT", "INOUT", "VARIADIC", "TABLE"]


class Param(BaseModel):
    name: str
    mode: ParamMode
    type: str
    position: int


class ReturnSpec(BaseModel):
    type: str | None = None
    setof: bool = False
    columns: list[Param] = Field(default_factory=list)  # RETURNS TABLE (...)


class SqlInfo(BaseModel):
    """O que se sabe sobre um trecho de SQL embutido (obtido por parsing, nao por regex)."""

    text: str
    statement_type: str | None = None  # select/insert/update/delete; None p/ expressoes
    tables: list[str] = Field(default_factory=list)
    cte_names: list[str] = Field(default_factory=list)
    recursive: bool = False
    locking: str | None = None  # ex.: "FOR UPDATE"
    functions: list[str] = Field(default_factory=list)
    parse_error: str | None = None


class Stmt(BaseModel):
    """No generico da arvore PL/pgSQL. `blocks` guarda sub-blocos nomeados (then/else/body/...)."""

    kind: str
    line: int | None = None
    text: str | None = None  # condicao, expressao, mensagem...
    sql: SqlInfo | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    blocks: dict[str, list[Stmt]] = Field(default_factory=dict)


class Variable(BaseModel):
    name: str
    type: str
    kind: Literal["variable", "cursor"] = "variable"
    default: str | None = None
    cursor_query: SqlInfo | None = None


class ParsedProcedure(BaseModel):
    name: str
    kind: Literal["function", "procedure"]
    language: str
    replace: bool = False
    params: list[Param] = Field(default_factory=list)
    returns: ReturnSpec | None = None
    variables: list[Variable] = Field(default_factory=list)
    body: list[Stmt] = Field(default_factory=list)


class Column(BaseModel):
    name: str
    type: str
    not_null: bool = False
    default: str | None = None
    checks: list[str] = Field(default_factory=list)


class Table(BaseModel):
    name: str
    columns: list[Column] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
