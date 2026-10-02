"""Executa o pipeline sobre os casos de avaliacao e calcula as metricas.

Sequencial de proposito: respeita o limite de requisicoes por minuto do provedor de LLM e mantem
a ordem (F depende de B). Cada caso passa pelo `ModernizationService`, entao gera historico e trace
reais; os traces da avaliacao compartilham uma sessao do Langfuse, que recebe os scores agregados.
"""

import logging
import uuid

from sqlalchemy.exc import SQLAlchemyError

from mirante.api.schemas import ModernizeRequest
from mirante.config import get_settings
from mirante.domain.evaluation import EvaluationRun
from mirante.evals.cases import CASES, SAMPLES_DIR, EvalCase
from mirante.evals.metrics import case_result, compute_metrics
from mirante.observability import Tracer
from mirante.persistence.evaluations import EvaluationRepository
from mirante.service import ModernizationService

logger = logging.getLogger(__name__)


def select_cases(keys: list[str] | None) -> list[EvalCase]:
    chosen = keys or list(CASES)
    unknown = [k for k in chosen if k not in CASES]
    if unknown:
        raise KeyError(f"Casos desconhecidos: {unknown}. Disponiveis: {list(CASES)}")
    return [CASES[k] for k in chosen]


async def run_evaluation(
    service: ModernizationService,
    repository: EvaluationRepository,
    tracer: Tracer,
    cases: list[EvalCase],
    *,
    max_attempts: int | None = None,
) -> EvaluationRun:
    schema_sql = (SAMPLES_DIR / "schema.sql").read_text()
    session_id = f"eval-{uuid.uuid4().hex[:8]}"
    results = []
    for case in cases:
        request = ModernizeRequest(
            source_code=case.source,
            schema_sql=schema_sql,
            dependencies=case.dependency_sources,
            max_attempts=max_attempts,
        )
        response = await service.run(request, session_id=session_id)
        results.append(
            case_result(
                case.key,
                case.expected_risks,
                response.status,
                response.report,
                procedure=response.procedure_name,
                history_id=response.id,
                errors=[e.message for e in response.errors],
            )
        )
    metrics = compute_metrics(results)
    used = sorted({m for r in results for m in r.models})  # o modelo REAL, nao o configurado
    model = ", ".join(used) or get_settings().resolved_model
    run = EvaluationRun(model=model, metrics=metrics, results=results)
    try:
        repository.save(run)
    except SQLAlchemyError:
        logger.exception("Falha ao gravar a avaliacao %s (resultado ainda retornado)", run.id)
    tracer.record_aggregate(
        session_id,
        {
            "eval_valid_rate": metrics.valid_rate,
            "eval_first_attempt_rate": metrics.first_attempt_rate,
            "eval_syntax_rate": metrics.syntax_rate,
            "eval_risk_recall": metrics.risk_recall,
            "eval_avg_attempts": metrics.avg_attempts,
        },
    )
    return run
