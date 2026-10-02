"""Regras de risco da analise semantica.

Cada regra e uma funcao `(Ctx) -> Iterable[Risk]`, registrada com `@rule`. Adicionar um risco novo
(ou um dialeto novo) = escrever uma funcao; o motor nao muda. Regras que dependem do schema
(Anexo A) sao marcadas com `needs_schema=True` e sao puladas, de forma explicita, sem ele.
"""

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from mirante.analysis.sqlutil import expr_facts, insert_values
from mirante.analysis.tree import Node, descendants, walk
from mirante.domain.analysis import Risk
from mirante.domain.ir import ParsedProcedure, SqlInfo, Stmt, Table

DML = {"insert", "update", "delete"}
LOOPS = {"loop", "while", "fors", "fori"}
_SCALED_NUMERIC = re.compile(r"numeric\(\d+,\s*(\d+)\)")
BUILTINS = {
    "sum", "count", "avg", "min", "max", "now", "date", "date_trunc", "to_char", "round",
    "lower", "upper", "abs", "length", "concat", "nullif", "generate_series",
    "jsonb_build_object", "json_build_object",
}  # fmt: skip


@dataclass
class Ctx:
    proc: ParsedProcedure
    dependencies: list[ParsedProcedure]
    schema: dict[str, Table]
    nodes: list[Node]
    var_types: dict[str, str] = field(init=False)
    into_vars: set[str] = field(init=False)

    def __post_init__(self) -> None:
        self.var_types = {v.name: v.type for v in self.proc.variables}
        self.var_types |= {p.name: p.type for p in self.proc.params}
        self.into_vars = {
            name for s, _ in self.nodes if s.kind == "execsql" for name in s.detail.get("into", [])
        }

    def of_kind(self, *kinds: str) -> list[Node]:
        return [(s, a) for s, a in self.nodes if s.kind in kinds]

    def sql_infos(self) -> list[tuple[Stmt | None, SqlInfo]]:
        found: list[tuple[Stmt | None, SqlInfo]] = [(s, s.sql) for s, _ in self.nodes if s.sql]
        found += [(None, v.cursor_query) for v in self.proc.variables if v.cursor_query]
        return found


Rule = Callable[[Ctx], Iterable[Risk]]
RULES: list[tuple[Rule, bool]] = []


def rule(*, needs_schema: bool = False) -> Callable[[Rule], Rule]:
    def register(fn: Rule) -> Rule:
        RULES.append((fn, needs_schema))
        return fn

    return register


def _lines(*stmts: Stmt) -> list[int]:
    return sorted({s.line for s in stmts if s.line})


def _writes(stmts: Iterable[Stmt]) -> list[Stmt]:
    return [s for s in stmts if s.kind == "execsql" and s.sql and s.sql.statement_type in DML]


# --- cursores e desempenho -------------------------------------------------------------------
@rule()
def cursor_loops(ctx: Ctx) -> Iterable[Risk]:
    for loop, _ in ctx.of_kind(*LOOPS):
        inner = list(descendants(loop))
        fetches = [s for s in inner if s.kind == "fetch"]
        if not fetches:
            continue
        queries = [s for s in inner if s.kind == "execsql"]
        reads = [q for q in queries if q.sql and q.sql.statement_type == "select"]
        writes = _writes(queries)
        yield Risk(
            code="CURSOR_N_PLUS_1" if queries else "CURSOR_LOOP",
            severity="high" if queries else "medium",
            lines=_lines(loop),
            message=(
                f"Cursor '{fetches[0].detail['cursor']}' e percorrido linha a linha e cada "
                f"iteracao executa {len(reads)} SELECT e {len(writes)} DML (N+1 idas ao banco)."
            ),
            recommendation=(
                "Carregar o conjunto em uma unica query (ou lookup em lote das tabelas "
                "auxiliares) e processar em memoria; aplicar os DML em lote. Preservar a ordem "
                "e o snapshot do cursor (linhas criadas durante o loop nao entram) e manter "
                "tudo na mesma transacao."
            ),
        )


