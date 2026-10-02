"""CLI: `uv run python -m mirante.evals [B C D ...] [--max-attempts N]` — roda e imprime o funil."""

import argparse
import asyncio

from mirante.api.deps import get_service
from mirante.evals.runner import run_evaluation, select_cases
from mirante.observability import flush, get_tracer
from mirante.persistence.evaluations import default_evaluation_repository


def _mark(ok: bool) -> str:
    return "ok" if ok else "--"


def render(run) -> str:
    m = run.metrics
    header = "caso  status   tent  1a-vez  sintaxe  lint  imports  politica  riscos"
    lines = [f"Avaliacao {str(run.id)[:8]} | modelo {run.model} | {m.cases} casos", "", header]
    for r in run.results:
        cells = [_mark(r.layers.get(k, False)) for k in ("syntax", "lint", "imports", "policy")]
        found = len(r.expected_risks) - len(r.missed_risks)
        lines.append(
            f"{r.case:5} {r.status.value:8} {r.attempts:4}  {_mark(r.first_attempt_valid):6}  "
            f"{cells[0]:7}  {cells[1]:4}  {cells[2]:7}  {cells[3]:8}  "
            f"{found}/{len(r.expected_risks)}"
        )
    lines += [
        "",
        f"sintaxe (ast.parse) : {m.syntax_rate:6.0%}   <- metrica ingenua",
        f"lint                : {m.lint_rate:6.0%}",
        f"imports             : {m.imports_rate:6.0%}",
        f"politica            : {m.policy_rate:6.0%}",
        f"VALIDO ponta-a-ponta: {m.valid_rate:6.0%}   <- metrica principal",
        f"  disponibilidade   : {m.generation_rate:6.0%}   (LLM devolveu codigo)",
        f"  qualidade         : {m.quality_rate:6.0%}   (validos entre os gerados)",
        f"1a tentativa        : {m.first_attempt_rate:6.0%}   (reparo: {m.repair_gain:+.0%})",
        f"superestimativa do ast.parse: {m.naive_gap:+.0%}",
        f"recall de riscos    : {m.risk_recall:6.0%}",
        f"tokens {m.input_tokens}/{m.output_tokens} | {m.total_duration_ms / 1000:.0f}s",
    ]
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("cases", nargs="*")
    parser.add_argument("--max-attempts", type=int)
    args = parser.parse_args()
    run = asyncio.run(
        run_evaluation(
            get_service(),
            default_evaluation_repository(),
            get_tracer(),
            select_cases(args.cases),
            max_attempts=args.max_attempts,
        )
    )
    flush()
    print(render(run))


if __name__ == "__main__":
    main()
