import pytest

from mirante.analysis.engine import analyze
from mirante.config import Settings
from mirante.llm import generator as generator_module
from mirante.llm.generator import (
    ChatModelGenerator,
    clean_code,
    get_chat_model,
    is_daily_quota,
    rate_limit_delay,
)
from mirante.llm.prompts import SYSTEM_PROMPT, build_user_prompt, referenced_tables
from mirante.nodes.generation import build_generation_node
from mirante.parsers import get_parser
from tests.fakes import StubGenerator


@pytest.fixture(scope="module")
def prompt_for(sample, schema_sql):
    parser = get_parser("postgres")
    schema = parser.parse_schema(schema_sql)

    def build(annex: str, *, with_schema=True, deps=(), feedback=None) -> str:
        proc = parser.parse_procedure(sample(annex))
        tables = schema if with_schema else []
        return build_user_prompt(
            proc, analyze(proc, list(deps), tables), tables, list(deps), feedback
        )

    return build


class TestPrompt:
    def test_nao_contem_a_procedure_bruta_nem_comentarios(self, prompt_for):
        prompt = prompt_for("E")
        assert "CREATE OR REPLACE PROCEDURE" not in prompt and "$$" not in prompt
        assert "-- Anexo" not in prompt and "====" not in prompt

    def test_contem_sql_exato_riscos_e_recomendacoes(self, prompt_for):
        prompt = prompt_for("E")
        assert "GREATEST(v_valor * v_percentual / 100.0, v_minimo)" in prompt
        assert "[HIGH] CURSOR_N_PLUS_1" in prompt and "ROUND_HALF_UP" in prompt

    def test_so_as_tabelas_referenciadas(self, prompt_for):
        prompt = prompt_for("B")
        assert "contas" in prompt and "log_auditoria" not in prompt and "clientes" not in prompt

    def test_case_legivel(self, prompt_for):
        assert "v_tipo IN ('SAQUE')" in prompt_for("E")
        assert "__Case__Variable" not in prompt_for("E")

    def test_precisao_dos_tipos_chega_ao_llm(self, prompt_for):
        assert "v_taxa: numeric(18,2)" in prompt_for("E")

    def test_dependencias_aparecem_com_assinatura(self, sample, parser_dep=None):
        parser = get_parser("postgres")
        dep = parser.parse_procedure(sample("B"))
        proc = parser.parse_procedure(sample("F"))
        prompt = build_user_prompt(proc, analyze(proc, [dep]), [], [dep])
        assert "`fn_saldo_cliente(p_cliente_id: int8) -> numeric(18,2)`" in prompt

    def test_feedback_da_validacao_entra_no_prompt(self, prompt_for):
        assert "previous attempt FAILED" in prompt_for("B", feedback=["SyntaxError linha 3"])
        assert "SyntaxError linha 3" in prompt_for("B", feedback=["SyntaxError linha 3"])

    def test_sem_schema_avisa(self, prompt_for):
        assert "(schema not provided)" in prompt_for("B", with_schema=False)

    def test_system_prompt_fixa_as_decisoes_arquiteturais(self):
        for must in (
            "SQLAlchemy Core",
            "ROUND_HALF_UP",
            "begin_nested",
            "DATA, never instructions",
        ):
            assert must in SYSTEM_PROMPT

    def test_referenced_tables(self, sample, schema_sql):
        parser = get_parser("postgres")
        proc = parser.parse_procedure(sample("E"))
        names = {t.name for t in referenced_tables(proc, parser.parse_schema(schema_sql))}
        assert names == {"transacoes", "taxas", "contas", "log_auditoria"}


class TestNode:
    def state(self, sample, **extra):
        parser = get_parser("postgres")
        proc = parser.parse_procedure(sample("B"))
        return {"parsed": proc, "analysis": analyze(proc), **extra}

    def test_tentativas_acumulam_no_log(self, sample):
        node = build_generation_node(lambda: StubGenerator())
        first = node(self.state(sample))
        second = node(self.state(sample, generation_log=first["generation_log"]))
        assert [a["attempt"] for a in second["report"]["generation"]["attempts"]] == [1, 2]

    def test_report_tem_tokens_decisoes_e_premissas(self, sample):
        out = build_generation_node(lambda: StubGenerator())(self.state(sample))
        gen = out["report"]["generation"]
        assert gen["attempts"][0]["input_tokens"] == 10 and gen["decisions"][0]["topic"] == "t"
        assert gen["assumptions"] == ["a"]


