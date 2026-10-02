from mirante.domain.status import Status
from mirante.graph.builder import build_graph
from tests.fakes import BAD_IMPORT_B, GOOD_B, GOOD_D, InMemoryRepository, StubGenerator


def run(state: dict, stub: StubGenerator, repo: InMemoryRepository | None = None):
    repo = repo if repo is not None else InMemoryRepository()
    return build_graph(lambda: stub, lambda: repo).invoke(state)


def test_fluxo_completo_gera_relatorio_das_quatro_etapas(sample, schema_sql):
    stub = StubGenerator(GOOD_D)
    out = run({"source_code": sample("D"), "schema_sql": schema_sql}, stub)
    r = out["report"]
    assert r["parsing"]["status"] == "ok" and r["generation"]["status"] == "ok"
    assert r["analysis"]["risks_by_severity"]["high"] == 1
    assert r["validation"]["status"] == "passed"
    assert out["generated_code"] == GOOD_D and len(stub.calls) == 1


def test_falha_de_parsing_encerra_sem_analise_nem_llm():
    stub = StubGenerator()
    out = run({"source_code": "CREATE FUNCTION f( BEGIN"}, stub)
    assert out["errors"][0].stage == "parsing"
    assert "analysis" not in out and "generated_code" not in out
    assert stub.calls == []  # nao gasta chamada de LLM quando a entrada e invalida


def test_falha_do_llm_vira_estado_de_falha_nao_excecao(sample):
    stub = StubGenerator(fail_with=TimeoutError("quota"))
    out = run({"source_code": sample("B")}, stub)
    assert out["errors"][0].stage == "generation"
    assert "TimeoutError" in out["report"]["generation"]["attempts"][0]["error"]
    assert out["report"]["parsing"]["status"] == "ok"  # o que ja foi feito e preservado
    assert "validation" not in out["report"]  # sem codigo, nao ha o que validar


class TestLacoDeReparo:
    def test_erro_na_primeira_corrige_na_segunda(self, sample):
        stub = StubGenerator(BAD_IMPORT_B, GOOD_B)
        out = run({"source_code": sample("B")}, stub)
        history = out["report"]["validation"]["history"]
        assert [h["passed"] for h in history] == [False, True]
        assert len(stub.calls) == 2 and out["generated_code"] == GOOD_B
        assert out["report"]["validation"]["status"] == "passed"

    def test_prompt_de_reparo_traz_o_erro_e_o_codigo_anterior(self, sample):
        stub = StubGenerator(BAD_IMPORT_B, GOOD_B)
        run({"source_code": sample("B")}, stub)
        first, second = stub.calls[0][1], stub.calls[1][1]
        assert "previous attempt FAILED" not in first
        assert "previous attempt FAILED" in second and "IMPORT_NAME" in second
        assert "cannot import name 'frozen'" in second
        assert "from dataclasses import frozen" in second  # o codigo a corrigir

    def test_sem_conserto_para_no_limite_com_status_de_falha(self, sample):
        stub = StubGenerator(BAD_IMPORT_B)
        out = run({"source_code": sample("B")}, stub)
        assert len(stub.calls) == 3  # padrao: 1 geracao + 2 reparos
        assert out["report"]["validation"]["status"] == "failed"
        assert len(out["report"]["generation"]["attempts"]) == 3

    def test_limite_configuravel_por_requisicao(self, sample):
        stub = StubGenerator(BAD_IMPORT_B)
        run({"source_code": sample("B"), "max_attempts": 1}, stub)
        assert len(stub.calls) == 1

    def test_codigo_valido_nao_repete(self, sample):
        stub = StubGenerator(GOOD_B)
        run({"source_code": sample("B")}, stub)
        assert len(stub.calls) == 1


class TestPersistenciaEmTodosOsDesfechos:
    """Requisito do desafio: toda execucao e gravada, independentemente do resultado."""

    def persisted(self, state, stub):
        repo = InMemoryRepository()
        out = run(state, stub, repo)
        assert len(repo.records) == 1  # exatamente uma linha por execucao
        return out, repo.records[0]

    def test_sucesso(self, sample):
        out, rec = self.persisted({"source_code": sample("B")}, StubGenerator(GOOD_B))
        assert rec.status == Status.SUCCESS and rec.generated_code == GOOD_B
        assert rec.procedure_name == "fn_saldo_cliente" and out["history_id"] == str(rec.id)
        assert (
            rec.report["validation"]["status"] == "passed" and rec.report["meta"]["attempts"] == 1
        )

    def test_parcial_quando_a_validacao_nao_converge(self, sample):
        out, rec = self.persisted({"source_code": sample("B")}, StubGenerator(BAD_IMPORT_B))
        assert rec.status == Status.PARTIAL and rec.generated_code  # guarda o ultimo codigo
        assert rec.report["meta"]["attempts"] == 3
        assert any("frozen" in e for e in out["validation_feedback"])

    def test_falha_de_parsing(self):
        _, rec = self.persisted({"source_code": "CREATE FUNCTION f( BEGIN"}, StubGenerator())
        assert rec.status == Status.FAILURE and rec.generated_code is None
        assert rec.procedure_name is None
        assert rec.report["errors"][0]["stage"] == "parsing"
        assert rec.source_code == "CREATE FUNCTION f( BEGIN"  # a entrada e sempre preservada

    def test_falha_do_llm(self, sample):
        _, rec = self.persisted({"source_code": sample("B")}, StubGenerator(fail_with=OSError("x")))
        assert rec.status == Status.FAILURE and rec.report["errors"][0]["stage"] == "generation"
        assert rec.procedure_name == "fn_saldo_cliente"  # o que foi parseado e aproveitado

    def test_banco_fora_do_ar_nao_derruba_nem_esconde_o_resultado(self, sample):
        from sqlalchemy.exc import OperationalError

        repo = InMemoryRepository(
            fail_with=OperationalError("INSERT", {}, Exception("conn refused"))
        )
        out = run({"source_code": sample("B")}, StubGenerator(GOOD_B), repo)
        assert out["generated_code"] == GOOD_B  # o usuario ainda recebe o codigo
        assert out["status"] == Status.SUCCESS and "history_id" not in out
        assert out["errors"][-1].stage == "persistence"
        assert out["report"]["persistence"]["status"] == "failed"
