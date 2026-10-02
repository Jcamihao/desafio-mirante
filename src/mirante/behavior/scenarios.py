"""Cenarios de equivalencia comportamental: mesmos dados, mesma chamada, original x gerado.

Cada cenario mira uma armadilha do enunciado (ver docs/decisions.md). Datas fixas (2026-...) onde o
resultado depende delas; `now() - interval` so onde a procedure usa NOW() (Anexo C).
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any


@dataclass(frozen=True)
class Scenario:
    annex: str
    name: str
    procedure: str
    call_sql: str  # SQL da chamada ORIGINAL (placeholders %s)
    args: tuple[Any, ...] = ()  # os mesmos argumentos vao ao Python gerado
    seed_sql: str = ""
    targets: str = field(default="", compare=False)  # armadilha que o cenario exercita


BASE = """
INSERT INTO clientes (nome, cpf) VALUES ('Ana', '11111111111'), ('Bia', '22222222222');
INSERT INTO contas (cliente_id, agencia, numero, tipo, saldo, status) VALUES
  (1, '001', '1001', 'CORRENTE', 100.00, 'ATIVA'),
  (1, '001', '1002', 'POUPANCA',  50.00, 'ATIVA'),
  (2, '001', '2001', 'CORRENTE',  30.00, 'ATIVA'),
  (2, '001', '2002', 'CORRENTE', 500.00, 'INATIVA');
"""

C_SEED = (
    BASE
    + """
INSERT INTO transacoes (conta_origem_id, conta_destino_id, tipo, valor, data_transacao) VALUES
  (1, NULL, 'SAQUE',    10.00, now() - interval '5 days'),
  (NULL, 3, 'DEPOSITO', 10.00, now() - interval '40 days');
"""
)

E_TAXAS = """
INSERT INTO taxas (tipo_operacao, percentual, valor_minimo, vigente_de, vigente_ate) VALUES
  ('TRANSFERENCIA', 2.5, 0.00, '2020-01-01', NULL),
  ('SAQUE',         2.0, 1.00, '2020-01-01', NULL),
  ('TRANSFERENCIA', 9.0, 9.00, '2019-01-01', '2019-12-31');
"""
E_TRANSACOES = """
INSERT INTO transacoes
  (conta_origem_id, conta_destino_id, tipo, valor, data_transacao, status) VALUES
  (1, 3,    'TRANSFERENCIA', 40.20, '2026-03-10 12:00', 'EFETIVADA'),
  (1, NULL, 'SAQUE',         50.00, '2026-03-10 13:00', 'EFETIVADA'),
  (NULL, 1, 'DEPOSITO',     300.00, '2026-03-10 14:00', 'EFETIVADA'),
  (3, 1,    'TRANSFERENCIA', 10.00, '2026-03-10 15:00', 'EFETIVADA'),
  (1, 3,    'TRANSFERENCIA', 99.00, '2026-03-11 09:00', 'EFETIVADA'),
  (1, 3,    'TRANSFERENCIA', 77.00, '2026-03-10 16:00', 'CANCELADA');
"""

F_SEED = (
    BASE
    + """
INSERT INTO transacoes (conta_origem_id, conta_destino_id, tipo, valor, data_transacao) VALUES
  (1, 3,    'TRANSFERENCIA',  40.00, '2026-01-20 10:00'),
  (NULL, 2, 'DEPOSITO',      100.00, '2026-01-25 10:00'),
  (2, NULL, 'SAQUE',          15.00, '2026-03-02 10:00');
