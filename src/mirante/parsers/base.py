from typing import Protocol

from mirante.domain.ir import ParsedProcedure, Table


class SqlParser(Protocol):
    """Contrato de um parser de dialeto. Novos dialetos (T-SQL, PL/SQL) implementam isto."""

    dialect: str

    def parse_procedure(self, source: str) -> ParsedProcedure: ...

    def parse_schema(self, ddl: str) -> list[Table]: ...
