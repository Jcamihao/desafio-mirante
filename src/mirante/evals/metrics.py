"""Metricas de avaliacao: funcoes puras sobre resultados de casos (testaveis sem LLM nem banco)."""

import uuid
from typing import Any

from mirante.domain.evaluation import LAYERS, CaseResult, Metrics
from mirante.domain.status import Status


def _rate(hits: int, total: int) -> float:
    return round(hits / total, 4) if total else 0.0


def case_result(
    key: str,
    expected: frozenset[str],
    status: Status,
    report: dict[str, Any],
    *,
    procedure: str | None = None,
    history_id: uuid.UUID | None = None,
    errors: list[str] | None = None,
) -> CaseResult:
    """Extrai de um relatorio do pipeline (o mesmo gravado em modernization_history)."""
    validation = report.get("validation", {})
    history = validation.get("history", [])
    layer_status = {x["name"]: x["status"] for x in validation.get("layers", [])}
    attempts = report.get("generation", {}).get("attempts", [])
    detected = sorted({r["code"] for r in report.get("analysis", {}).get("risks", [])})
    stage_ms = sum(
        report.get(s, {}).get("duration_ms", 0) for s in ("parsing", "analysis", "validation")
    )
    return CaseResult(
        case=key,
        procedure=procedure,
        status=status,
        attempts=sum(a.get("status") == "ok" for a in attempts),
        models=sorted({a["model"] for a in attempts if a.get("status") == "ok" and a.get("model")}),
        first_attempt_valid=bool(history and history[0]["passed"]),
        final_valid=validation.get("status") == "passed",
        layers={name: layer_status.get(name) == "passed" for name in LAYERS},
        expected_risks=sorted(expected),
        detected_risks=detected,
        missed_risks=sorted(expected - set(detected)),
        input_tokens=sum(a.get("input_tokens", 0) for a in attempts),
        output_tokens=sum(a.get("output_tokens", 0) for a in attempts),
        duration_ms=round(sum(a.get("duration_ms", 0) for a in attempts) + stage_ms),
        history_id=history_id,
        trace_id=report.get("meta", {}).get("langfuse_trace_id"),
        errors=errors or [],
    )


def compute_metrics(results: list[CaseResult]) -> Metrics:
    n = len(results)
    by_layer = {
        layer: _rate(sum(r.layers.get(layer, False) for r in results), n) for layer in LAYERS
    }
    valid = _rate(sum(r.final_valid for r in results), n)
    generated = [r for r in results if r.attempts > 0]
    first = _rate(sum(r.first_attempt_valid for r in results), n)
    expected = sum(len(r.expected_risks) for r in results)
    found = sum(len(r.expected_risks) - len(r.missed_risks) for r in results)
    return Metrics(
        cases=n,
        syntax_rate=by_layer["syntax"],
        lint_rate=by_layer["lint"],
        imports_rate=by_layer["imports"],
        policy_rate=by_layer["policy"],
        valid_rate=valid,
        generation_rate=_rate(len(generated), n),
        quality_rate=_rate(sum(r.final_valid for r in generated), len(generated)),
        first_attempt_rate=first,
        repair_gain=round(valid - first, 4),
        naive_gap=round(by_layer["syntax"] - valid, 4),
        risk_recall=_rate(found, expected) if expected else 1.0,
        avg_attempts=round(sum(r.attempts for r in results) / n, 2) if n else 0.0,
        input_tokens=sum(r.input_tokens for r in results),
        output_tokens=sum(r.output_tokens for r in results),
        total_duration_ms=sum(r.duration_ms for r in results),
    )
