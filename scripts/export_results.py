"""Exporta para `results/` o codigo gerado e o relatorio de cada caso de uma avaliacao.

    uv run python scripts/export_results.py                    # ultima avaliacao
    uv run python scripts/export_results.py <run-id> [<run-id>...]   # combina avaliacoes

Com varios ids, o ULTIMO resultado de cada caso vence (util quando o provedor de LLM falhou em parte
dos casos numa rodada). A origem de cada caso fica registrada em SUMMARY.md.

Gera, por anexo: `<anexo>_<procedure>.py` (modulo Python gerado) e `<anexo>_<procedure>.report.json`
(o mesmo relatorio gravado em modernization_history), alem de `evaluation.json` e `SUMMARY.md`.
"""

import json
import sys
import uuid
from pathlib import Path

from mirante.domain.evaluation import EvaluationRun
from mirante.evals.__main__ import render
from mirante.evals.cases import CASES
from mirante.evals.metrics import compute_metrics
from mirante.persistence.db import get_engine
from mirante.persistence.evaluations import EvaluationRepository
from mirante.persistence.repository import HistoryRepository

RESULTS = Path(__file__).resolve().parent.parent / "results"


def merge(runs: list[EvaluationRun]) -> tuple[EvaluationRun, dict[str, str]]:
    """Combina avaliacoes por caso (o ultimo vence); devolve tambem a origem de cada caso."""
    chosen, origin = {}, {}
    for run in runs:
        for result in run.results:
            chosen[result.case], origin[result.case] = result, str(run.id)[:8]
    results = [chosen[k] for k in sorted(chosen)]
    models = ", ".join(sorted({m for r in results for m in r.models}))
    merged = EvaluationRun(
        id=runs[-1].id, model=models, metrics=compute_metrics(results), results=results
    )
    return merged, origin


def export(
    run: EvaluationRun, history: HistoryRepository, origin: dict[str, str] | None = None
) -> list[str]:
    RESULTS.mkdir(exist_ok=True)
    written = []
    for result in run.results:
        record = history.get(result.history_id) if result.history_id else None
        if record is None:
            continue
        stem = f"{result.case}_{result.procedure or 'sem_procedure'}"
        if record.generated_code:
            (RESULTS / f"{stem}.py").write_text(record.generated_code)
            written.append(f"{stem}.py")
        report = {"status": record.status.value, "duration_ms": record.duration_ms, **record.report}
        (RESULTS / f"{stem}.report.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str) + "\n"
        )
        written.append(f"{stem}.report.json")
    (RESULTS / "evaluation.json").write_text(run.model_dump_json(indent=2) + "\n")
    sources = ""
    if origin and len(set(origin.values())) > 1:
        lines = [f"- caso {case}: avaliacao `{run_id}`" for case, run_id in sorted(origin.items())]
        sources = (
            "\nResultado composto por mais de uma avaliacao (o provedor de LLM falhou em parte):\n"
            + "\n".join(lines)
            + "\n"
        )
    (RESULTS / "SUMMARY.md").write_text(f"```\n{render(run)}\n```\n{sources}")
    return [*written, "evaluation.json", "SUMMARY.md"]


def main() -> None:
    engine = get_engine()
    evaluations = EvaluationRepository(engine)
    if sys.argv[1:]:
        runs = [evaluations.get(uuid.UUID(arg)) for arg in sys.argv[1:]]
        if None in runs:
            sys.exit("Algum run-id nao foi encontrado em evaluation_runs.")
    else:
        runs = list(evaluations.list(1))
    if not runs:
        sys.exit("Nenhuma avaliacao encontrada: rode `uv run python -m mirante.evals` antes.")
    run, origin = merge([r for r in runs if r is not None])
    missing = [k for k in CASES if k not in {r.case for r in run.results}]
    files = export(run, HistoryRepository(engine), origin)
    print(f"{len(files)} arquivos em results/" + (f" (sem: {missing})" if missing else ""))


if __name__ == "__main__":
    main()
