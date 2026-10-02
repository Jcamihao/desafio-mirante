"""Monta e compila o grafo LangGraph. Exposto ao servidor via langgraph.json."""

from collections.abc import Callable

from langgraph.graph import END, START, StateGraph

from mirante.domain.state import PipelineState
from mirante.graph.routing import after_analysis, after_generation, after_parsing, after_validation
from mirante.llm.generator import CodeGenerator, default_generator
from mirante.nodes.analysis import analysis_node
from mirante.nodes.generation import build_generation_node
from mirante.nodes.parsing import parsing_node
from mirante.nodes.persistence import build_persistence_node
from mirante.nodes.validation import validation_node
from mirante.persistence.repository import HistoryRepository, default_repository


def build_graph(
    generator_factory: Callable[[], CodeGenerator] = default_generator,
    repository_factory: Callable[[], HistoryRepository] = default_repository,
):
    builder = StateGraph(PipelineState)
    builder.add_node("parsing", parsing_node)
    builder.add_node("analysis", analysis_node)
    # mypy: o LangGraph nao infere `NodeInputT` para nos criados por fabrica (limitacao da lib);
    # o contrato dos dois nos e coberto por testes de grafo.
    builder.add_node("generation", build_generation_node(generator_factory))  # type: ignore[call-overload]
    builder.add_node("persist", build_persistence_node(repository_factory))  # type: ignore[call-overload]
    builder.add_edge("persist", END)
    builder.add_edge(START, "parsing")
    builder.add_conditional_edges(
        "parsing", after_parsing, {"analysis": "analysis", "failed": "persist"}
    )
    builder.add_conditional_edges(
        "analysis", after_analysis, {"generation": "generation", "failed": "persist"}
    )
    builder.add_node("validation", validation_node)
    builder.add_conditional_edges(
        "generation", after_generation, {"validation": "validation", "failed": "persist"}
    )
    builder.add_conditional_edges(
        "validation", after_validation, {"done": "persist", "repair": "generation"}
    )
    return builder.compile()


graph = build_graph()
