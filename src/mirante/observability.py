"""Observabilidade com Langfuse: um trace por execucao, spans por no do grafo, scores de qualidade.

Regra de ouro: observabilidade NUNCA derruba o pipeline. Falhas aqui viram log, nao excecao.
O `Tracer` fica atras de uma interface (como o LLM): os testes unitarios nao dependem do Langfuse.
"""

import logging
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Protocol

from langchain_core.runnables import RunnableConfig

from mirante.config import get_settings

if TYPE_CHECKING:
    from mirante.api.schemas import ModernizeRequest, ModernizeResponse

logger = logging.getLogger(__name__)


class RunTrace(Protocol):
    """Propriedades somente-leitura: `str` (Langfuse) e `None` (Noop) cumprem o contrato."""

    @property
    def trace_id(self) -> str | None: ...

    @property
    def config(self) -> RunnableConfig: ...  # callbacks, run_name e metadata do LangGraph

    def finish(self, response: ModernizeResponse) -> None: ...


class Tracer(Protocol):
    def start(self, request: ModernizeRequest, session_id: str | None = None) -> RunTrace: ...

    def record_aggregate(self, session_id: str, scores: dict[str, float]) -> None:
        """Scores de uma SESSAO (ex.: uma avaliacao inteira), nao de um trace."""


class NoopRunTrace:
    trace_id: str | None = None
    config: RunnableConfig = {}  # noqa: RUF012 - nunca e mutado

    def finish(self, response: ModernizeResponse) -> None:
        return None


class NoopTracer:
    def start(self, request: ModernizeRequest, session_id: str | None = None) -> RunTrace:
        return NoopRunTrace()

    def record_aggregate(self, session_id: str, scores: dict[str, float]) -> None:
        return None


def scores_for(response: ModernizeResponse) -> list[dict[str, Any]]:
    """Metricas de qualidade de UMA execucao, anexadas ao trace como scores do Langfuse."""
    report = response.report
    risks = report.get("analysis", {}).get("risks_by_severity", {})
    passed = report.get("validation", {}).get("status") == "passed"
    return [
        {"name": "validation_passed", "value": 1.0 if passed else 0.0, "data_type": "BOOLEAN"},
        {
            "name": "generation_attempts",
            "value": float(report.get("meta", {}).get("attempts", 0)),
            "data_type": "NUMERIC",
        },
        {
            "name": "high_risks_detected",
            "value": float(risks.get("high", 0)),
            "data_type": "NUMERIC",
        },
        {"name": "pipeline_status", "value": response.status.value, "data_type": "CATEGORICAL"},
    ]


class LangfuseRunTrace:
    def __init__(self, client: Any, trace_id: str, config: RunnableConfig) -> None:
        self._client = client
        self.trace_id = trace_id
        self.config = config

    def finish(self, response: ModernizeResponse) -> None:
        try:
            for score in scores_for(response):
                self._client.create_score(trace_id=self.trace_id, **score)
        except Exception:  # noqa: BLE001 - observabilidade nunca derruba o pipeline
            logger.exception("Falha ao enviar scores ao Langfuse (trace %s)", self.trace_id)


class LangfuseTracer:
    def __init__(self, client: Any, public_key: str) -> None:
        self._client = client
        self._public_key = public_key

    def start(self, request: ModernizeRequest, session_id: str | None = None) -> RunTrace:
        from langfuse import Langfuse
        from langfuse.langchain import CallbackHandler

        try:
            trace_id = Langfuse.create_trace_id()
            handler = CallbackHandler(
                public_key=self._public_key, trace_context={"trace_id": trace_id}
            )
        except Exception:  # noqa: BLE001
            logger.exception("Langfuse indisponivel: executando sem tracing")
            return NoopRunTrace()
        metadata: dict[str, Any] = {
            "langfuse_tags": ["sql-modernizer", f"dialect:{request.dialect}"]
        }
        if session_id:
            metadata["langfuse_session_id"] = session_id  # agrupa os traces de uma avaliacao
        config = RunnableConfig(run_name="modernize", callbacks=[handler], metadata=metadata)
        return LangfuseRunTrace(self._client, trace_id, config)

    def record_aggregate(self, session_id: str, scores: dict[str, float]) -> None:
        try:
            for name, value in scores.items():
                self._client.create_score(
                    session_id=session_id, name=name, value=float(value), data_type="NUMERIC"
                )
        except Exception:  # noqa: BLE001 - observabilidade nunca derruba o pipeline
            logger.exception("Falha ao enviar scores agregados (sessao %s)", session_id)


@lru_cache
def get_langfuse_client() -> Any | None:
    s = get_settings()
    if not (s.langfuse_enabled and s.langfuse_public_key and s.langfuse_secret_key):
        return None
    from langfuse import Langfuse

    return Langfuse(
        public_key=s.langfuse_public_key, secret_key=s.langfuse_secret_key, host=s.langfuse_host
    )


def get_tracer() -> Tracer:
    client = get_langfuse_client()
    if client is None:
        return NoopTracer()
    return LangfuseTracer(client, get_settings().langfuse_public_key)


def flush() -> None:
    """Envia o que estiver pendente (o SDK exporta em lote, em segundo plano)."""
    client = get_langfuse_client()
    if client is not None:
        try:
            client.flush()
        except Exception:  # noqa: BLE001
            logger.exception("Falha ao dar flush no Langfuse")