@rule()
def non_sargable_predicates(ctx: Ctx) -> Iterable[Risk]:
    seen: set[str] = set()
    for stmt, info in ctx.sql_infos():
        if info.statement_type not in {"select", "update", "delete"} or info.text in seen:
            continue
        seen.add(info.text)
        names = frozenset(ctx.var_types)
        funcs = expr_facts(info.text, is_expression=False, plpgsql_names=names).funcs_on_columns
        if funcs:
            yield Risk(
                code="NON_SARGABLE_PREDICATE",
                severity="low",
                lines=_lines(stmt) if stmt else [],
                message=f"Funcao {sorted(funcs)} aplicada a coluna no filtro impede uso de indice.",
                recommendation=(
                    "Manter a semantica, mas preferir intervalo (col >= d AND col < d + 1) ao "
                    "reescrever a query; se o SQL for mantido como texto, sinalizar no relatorio."
                ),
            )


# --- transacao, locks e excecoes --------------------------------------------------------------
@rule()
def row_locks(ctx: Ctx) -> Iterable[Risk]:
    locked = [(s, s.sql) for s, _ in ctx.nodes if s.kind == "execsql" and s.sql and s.sql.locking]
    if not locked:
        return
    tables = [t for _, info in locked for t in info.tables]
    yield Risk(
        code="ROW_LOCK",
        severity="medium",
        lines=_lines(*(s for s, _ in locked)),
        message=(
            f"{len(locked)} SELECT ... {locked[0][1].locking} em {tables}: o lock so vale dentro "
            "de uma transacao ativa e a ordem de aquisicao define risco de deadlock."
        ),
        recommendation=(
            "Manter o FOR UPDATE no SQL (texto) dentro de uma unica transacao; se varias linhas "
            "forem travadas, travar em ordem deterministica (ex.: por id) — a ordem original "
            "(origem, depois destino) pode causar deadlock em transferencias cruzadas."
        ),
    )


@rule()
def transaction_scope(ctx: Ctx) -> Iterable[Risk]:
    writes = _writes(s for s, _ in ctx.nodes)
    if writes:
        yield Risk(
            code="TRANSACTION_SCOPE",
            severity="medium",
            lines=_lines(*writes),
            message=(
                f"{len(writes)} comandos DML compartilham a transacao do chamador; a procedure "
                "nao faz COMMIT proprio: ou tudo persiste ou nada."
            ),
            recommendation=(
                "Uma unica unidade de trabalho (transacao) por chamada, com commit no fim e "
                "rollback em qualquer excecao; nao commitar por comando."
            ),
        )