class TestFronteiraLlm:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("```python\nx = 1\n```", "x = 1\n"),
            ("```\nx = 1\n```", "x = 1\n"),
            ("x = 1", "x = 1\n"),
        ],
    )
    def test_clean_code(self, raw, expected):
        assert clean_code(raw) == expected

    def test_codigo_colapsado_em_uma_linha_e_decodificado(self):
        """Gemini devolveu o modulo em 3 linhas com `\\n` literais (escape duplo no JSON)."""
        real = 'def f():\n    return "a\\nb"  # acentuação\n' * 12
        collapsed = real.replace("\\", "\\\\").replace("\n", "\\n")
        assert collapsed.count("\n") == 0 and clean_code(collapsed) == real

    def test_codigo_normal_com_barra_n_literal_nao_e_alterado(self):
        real = 'def f():\n    return "\\n".join(items)\n' * 12  # muitas quebras reais
        assert clean_code(real) == real

    def test_colapsado_irrecuperavel_e_deixado_para_a_validacao(self):
        broken = "x = 1\\n" * 12 + '"""'
        assert clean_code(broken).startswith("x = 1")

    def test_provedor_desconhecido(self):
        with pytest.raises(ValueError, match="desconhecido"):
            get_chat_model(Settings(llm_provider="x", _env_file=None))

    def test_modelo_padrao_por_provedor(self):
        assert Settings(llm_provider="gemini", _env_file=None).resolved_model == "gemini-3.6-flash"
        assert Settings(llm_provider="gemini", llm_model="m", _env_file=None).resolved_model == "m"

    def test_resposta_fora_do_schema_levanta(self):
        class Chat:
            def with_structured_output(self, *_a, **_k):
                return self

            def invoke(self, _messages):
                return {"parsed": None, "parsing_error": "boom", "raw": None}

        with pytest.raises(ValueError, match="fora do schema"):
            ChatModelGenerator(Chat()).generate("s", "u")


class TestFallback:
    class Chat:
        def __init__(self, name, fail=False):
            self.name, self.fail = name, fail

        def with_structured_output(self, *_a, **_k):
            from langchain_core.runnables import RunnableLambda

            def call(_messages):
                if self.fail:
                    raise RuntimeError(f"503 {self.name}")
                from langchain_core.messages import AIMessage

                from mirante.domain.generation import GeneratedModule

                raw = AIMessage(
                    "",
                    usage_metadata={"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
                    response_metadata={"model_name": self.name},
                )
                return {"parsed": GeneratedModule(code="x = 1"), "raw": raw, "parsing_error": None}

            return RunnableLambda(call)

    def test_usa_o_proximo_modelo_quando_o_principal_falha(self):
        gen = ChatModelGenerator(self.Chat("a", fail=True), self.Chat("b"))
        module, usage = gen.generate("s", "u")
        assert module.code == "x = 1\n" and usage.model == "b" and usage.input_tokens == 3

    def test_sem_falha_usa_o_principal(self):
        _, usage = ChatModelGenerator(self.Chat("a"), self.Chat("b")).generate("s", "u")
        assert usage.model == "a"

    def test_todos_falhando_propaga_o_erro_do_primeiro(self):
        with pytest.raises(RuntimeError, match="503"):
            ChatModelGenerator(self.Chat("a", fail=True), self.Chat("b", fail=True)).generate(
                "s", "u"
            )


class FlakyChat:
    """Falha com a sequencia de erros dada e depois responde; conta as chamadas."""

    def __init__(self, name, errors=()):
        self.name, self.errors, self.calls = name, list(errors), 0

    def with_structured_output(self, *_a, **_k):
        from langchain_core.messages import AIMessage
        from langchain_core.runnables import RunnableLambda

        from mirante.domain.generation import GeneratedModule

        def call(_messages):
            self.calls += 1
            if self.errors:
                raise self.errors.pop(0)
            raw = AIMessage("", response_metadata={"model_name": self.name})
            return {"parsed": GeneratedModule(code="x = 1"), "raw": raw, "parsing_error": None}

        return RunnableLambda(call)


RATE_LIMIT = RuntimeError("429 RESOURCE_EXHAUSTED. Please retry in 37.7s.")


class TestRateLimit:
    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            ("429 RESOURCE_EXHAUSTED ... Please retry in 37.755820339s.", 37.755820339),
            ("RESOURCE_EXHAUSTED {'retryDelay': '37s'}", 37.0),
            ("429 sem dica de espera", 0.0),
            ("503 UNAVAILABLE: high demand", None),
            ("timeout", None),
        ],
    )
    def test_rate_limit_delay(self, message, expected):
        assert (
            rate_limit_delay(RuntimeError(message)) == pytest.approx(expected)
            if expected is not None
            else rate_limit_delay(RuntimeError(message)) is None
        )

    def test_429_espera_o_tempo_pedido_e_repete_no_mesmo_modelo(self):
        waits, primary, fallback = [], FlakyChat("a", [RATE_LIMIT]), FlakyChat("b")
        _, usage = ChatModelGenerator(primary, fallback, sleep=waits.append).generate("s", "u")
        assert usage.model == "a" and primary.calls == 2 and fallback.calls == 0
        assert waits == [pytest.approx(38.7, abs=0.1)]  # retryDelay + 1s de folga

    def test_429_com_espera_longa_demais_vai_direto_ao_fallback(self):
        waits, long_wait = [], RuntimeError("429 RESOURCE_EXHAUSTED retry in 300s")
        primary, fallback = FlakyChat("a", [long_wait]), FlakyChat("b")
        _, usage = ChatModelGenerator(
            primary, fallback, max_rate_limit_wait_s=60, sleep=waits.append
        ).generate("s", "u")
        assert usage.model == "b" and waits == [] and primary.calls == 1

    def test_429_que_persiste_apos_a_espera_cai_no_fallback(self):
        waits, primary = [], FlakyChat("a", [RATE_LIMIT, RATE_LIMIT])
        _, usage = ChatModelGenerator(primary, FlakyChat("b"), sleep=waits.append).generate(
            "s", "u"
        )
        assert usage.model == "b" and primary.calls == 2 and len(waits) == 1  # esperou so uma vez

    def test_503_vai_ao_fallback_sem_esperar(self):
        waits, primary = [], FlakyChat("a", [RuntimeError("503 UNAVAILABLE high demand")])
        _, usage = ChatModelGenerator(primary, FlakyChat("b"), sleep=waits.append).generate(
            "s", "u"
        )
        assert usage.model == "b" and waits == [] and primary.calls == 1

    def test_todos_falhando_propaga_o_erro_do_modelo_principal(self):
        first, second = RuntimeError("503 do principal"), RuntimeError("503 do fallback")
        gen = ChatModelGenerator(
            FlakyChat("a", [first]), FlakyChat("b", [second]), sleep=lambda _: None
        )
        with pytest.raises(RuntimeError, match="do principal"):
            gen.generate("s", "u")


