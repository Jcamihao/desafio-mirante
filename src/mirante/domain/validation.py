from typing import Literal

from pydantic import BaseModel, Field

IssueSeverity = Literal["error", "warning"]
LayerStatus = Literal["passed", "failed", "skipped"]


class Issue(BaseModel):
    code: str
    message: str
    line: int | None = None
    severity: IssueSeverity = "error"

    def as_feedback(self) -> str:
        where = f"line {self.line}: " if self.line else ""
        return f"[{self.code}] {where}{self.message}"


class LayerResult(BaseModel):
    name: str
    status: LayerStatus
    issues: list[Issue] = Field(default_factory=list)
    detail: str = ""

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]


class ValidationReport(BaseModel):
    layers: list[LayerResult] = Field(default_factory=list)
    autofixed: list[str] = Field(default_factory=list)  # correcoes deterministicas aplicadas

    @property
    def passed(self) -> bool:
        return all(layer.status != "failed" for layer in self.layers)

    @property
    def feedback(self) -> list[str]:
        """Erros (nao avisos) formatados para o prompt de reparo."""
        return [i.as_feedback() for layer in self.layers for i in layer.errors]
