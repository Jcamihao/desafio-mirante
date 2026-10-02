"""No 4 — Validacao: camadas estaticas sobre o Python gerado. Nao executa o codigo gerado."""

import sys
import time
from typing import Any

from mirante.domain.state import PipelineState
from mirante.validation.engine import validate
from mirante.validation.layers import Context


def validation_node(state: PipelineState) -> dict[str, Any]:
    started = time.perf_counter()
    ctx = Context(state["parsed"], state.get("parsed_dependencies", []))
    result, code = validate(state["generated_code"], ctx)

    log = list(state.get("validation_log", []))
    log.append(
        {
            "attempt": len(log) + 1,
            "passed": result.passed,
            "errors": len(result.feedback),
            "failed_layers": [layer.name for layer in result.layers if layer.status == "failed"],
        }
    )
    return {
        "validation": result,
        "generated_code": code,  # com o autofix aplicado
        "validation_feedback": [] if result.passed else result.feedback,
        "validation_log": log,
        "report": {
            "validation": {
                "status": "passed" if result.passed else "failed",
                "python": sys.version.split()[0],
                "layers": [layer.model_dump() for layer in result.layers],
                "autofixed": result.autofixed,
                "history": log,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            }
        },
    }
