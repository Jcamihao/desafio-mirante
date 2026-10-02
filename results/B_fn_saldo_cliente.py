from decimal import Decimal, ROUND_HALF_UP
from sqlalchemy import Connection, text

def fn_saldo_cliente(conn: Connection, p_cliente_id: int) -> Decimal:
    """
    Calcula o saldo total ativo de um cliente.
    """
    stmt = text(
        "SELECT COALESCE(SUM(saldo), 0) FROM contas WHERE cliente_id = :p_cliente_id AND status = 'ATIVA'"
    )
    result = conn.execute(stmt, {"p_cliente_id": p_cliente_id}).scalar()
    
    v_total = Decimal(str(result)) if result is not None else Decimal("0.00")
    return v_total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
