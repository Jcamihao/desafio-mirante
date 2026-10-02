"""Integracao com PostgreSQL real (banco temporario criado pelo fixture `pg_engine`)."""

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from mirante.domain.evaluation import CaseResult, EvaluationRun
from mirante.domain.history import HistoryRecord
from mirante.domain.status import Status
from mirante.evals.metrics import compute_metrics
from mirante.graph.builder import build_graph
from mirante.persistence.evaluations import EvaluationRepository
from mirante.persistence.migrate import MIGRATIONS_DIR, apply_migrations
from mirante.persistence.repository import history
from tests.fakes import BAD_IMPORT_B, GOOD_B, StubGenerator

pytestmark = pytest.mark.integration
ALL_MIGRATIONS = sorted(p.name for p in MIGRATIONS_DIR.glob("*.sql"))  # nao fixa a quantidade


def stamp(engine, table: str, record_id, seconds_ago: int) -> None:
    """Fixa created_at: o teste verifica a ORDENACAO, nao o relogio (o do WSL chega a voltar 7s)."""
    sql = text(f"UPDATE {table} SET created_at = now() - make_interval(secs => :s) WHERE id = :id")
    with engine.begin() as conn:
        conn.execute(sql, {"s": seconds_ago, "id": record_id})


def record(**kw) -> HistoryRecord:
    base = {"source_code": "CREATE FUNCTION f()", "status": Status.SUCCESS, "report": {}}
    return HistoryRecord(**{**base, **kw})


class TestMigracoes:
    def columns(self, engine) -> dict[str, tuple[str, str]]:
        sql = text(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'modernization_history'"
        )
        with engine.connect() as conn:
            return {r.column_name: (r.data_type, r.is_nullable) for r in conn.execute(sql)}

    def test_tabela_tem_os_campos_exigidos_pelo_desafio(self, repo, pg_engine):
        cols = self.columns(pg_engine)
        assert cols["id"][0] == "uuid" and cols["report"] == ("jsonb", "NO")
        assert cols["source_code"] == ("text", "NO") and cols["generated_code"][1] == "YES"
        assert cols["status"][0] == "USER-DEFINED" and cols["created_at"][0].startswith(
            "timestamp with"
        )

    def test_tabela_do_sqlalchemy_espelha_o_sql(self, repo, pg_engine):
        assert set(self.columns(pg_engine)) == {c.name for c in history.c}

    def test_reaplicar_nao_faz_nada(self, repo, pg_engine):
        assert apply_migrations(pg_engine) == []

    def test_sql_e_idempotente_mesmo_sem_o_rastro(self, repo, pg_engine):
        """Banco ja inicializado pelo docker-entrypoint-initdb.d (sem schema_migrations)."""
        with pg_engine.begin() as conn:
            conn.execute(text("DROP TABLE schema_migrations"))
        assert apply_migrations(pg_engine) == ALL_MIGRATIONS  # reaplica TODAS sem falhar

    def test_instancias_subindo_juntas_nao_colidem(self, pg_engine):
        with pg_engine.begin() as conn:
            conn.execute(
                text(
                    "DROP TABLE IF EXISTS modernization_history, evaluation_runs, schema_migrations"
                )
            )
            conn.execute(text("DROP TYPE IF EXISTS modernization_status"))
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: apply_migrations(pg_engine), range(4)))
        counts = sorted(len(r) for r in results)
        assert counts == [0, 0, 0, len(ALL_MIGRATIONS)]  # so uma instancia aplica, e aplica tudo


