import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from mirante.domain.status import Status

LAYERS = ("syntax", "lint", "imports", "policy")


class CaseResult(BaseModel):
    case: str  # "B".."F"
    procedure: str | None = None
    status: Status
    attempts: int = 0
    models: list[str] = Field(default_factory=list)  # modelos que de fato geraram codigo
    first_attempt_valid: bool = False  # passou na validacao sem precisar de reparo
    final_valid: bool = False
    layers: dict[str, bool] = Field(default_factory=dict)  # camada -> passou (codigo final)
    expected_risks: list[str] = Field(default_factory=list)
    detected_risks: list[str] = Field(default_factory=list)
    missed_risks: list[str] = Field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    history_id: uuid.UUID | None = None
    trace_id: str | None = None
    errors: list[str] = Field(default_factory=list)


class Metrics(BaseModel):
    cases: int
    # Funil por camada (sobre o codigo final de cada caso; caso sem codigo conta como falha).
    syntax_rate: float  # a metrica "ingenua" (ast.parse): so para contraste
    lint_rate: float
    imports_rate: float
    policy_rate: float
    valid_rate: float  # passou em TODAS as camadas, ponta a ponta (inclui falhas do provedor)
    generation_rate: float  # disponibilidade: o LLM chegou a devolver codigo
    quality_rate: float  # qualidade: validos ENTRE os que o LLM gerou (isola o provedor)
    first_attempt_rate: float  # valid sem nenhum reparo (pass@1)
    repair_gain: float  # valid_rate - first_attempt_rate: o que o laco de reparo entrega
    naive_gap: float  # syntax_rate - valid_rate: quanto o ast.parse sozinho superestima
    risk_recall: float  # riscos esperados que a analise detectou
    avg_attempts: float
    input_tokens: int
    output_tokens: int
    total_duration_ms: int


class EvaluationRun(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    created_at: datetime | None = None
    model: str = ""
    metrics: Metrics
    results: list[CaseResult]
