import pytest

from mirante.analysis.engine import analyze
from mirante.domain.analysis import Analysis
from mirante.parsers import get_parser


@pytest.fixture(scope="module")
def parser():
    return get_parser("postgres")


@pytest.fixture(scope="module")
def run(parser, sample, schema_sql):
    """run("D") -> Analysis do anexo, com schema; F recebe B como dependencia."""

    def go(annex: str, *, schema: bool = True, deps: bool = True) -> Analysis:
        dependencies = [parser.parse_procedure(sample("B"))] if annex == "F" and deps else []
        tables = parser.parse_schema(schema_sql) if schema else []
        return analyze(parser.parse_procedure(sample(annex)), dependencies, tables)

    return go


def codes(analysis: Analysis) -> set[str]:
    return {r.code for r in analysis.risks}


def risk(analysis: Analysis, code: str):
    return next(r for r in analysis.risks if r.code == code)


class TestAnexos:
    def test_b_funcao_simples_nao_tem_riscos(self, run):
        assert run("B").risks == []

    def test_c_procedure_out_e_diagnostics(self, run):
        a = run("C")
        assert {"OUT_PARAMS", "GET_DIAGNOSTICS", "NON_DETERMINISTIC_TIME"} <= codes(a)
        assert a.constructs.out_params == ["p_afetadas"]
        assert a.constructs.tables_written == {"contas": ["UPDATE"], "log_auditoria": ["INSERT"]}

    def test_d_log_de_erro_desfeito_pelo_reraise(self, run):
        """Achado 2."""
        r = risk(run("D"), "HANDLER_WRITES_ROLLED_BACK")
        assert r.severity == "high" and "0 linhas persistem" in r.message
        assert "begin_nested" in r.recommendation  # a correcao foi VERIFICADA no PostgreSQL

    def test_d_null_so_nas_comparacoes_sem_is_null_previo(self, run):
        """Achado 3: status_origem/destino (nunca testados) sim; saldo (IS NULL antes) nao."""
        r = risk(run("D"), "NULL_COMPARISON")
        assert r.lines == [25]
        assert "v_status_origem" in r.message and "v_saldo_origem" not in r.message

    def test_d_locks_e_subtransacao(self, run):
        a = run("D")
        assert {"ROW_LOCK", "SUBTRANSACTION_ROLLBACK", "TRANSACTION_SCOPE"} <= codes(a)
        assert a.constructs.locks == ["contas FOR UPDATE", "contas FOR UPDATE"]
        assert a.constructs.exception_handlers == ["others"]

    def test_e_cursor_n_mais_1(self, run):
        r = risk(run("E"), "CURSOR_N_PLUS_1")
        assert r.severity == "high" and "4 DML" not in r.message
        assert "1 SELECT e 3 DML" in r.message

    def test_e_arredondamento_nas_tres_atribuicoes(self, run):
        """Achado 4: GREATEST(...*.../100.0), *1.10 e *0.90 — mas nao `total + taxa`."""
        r = risk(run("E"), "NUMERIC_ROUNDING")
        assert r.severity == "high" and r.lines == [39, 43, 44]

    def test_e_check_aborta_o_lote(self, run):
        """Achado 5: valor calculado num loop, sujeito a CHECK (valor > 0)."""
        r = risk(run("E"), "CHECK_CONSTRAINT")
        assert r.severity == "high" and "valor > 0" in r.message and "aborta o lote" in r.message

    def test_f_raise_capturado_sem_reraise_e_alto(self, run):
        """Achado 1."""
        a = run("F")
        assert risk(a, "RAISE_CAUGHT_BY_OWN_HANDLER").severity == "high"
        assert "DEGRADED_FALLBACK" in codes(a)

    def test_f_cte_recursiva_setof_e_chamada_aninhada(self, run):
        a = run("F")
        assert {"RECURSIVE_CTE", "SET_RETURNING", "NESTED_CALL", "RAISE_LOGGING"} <= codes(a)
        assert a.constructs.recursive_cte and a.constructs.calls_resolved == ["fn_saldo_cliente"]
        assert a.constructs.raises == {"EXCEPTION": 1, "NOTICE": 1, "WARNING": 1}


class TestPrecisao:
    def test_parametro_plpgsql_nao_e_coluna(self, run):
        """Regressao: DATE_TRUNC('month', p_data_fim) nao impede indice (p_data_fim e parametro)."""
        assert "NON_SARGABLE_PREDICATE" not in codes(run("F"))

    def test_funcao_sobre_coluna_real_e_detectada(self, run):
        assert "NON_SARGABLE_PREDICATE" in codes(run("E"))

    def test_chamada_sem_definicao_e_alto_risco(self, run):
        a = run("F", deps=False)
        assert risk(a, "UNRESOLVED_CALL").severity == "high"
        assert a.constructs.calls_unresolved == ["fn_saldo_cliente"]

    def test_sem_schema_pula_regras_que_dependem_dele(self, run):
        a = run("E", schema=False)
        assert a.skipped_rules == ["check_constraints"]
        assert "CHECK_CONSTRAINT" not in codes(a)

    def test_riscos_ordenados_por_severidade(self, run):
        order = {"high": 0, "medium": 1, "low": 2}
        ranks = [order[r.severity] for r in run("D").risks]
        assert ranks == sorted(ranks)


class TestRegras:
    def test_handler_que_relanca_rebaixa_severidade(self, run):
        assert risk(run("D"), "RAISE_CAUGHT_BY_OWN_HANDLER").severity == "medium"

    def test_loop_sem_queries_e_medio(self, parser):
        sql = """CREATE PROCEDURE p() LANGUAGE plpgsql AS $$
        DECLARE c CURSOR FOR SELECT id FROM t; v BIGINT;
        BEGIN OPEN c; LOOP FETCH c INTO v; EXIT WHEN NOT FOUND; END LOOP; CLOSE c; END; $$;"""
        a = analyze(parser.parse_procedure(sql))
        assert risk(a, "CURSOR_LOOP").severity == "medium" and "CURSOR_N_PLUS_1" not in codes(a)

    def test_escala_so_para_multiplicacao_e_divisao(self, parser):
        sql = """CREATE FUNCTION f(a NUMERIC) RETURNS NUMERIC LANGUAGE plpgsql AS $$
        DECLARE x NUMERIC(18,2); BEGIN x := a + 1; RETURN x; END; $$;"""
        assert "NUMERIC_ROUNDING" not in codes(analyze(parser.parse_procedure(sql)))


class TestRegrasVerificadasNoPostgres:
    """Orientacoes confirmadas empiricamente (harness comportamental / experimento no PG 16)."""

    def test_jsonb_com_variaveis_alerta_sobre_binds_sem_tipo(self, run):
        r = risk(run("D"), "JSON_SERIALIZATION")
        assert "CAST(:p AS text)" in r.recommendation and "could not determine" in r.message

    def test_raise_com_placeholders_avisa_do_formato_null(self, run):
        r = risk(run("C"), "RAISE_NULL_FORMAT")
        assert "<NULL>" in r.message and r.severity == "low"

    def test_funcao_sem_raise_formatado_nao_dispara(self, run):
        assert "RAISE_NULL_FORMAT" not in codes(run("B"))