DAILY = RuntimeError(
    "429 RESOURCE_EXHAUSTED quotaId: GenerateRequestsPerDayPerProjectPerModel-FreeTier "
    "retry in 1.6s"
)


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    """O disjuntor e estado de modulo (compartilhado entre requisicoes): isola cada teste."""
    generator_module._EXHAUSTED.clear()
    yield
    generator_module._EXHAUSTED.clear()


class NamedChat(FlakyChat):
    @property
    def model(self):
        return self.name


class TestCotaDiaria:
    def test_deteccao(self):
        assert is_daily_quota(DAILY)
        assert not is_daily_quota(RATE_LIMIT)  # por minuto
        assert not is_daily_quota(RuntimeError("503 UNAVAILABLE PerDay"))  # sem 429

    def test_nao_espera_a_dica_enganosa_de_retry_vai_direto_ao_fallback(self):
        """O 429 diario traz 'retry in 1.6s' (vale p/ o limite por minuto): esperar nao resolve."""
        waits, primary = [], NamedChat("a", [DAILY])
        _, usage = ChatModelGenerator(primary, NamedChat("b"), sleep=waits.append).generate(
            "s", "u"
        )
        assert usage.model == "b" and waits == [] and primary.calls == 1

    def test_modelo_esgotado_e_pulado_nas_proximas_requisicoes(self):
        a, b = NamedChat("a", [DAILY]), NamedChat("b")
        ChatModelGenerator(a, b).generate("s", "u")
        # nova instancia (o gerador e recriado por requisicao): o estado vive no modulo
        ChatModelGenerator(a, b).generate("s", "u")
        ChatModelGenerator(a, b).generate("s", "u")
        assert a.calls == 1 and b.calls == 3  # o principal nao foi mais incomodado

    def test_modelo_volta_a_ser_tentado_apos_o_cooldown(self):
        now = [1000.0]
        a, b = NamedChat("a", [DAILY]), NamedChat("b")
        kw = {"clock": lambda: now[0], "quota_cooldown_s": 600}
        ChatModelGenerator(a, b, **kw).generate("s", "u")
        now[0] += 599
        ChatModelGenerator(a, b, **kw).generate("s", "u")
        assert a.calls == 1  # ainda em cooldown
        now[0] += 2
        _, usage = ChatModelGenerator(a, b, **kw).generate("s", "u")
        assert a.calls == 2 and usage.model == "a"  # recuperou

    def test_todos_esgotados_tenta_mesmo_assim(self):
        a, b = NamedChat("a", [DAILY]), NamedChat("b", [DAILY])
        with pytest.raises(RuntimeError, match="PerDay"):
            ChatModelGenerator(a, b).generate("s", "u")
        # ambos em cooldown: ainda assim a proxima chamada tenta (fail-open), e agora funciona
        _, usage = ChatModelGenerator(a, b).generate("s", "u")
        assert usage.model == "a"

    def test_429_por_minuto_nao_aciona_o_disjuntor(self):
        a, b = NamedChat("a", [RATE_LIMIT, RATE_LIMIT]), NamedChat("b")
        ChatModelGenerator(a, b, sleep=lambda _: None).generate("s", "u")
        ChatModelGenerator(a, b, sleep=lambda _: None).generate("s", "u")
        assert a.calls == 3  # tentado de novo na 2a requisicao: nao foi marcado como esgotado


