import pytest

from mirante.parsers import get_parser
from mirante.validation.engine import validate
from mirante.validation.layers import Context
from tests.fakes import BAD_IMPORT_B, GOOD_B

HEADER = "from decimal import Decimal\nfrom sqlalchemy import Connection, text\n\n"


@pytest.fixture(scope="module")
def ctx(sample):
    parser = get_parser("postgres")
    return Context(parser.parse_procedure(sample("B")))


@pytest.fixture(scope="module")
def ctx_f(sample):
    parser = get_parser("postgres")
    return Context(parser.parse_procedure(sample("F")), [parser.parse_procedure(sample("B"))])


def layer(report, name):
    return next(x for x in report.layers if x.name == name)


def codes(report, name):
    return {i.code for i in layer(report, name).issues}


def body(sql="SELECT 1"):
    return (
        HEADER
        + "def fn_saldo_cliente(conn: Connection, p_cliente_id: int) -> Decimal:\n"
        + f'    return conn.execute(text("{sql}"), {{"c": p_cliente_id}}).scalar_one()\n'
    )


class TestCamadas:
    def test_codigo_correto_passa_em_todas(self, ctx):
        report, _ = validate(GOOD_B, ctx)
        assert report.passed and [x.status for x in report.layers] == ["passed"] * 4

    def test_sintaxe_invalida_pula_as_demais(self, ctx):
        report, _ = validate("def f(:\n  pass", ctx)
        assert [x.status for x in report.layers] == ["failed", "skipped", "skipped", "skipped"]
        assert layer(report, "syntax").issues[0].line == 1

    def test_import_inexistente_passa_no_ast_e_no_ruff_mas_falha_nos_imports(self, ctx):
        """O caso real do Anexo F: `from dataclasses import frozen`."""
        report, _ = validate(BAD_IMPORT_B, ctx)
        assert layer(report, "syntax").status == "passed"
        assert layer(report, "lint").status == "passed"
        assert codes(report, "imports") == {"IMPORT_NAME"}
        assert "cannot import name 'frozen'" in report.feedback[0]

    def test_modulo_fora_da_allowlist(self, ctx):
        code = "import requests\n" + body().replace("scalar_one()", "scalar_one() + requests.get")
        assert codes(validate(code, ctx)[0], "imports") == {"IMPORT_FORBIDDEN"}

    def test_submodulo_valido_nao_e_falso_positivo(self, ctx):
        header = "from sqlalchemy.engine import Connection\nfrom sqlalchemy import text\n"
        code = header + "from decimal import Decimal\n\n" + body().split("\n\n", 1)[1]
        assert validate(code, ctx)[0].passed

    def test_undefined_name_e_erro_de_lint(self, ctx):
        code = body().replace("scalar_one()", "scalar_one() + nao_existe")
        assert codes(validate(code, ctx)[0], "lint") == {"F821"}

    def test_import_invalido_mas_nao_usado_e_removido_pelo_autofix(self, ctx):
        """Autofix roda antes dos imports: remover o import morto e seguro e o modulo funciona."""
        code = "from dataclasses import frozen\n" + body()
        report, fixed = validate(code, ctx)
        assert report.passed and "frozen" not in fixed

    def test_imports_nao_usados_sao_corrigidos_sem_llm(self, ctx):
        code = "import os\nfrom typing import List\n" + body()
        report, fixed = validate(code, ctx)
        assert report.passed and "import os" not in fixed and "List" not in fixed
        assert len(report.autofixed) == 2


class TestDependencias:
    DEP = "from fn_saldo_cliente import fn_saldo_cliente\n"
    PROC = (
        "from sqlalchemy import Connection\n\n\n"
        "def sp_relatorio_mensal_cliente(\n"
        "    conn: Connection, p_cliente_id, p_data_inicio, p_data_fim\n"
        "):\n"
        "    return fn_saldo_cliente(conn, p_cliente_id)\n"
    )

    def test_dependencia_traduzida_resolve(self, ctx_f):
        assert validate(self.DEP + self.PROC, ctx_f)[0].passed

    def test_nome_errado_em_modulo_de_dependencia(self, ctx_f):
        code = "from fn_saldo_cliente import calcular\n" + self.PROC.replace(
            "return fn_saldo_cliente", "return calcular"
        )
        assert "IMPORT_NAME" in codes(validate(code, ctx_f)[0], "imports")

    def test_modulo_que_nao_e_dependencia_declarada(self, ctx):
        assert "IMPORT_FORBIDDEN" in codes(validate(self.DEP + self.PROC, ctx)[0], "imports")


class TestPolitica:
    def test_commit_e_rollback_proibidos(self, ctx):
        code = body() + "    conn.commit()\n"
        assert "POLICY_TX_CONTROL" in codes(validate(code, ctx)[0], "policy")

    def test_sql_montado_com_fstring(self, ctx):
        code = body().replace('text("SELECT 1")', 'text(f"SELECT {p_cliente_id}")')
        assert codes(validate(code, ctx)[0], "policy") == {"POLICY_SQL_INJECTION"}

    def test_float_e_so_aviso(self, ctx):
        report, _ = validate(body() + "\n\nTAXA = 1.5\n", ctx)
        assert report.passed and "POLICY_FLOAT" in codes(report, "policy")
        assert report.feedback == []  # avisos nao disparam reparo

    def test_funcao_ausente(self, ctx):
        code = body().replace("def fn_saldo_cliente", "def outro_nome")
        assert codes(validate(code, ctx)[0], "policy") == {"CONTRACT_MISSING"}

    def test_parametros_diferentes_do_in(self, ctx):
        code = body().replace("p_cliente_id: int", "cliente: int")
        assert "CONTRACT_PARAMS" in codes(validate(code, ctx)[0], "policy")

    def test_primeiro_parametro_deve_ser_conn(self, ctx):
        code = body().replace("conn: Connection, ", "")
        assert "CONTRACT_CONN" in codes(validate(code, ctx)[0], "policy")


class TestBindsSemTipo:
    """Achado do harness: passa em ast/ruff/imports, mas quebra em runtime (psycopg 3)."""

    SQL_RUIM = (
        "INSERT INTO log (a, detalhes) VALUES ('x', jsonb_build_object('origem', :o, 'erro', :msg))"
    )

    def code(self, sql):
        return body().replace('text("SELECT 1")', f'text("{sql}")')

    def test_bind_sem_tipo_em_jsonb_build_object_e_erro(self, ctx):
        report, _ = validate(self.code(self.SQL_RUIM), ctx)
        assert codes(report, "policy") == {"POLICY_UNTYPED_BIND"} and not report.passed
        assert "CAST(:p AS" in report.feedback[0] and "'msg'" in report.feedback[0]

    def test_tipado_com_cast_passa(self, ctx):
        sql = "SELECT jsonb_build_object('o', CAST(:o AS bigint), 'v', CAST(:v AS numeric(18,2)))"
        assert validate(self.code(sql), ctx)[0].passed

    def test_tipado_com_dois_pontos_passa(self, ctx):
        assert validate(self.code("SELECT jsonb_build_object('o', :o::text)"), ctx)[0].passed

    def test_bind_fora_do_json_nao_e_falso_positivo(self, ctx):
        assert validate(self.code("UPDATE contas SET saldo = saldo - :v WHERE id = :o"), ctx)[
            0
        ].passed

    def test_bind_aninhado_em_funcao_dentro_do_json_e_pego(self, ctx):
        report, _ = validate(self.code("SELECT jsonb_build_object('a', COALESCE(:x, 0))"), ctx)
        assert "POLICY_UNTYPED_BIND" in codes(report, "policy")
