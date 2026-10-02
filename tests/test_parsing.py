from mirante.domain.ir import ParsedProcedure, Stmt
from mirante.nodes.parsing import _walk, parsing_node
from mirante.parsers import get_parser


def parse(source: str) -> ParsedProcedure:
    return get_parser("postgres").parse_procedure(source)


def find(proc: ParsedProcedure, kind: str) -> list[Stmt]:
    return [s for s in _walk(proc.body) if s.kind == kind]


class TestSignature:
    def test_funcao_escalar_sem_modo_explicito(self, sample):
        proc = parse(sample("B"))
        assert proc.kind == "function"
        assert [(p.mode, p.name, p.type) for p in proc.params] == [("IN", "p_cliente_id", "int8")]
        assert proc.returns and proc.returns.type == "numeric(18,2)"

    def test_procedure_com_parametro_out(self, sample):
        proc = parse(sample("C"))
        assert proc.kind == "procedure" and proc.returns is None
        assert [(p.mode, p.name) for p in proc.params] == [("IN", "p_dias"), ("OUT", "p_afetadas")]

    def test_returns_table_nao_vaza_para_params(self, sample):
        proc = parse(sample("F"))
        assert all(p.mode != "TABLE" for p in proc.params)
        assert proc.returns and proc.returns.setof
        assert [c.name for c in proc.returns.columns][:2] == ["mes_referencia", "total_creditos"]


class TestVariables:
    def test_precisao_numerica_preservada(self, sample):
        """O datum do plpgsql so traz `numeric`; a precisao define o arredondamento."""
        types = {v.name: v.type for v in parse(sample("E")).variables}
        assert types["v_taxa"] == "numeric(18,2)"
        assert types["v_percentual"] == "numeric(7,4)"
        assert types["v_tipo"] == "varchar(20)"

    def test_cursor_explicito_com_sua_query(self, sample):
        cursor = next(v for v in parse(sample("E")).variables if v.kind == "cursor")
        assert cursor.name == "cur_transacoes"
        assert cursor.cursor_query and cursor.cursor_query.tables == ["transacoes"]

    def test_variaveis_implicitas_sao_ignoradas(self, sample):
        names = {v.name for v in parse(sample("D")).variables}
        assert names == {"v_saldo_origem", "v_status_origem", "v_status_destino"}


class TestConstrucoes:
    def test_for_update_detectado(self, sample):
        locks = [s.sql.locking for s in find(parse(sample("D")), "execsql") if s.sql.locking]
        assert locks == ["FOR UPDATE", "FOR UPDATE"]

    def test_get_diagnostics_resolve_alvo(self, sample):
        (diag,) = find(parse(sample("C")), "getdiag")
        assert diag.detail["items"] == [{"item": "ROW_COUNT", "target": "p_afetadas"}]

    def test_bloco_exception_others_com_reraise(self, sample):
        proc = parse(sample("D"))
        (block,) = [b for b in find(proc, "block") if b.detail.get("exception_conditions")]
        assert block.detail["exception_conditions"] == ["others"]
        handler = block.blocks["exception:others"]
        assert [s.kind for s in handler] == ["execsql", "raise"]
        assert handler[-1].detail["reraise"] is True

    def test_cursor_loop_fetch_exit_continue(self, sample):
        kinds = {s.kind for s in _walk(parse(sample("E")).body)}
        assert {"open", "fetch", "loop", "exit", "continue", "case", "close"} <= kinds

    def test_cte_recursiva_e_funcao_aninhada(self, sample):
        proc = parse(sample("F"))
        rq = find(proc, "return_query")[0]
        assert rq.sql and rq.sql.recursive and rq.sql.cte_names == ["meses", "movimento"]
        assert {"transacoes", "contas"} <= set(rq.sql.tables)
        (assign,) = find(proc, "assign")
        assert assign.sql and assign.sql.functions == ["fn_saldo_cliente"]

    def test_niveis_de_raise(self, sample):
        levels = {s.detail["level"] for s in find(parse(sample("F")), "raise")}
        assert levels == {"EXCEPTION", "NOTICE", "WARNING"}

    def test_raise_de_validacao_esta_dentro_do_bloco_com_exception(self, sample):
        """Achado 1: em F, o RAISE de periodo invalido e capturado pelo proprio handler."""
        proc = parse(sample("F"))
        inner = next(b for b in find(proc, "block") if b.detail.get("exception_conditions"))
        raises_in_body = [s for s in _walk(inner.blocks["body"]) if s.kind == "raise"]
        assert any(s.detail["level"] == "EXCEPTION" for s in raises_in_body)


class TestSchema:
    def test_check_constraints_e_pk(self, schema_sql):
        tables = {t.name: t for t in get_parser("postgres").parse_schema(schema_sql)}
        assert tables["transacoes"].primary_key == ["id"]
        valor = next(c for c in tables["transacoes"].columns if c.name == "valor")
        assert valor.checks == ["valor > 0"]  # base do achado 5 (taxa 0 viola o CHECK)
        assert (
            next(c for c in tables["contas"].columns if c.name == "saldo").type == "numeric(18,2)"
        )


class TestNode:
    def test_sucesso_gera_relatorio(self, sample, schema_sql):
        out = parsing_node({"source_code": sample("E"), "schema_sql": schema_sql})
        assert out["parsed"].name == "sp_processar_lote_taxas"
        assert out["report"]["parsing"]["status"] == "ok"
        assert out["report"]["parsing"]["tables"] == [
            "contas",
            "log_auditoria",
            "taxas",
            "transacoes",
        ]
        assert len(out["schema"]) == 5

    def test_dependencias_sao_parseadas(self, sample):
        out = parsing_node({"source_code": sample("F"), "dependencies": [sample("B")]})
        assert [d.name for d in out["parsed_dependencies"]] == ["fn_saldo_cliente"]

    def test_sintaxe_invalida_vira_estado_de_falha_nao_excecao(self):
        out = parsing_node({"source_code": "CREATE FUNCTION f( BEGIN"})
        assert out["errors"][0].stage == "parsing"
        assert out["report"]["parsing"]["status"] == "failed"
        assert "parsed" not in out

    def test_sem_create_function(self):
        out = parsing_node({"source_code": "SELECT 1;"})
        assert "Nenhum CREATE" in out["errors"][0].message

    def test_linguagem_nao_plpgsql(self):
        sql = "CREATE FUNCTION f() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$;"
        assert "nao suportada" in parsing_node({"source_code": sql})["errors"][0].message

    def test_dialeto_desconhecido(self, sample):
        out = parsing_node({"source_code": sample("B"), "dialect": "tsql"})
        assert "nao suportado" in out["errors"][0].message


def test_case_nao_vaza_variavel_sintetica_do_parser(sample):
    (case,) = find(parse(sample("E")), "case")
    assert case.detail["when"] == ["v_tipo IN ('TRANSFERENCIA')", "v_tipo IN ('SAQUE')"]
