from dataclasses import dataclass
import logging
from sqlalchemy import Connection, text

logger = logging.getLogger(__name__)

class ProcedureError(Exception):
    """Exceção lançada para erros na procedure."""
    pass

@dataclass(frozen=True)
class SpAtualizarStatusContasInativasResult:
    p_afetadas: int

def sp_atualizar_status_contas_inativas(
    conn: Connection, p_dias: int | None
) -> SpAtualizarStatusContasInativasResult:
    # L3: Validação do parâmetro p_dias
    if p_dias is None or p_dias <= 0:
        p_dias_str = "<NULL>" if p_dias is None else str(p_dias)
        raise ProcedureError(f"Parametro p_dias deve ser positivo, recebido: {p_dias_str}")

    # L7: Executa o UPDATE para inativar as contas sem movimentação recente
    update_stmt = text("""
        UPDATE contas c 
        SET status = 'INATIVA' 
        WHERE c.status = 'ATIVA' 
          AND NOT EXISTS (
            SELECT 1 
            FROM transacoes t 
            WHERE (t.conta_origem_id = c.id OR t.conta_destino_id = c.id) 
              AND t.data_transacao >= NOW() - (CAST(:p_dias AS text) || ' days')::INTERVAL
          )
    """)
    result = conn.execute(update_stmt, {"p_dias": p_dias})
    
    # L17: GET DIAGNOSTICS p_afetadas = ROW_COUNT
    p_afetadas = result.rowcount

    # L19: Insere o log de auditoria com os detalhes em JSONB
    insert_stmt = text("""
        INSERT INTO log_auditoria (entidade, acao, detalhes) 
        VALUES (
            'contas',
            'INATIVACAO_LOTE',
            jsonb_build_object('dias', CAST(:p_dias AS integer), 'afetadas', CAST(:p_afetadas AS integer))
        )
    """)
    conn.execute(insert_stmt, {"p_dias": p_dias, "p_afetadas": p_afetadas})

    return SpAtualizarStatusContasInativasResult(p_afetadas=p_afetadas)
