"""Casos de avaliacao: Anexos B-F + os riscos que a analise DEVE detectar em cada um.

Os riscos esperados vem da leitura do enunciado (ver docs/decisions.md): sao a "resposta certa" da
etapa de analise, entao medem-na de forma deterministica e sem gastar LLM.
"""

from dataclasses import dataclass, field
from pathlib import Path

SAMPLES_DIR = Path(__file__).resolve().parents[3] / "samples"


@dataclass(frozen=True)
class EvalCase:
    key: str  # "B".."F"
    expected_risks: frozenset[str] = frozenset()
    depends_on: tuple[str, ...] = ()
    notes: str = field(default="", compare=False)

    def read(self, annex: str | None = None) -> str:
        return next(SAMPLES_DIR.glob(f"{annex or self.key}_*.sql")).read_text()

    @property
    def source(self) -> str:
        return self.read()

    @property
    def dependency_sources(self) -> list[str]:
        return [self.read(d) for d in self.depends_on]


CASES: dict[str, EvalCase] = {
    c.key: c
    for c in (
        EvalCase("B", notes="funcao escalar: caso de aquecimento, sem riscos"),
        EvalCase("C", frozenset({"OUT_PARAMS", "GET_DIAGNOSTICS", "NON_DETERMINISTIC_TIME"})),
        EvalCase(
            "D",
            frozenset({"HANDLER_WRITES_ROLLED_BACK", "NULL_COMPARISON", "ROW_LOCK"}),
            notes="achados 2 e 3",
        ),
        EvalCase(
            "E",
            frozenset({"CURSOR_N_PLUS_1", "NUMERIC_ROUNDING", "CHECK_CONSTRAINT"}),
            notes="achados 4 e 5",
        ),
        EvalCase(
            "F",
            frozenset({"RAISE_CAUGHT_BY_OWN_HANDLER", "NESTED_CALL", "RECURSIVE_CTE"}),
            depends_on=("B",),
            notes="achado 1",
        ),
    )
}
