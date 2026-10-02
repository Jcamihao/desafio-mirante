"""Estado tipado compartilhado pelos nos do grafo LangGraph.

Regra: cada no le do estado e devolve so as chaves que altera (atualizacao parcial).
`errors` e `report` usam reducers para acumular entre nos em vez de sobrescrever.
"""

import operator
from typing import Annotated, Any, Protocol, TypedDict

from pydantic import BaseModel

from mirante.domain.analysis import Analysis
from mirante.domain.generation import GeneratedModule
from mirante.domain.ir import ParsedProcedure, Table
from mirante.domain.status import Status
from mirante.domain.validation import ValidationReport


class StageError(BaseModel):
    stage: str
    message: str
    location: int | None = None


class PipelineState(TypedDict, total=False):
    # --- entrada ---
    source_code: str
    dialect: str
    schema_sql: str | None  # Anexo A (opcional): enriquece geracao/validacao
    dependencies: list[str]  # outras procedures chamadas (ex.: Anexo B para o F)

    # --- saida do parsing ---
    parsed: ParsedProcedure
    parsed_dependencies: list[ParsedProcedure]
    schema: list[Table]

    # --- saida da analise semantica ---
    analysis: Analysis

    # --- saida da geracao ---
    generated: GeneratedModule
    generated_code: str
    generation_log: list[dict[str, Any]]  # uma entrada por tentativa (tokens, duracao)
    validation_feedback: list[str]  # problemas da validacao a corrigir na proxima tentativa

    # --- saida da validacao ---
    validation: ValidationReport
    validation_log: list[dict[str, Any]]  # uma entrada por tentativa

    # --- controle do laco de reparo ---
    trace_id: str | None  # id do trace no Langfuse (liga a linha do historico ao trace)
    max_attempts: int  # opcional na entrada; padrao em Settings.max_generation_attempts

    # --- saida da persistencia ---
    status: Status
    history_id: str
    final_report: dict[str, Any]  # o mesmo relatorio gravado em modernization_history

    # --- acumuladores ---
    errors: Annotated[list[StageError], operator.add]
    report: Annotated[dict[str, Any], operator.or_]


class NodeFunction(Protocol):
    """Forma de um no do grafo. O parametro posicional-somente e o que o LangGraph tipa."""

    def __call__(self, state: PipelineState, /) -> dict[str, Any]: ...
