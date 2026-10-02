import pytest

from mirante.api.schemas import ModernizeRequest, ModernizeResponse
from mirante.domain.status import Status
from mirante.graph.builder import build_graph
from mirante.observability import LangfuseRunTrace, NoopTracer, scores_for
from mirante.service import ModernizationService
from tests.fakes import BAD_IMPORT_B, GOOD_B, InMemoryRepository, StubGenerator


class FakeRunTrace:
    def __init__(self, trace_id="trace-123"):
        self.trace_id, self.config, self.finished = trace_id, {"run_name": "modernize"}, []

    def finish(self, response):
        self.finished.append(response)


class FakeTracer:
    def __init__(self):
        self.runs: list[FakeRunTrace] = []

    def start(self, request, session_id=None):
        run = FakeRunTrace()
        run.session_id = session_id
        self.runs.append(run)
        return run

    def record_aggregate(self, session_id, scores):
        self.aggregates = getattr(self, "aggregates", []) + [(session_id, scores)]


def service(stub, repo=None, tracer=None, graph=None):
    repo = repo if repo is not None else InMemoryRepository()
    graph = graph or build_graph(lambda: stub, lambda: repo)
    return ModernizationService(graph, repo, tracer), repo


def run(svc, sample, **extra):
    import asyncio

    return asyncio.run(svc.run(ModernizeRequest(source_code=sample("B"), **extra)))


class TestIntegracaoNoServico:
    def test_trace_id_e_gravado_no_historico_e_o_config_chega_ao_grafo(self, sample):
        tracer = FakeTracer()
        svc, repo = service(StubGenerator(GOOD_B), tracer=tracer)
        run(svc, sample)
        assert repo.records[0].report["meta"]["langfuse_trace_id"] == "trace-123"

    def test_finish_recebe_a_resposta_final(self, sample):
        tracer = FakeTracer()
        svc, _ = service(StubGenerator(GOOD_B), tracer=tracer)
        response = run(svc, sample)
        assert tracer.runs[0].finished == [response]

    def test_crash_tambem_finaliza_o_trace(self, sample):
        class Exploding:
            async def ainvoke(self, _state, config=None):
                raise RuntimeError("boom")

        tracer = FakeTracer()
        svc, _ = service(None, tracer=tracer, graph=Exploding())
        assert run(svc, sample).status == Status.FAILURE
        assert tracer.runs[0].finished[0].status == Status.FAILURE

    def test_sem_tracer_funciona_normalmente(self, sample):
        svc, repo = service(StubGenerator(GOOD_B), tracer=NoopTracer())
        assert run(svc, sample).status == Status.SUCCESS
        assert repo.records[0].report["meta"]["langfuse_trace_id"] is None


class TestScores:
    def response(self, sample, stub):
        svc, _ = service(stub)
        return run(svc, sample)

    def test_sucesso(self, sample):
        scores = {
            s["name"]: s["value"] for s in scores_for(self.response(sample, StubGenerator(GOOD_B)))
        }
        assert scores == {
            "validation_passed": 1.0,
            "generation_attempts": 1.0,
            "high_risks_detected": 0.0,
            "pipeline_status": "SUCCESS",
        }

    def test_parcial_registra_falha_de_validacao_e_tentativas(self, sample):
        scores = {
            s["name"]: s["value"]
            for s in scores_for(self.response(sample, StubGenerator(BAD_IMPORT_B)))
        }
        assert scores["validation_passed"] == 0.0 and scores["generation_attempts"] == 3.0
        assert scores["pipeline_status"] == "PARTIAL"

    def test_tipos_de_dado_do_langfuse(self):
        response = ModernizeResponse(id=None, status=Status.FAILURE, report={})
        kinds = {s["name"]: s["data_type"] for s in scores_for(response)}
        assert kinds == {
            "validation_passed": "BOOLEAN",
            "generation_attempts": "NUMERIC",
            "high_risks_detected": "NUMERIC",
            "pipeline_status": "CATEGORICAL",
        }


class TestResiliencia:
    def test_falha_ao_enviar_scores_nao_levanta(self):
        class Broken:
            def create_score(self, **_):
                raise ConnectionError("langfuse fora")

        trace = LangfuseRunTrace(Broken(), "t", {})
        trace.finish(ModernizeResponse(id=None, status=Status.SUCCESS, report={}))  # nao levanta

    def test_scores_sao_enviados_com_o_trace_id(self):
        sent = []

        class Client:
            def create_score(self, **kw):
                sent.append(kw)

        LangfuseRunTrace(Client(), "abc", {}).finish(
            ModernizeResponse(id=None, status=Status.SUCCESS, report={})
        )
        assert {s["trace_id"] for s in sent} == {"abc"} and len(sent) == 4

    @pytest.mark.parametrize(("key", "enabled"), [("", True), ("pk", False)])
    def test_sem_chaves_ou_desligado_vira_noop(self, monkeypatch, key, enabled):
        from mirante import observability as obs
        from mirante.config import Settings

        monkeypatch.setattr(
            obs,
            "get_settings",
            lambda: Settings(
                langfuse_public_key=key,
                langfuse_secret_key=key,
                langfuse_enabled=enabled,
                _env_file=None,
            ),
        )
        obs.get_langfuse_client.cache_clear()
        try:
            assert isinstance(obs.get_tracer(), NoopTracer)
        finally:
            obs.get_langfuse_client.cache_clear()
