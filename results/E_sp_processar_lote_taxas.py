from decimal import Decimal, ROUND_HALF_UP
from datetime import date
from sqlalchemy import Connection, text
import logging

logger = logging.getLogger(__name__)

class ProcedureError(Exception):
    pass

def sp_processar_lote_taxas(conn: Connection, p_data_referencia: date) -> None:
    """Processa taxas para transacoes de um lote diario."""
    
    # L20: Snapshot do cursor via query unica
    query_transacoes = text("""
        SELECT id, conta_origem_id, tipo, valor 
        FROM transacoes 
        WHERE data_transacao >= :data_inicio AND data_transacao < :data_fim 
        AND status = 'EFETIVADA' AND tipo <> 'TARIFA'
        ORDER BY id ASC
    """)
    
    transacoes = conn.execute(query_transacoes, {
        "data_inicio": p_data_referencia,
        "data_fim": p_data_referencia + date.resolution
    }).fetchall()
    
    v_total_taxas = Decimal("0.00")
    v_count = 0
    
    for row in transacoes:
        v_id, v_origem, v_tipo, v_valor = row.id, row.conta_origem_id, row.tipo, row.valor
        
        # L26: Lookup de taxas
        taxa_row = conn.execute(text("""
            SELECT percentual, valor_minimo FROM taxas 
            WHERE tipo_operacao = :tipo 
            AND vigente_de <= :data 
            AND (vigente_ate IS NULL OR vigente_ate >= :data) 
            ORDER BY vigente_de DESC LIMIT 1
        """), {"tipo": v_tipo, "data": p_data_referencia}).fetchone()
        
        if not taxa_row:
            continue
            
        v_percentual, v_minimo = taxa_row.percentual, taxa_row.valor_minimo
        
        # L39: Calculo com arredondamento ROUND_HALF_UP
        v_taxa = (v_valor * v_percentual / Decimal("100.0000")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        v_taxa = max(v_taxa, v_minimo).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        
        # L41: Logica de ajuste
        if v_tipo in ('TRANSFERENCIA', 'SAQUE'):
            if v_tipo == 'SAQUE':
                v_taxa = (v_taxa * Decimal("1.10")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        else:
            v_taxa = (v_taxa * Decimal("0.90")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            
        if v_origem is not None:
            conn.execute(text("UPDATE contas SET saldo = saldo - :taxa WHERE id = :origem"), 
                         {"taxa": v_taxa, "origem": v_origem})
            
            conn.execute(text("INSERT INTO transacoes (conta_origem_id, tipo, valor, status) VALUES (:origem, 'TARIFA', :taxa, 'EFETIVADA')"),
                         {"origem": v_origem, "taxa": v_taxa})
            
            conn.execute(text("""
                INSERT INTO log_auditoria (entidade, entidade_id, acao, detalhes) 
                VALUES ('transacoes', :id, 'TARIFA_APLICADA', 
                jsonb_build_object('transacao_origem', CAST(:id AS text), 'tipo_origem', CAST(:tipo AS text), 
                                   'valor_origem', CAST(:valor AS numeric), 'percentual', CAST(:perc AS numeric), 
                                   'taxa_aplicada', CAST(:taxa AS numeric)))
            """), {"id": v_id, "tipo": v_tipo, "valor": v_valor, "perc": v_percentual, "taxa": v_taxa})
            
            v_total_taxas = (v_total_taxas + v_taxa).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            v_count += 1
            
    conn.execute(text("""
        INSERT INTO log_auditoria (entidade, acao, detalhes) 
        VALUES ('lote_taxas', 'LOTE_PROCESSADO', 
        jsonb_build_object('data_referencia', CAST(:data AS text), 'transacoes', CAST(:count AS numeric), 
                           'total_taxas', CAST(:total AS numeric)))
    """), {"data": str(p_data_referencia), "count": v_count, "total": v_total_taxas})
