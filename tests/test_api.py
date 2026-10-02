import asyncio
import threading
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

from mirante.api.app import create_app
from mirante.api.deps import (
    get_app_settings,
    get_database_check,
    get_evaluation_repository,
    get_repository,
    get_run_limiter,
    get_service,
    get_tracer_dep,
)
from mirante.api.schemas import ModernizeResponse, StageErrorOut
from mirante.config import Settings
from mirante.domain.status import Status
from mirante.graph.builder import build_graph
from mirante.service import ModernizationService, http_status_for
from tests.fakes import (
    BAD_IMPORT_B,
    GOOD_B,
    GOOD_F,
    InMemoryEvaluationRepository,
    InMemoryRepository,
    RecordingTracer,
    RoutedStub,
    StubGenerator,
)


class Api:
    """Monta a app com dependencias falsas (sem rede, sem banco, sem LLM)."""

    def __init__(self, stub=None, repo=None, graph=None, db_ok=True, settings=None, limiter=None):
        self.stub = stub or StubGenerator(GOOD_B)
        self.repo = repo if repo is not None else InMemoryRepository()
        graph = graph or build_graph(lambda: self.stub, lambda: self.repo)
        service = ModernizationService(graph, self.repo)
        self.app = create_app()
        overrides = self.app.dependency_overrides
        overrides[get_service] = lambda: service
        overrides[get_repository] = lambda: self.repo
        overrides[get_database_check] = lambda: lambda: db_ok
        if settings:
            overrides[get_app_settings] = lambda: settings
        if limiter:
            overrides[get_run_limiter] = lambda: limiter
        self.client = TestClient(self.app)  # sem `with`: o lifespan (migracao) nao roda

    def post(self, sample, annex="B", **extra):
        return self.client.post("/modernize", json={"source_code": sample(annex), **extra})


class TestModernize:
    def test_sucesso_devolve_codigo_e_relatorio_das_quatro_etapas(self, sample):
        api = Api()
        res = api.post(sample)
        body = res.json()
        assert res.status_code == 200 and body["status"] == "SUCCESS"
        assert body["generated_code"] == GOOD_B and body["procedure_name"] == "fn_saldo_cliente"
        assert set(body["report"]) >= {"parsing", "analysis", "generation", "validation", "meta"}
        assert body["id"] == str(api.repo.records[0].id) and body["errors"] == []

    def test_parcial_e_200_com_o_aviso_no_corpo(self, sample):
        res = Api(StubGenerator(BAD_IMPORT_B)).post(sample)
        assert res.status_code == 200 and res.json()["status"] == "PARTIAL"
        assert res.json()["report"]["validation"]["status"] == "failed"

    def test_max_attempts_da_requisicao_e_respeitado(self, sample):
        api = Api(StubGenerator(BAD_IMPORT_B))
        api.post(sample, max_attempts=1)
        assert len(api.stub.calls) == 1

    def test_schema_e_dependencias_chegam_ao_pipeline(self, sample, schema_sql):
        api = Api(StubGenerator(GOOD_F))
        res = api.post(sample, "F", schema_sql=schema_sql, dependencies=[sample("B")])
        assert res.json()["status"] == "SUCCESS"  # `from fn_saldo_cliente import ...` resolveu
        assert "`fn_saldo_cliente(p_cliente_id: int8)" in api.stub.calls[0][1]

    def test_sql_invalido_e_422_com_relatorio_e_registro(self):
        api = Api()
        res = api.client.post("/modernize", json={"source_code": "CREATE FUNCTION f( BEGIN"})
        body = res.json()
        assert res.status_code == 422 and body["status"] == "FAILURE"
        assert body["errors"][0]["stage"] == "parsing" and body["id"]
        assert api.stub.calls == [] and len(api.repo.records) == 1

    def test_dialeto_nao_suportado(self, sample):
        res = Api().post(sample, dialect="tsql")
        assert res.status_code == 422 and "nao suportado" in res.json()["errors"][0]["message"]

    def test_falha_do_llm_e_502(self, sample):
        res = Api(StubGenerator(fail_with=TimeoutError("503"))).post(sample)
        assert res.status_code == 502 and res.json()["errors"][0]["stage"] == "generation"

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"source_code": ""},
            {"source_code": "x" * 200_001},
            {"source_code": "x", "max_attempts": 0},
            {"source_code": "x", "max_attempts": 6},
            {"source_code": "x", "dependencies": ["a"] * 11},
        ],
    )
    def test_payload_invalido_e_rejeitado_antes_do_pipeline(self, payload):
        api = Api()
        assert api.client.post("/modernize", json=payload).status_code == 422
        assert api.stub.calls == [] and api.repo.records == []


