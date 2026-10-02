"""No 3 — Geracao: IR + riscos + schema -> modulo Python (via LLM atras de `CodeGenerator`)."""

import time
from collections.abc import Callable
from typing import Any

from mirante.domain.state import NodeFunction, PipelineState, StageError
from mirante.llm.generator import CodeGenerator, default_generator
from mirante.llm.prompts import SYSTEM_PROMPT, build_user_prompt


def build_generation_node(
    generator_factory: Callable[[], CodeGenerator] = default_generator,
) -> NodeFunction:
    """O gerador e criado sob demanda: importar o grafo nao exige chave de API."""

    def generation_node(state: PipelineState) -> dict[str, Any]:
        log = list(state.get("generation_log", []))
        attempt = len(log) + 1
        prompt = build_user_prompt(
            state["parsed"],
            state["analysis"],
            state.get("schema", []),
            state.get("parsed_dependencies", []),
            state.get("validation_feedback"),
            state.get("generated_code") if state.get("validation_feedback") else None,
        )
        started = time.perf_counter()
        try:
            module, usage = generator_factory().generate(SYSTEM_PROMPT, prompt)
        except Exception as exc:  # fronteira externa: rede, quota, schema invalido
            entry = {
                "attempt": attempt,
                "status": "failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
            return {
                "generation_log": [*log, entry],
                "errors": [StageError(stage="generation", message=entry["error"])],
                "report": {"generation": {"status": "failed", "attempts": [*log, entry]}},
            }

        entry = {
            "attempt": attempt,
            "status": "ok",
            "model": usage.model,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
            "prompt_chars": len(prompt),
        }
        return {
            "generated": module,
            "generated_code": module.code,
            "generation_log": [*log, entry],
            "report": {
                "generation": {
                    "status": "ok",
                    "attempts": [*log, entry],
                    "decisions": [d.model_dump() for d in module.decisions],
                    "assumptions": module.assumptions,
                }
            },
        }

    return generation_node
