from dataclasses import dataclass
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
import logging
from typing import List, Optional
from sqlalchemy import text
from sqlalchemy.engine import Connection
from fn_saldo_cliente import fn_saldo_cliente

logger = logging.getLogger(__name__)

class ProcedureError(Exception):
    pass

@dataclass(frozen=True)
class RelatorioMensalClienteRow:
    mes_referencia: date
    total_creditos: Decimal
    total_debitos: Decimal
    saldo_consolidado: Decimal
    qtd_transacoes: int

def _format_val(val: object) -> str:
    if val is None:
        return '<NULL>'
    if isinstance(val, Decimal):
        return str(val)
    return str(val)

def sp_relatorio_mensal_cliente(conn: Connection, p_cliente_id: Optional[int], p_data_inicio: Optional[date], p_data_fim: Optional[date]) -> List[RelatorioMensalClienteRow]:
    v_saldo_atual: Decimal = Decimal('0.00')
    try:
        with conn.begin_nested():
            if p_data_inicio is not None and p_data_fim is not None and p_data_inicio > p_data_fim:
                msg = f"Periodo invalido: inicio {_format_val(p_data_inicio)} > fim {_format_val(p_data_fim)}"
                raise ProcedureError(msg)
            
            v_saldo_atual = fn_saldo_cliente(conn, p_cliente_id)
            if v_saldo_atual is None:
                v_saldo_atual = Decimal('0.00')
            v_saldo_atual = v_saldo_atual.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
            
            notice_msg = f"Saldo atual do cliente {_format_val(p_cliente_id)}: {_format_val(v_saldo_atual)}"
            logger.info(notice_msg)
            
            sql = text(
                "WITH RECURSIVE meses AS (" \
                "    SELECT DATE_TRUNC('month', CAST(:p_data_inicio AS date))::DATE AS mes " \
                "    UNION ALL " \
                "    SELECT (mes + INTERVAL '1 month')::DATE FROM meses WHERE mes < DATE_TRUNC('month', CAST(:p_data_fim AS date)) " \
                "), movimento AS (" \
                "    SELECT DATE_TRUNC('month', t.data_transacao)::DATE AS mes, " \
                "           SUM(CASE WHEN t.conta_destino_id IN ( SELECT id FROM contas WHERE cliente_id = CAST(:p_cliente_id AS int8) ) THEN t.valor ELSE 0 END) AS creditos, " \
                "           SUM(CASE WHEN t.conta_origem_id IN ( SELECT id FROM contas WHERE cliente_id = CAST(:p_cliente_id AS int8) ) THEN t.valor ELSE 0 END) AS debitos, " \
                "           COUNT(*) AS qtd " \
                "    FROM transacoes t " \
                "    WHERE t.status = 'EFETIVADA' " \
                "      AND t.data_transacao >= CAST(:p_data_inicio AS date) " \
                "      AND t.data_transacao < CAST(:p_data_fim AS date) + INTERVAL '1 day' " \
                "      AND (t.conta_origem_id IN (SELECT id FROM contas WHERE cliente_id = CAST(:p_cliente_id AS int8)) " \
                "           OR t.conta_destino_id IN (SELECT id FROM contas WHERE cliente_id = CAST(:p_cliente_id AS int8))) " \
                "    GROUP BY 1 " \
                ") " \
                "SELECT m.mes AS mes_referencia, " \
                "       COALESCE(mv.creditos, 0) AS total_creditos, " \
                "       COALESCE(mv.debitos, 0) AS total_debitos, " \
                "       CAST(:v_saldo_atual AS numeric) + COALESCE(mv.creditos, 0) - COALESCE(mv.debitos, 0) AS saldo_consolidado, " \
                "       COALESCE(mv.qtd, 0)::INT AS qtd_transacoes " \
                "FROM meses m " \
                "LEFT JOIN movimento mv ON mv.mes = m.mes " \
                "ORDER BY m.mes"
            )
            
            result = conn.execute(sql, {
                'p_data_inicio': p_data_inicio,
                'p_data_fim': p_data_fim,
                'p_cliente_id': p_cliente_id,
                'v_saldo_atual': v_saldo_atual
            })
            
            rows: List[RelatorioMensalClienteRow] = []
            for r in result:
                tc = Decimal(str(r.total_creditos or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                td = Decimal(str(r.total_debitos or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                sc = Decimal(str(r.saldo_consolidado or 0)).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
                qt = int(r.qtd_transacoes or 0)
                rows.append(RelatorioMensalClienteRow(
                    mes_referencia=r.mes_referencia,
                    total_creditos=tc,
                    total_debitos=td,
                    saldo_consolidado=sc,
                    qtd_transacoes=qt
                ))
            return rows
            
    except Exception as e:
        warning_msg = f"Falha ao gerar relatorio: {e}. Retornando linha de fallback."
        logger.warning(warning_msg)
        
        fallback_mes = p_data_inicio
        if fallback_mes is not None:
            # emulate date_trunc('month', p_data_inicio)
            fallback_mes = date(fallback_mes.year, fallback_mes.month, 1)
        
        safe_saldo = v_saldo_atual if v_saldo_atual is not None else Decimal('0.00')
        safe_saldo = safe_saldo.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)
        
        return [
            RelatorioMensalClienteRow(
                mes_referencia=fallback_mes,
                total_creditos=Decimal('0.00').quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                total_debitos=Decimal('0.00').quantize(Decimal('0.01'), rounding=ROUND_HALF_UP),
                saldo_consolidado=safe_saldo,
                qtd_transacoes=0
            )
        ]
