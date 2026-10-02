"""Harness de equivalencia comportamental contra PostgreSQL real (banco temporario)."""

from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from mirante.behavior.harness import Harness, discover_modules
from mirante.behavior.scenarios import SCENARIOS
from mirante.behavior.worker import canon, canonical_rows
from mirante.config import get_settings
from tests.fakes import GOOD_B

# Traducao de referencia do Anexo C, escrita a mao: o que o pipeline deveria produzir.
REFERENCE_C = """\
from dataclasses import dataclass

from sqlalchemy import Connection, text


class ProcedureError(Exception):
    pass


@dataclass(frozen=True)
class AtualizarStatusResult:
    p_afetadas: int


def sp_atualizar_status_contas_inativas(conn: Connection, p_dias: int | None):
    if p_dias is None or p_dias <= 0:
        shown = "<NULL>" if p_dias is None else str(p_dias)
        raise ProcedureError(f"Parametro p_dias deve ser positivo, recebido: {shown}")
    updated = conn.execute(
        text(
            "UPDATE contas c SET status = 'INATIVA' WHERE c.status = 'ATIVA' AND NOT EXISTS ("
            "SELECT 1 FROM transacoes t "
            "WHERE (t.conta_origem_id = c.id OR t.conta_destino_id = c.id) "
            "AND t.data_transacao >= NOW() - (CAST(:d AS text) || ' days')::INTERVAL)"
        ),
        {"d": p_dias},
    )
    affected = updated.rowcount
    conn.execute(
        text(
            "INSERT INTO log_auditoria (entidade, acao, detalhes) "
            "VALUES ('contas', 'INATIVACAO_LOTE', "
            "jsonb_build_object('dias', CAST(:d AS integer), 'afetadas', CAST(:a AS integer)))"
        ),
        {"d": p_dias, "a": affected},
    )
    return AtualizarStatusResult(p_afetadas=affected)
"""


@pytest.fixture(scope="module")
def harness_for(pg_engine, tmp_path_factory):  # pg_engine: so garante PG acessivel (ou pula)
    def build(**modules: str) -> Harness:
        folder = tmp_path_factory.mktemp("modules")
        for procedure, code in modules.items():
            annex = next(s.annex for s in SCENARIOS if s.procedure == procedure)
            (folder / f"{annex}_{procedure}.py").write_text(code)
        return Harness(make_url(get_settings().database_url), discover_modules(folder))

    return build


def run(harness, annex):
    return harness.run([s for s in SCENARIOS if s.annex == annex])


class TestCanonicalizacao:
    def test_numeros_equivalentes_comparam_iguais(self):
        from decimal import Decimal

        assert canon(Decimal("10.00")) == canon(10) == canon(Decimal("10")) == "10"
        assert canon(Decimal("0.00")) == canon(Decimal("-0")) == "0"

    def test_formas_de_retorno_convergem(self):
        from dataclasses import dataclass

        @dataclass
        class Row:
            n: int

        assert canonical_rows(Row(3)) == canonical_rows((3,)) == canonical_rows(3) == [["3"]]
        assert canonical_rows(None) == canonical_rows([]) == []


@pytest.mark.integration
class TestHarness:
    def test_traducao_correta_e_equivalente_em_todos_os_cenarios(self, harness_for):
        outcomes = run(harness_for(sp_atualizar_status_contas_inativas=REFERENCE_C), "C")
        assert len(outcomes) == 4
        assert all(o.passed for o in outcomes), [
            (o.scenario, o.diffs) for o in outcomes if not o.passed
        ]

    def test_detecta_none_no_lugar_de_NULL_na_mensagem(self, harness_for):
        broken = REFERENCE_C.replace('"<NULL>" if p_dias is None else str(p_dias)', "str(p_dias)")
        by_name = {
            o.scenario: o for o in run(harness_for(sp_atualizar_status_contas_inativas=broken), "C")
        }
        null_case = by_name["p_dias NULL: mensagem mostra <NULL>"]
        assert (
            not null_case.passed and "<NULL>" in null_case.diffs[0] and "None" in null_case.diffs[0]
        )
        assert by_name["p_dias zero levanta erro"].passed  # os outros cenarios seguem iguais

    def test_detecta_divergencia_no_estado_das_tabelas(self, harness_for):
        """Sem o INSERT de auditoria o retorno e igual, mas o estado final nao."""
        start = REFERENCE_C.index('    conn.execute(\n        text(\n            "INSERT INTO log')
        end = REFERENCE_C.index("    return AtualizarStatusResult")
        broken = REFERENCE_C[:start] + REFERENCE_C[end:]
        outcome = run(harness_for(sp_atualizar_status_contas_inativas=broken), "C")[0]
        assert not outcome.passed and any("estado de log_auditoria" in d for d in outcome.diffs)

    def test_detecta_retorno_diferente(self, harness_for):
        wrong = GOOD_B.replace("COALESCE(SUM(saldo), 0)", "COALESCE(SUM(saldo), 0) + 1")
        by_name = {o.scenario: o for o in run(harness_for(fn_saldo_cliente=wrong), "B")}
        assert all(not o.passed and o.diffs[0].startswith("retorno") for o in by_name.values())

    def test_modulo_que_nao_importa_vira_falha_e_nao_derruba_o_harness(self, harness_for):
        outcomes = run(harness_for(fn_saldo_cliente="def nope(:\n"), "B")
        assert all(not o.passed and "WorkerCrash" in o.diffs[0] for o in outcomes)

    def test_so_testa_procedures_com_codigo_gerado(self, harness_for):
        assert {o.annex for o in harness_for(fn_saldo_cliente=GOOD_B).run()} == {"B"}

    def test_codigo_gerado_nao_enxerga_as_procedures_originais(self, harness_for):
        """O lado gerado so tem as tabelas: chamar a procedure original falha (sem trapaca)."""
        cheat = GOOD_B.replace(
            'text("SELECT COALESCE(SUM(saldo), 0) FROM contas WHERE cliente_id = :c")',
            'text("SELECT fn_saldo_cliente(:c)")',
        )
        assert not any(o.passed for o in run(harness_for(fn_saldo_cliente=cheat), "B"))


def test_cenarios_cobrem_os_cinco_anexos_e_as_armadilhas():
    assert {s.annex for s in SCENARIOS} == set("BCDEF")
    targets = " ".join(s.targets for s in SCENARIOS)
    for achado in ("achado 1", "achado 2", "achado 3", "achado 4", "achado 5"):
        assert achado in targets
    assert Path(__file__).exists()
