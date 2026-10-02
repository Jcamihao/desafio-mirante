import asyncio

import pytest

from mirante.api.schemas import ModernizeRequest
from mirante.domain.evaluation import CaseResult
from mirante.domain.status import Status
from mirante.evals.cases import CASES
from mirante.evals.metrics import case_result, compute_metrics
from mirante.evals.runner import run_evaluation, select_cases
from mirante.graph.builder import build_graph
from mirante.service import ModernizationService
from tests.fakes import (
    BAD_IMPORT_B,
    GOOD_C,
    InMemoryEvaluationRepository,
    InMemoryRepository,
    RecordingTracer,
    RoutedStub,
)


def result(key="B", *, layers=None, first=True, final=True, expected=(), missed=(), attempts=1):
    layers = layers or dict.fromkeys(("syntax", "lint", "imports", "policy"), True)
    return CaseResult(
        case=key, status=Status.SUCCESS if final else Status.PARTIAL, attempts=attempts,
        first_attempt_valid=first, final_valid=final, layers=layers,
        expected_risks=sorted(expected), missed_risks=sorted(missed),
    )  # fmt: skip


class TestMetricas:
    def test_o_ast_parse_sozinho_superestima_a_qualidade(self):
        """Caso real: 5/5 passam em ast.parse, mas 1 nao importa (`dataclasses.frozen`)."""
        bad = dict.fromkeys(("syntax", "lint", "policy"), True) | {"imports": False}
        rs = [result(k) for k in "BCDE"] + [result("F", layers=bad, first=False, final=False)]
        m = compute_metrics(rs)
        assert m.syntax_rate == 1.0 and m.valid_rate == 0.8 and m.naive_gap == pytest.approx(0.2)
        assert m.imports_rate == 0.8 and m.lint_rate == 1.0

    def test_ganho_do_reparo_e_pass_at_1(self):
        rs = [
            result("B"),
            result("C", first=False, attempts=2),
            result("D", first=False, final=False),
        ]
        m = compute_metrics(rs)
        assert m.first_attempt_rate == pytest.approx(1 / 3, abs=1e-4)
        assert m.valid_rate == pytest.approx(2 / 3, abs=1e-4)
        assert m.repair_gain == pytest.approx(1 / 3, abs=1e-4) and m.avg_attempts == pytest.approx(
            1.33
        )

    def test_disponibilidade_do_provedor_separada_da_qualidade_do_codigo(self):
        """Caso real: o C falhou por 429 antes de gerar. Isso nao e culpa da qualidade do codigo."""
        no_llm = CaseResult(case="C", status=Status.FAILURE, attempts=0)
        m = compute_metrics([result("B"), no_llm, result("D"), result("E"), result("F")])
        assert m.valid_rate == 0.8  # ponta a ponta: conta a falha do provedor
        assert m.generation_rate == 0.8 and m.quality_rate == 1.0  # os 4 gerados estavam bons

    def test_qualidade_cai_quando_o_codigo_gerado_e_ruim(self):
        bad = result("C", final=False, first=False)
        m = compute_metrics([result("B"), bad])
        assert m.generation_rate == 1.0 and m.quality_rate == 0.5

    def test_recall_de_riscos(self):
        rs = [result(expected={"A", "B"}, missed={"B"}), result(expected={"C", "D"})]
        assert compute_metrics(rs).risk_recall == 0.75

    def test_sem_riscos_esperados_o_recall_e_total(self):
        assert compute_metrics([result()]).risk_recall == 1.0

    def test_lista_vazia_nao_divide_por_zero(self):
        m = compute_metrics([])
        assert m.cases == 0 and m.valid_rate == 0.0 and m.avg_attempts == 0.0


class TestExtracaoDoRelatorio:
    REPORT = {
        "analysis": {"risks": [{"code": "ROW_LOCK"}, {"code": "OUT_PARAMS"}], "duration_ms": 3},
        "generation": {"attempts": [
            {"status": "ok", "input_tokens": 100, "output_tokens": 50, "duration_ms": 1000},
            {"status": "ok", "input_tokens": 120, "output_tokens": 60, "duration_ms": 2000},
        ]},
        "validation": {
            "status": "passed", "duration_ms": 7,
            "history": [{"attempt": 1, "passed": False}, {"attempt": 2, "passed": True}],
            "layers": [
                {"name": n, "status": "passed"} for n in ("syntax", "lint", "imports", "policy")
            ],
        },
        "meta": {"langfuse_trace_id": "t1"},
    }  # fmt: skip

    def test_registra_o_modelo_que_de_fato_gerou(self):
        report = {"generation": {"attempts": [
            {"status": "failed", "model": None},
            {"status": "ok", "model": "gemini-3.6-flash"},
            {"status": "ok", "model": "gemini-3.5-flash"},
        ]}}  # fmt: skip
        r = case_result("B", frozenset(), Status.SUCCESS, report)
        assert r.models == ["gemini-3.5-flash", "gemini-3.6-flash"]

    def test_campos(self):
        r = case_result(
            "D", frozenset({"ROW_LOCK", "NULL_COMPARISON"}), Status.SUCCESS, self.REPORT
        )
        assert r.attempts == 2 and r.first_attempt_valid is False and r.final_valid is True
        assert r.detected_risks == ["OUT_PARAMS", "ROW_LOCK"] and r.missed_risks == [
            "NULL_COMPARISON"
        ]
        assert (r.input_tokens, r.output_tokens, r.duration_ms) == (220, 110, 3010)
        assert r.trace_id == "t1" and all(r.layers.values())

    def test_falha_antes_de_gerar_conta_como_invalido_em_todas_as_camadas(self):
        r = case_result("B", frozenset(), Status.FAILURE, {"parsing": {"status": "failed"}})
        assert r.layers == dict.fromkeys(("syntax", "lint", "imports", "policy"), False)
        assert not r.final_valid and not r.first_attempt_valid and r.attempts == 0


