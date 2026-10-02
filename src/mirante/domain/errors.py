class SqlParseError(Exception):
    """Entrada SQL invalida ou fora do escopo suportado pelo parser."""

    def __init__(self, message: str, location: int | None = None) -> None:
        super().__init__(message)
        self.location = location


class UnsupportedDialectError(Exception):
    """Nenhum parser registrado para o dialeto pedido."""