@rule()
def exception_handlers(ctx: Ctx) -> Iterable[Risk]:
    for block, _ in ctx.of_kind("block"):
        conditions = block.detail.get("exception_conditions")
        if not conditions:
            continue
        catches_all = bool({"others", "raise_exception"} & set(conditions))
        body = [s for s, _ in _walk_blocks(block, "body")]
        handler = [s for s, _ in _walk_blocks(block, *_handler_keys(block))]
        reraises = any(s.kind == "raise" and s.detail.get("reraise") for s in handler)

        raises = [s for s in body if s.kind == "raise" and s.detail["level"] == "EXCEPTION"]
        if catches_all and raises:
            yield Risk(
                code="RAISE_CAUGHT_BY_OWN_HANDLER",
                severity="medium" if reraises else "high",
                lines=_lines(*raises),
                message=(
                    "RAISE EXCEPTION dentro do bloco e capturado pelo proprio WHEN "
                    f"{'|'.join(conditions).upper()}. "
                    + (
                        "O handler registra e re-lanca, entao o erro ainda propaga."
                        if reraises
                        else "O handler NAO re-lanca: a validacao nao propaga erro, e o handler "
                        "decide o desfecho (ex.: retornar um fallback)."
                    )
                ),
                recommendation=(
                    "Traduzir a estrutura como esta (try/except englobando a validacao); nao "
                    "'corrigir' levantando a excecao fora do try, pois muda o comportamento."
                ),
            )
        if _writes(body):
            yield Risk(
                code="SUBTRANSACTION_ROLLBACK",
                severity="medium",
                lines=_lines(block),
                message=(
                    "Bloco com EXCEPTION e uma subtransacao: ao entrar no handler, os efeitos "
                    "DML do bloco sao desfeitos."
                ),
                recommendation=(
                    "try: with conn.begin_nested(): <corpo> / except: <handler>. O handler roda "
                    "depois do rollback do savepoint do corpo. Se o handler NAO re-lanca (retorna "
                    "um fallback), suas escritas persistem normalmente."
                ),
            )
        handler_writes = _writes(handler)
        if reraises and handler_writes:
            yield Risk(
                code="HANDLER_WRITES_ROLLED_BACK",
                severity="high",
                lines=_lines(*handler_writes),
                message=(
                    "O handler grava (ex.: log de erro) e re-lanca. VERIFICADO no PostgreSQL 16: o "
                    "erro que sai da procedure desfaz a chamada INTEIRA, inclusive essa escrita, "
                    "mesmo que o chamador capture o erro (EXCEPTION ou SAVEPOINT): 0 linhas "
                    "persistem. O log de erro, na pratica, nunca e gravado."
                ),
                recommendation=(
                    "Reproduzir: envolver a funcao inteira (try/except incluso) em `with "
                    "conn.begin_nested():`; o erro que propaga desfaz tudo, handler incluso. NAO "
                    "gravar o log fora do savepoint nem em transacao separada: ele persistiria e o "
                    "PostgreSQL nao persiste."
                ),
            )
        fallback = [s for s in handler if s.kind in {"return", "return_query"}]
        if catches_all and fallback and not reraises:
            yield Risk(
                code="DEGRADED_FALLBACK",
                severity="medium",
                lines=_lines(*fallback),
                message="O handler engole o erro e retorna um resultado degradado (fallback).",
                recommendation=(
                    "Capturar a excecao, logar (WARNING) com a mensagem original e retornar o "
                    "mesmo fallback; o chamador nao recebe excecao."
                ),
            )


def _handler_keys(block: Stmt) -> list[str]:
    return [k for k in block.blocks if k.startswith("exception:")]


def _walk_blocks(block: Stmt, *keys: str) -> Iterable[Node]:
    for key in keys:
        yield from walk(block.blocks.get(key, []))


# --- semantica de valores ---------------------------------------------------------------------
@rule()
def null_comparisons(ctx: Ctx) -> Iterable[Risk]:
    null_tested: set[str] = set()
    for stmt, _ in ctx.nodes:
        if stmt.kind not in {"if", "exit", "continue"} or not stmt.text:
            continue
        facts = expr_facts(stmt.text)
        risky = (facts.compared & ctx.into_vars) - facts.null_tested - null_tested
        null_tested |= facts.null_tested
        if risky:
            yield Risk(
                code="NULL_COMPARISON",
                severity="medium",
                lines=_lines(stmt),
                message=(
                    f"Condicao compara {sorted(risky)}, preenchidas por SELECT INTO: se nenhuma "
                    "linha for encontrada ficam NULL, e 'NULL <> x' e NULL (tratado como falso, "
                    "o IF nao dispara). Em Python, None != x e True."
                ),
                recommendation=(
                    "Modelar a logica de tres valores: ausencia de linha => None, e a "
                    "comparacao com None nao deve entrar no ramo. Testar o caso "
                    "'linha inexistente'."
                ),
            )


