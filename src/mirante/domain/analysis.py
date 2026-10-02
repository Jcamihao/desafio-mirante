"""Saida da analise semantica: construcoes encontradas + pontos de risco para a traducao."""

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["low", "medium", "high"]


class Risk(BaseModel):
    code: str
    severity: Severity
    lines: list[int] = Field(default_factory=list)
    message: str  # o que e o risco e por que importa
    recommendation: str  # como traduzir sem perder a semantica


class Constructs(BaseModel):
    in_params: list[str] = Field(default_factory=list)
    out_params: list[str] = Field(default_factory=list)
    returns_set: bool = False
    variables: int = 0
    cursors: list[str] = Field(default_factory=list)
    locks: list[str] = Field(default_factory=list)  # "contas FOR UPDATE"
    exception_handlers: list[str] = Field(default_factory=list)
    raises: dict[str, int] = Field(default_factory=dict)  # nivel -> quantidade
    ctes: list[str] = Field(default_factory=list)
    recursive_cte: bool = False
    calls_resolved: list[str] = Field(default_factory=list)
    calls_builtin: list[str] = Field(default_factory=list)
    calls_unresolved: list[str] = Field(default_factory=list)
    tables_read: list[str] = Field(default_factory=list)
    tables_written: dict[str, list[str]] = Field(default_factory=dict)  # tabela -> operacoes
    uses_json: bool = False
    statement_counts: dict[str, int] = Field(default_factory=dict)


class Analysis(BaseModel):
    constructs: Constructs
    risks: list[Risk] = Field(default_factory=list)
    skipped_rules: list[str] = Field(default_factory=list)  # regras que exigiam schema

    def by_severity(self) -> dict[str, int]:
        counts = {"high": 0, "medium": 0, "low": 0}
        for risk in self.risks:
            counts[risk.severity] += 1
        return counts
