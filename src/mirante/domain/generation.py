from pydantic import BaseModel, Field


class Decision(BaseModel):
    topic: str = Field(
        description="Assunto da decisao (ex.: 'cursor', 'transacao', 'arredondamento')"
    )
    choice: str = Field(description="O que foi feito")
    rationale: str = Field(description="Por que, ligando ao risco da analise quando houver")


class GeneratedModule(BaseModel):
    """Contrato de saida do LLM (structured output)."""

    code: str = Field(description="Modulo Python 3.14 completo, pronto para salvar em .py")
    decisions: list[Decision] = Field(default_factory=list)
    assumptions: list[str] = Field(
        default_factory=list, description="Premissas e pontos que a revisao humana deve checar"
    )


class LlmUsage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