@rule()
def numeric_rounding(ctx: Ctx) -> Iterable[Risk]:
    lines = []
    for stmt, _ in ctx.of_kind("assign"):
        target = stmt.detail.get("target") or ""
        if (
            _SCALED_NUMERIC.fullmatch(ctx.var_types.get(target, ""))
            and stmt.text
            and expr_facts(stmt.text.split(":=", 1)[-1]).scaling_ops
        ):
            lines.append(stmt)
    if lines:
        yield Risk(
            code="NUMERIC_ROUNDING",
            severity="high",
            lines=_lines(*lines),
            message=(
                f"{len(lines)} atribuicoes a NUMERIC(p,s) com multiplicacao/divisao: o PostgreSQL "
                "arredonda a cada atribuicao (half away from zero). Decimal.quantize do Python "
                "usa ROUND_HALF_EVEN por padrao."
            ),
            recommendation=(
                "Usar Decimal (nunca float; literais como 100.0 e 1.10 sao numeric no PG) e "
                "quantize(Decimal('0.01'), ROUND_HALF_UP) apos CADA atribuicao, na mesma ordem."
            ),
        )


@rule(needs_schema=True)
def check_constraints(ctx: Ctx) -> Iterable[Risk]:
    for stmt, ancestors in ctx.of_kind("execsql"):
        parsed = insert_values(stmt.sql.text) if stmt.sql else None
        table = ctx.schema.get(parsed[0]) if parsed else None
        if not parsed or not table:
            continue
        for column in table.columns:
            if column.checks and parsed[1].get(column.name) is False:  # valor dinamico
                in_loop = any(a.kind in LOOPS for a in ancestors)
                yield Risk(
                    code="CHECK_CONSTRAINT",
                    severity="high" if in_loop else "low",
                    lines=_lines(stmt),
                    message=(
                        f"INSERT em {table.name}.{column.name} com valor calculado sujeito a "
                        f"CHECK ({'; '.join(column.checks)}). "
                        + (
                            "Dentro de loop sem handler: uma violacao aborta o lote inteiro."
                            if in_loop
                            else ""
                        )
                    ),
                    recommendation=(
                        "Nao 'proteger' com um IF novo: reproduzir o erro do banco "
                        "(IntegrityError) e o rollback do lote; cobrir em teste o caso "
                        "limite (ex.: valor = 0)."
                    ),
                )


# --- interface, chamadas e I/O -----------------------------------------------------------------
@rule()
def output_shape(ctx: Ctx) -> Iterable[Risk]:
    outs = [p for p in ctx.proc.params if p.mode in {"OUT", "INOUT"}]
    if outs:
        yield Risk(
            code="OUT_PARAMS",
            severity="low",
            message=f"Parametros de saida {[p.name for p in outs]} nao existem em Python.",
            recommendation=(
                "Retornar um dataclass(frozen) com os campos OUT (nome explicito > tupla)."
            ),
        )
    returns = ctx.proc.returns
    if returns and returns.setof:
        yield Risk(
            code="SET_RETURNING",
            severity="medium",
            lines=_lines(*(s for s, _ in ctx.of_kind("return_query"))),
            message="Funcao retorna conjunto (RETURN QUERY / RETURNS TABLE): multiplas linhas.",
            recommendation=(
                "Retornar list[dataclass] com os campos de RETURNS TABLE; o resultado so e "
                "devolvido ao final (RETURN QUERY acumula), entao nao usar generator lazy."
            ),
        )
    for stmt, _ in ctx.of_kind("getdiag"):
        yield Risk(
            code="GET_DIAGNOSTICS",
            severity="low",
            lines=_lines(stmt),
            message="GET DIAGNOSTICS ... ROW_COUNT depende do ultimo comando SQL executado.",
            recommendation=(
                "Usar result.rowcount do comando imediatamente anterior, sem nada no meio."
            ),
        )


