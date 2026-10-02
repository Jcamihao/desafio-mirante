from importlib.metadata import PackageNotFoundError, version

from fastapi import APIRouter

from mirante.api.deps import DatabaseCheckDep, SettingsDep
from mirante.api.schemas import ComponentHealth, HealthResponse

router = APIRouter(tags=["ops"])


def _version() -> str:
    try:
        return version("mirante-sql-modernizer")
    except PackageNotFoundError:
        return "0+unknown"


@router.get("/health", response_model=HealthResponse, summary="Status do pipeline")
def health(settings: SettingsDep, database_ok: DatabaseCheckDep) -> HealthResponse:
    """`degraded` (HTTP 200) = o pipeline ainda atende, mas nao consegue gravar o historico."""
    key = (
        settings.google_api_key if settings.llm_provider == "gemini" else settings.anthropic_api_key
    )
    db = database_ok()
    return HealthResponse(
        status="ok" if db else "degraded",
        version=_version(),
        components=ComponentHealth(
            database="ok" if db else "unavailable",
            llm_provider=settings.llm_provider,
            llm_model=settings.resolved_model,
            llm_configured=bool(key),  # so informa SE ha chave, nunca o valor
        ),
    )
