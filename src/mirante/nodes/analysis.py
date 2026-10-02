"""No 2 — Analise semantica: IR -> construcoes + riscos de traducao. Deterministico, sem LLM."""

import time
from typing import Any

from mirante.analysis.engine import analyze
from mirante.domain.state import PipelineState


def analysis_node(state: PipelineState) -> dict[str, Any]:
    started = time.perf_counter()
    analysis = analyze(state["parsed"], state.get("parsed_dependencies"), state.get("schema"))
    return {
        "analysis": analysis,
        "report": {
            "analysis": {
                "status": "ok",
                "risks_by_severity": analysis.by_severity(),
                "risks": [r.model_dump() for r in analysis.risks],
                "constructs": analysis.constructs.model_dump(),
                "skipped_rules": analysis.skipped_rules,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        },
    }