@rule()
def recursive_cte(ctx: Ctx) -> Iterable[Risk]:
    found = [(s, i) for s, i in ctx.sql_infos() if i.recursive]
    for stmt, info in found:
        yield Risk(
            code="RECURSIVE_CTE",
            severity="medium",
            lines=_lines(stmt) if stmt else [],
            message=f"CTE recursiva ({', '.join(info.cte_names)}): nao tem equivalente direto.",
            recommendation=(
                "Manter a query como texto SQL executado pelo banco (equivalencia garantida, 1 "
                "round-trip); reescrever em Python so se precisar rodar sem banco."
            ),
        )


@rule()
def function_calls(ctx: Ctx) -> Iterable[Risk]:
    known = {d.name.lower() for d in ctx.dependencies} | {ctx.proc.name.lower()}
    called: dict[str, list[int]] = {}
    for stmt, info in ctx.sql_infos():
        for fn in info.functions:
            called.setdefault(fn.lower(), []).extend(_lines(stmt) if stmt else [])
    for name, lines in called.items():
        if name in BUILTINS:
            continue
        resolved = name in known
        yield Risk(
            code="NESTED_CALL" if resolved else "UNRESOLVED_CALL",
            severity="medium" if resolved else "high",
            lines=sorted(set(lines)),
            message=(
                f"Chamada a '{name}' (definicao fornecida)."
                if resolved
                else f"Chamada a '{name}' sem definicao: nao da para garantir o comportamento."
            ),
            recommendation=(
                "Chamar a versao Python ja traduzida (import) em vez de inlining; passar a mesma "
                "conexao/transacao."
                if resolved
                else "Fornecer a definicao em `dependencies` ou tratar como funcao externa."
            ),
        )


@rule()
def side_channels(ctx: Ctx) -> Iterable[Risk]:
    logs = [s for s, _ in ctx.of_kind("raise") if s.detail["level"] != "EXCEPTION"]
    if logs:
        yield Risk(
            code="RAISE_LOGGING",
            severity="low",
            lines=_lines(*logs),
            message=f"{len(logs)} RAISE NOTICE/WARNING: canal de log do banco, nao de erro.",
            recommendation="Traduzir para logging (NOTICE->info, WARNING->warning); sem excecao.",
        )
    infos = [i for _, i in ctx.sql_infos()]
    if any(f.startswith(("jsonb_", "json_")) for i in infos for f in i.functions):
        yield Risk(
            code="JSON_SERIALIZATION",
            severity="low",
            message=(
                "Usa jsonb_build_object com variaveis: no psycopg 3 um bind sem tipo inferivel "
                'falha em runtime ("could not determine data type of parameter $N"; VERIFICADO).'
            ),
            recommendation=(
                "Manter jsonb_build_object no SQL e tipar TODO bind dentro dele: CAST(:p AS text), "
                "CAST(:n AS numeric), CAST(:d AS date). Se montar em Python, nao converter Decimal "
                "para float (perde exatidao)."
            ),
        )
    formatted = [
        s
        for s, _ in ctx.of_kind("raise")
        if s.detail["level"] == "EXCEPTION" and s.detail["params"]
    ]
    if formatted:
        yield Risk(
            code="RAISE_NULL_FORMAT",
            severity="low",
            lines=_lines(*formatted),
            message=(
                f"{len(formatted)} RAISE EXCEPTION com placeholders %: o PostgreSQL imprime NULL "
                "como <NULL> (VERIFICADO), o Python imprimiria None."
            ),
            recommendation=(
                "Formatar None como '<NULL>' nas mensagens; numeros e Decimals com str()."
            ),
        )
    if any("now" in i.functions for i in infos):
        yield Risk(
            code="NON_DETERMINISTIC_TIME",
            severity="low",
            message="Depende de NOW() (relogio do banco): resultado muda com o tempo.",
            recommendation=(
                "Manter NOW() no SQL; nao trocar por datetime.now() (fuso/relogio diferentes)."
            ),
        )
