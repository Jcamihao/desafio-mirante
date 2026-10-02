"""CLI: `uv run python -m mirante.behavior [B C ...] [--results results]`.

Compara o Python gerado (results/*.py) com a procedure original num Postgres temporario.
Requer um PostgreSQL acessivel por DATABASE_URL (veja scripts/dev_postgres.py ou o compose).
"""

import argparse
import json
from dataclasses import asdict
from pathlib import Path

from sqlalchemy.engine import make_url

from mirante.behavior.harness import Harness, Outcome, discover_modules
from mirante.behavior.scenarios import SCENARIOS
from mirante.config import get_settings


def render(outcomes: list[Outcome]) -> str:
    lines = []
    for annex in sorted({o.annex for o in outcomes}):
        group = [o for o in outcomes if o.annex == annex]
        lines.append(
            f"\n[{annex}] {sum(o.passed for o in group)}/{len(group)} cenarios equivalentes"
        )
        for o in group:
            lines.append(f"  {'OK ' if o.passed else 'DIF'} {o.scenario}")
            lines += [f"        -> {d}" for d in o.diffs]
    total = len(outcomes)
    ok = sum(o.passed for o in outcomes)
    lines.append(
        f"\nEQUIVALENCIA COMPORTAMENTAL: {ok}/{total} = {ok / total:.0%}"
        if total
        else "sem cenarios"
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "annexes", nargs="*", help="anexos a testar (padrao: todos com codigo gerado)"
    )
    parser.add_argument("--results", default="results", type=Path)
    parser.add_argument("--json", type=Path, help="grava o relatorio completo neste arquivo")
    args = parser.parse_args()

    modules = discover_modules(args.results)
    wanted = [s for s in SCENARIOS if not args.annexes or s.annex in args.annexes]
    outcomes = Harness(make_url(get_settings().database_url), modules).run(wanted)
    print(render(outcomes))
    if args.json:
        args.json.write_text(
            json.dumps([asdict(o) for o in outcomes], indent=2, ensure_ascii=False, default=str)
            + "\n"
        )


if __name__ == "__main__":
    main()