class TestRepositorio:
    def test_ida_e_volta_preserva_jsonb_e_unicode(self, repo):
        rec = record(
            generated_code="x = 'ação'",
            report={"parsing": {"tables": ["contas"], "n": 3}, "nota": "validação ✓"},
            procedure_name="fn_saldo_cliente",
            duration_ms=1234,
        )
        repo.save(rec)
        got = repo.get(rec.id)
        assert got.report == rec.report and got.generated_code == "x = 'ação'"
        assert got.status == Status.SUCCESS and got.duration_ms == 1234
        assert got.created_at is not None  # preenchido pelo banco

    def test_get_inexistente(self, repo):
        assert repo.get(uuid.uuid4()) is None

    def test_list_ordena_do_mais_recente_filtra_e_limita(self, repo, pg_engine):
        ids = []
        for age, status in ((30, Status.SUCCESS), (20, Status.FAILURE), (10, Status.FAILURE)):
            rec = record(status=status)
            repo.save(rec)
            stamp(pg_engine, "modernization_history", rec.id, age)
            ids.append(rec.id)
        assert [r.id for r in repo.list()] == ids[::-1]  # mais recente primeiro
        assert {r.status for r in repo.list(status=Status.FAILURE)} == {Status.FAILURE}
        assert len(repo.list(status=Status.FAILURE)) == 2 and len(repo.list(limit=1)) == 1

    def test_banco_rejeita_status_invalido_e_entrada_nula(self, repo, pg_engine):
        with pytest.raises(DBAPIError), pg_engine.begin() as conn:
            conn.execute(
                text("INSERT INTO modernization_history (source_code, status) VALUES ('x', 'XYZ')")
            )
        with pytest.raises(IntegrityError), pg_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO modernization_history (source_code, status) "
                    "VALUES (NULL, 'SUCCESS')"
                )
            )

    def test_relatorio_e_consultavel_por_jsonb(self, repo, pg_engine):
        repo.save(record(report={"validation": {"status": "passed"}}))
        repo.save(record(report={"validation": {"status": "failed"}}))
        sql = text(
            "SELECT count(*) FROM modernization_history "
            """WHERE report @> '{"validation": {"status": "passed"}}'"""
        )
        with pg_engine.connect() as conn:
            assert conn.execute(sql).scalar_one() == 1


class TestGrafoGravandoDeVerdade:
    def run(self, repo, source, stub):
        return build_graph(lambda: stub, lambda: repo).invoke({"source_code": source})

    def test_tres_desfechos_viram_tres_linhas_consultaveis(self, repo, sample):
        self.run(repo, sample("B"), StubGenerator(GOOD_B))
        self.run(repo, sample("B"), StubGenerator(BAD_IMPORT_B))
        self.run(repo, "CREATE FUNCTION f( BEGIN", StubGenerator())
        by_status = {s: repo.list(status=s) for s in Status}
        assert {s: len(v) for s, v in by_status.items()} == {
            "SUCCESS": 1,
            "PARTIAL": 1,
            "FAILURE": 1,
        }
        (ok,) = by_status[Status.SUCCESS]
        assert ok.generated_code == GOOD_B and ok.report["analysis"]["status"] == "ok"
        assert ok.report["meta"]["history_id"] == str(ok.id) and ok.duration_ms is not None
        (failed,) = by_status[Status.FAILURE]
        assert failed.generated_code is None and failed.report["errors"][0]["stage"] == "parsing"


class TestRepositorioDeAvaliacoes:
    def run(self, **kw) -> EvaluationRun:
        layers = dict.fromkeys(("syntax", "lint", "imports", "policy"), True)
        results = [
            CaseResult(case="B", status=Status.SUCCESS, final_valid=True, layers=layers, **kw)
        ]
        return EvaluationRun(
            model="gemini-2.5-flash", metrics=compute_metrics(results), results=results
        )

    def test_ida_e_volta_preserva_metricas_e_resultados(self, repo, pg_engine):
        evals = EvaluationRepository(pg_engine)
        run = self.run(procedure="fn_saldo_cliente", missed_risks=["X"])
        evals.save(run)
        got = evals.get(run.id)
        assert got.metrics == run.metrics and got.results == run.results
        assert got.model == "gemini-2.5-flash" and got.created_at is not None

    def test_list_do_mais_recente_e_inexistente(self, repo, pg_engine):
        evals = EvaluationRepository(pg_engine)
        with pg_engine.begin() as conn:
            conn.execute(text("TRUNCATE evaluation_runs"))
        first, second = self.run(), self.run()
        for age, run in ((20, first), (10, second)):
            evals.save(run)
            stamp(pg_engine, "evaluation_runs", run.id, age)
        assert [r.id for r in evals.list()] == [second.id, first.id]
        assert evals.get(uuid.uuid4()) is None

    def test_metricas_sao_consultaveis_no_jsonb(self, repo, pg_engine):
        evals = EvaluationRepository(pg_engine)
        evals.save(self.run())
        sql = text("SELECT (metrics->>'valid_rate')::float FROM evaluation_runs LIMIT 1")
        with pg_engine.connect() as conn:
            assert conn.execute(sql).scalar_one() == 1.0
