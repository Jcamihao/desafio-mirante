import logging
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
from sqlalchemy import Connection, text

logger = logging.getLogger(__name__)

class ProcedureError(Exception):
    """Exceção lançada para erros de validação de negócio na procedure."""
    pass

def _fmt(val) -> str:
    """Formata valores para mensagens de erro seguindo o padrão do PostgreSQL."""
    return "<NULL>" if val is None else str(val)

def sp_transferir_entre_contas(
    conn: Connection,
    p_conta_origem: int,
    p_conta_destino: int,
    p_valor: Decimal
) -> None:
    """
    Transfere valores entre duas contas, realizando validações de saldo e status,
    e registrando a transação e logs de auditoria.
    """
    # L6: Inicialização e quantização do valor decimal
    if p_valor is not None:
        p_valor = p_valor.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    # L6: Envolvemos toda a execução em um savepoint externo para garantir que,
    # se a exceção propagar para fora da procedure, todas as escritas (inclusive as do handler)
    # sejam desfeitas, reproduzindo fielmente o comportamento do PostgreSQL.
    with conn.begin_nested():
        try:
            # Bloco interno representando a subtransação do bloco BEGIN ... EXCEPTION
            with conn.begin_nested():
                # L7: Validação do valor
                if p_valor is None or p_valor <= 0:
                    raise ProcedureError(f"Valor invalido para transferencia: {_fmt(p_valor)}")

                # L11: Validação de contas iguais
                if p_conta_origem == p_conta_destino:
                    raise ProcedureError("Conta de origem e destino nao podem ser iguais")

                v_saldo_origem: Optional[Decimal] = None
                v_status_origem: Optional[str] = None
                v_status_destino: Optional[str] = None

                # L15 & L18: Bloqueio determinístico de linhas para evitar deadlocks (ROW_LOCK)
                if p_conta_origem < p_conta_destino:
                    res_origem = conn.execute(
                        text("SELECT saldo, status FROM contas WHERE id = :p_conta_origem FOR UPDATE"),
                        {"p_conta_origem": p_conta_origem}
                    ).fetchone()
                    if res_origem:
                        v_saldo_origem = res_origem[0]
                        v_status_origem = res_origem[1]

                    res_destino = conn.execute(
                        text("SELECT status FROM contas WHERE id = :p_conta_destino FOR UPDATE"),
                        {"p_conta_destino": p_conta_destino}
                    ).fetchone()
                    if res_destino:
                        v_status_destino = res_destino[0]
                else:
                    res_destino = conn.execute(
                        text("SELECT status FROM contas WHERE id = :p_conta_destino FOR UPDATE"),
                        {"p_conta_destino": p_conta_destino}
                    ).fetchone()
                    if res_destino:
                        v_status_destino = res_destino[0]

                    res_origem = conn.execute(
                        text("SELECT saldo, status FROM contas WHERE id = :p_conta_origem FOR UPDATE"),
                        {"p_conta_origem": p_conta_origem}
                    ).fetchone()
                    if res_origem:
                        v_saldo_origem = res_origem[0]
                        v_status_origem = res_origem[1]

                # L21: Validação de existência da conta de origem
                if v_saldo_origem is None:
                    raise ProcedureError(f"Conta de origem {_fmt(p_conta_origem)} nao encontrada")

                # L25: Validação de status ativo (preservando lógica de três valores do SQL NULL)
                if (v_status_origem != 'ATIVA') or (v_status_destino is not None and v_status_destino != 'ATIVA'):
                    raise ProcedureError("Ambas as contas precisam estar ATIVAS")

                # L29: Validação de saldo suficiente
                if v_saldo_origem < p_valor:
                    raise ProcedureError(f"Saldo insuficiente: saldo={_fmt(v_saldo_origem)} valor={_fmt(p_valor)}")

                # L33: Atualização do saldo da conta de origem
                conn.execute(
                    text("UPDATE contas SET saldo = saldo - :p_valor WHERE id = :p_conta_origem"),
                    {"p_valor": p_valor, "p_conta_origem": p_conta_origem}
                )

                # L34: Atualização do saldo da conta de destino
                conn.execute(
                    text("UPDATE contas SET saldo = saldo + :p_valor WHERE id = :p_conta_destino"),
                    {"p_valor": p_valor, "p_conta_destino": p_conta_destino}
                )

                # L36: Inserção da transação
                conn.execute(
                    text("""
                        INSERT INTO transacoes (conta_origem_id, conta_destino_id, tipo, valor)
                        VALUES (:p_conta_origem, :p_conta_destino, 'TRANSFERENCIA', :p_valor)
                    """),
                    {"p_conta_origem": p_conta_origem, "p_conta_destino": p_conta_destino, "p_valor": p_valor}
                )

                # L39: Registro de auditoria de sucesso
                conn.execute(
                    text("""
                        INSERT INTO log_auditoria (entidade, entidade_id, acao, detalhes)
                        VALUES (
                            'transacoes',
                            NULL,
                            'TRANSFERENCIA_OK',
                            jsonb_build_object(
                                'origem', CAST(:p_conta_origem AS int8),
                                'destino', CAST(:p_conta_destino AS int8),
                                'valor', CAST(:p_valor AS numeric)
                            )
                        )
                    """),
                    {"p_conta_origem": p_conta_origem, "p_conta_destino": p_conta_destino, "p_valor": p_valor}
                )

        except Exception as e:
            # L53: Registro de auditoria de erro
            error_msg = str(e)
            conn.execute(
                text("""
                    INSERT INTO log_auditoria (entidade, acao, detalhes)
                    VALUES (
                        'transacoes',
                        'TRANSFERENCIA_ERRO',
                        jsonb_build_object(
                            'origem', CAST(:p_conta_origem AS int8),
                            'destino', CAST(:p_conta_destino AS int8),
                            'valor', CAST(:p_valor AS numeric),
                            'erro', CAST(:error_msg AS text)
                        )
                    )
                """),
                {
                    "p_conta_origem": p_conta_origem,
                    "p_conta_destino": p_conta_destino,
                    "p_valor": p_valor,
                    "error_msg": error_msg
                }
            )
            # L64: Re-lança a exceção para propagar o erro
            raise
