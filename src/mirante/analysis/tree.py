from collections.abc import Iterator

from mirante.domain.ir import Stmt

Node = tuple[Stmt, tuple[Stmt, ...]]  # (comando, ancestrais do mais externo ao pai)


def walk(stmts: list[Stmt], ancestors: tuple[Stmt, ...] = ()) -> Iterator[Node]:
    """Percorre em profundidade, na ordem do codigo-fonte, carregando os ancestrais."""
    for stmt in stmts:
        yield stmt, ancestors
        for children in stmt.blocks.values():
            yield from walk(children, (*ancestors, stmt))


def descendants(stmt: Stmt) -> Iterator[Stmt]:
    for children in stmt.blocks.values():
        for child, _ in walk(children):
            yield child