class TestRobustez:
    class ExplodingGraph:
        async def ainvoke(self, _state, config=None):
            raise RuntimeError("bug num no")

    def test_crash_do_grafo_e_500_mas_fica_registrado(self, sample):
        api = Api(graph=self.ExplodingGraph())
        res = api.post(sample)
        body = res.json()
        assert res.status_code == 500 and body["status"] == "FAILURE"
        assert body["errors"][0] == {"stage": "pipeline", "message": "RuntimeError: bug num no"}
        (rec,) = api.repo.records
        assert rec.status == Status.FAILURE and rec.source_code == sample("B")
        assert body["id"] == str(rec.id)

    def test_crash_com_banco_fora_nao_esconde_o_erro(self, sample):
        down = OperationalError("INSERT", {}, Exception("refused"))
        api = Api(graph=self.ExplodingGraph(), repo=InMemoryRepository(fail_with=down))
        res = api.post(sample)
        assert res.status_code == 500 and res.json()["id"] is None

    def test_banco_fora_do_ar_nao_impede_entregar_o_codigo(self, sample):
        down = OperationalError("INSERT", {}, Exception("refused"))
        res = Api(repo=InMemoryRepository(fail_with=down)).post(sample)
        body = res.json()
        assert res.status_code == 200 and body["status"] == "SUCCESS" and body["id"] is None
        assert body["generated_code"] == GOOD_B
        assert body["errors"][-1]["stage"] == "persistence"

    def test_limitador_nunca_excede_o_maximo_de_execucoes_simultaneas(self, sample):
        class Slow(StubGenerator):
            active = peak = 0
            lock = threading.Lock()

            def generate(self, system, user):
                with self.lock:
                    Slow.active += 1
                    Slow.peak = max(Slow.peak, Slow.active)
                time.sleep(0.15)
                try:
                    return super().generate(system, user)
                finally:
                    with self.lock:
                        Slow.active -= 1

        api = Api(Slow(GOOD_B), limiter=asyncio.Semaphore(2))

        async def burst():
            transport = httpx.ASGITransport(app=api.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                payload = {"source_code": sample("B")}
                return await asyncio.gather(
                    *[client.post("/modernize", json=payload) for _ in range(6)]
                )

        responses = asyncio.run(burst())
        assert [r.status_code for r in responses] == [200] * 6
        assert Slow.peak == 2  # simultaneidade real, mas contida pelo limite
        assert len(api.repo.records) == 6


class TestHealth:
    def test_ok(self):
        body = Api().client.get("/health").json()
        assert body["status"] == "ok" and body["components"]["database"] == "ok"
        assert body["components"]["llm_provider"] == "gemini" and body["version"]

    def test_banco_fora_e_degradado_mas_responde_200(self):
        res = Api(db_ok=False).client.get("/health")
        assert res.status_code == 200
        assert (
            res.json()["status"] == "degraded"
            and res.json()["components"]["database"] == "unavailable"
        )

    def test_informa_se_ha_chave_mas_nunca_vaza_o_valor(self):
        secret = "AIza-SEGREDO-123"
        res = Api(settings=Settings(google_api_key=secret, _env_file=None)).client.get("/health")
        assert res.json()["components"]["llm_configured"] is True and secret not in res.text
        no_key = Api(settings=Settings(google_api_key="", _env_file=None)).client.get("/health")
        assert no_key.json()["components"]["llm_configured"] is False


class TestHistorico:
    def test_lista_e_detalha(self, sample):
        api = Api()
        api.post(sample)
        api.client.post("/modernize", json={"source_code": "CREATE FUNCTION f( BEGIN"})
        rows = api.client.get("/history").json()
        assert [r["status"] for r in rows] == ["FAILURE", "SUCCESS"]
        assert "generated_code" not in rows[0]  # listagem e resumo
        full = api.client.get(f"/history/{rows[1]['id']}").json()
        assert full["generated_code"] == GOOD_B and full["report"]["parsing"]["status"] == "ok"

    def test_filtro_por_status_e_validacao(self, sample):
        api = Api()
        api.post(sample)
        assert api.client.get("/history", params={"status": "FAILURE"}).json() == []
        assert api.client.get("/history", params={"limit": 0}).status_code == 422
        assert api.client.get("/history", params={"status": "XYZ"}).status_code == 422

    def test_inexistente_e_404_e_id_invalido_e_422(self):
        client = Api().client
        assert client.get("/history/00000000-0000-0000-0000-000000000000").status_code == 404
        assert client.get("/history/nao-e-uuid").status_code == 422


def test_openapi_expõe_os_endpoints_exigidos():
    paths = Api().client.get("/openapi.json").json()["paths"]
    assert "post" in paths["/modernize"] and "get" in paths["/health"]


@pytest.mark.parametrize(
    ("status", "stages", "expected"),
    [
        (Status.SUCCESS, [], 200),
        (Status.PARTIAL, [], 200),
        (Status.FAILURE, ["parsing"], 422),
        (Status.FAILURE, ["generation"], 502),
        (Status.FAILURE, ["pipeline"], 500),
        (Status.FAILURE, ["persistence"], 500),
    ],
)
def test_mapeamento_de_status_http(status, stages, expected):
    errors = [StageErrorOut(stage=s, message="m") for s in stages]
    response = ModernizeResponse(id=None, status=status, report={}, errors=errors)
    assert http_status_for(response) == expected


class TestAvaliacao:
    def api(self, overrides=None):
        stub, repo = RoutedStub(overrides), InMemoryRepository()
        api = Api(stub, repo, build_graph(lambda: stub, lambda: repo))
        evals = InMemoryEvaluationRepository()
        api.evals, api.tracer = evals, RecordingTracer()
        api.app.dependency_overrides[get_evaluation_repository] = lambda: evals
        api.app.dependency_overrides[get_tracer_dep] = lambda: api.tracer
        return api

    def test_roda_os_cinco_anexos_e_devolve_o_funil(self):
        res = self.api().client.post("/evaluate", json={})
        body = res.json()
        assert res.status_code == 200 and body["metrics"]["cases"] == 5
        assert body["metrics"]["valid_rate"] == 1.0 and body["metrics"]["risk_recall"] == 1.0
        assert [r["case"] for r in body["results"]] == list("BCDEF")

    def test_subconjunto_de_casos(self):
        body = self.api().client.post("/evaluate", json={"cases": ["B", "F"]}).json()
        assert [r["case"] for r in body["results"]] == ["B", "F"]

    def test_caso_desconhecido_e_422(self):
        res = self.api().client.post("/evaluate", json={"cases": ["Z"]})
        assert res.status_code == 422 and "desconhecidos" in res.json()["detail"]

    def test_validacao_do_payload(self):
        assert self.api().client.post("/evaluate", json={"max_attempts": 9}).status_code == 422

    def test_historico_de_avaliacoes(self):
        api = self.api()
        run_id = api.client.post("/evaluate", json={"cases": ["B"]}).json()["id"]
        (summary,) = api.client.get("/evaluate").json()
        assert summary["id"] == run_id and "results" not in summary
        assert api.client.get(f"/evaluate/{run_id}").json()["results"][0]["case"] == "B"

    def test_avaliacao_inexistente(self):
        client = self.api().client
        assert client.get("/evaluate/00000000-0000-0000-0000-000000000000").status_code == 404