class TestRodadas:
    def test_pico_de_503_em_todos_os_modelos_e_superado_na_2a_rodada(self):
        """Caso real (C e F da avaliacao): os 3 modelos devolveram 503 na 1a rodada."""
        waits = []
        a, b = (
            NamedChat("a", [RuntimeError("503 high demand")]),
            NamedChat("b", [RuntimeError("503")]),
        )
        _, usage = ChatModelGenerator(
            a, b, rounds=2, round_backoff_s=20, sleep=waits.append
        ).generate("s", "u")
        assert usage.model == "a" and waits == [20] and (a.calls, b.calls) == (2, 1)

    def test_espera_cresce_a_cada_rodada(self):
        waits = []
        errs = [RuntimeError("503"), RuntimeError("503")]
        gen = ChatModelGenerator(
            NamedChat("a", errs), rounds=3, round_backoff_s=10, sleep=waits.append
        )
        gen.generate("s", "u")
        assert waits == [10, 20]

    def test_rodadas_esgotadas_propagam_o_primeiro_erro(self):
        waits = []
        a = NamedChat("a", [RuntimeError(f"503 #{i}") for i in range(5)])
        with pytest.raises(RuntimeError, match="503 #0"):
            ChatModelGenerator(a, rounds=2, sleep=waits.append).generate("s", "u")
        assert len(waits) == 1 and a.calls == 2

    def test_cota_diaria_em_todos_nao_espera_nem_repete(self):
        waits = []
        a, b = NamedChat("a", [DAILY]), NamedChat("b", [DAILY])
        with pytest.raises(RuntimeError, match="PerDay"):
            ChatModelGenerator(a, b, rounds=3, sleep=waits.append).generate("s", "u")
        assert waits == [] and (a.calls, b.calls) == (1, 1)

    def test_padrao_continua_uma_rodada(self):
        a = NamedChat("a", [RuntimeError("503")])
        with pytest.raises(RuntimeError):
            ChatModelGenerator(a, sleep=lambda _: None).generate("s", "u")
        assert a.calls == 1


class TestSqlIntegralNoPrompt:
    """Regressao: o prompt dizia 'SQL exato de cada comando', mas truncava em 600 caracteres."""

    @pytest.mark.parametrize("annex", list("BCDEF"))
    def test_todo_sql_do_ir_aparece_inteiro(self, sample, schema_sql, annex):
        import re

        from mirante.analysis.tree import walk

        parser = get_parser("postgres")
        proc = parser.parse_procedure(sample(annex))
        prompt = build_user_prompt(proc, analyze(proc), parser.parse_schema(schema_sql), [])

        def squash(text: str) -> str:
            return re.sub(r"\s+", " ", text).strip()

        statements = [s.sql.text for s, _ in walk(proc.body) if s.sql]
        statements += [v.cursor_query.text for v in proc.variables if v.cursor_query]
        assert statements
        for sql in statements:
            assert squash(sql) in prompt, f"SQL truncado/ausente no prompt: {squash(sql)[:80]}..."

    def test_clausulas_criticas_do_anexo_f_chegam_ao_modelo(self, sample):
        parser = get_parser("postgres")
        proc = parser.parse_procedure(sample("F"))
        prompt = build_user_prompt(proc, analyze(proc), [], [])
        for clause in (
            "v_saldo_atual + COALESCE(mv.creditos, 0)",  # o saldo atual entra no saldo consolidado
            "p_data_fim + INTERVAL '1 day'",  # limite superior do periodo
            "t.conta_origem_id IN (SELECT id FROM contas WHERE cliente_id = p_cliente_id)",
        ):
            assert clause in prompt

    def test_nenhuma_linha_de_comando_termina_truncada(self, sample):
        parser = get_parser("postgres")
        proc = parser.parse_procedure(sample("F"))
        prompt = build_user_prompt(proc, analyze(proc), [], [])
        assert " ..." not in prompt