"""
)

TRANSFER = "CALL sp_transferir_entre_contas(%s, %s, %s)"
D = "sp_transferir_entre_contas"
LOTE = "CALL sp_processar_lote_taxas(%s)"
REL = "SELECT * FROM sp_relatorio_mensal_cliente(%s, %s, %s)"
MARCH_10 = date(2026, 3, 10)

SCENARIOS: list[Scenario] = [
    # --- B: funcao escalar ---------------------------------------------------------------------
    Scenario(
        "B",
        "soma so as contas ativas",
        "fn_saldo_cliente",
        "SELECT fn_saldo_cliente(%s)",
        (1,),
        BASE,
    ),
    Scenario(
        "B", "ignora conta inativa", "fn_saldo_cliente", "SELECT fn_saldo_cliente(%s)", (2,), BASE
    ),
    Scenario(
        "B",
        "cliente inexistente retorna 0",
        "fn_saldo_cliente",
        "SELECT fn_saldo_cliente(%s)",
        (999,),
        BASE,
    ),
    # --- C: procedure com OUT + GET DIAGNOSTICS -------------------------------------------------
    Scenario(
        "C",
        "inativa contas sem movimento ha 30 dias",
        "sp_atualizar_status_contas_inativas",
        "CALL sp_atualizar_status_contas_inativas(%s, NULL)",
        (30,),
        C_SEED,
        "OUT params, ROW_COUNT",
    ),
    Scenario(
        "C",
        "janela grande inativa menos contas",
        "sp_atualizar_status_contas_inativas",
        "CALL sp_atualizar_status_contas_inativas(%s, NULL)",
        (100,),
        C_SEED,
    ),
    Scenario(
        "C",
        "p_dias zero levanta erro",
        "sp_atualizar_status_contas_inativas",
        "CALL sp_atualizar_status_contas_inativas(%s, NULL)",
        (0,),
        C_SEED,
        "RAISE com %",
    ),
    Scenario(
        "C",
        "p_dias NULL: mensagem mostra <NULL>",
        "sp_atualizar_status_contas_inativas",
        "CALL sp_atualizar_status_contas_inativas(%s, NULL)",
        (None,),
        C_SEED,
        "NULL em RAISE",
    ),
    # --- D: transacao, locks, validacoes, handler -----------------------------------------------
    Scenario("D", "transferencia valida", D, TRANSFER, (1, 2, Decimal("25.00")), BASE),
    Scenario(
        "D",
        "saldo insuficiente (log de erro nao persiste)",
        D,
        TRANSFER,
        (1, 2, Decimal("500.00")),
        BASE,
        "achado 2: handler grava e re-lanca",
    ),
    Scenario(
        "D",
        "destino inexistente: NULL <> 'ATIVA' nao dispara",
        D,
        TRANSFER,
        (1, 999, Decimal("10.00")),
        BASE,
        "achado 3: logica de 3 valores; FK violada depois",
    ),
    Scenario("D", "origem inexistente", D, TRANSFER, (999, 2, Decimal("10.00")), BASE),
    Scenario("D", "destino inativo", D, TRANSFER, (1, 4, Decimal("10.00")), BASE),
    Scenario("D", "origem igual ao destino", D, TRANSFER, (1, 1, Decimal("10.00")), BASE),
    Scenario("D", "valor zero", D, TRANSFER, (1, 2, Decimal("0.00")), BASE),
    Scenario("D", "valor NULL", D, TRANSFER, (1, 2, None), BASE, "NULL em RAISE"),
    # --- E: cursor, CASE, arredondamento, CHECK --------------------------------------------------
    Scenario(
        "E",
        "lote com empate de arredondamento (40,20 x 2,5% = 1,005)",
        "sp_processar_lote_taxas",
        LOTE,
        (MARCH_10,),
        BASE + E_TAXAS + E_TRANSACOES,
        "achado 4: half-up x half-even; N+1",
    ),
    Scenario(
        "E",
        "taxa zero viola CHECK e aborta o lote",
        "sp_processar_lote_taxas",
        LOTE,
        (MARCH_10,),
        BASE + "INSERT INTO taxas (tipo_operacao, percentual, valor_minimo, vigente_de) "
        "VALUES ('TRANSFERENCIA', 0, 0, '2020-01-01');" + E_TRANSACOES,
        "achado 5",
    ),
    Scenario(
        "E",
        "data sem transacoes so registra o lote",
        "sp_processar_lote_taxas",
        LOTE,
        (date(2030, 1, 1),),
        BASE + E_TAXAS + E_TRANSACOES,
    ),
    # --- F: CTE recursiva, funcao aninhada, fallback --------------------------------------------
    Scenario(
        "F",
        "periodo de varios meses (incl. mes sem movimento)",
        "sp_relatorio_mensal_cliente",
        REL,
        (1, date(2026, 1, 15), date(2026, 3, 20)),
        F_SEED,
        "CTE recursiva, SETOF",
    ),
    Scenario(
        "F",
        "periodo invertido cai no fallback, sem erro",
        "sp_relatorio_mensal_cliente",
        REL,
        (1, date(2026, 3, 1), date(2026, 1, 1)),
        F_SEED,
        "achado 1: RAISE capturado pelo proprio handler",
    ),
    Scenario(
        "F",
        "cliente sem contas",
        "sp_relatorio_mensal_cliente",
        REL,
        (999, date(2026, 1, 1), date(2026, 2, 28)),
        F_SEED,
    ),
    Scenario(
        "F",
        "um unico mes",
        "sp_relatorio_mensal_cliente",
        REL,
        (1, date(2026, 1, 1), date(2026, 1, 31)),
        F_SEED,
    ),
]