def test_casos_definidos_para_os_cinco_anexos():
    assert list(CASES) == list("BCDEF") and CASES["F"].depends_on == ("B",)
    assert not CASES["B"].expected_risks  # B e o caso de aquecimento: sem riscos
    assert all(c.source.startswith("--") for c in CASES.values())


def test_select_cases():
    assert [c.key for c in select_cases(["D", "B"])] == ["D", "B"]
    assert len(select_cases(None)) == 5
    with pytest.raises(KeyError, match="desconhecidos"):
        select_cases(["Z"])


class TestExecutor:
    def run(self, overrides=None, cases=None, **kw):
        stub, repo, evals, tracer = (
            RoutedStub(overrides),
            InMemoryRepository(),
            InMemoryEvaluationRepository(),
            RecordingTracer(),
        )
        service = ModernizationService(build_graph(lambda: stub, lambda: repo), repo, tracer)
        evals = kw.pop("evals", evals)
        run = asyncio.run(run_evaluation(service, evals, tracer, select_cases(cases), **kw))
        return run, stub, repo, evals, tracer

    def test_todos_validos_e_a_analise_detecta_todos_os_riscos_esperados(self):
        """Protege a analise: os riscos esperados dos achados 1-5 sao de fato detectados."""
        run, *_ = self.run()
        m = run.metrics
        assert m.cases == 5 and m.valid_rate == 1.0 and m.first_attempt_rate == 1.0
        assert m.risk_recall == 1.0, [
            (r.case, r.missed_risks) for r in run.results if r.missed_risks
        ]
        assert [r.case for r in run.results] == list("BCDEF")

    def test_caso_que_precisa_de_reparo_baixa_a_taxa_da_primeira_tentativa(self):
        run, stub, *_ = self.run({"sp_atualizar_status_contas_inativas": [BAD_IMPORT_B, GOOD_C]})
        by_case = {r.case: r for r in run.results}
        assert by_case["C"].attempts == 2 and not by_case["C"].first_attempt_valid
        assert run.metrics.valid_rate == 1.0 and run.metrics.first_attempt_rate == 0.8
        assert run.metrics.repair_gain == pytest.approx(0.2) and len(stub.calls) == 6

    def test_caso_que_nao_converge_derruba_so_o_valid_rate(self):
        run, *_ = self.run({"sp_transferir_entre_contas": [BAD_IMPORT_B]})
        d = next(r for r in run.results if r.case == "D")
        assert d.status == Status.PARTIAL and d.layers["imports"] is False and d.attempts == 3
        # o codigo ruim passa no ast.parse (syntax 100%) mas nao no resto: eis a superestimativa
        assert run.metrics.valid_rate == 0.8 and run.metrics.syntax_rate == 1.0
        assert run.metrics.naive_gap == pytest.approx(0.2)

    def test_dependencia_do_f_chega_ao_pipeline(self):
        _, stub, *_ = self.run(cases=["F"])
        assert "fn_saldo_cliente(p_cliente_id: int8)" in stub.calls[0][1]

    def test_o_modelo_da_avaliacao_e_o_que_gerou_nao_o_configurado(self):
        run, *_ = self.run(cases=["B"])
        assert run.model == "stub"  # os atributos vem do relatorio, nao de Settings.llm_model

    def test_grava_a_avaliacao_e_agrega_scores_na_sessao(self):
        run, _, repo, evals, tracer = self.run()
        assert evals.runs == [run] and len(repo.records) == 5  # 1 linha de historico por caso
        ((session, scores),) = tracer.aggregates
        assert scores["eval_valid_rate"] == 1.0 and "eval_syntax_rate" in scores
        assert set(tracer.sessions) == {session}  # os 5 traces compartilham a sessao

    def test_falha_ao_gravar_a_avaliacao_nao_perde_o_resultado(self):
        from sqlalchemy.exc import OperationalError

        down = InMemoryEvaluationRepository(
            fail_with=OperationalError("INSERT", {}, Exception("x"))
        )
        run, *_ = self.run(cases=["B"], evals=down)
        assert run.metrics.cases == 1


def test_cada_caso_passa_pelo_servico_com_a_sessao(monkeypatch):
    seen = []
    original = ModernizationService.run

    async def spy(self, request: ModernizeRequest, session_id=None):
        seen.append(session_id)
        return await original(self, request, session_id)

    monkeypatch.setattr(ModernizationService, "run", spy)
    TestExecutor().run(cases=["B", "C"])
    assert len(seen) == 2 and seen[0] == seen[1] and seen[0].startswith("eval-")
