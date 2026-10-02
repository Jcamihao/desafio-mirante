from mirante.domain.generation import Decision, GeneratedModule, LlmUsage

GOOD_B = """\
from decimal import Decimal

from sqlalchemy import Connection, text


def fn_saldo_cliente(conn: Connection, p_cliente_id: int) -> Decimal:
    sql = text("SELECT COALESCE(SUM(saldo), 0) FROM contas WHERE cliente_id = :c")
    return conn.execute(sql, {"c": p_cliente_id}).scalar_one()
"""

GOOD_D = """\
from decimal import Decimal

from sqlalchemy import Connection, text


def sp_transferir_entre_contas(
    conn: Connection, p_conta_origem: int, p_conta_destino: int, p_valor: Decimal
) -> None:
    sql = text("UPDATE contas SET saldo = saldo - :v WHERE id = :o")
    conn.execute(sql, {"v": p_valor, "o": p_conta_origem})
"""

# Passa em ast.parse e no ruff, mas quebra na importacao (caso real do Anexo F gerado pelo Gemini).
# O nome inexistente precisa ser USADO: import nao usado e removido pelo autofix.
BAD_IMPORT_B = (
    GOOD_B.replace(
        "from decimal import Decimal", "from dataclasses import frozen\nfrom decimal import Decimal"
    )
    .replace("return conn", "return frozen(conn")
    .replace(".scalar_one()", ".scalar_one())")
)

STUB_CODE = GOOD_B


class StubGenerator:
    """Substitui o LLM nos testes: sem rede, sem chave, sem custo, deterministico.

    `codes` e consumido em ordem (o ultimo se repete): permite simular "errou, depois acertou".
    """

    def __init__(self, *codes: str, fail_with: Exception | None = None) -> None:
        self.codes = list(codes) or [STUB_CODE]
        self.fail_with = fail_with
        self.calls: list[tuple[str, str]] = []

    def generate(self, system: str, user: str) -> tuple[GeneratedModule, LlmUsage]:
        self.calls.append((system, user))
        if self.fail_with:
            raise self.fail_with
        code = self.codes[min(len(self.calls) - 1, len(self.codes) - 1)]
        module = GeneratedModule(
            code=code,
            decisions=[Decision(topic="t", choice="c", rationale="r")],
            assumptions=["a"],
        )
        return module, LlmUsage(input_tokens=10, output_tokens=5, model="stub")


class InMemoryRepository:
    """Repositorio falso para testes unitarios do grafo: sem banco, sem efeitos colaterais."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.records: list = []
        self.fail_with = fail_with

    def save(self, record):
        if self.fail_with:
            raise self.fail_with
        self.records.append(record)
        return record.id

    def get(self, record_id):
        return next((r for r in self.records if r.id == record_id), None)

    def list(self, limit: int = 50, status=None):
        return [r for r in reversed(self.records) if status is None or r.status == status][:limit]


GOOD_F = """\
from fn_saldo_cliente import fn_saldo_cliente
from sqlalchemy import Connection


def sp_relatorio_mensal_cliente(
    conn: Connection, p_cliente_id: int, p_data_inicio, p_data_fim
) -> list:
    return [fn_saldo_cliente(conn, p_cliente_id)]
"""


GOOD_C = """\
from sqlalchemy import Connection


def sp_atualizar_status_contas_inativas(conn: Connection, p_dias: int) -> int:
    return 0
"""

GOOD_E = """\
from datetime import date

from sqlalchemy import Connection


def sp_processar_lote_taxas(conn: Connection, p_data_referencia: date) -> None:
    return None
"""

GOOD_BY_PROCEDURE = {
    "fn_saldo_cliente": GOOD_B,
    "sp_atualizar_status_contas_inativas": GOOD_C,
    "sp_transferir_entre_contas": GOOD_D,
    "sp_processar_lote_taxas": GOOD_E,
    "sp_relatorio_mensal_cliente": GOOD_F,
}


class RoutedStub:
    """Escolhe o codigo pela procedure do prompt: permite "errou e depois acertou" por caso."""

    def __init__(self, overrides: dict[str, list[str]] | None = None) -> None:
        self.overrides = overrides or {}
        self.seen: dict[str, int] = {}
        self.calls: list[tuple[str, str]] = []

    def generate(self, system: str, user: str) -> tuple[GeneratedModule, LlmUsage]:
        self.calls.append((system, user))
        name = next(n for n in GOOD_BY_PROCEDURE if f"# Procedure `{n}`" in user)
        sequence = self.overrides.get(name) or [GOOD_BY_PROCEDURE[name]]
        index = self.seen.get(name, 0)
        self.seen[name] = index + 1
        code = sequence[min(index, len(sequence) - 1)]
        return GeneratedModule(code=code), LlmUsage(input_tokens=10, output_tokens=5, model="stub")


class InMemoryEvaluationRepository:
    def __init__(self, fail_with: Exception | None = None) -> None:
        self.runs: list = []
        self.fail_with = fail_with

    def save(self, run):
        if self.fail_with:
            raise self.fail_with
        self.runs.append(run)
        return run.id

    def get(self, run_id):
        return next((r for r in self.runs if r.id == run_id), None)

    def list(self, limit: int = 20):
        return list(reversed(self.runs))[:limit]


class RecordingTracer:
    """Tracer falso que so registra os scores agregados de sessao."""

    def __init__(self):
        self.aggregates: list[tuple[str, dict]] = []
        self.sessions: list[str | None] = []

    def start(self, request, session_id=None):
        from mirante.observability import NoopRunTrace

        self.sessions.append(session_id)
        return NoopRunTrace()

    def record_aggregate(self, session_id, scores):
        self.aggregates.append((session_id, scores))
