from fastapi import APIRouter, Response

from mirante.api.deps import LimiterDep, ServiceDep
from mirante.api.schemas import ModernizeRequest, ModernizeResponse
from mirante.service import http_status_for

router = APIRouter(tags=["pipeline"])


@router.post(
    "/modernize",
    response_model=ModernizeResponse,
    summary="Modernizar uma stored procedure PL/pgSQL para Python 3.14",
    responses={
        422: {"model": ModernizeResponse, "description": "SQL de entrada invalido"},
        502: {"model": ModernizeResponse, "description": "Falha do provedor de LLM"},
        500: {"model": ModernizeResponse, "description": "Falha interna (execucao registrada)"},
    },
)
async def modernize(
    request: ModernizeRequest,
    response: Response,
    service: ServiceDep,
    limiter: LimiterDep,
) -> ModernizeResponse:
    async with limiter:  # excesso de requisicoes espera a vez em vez de estourar a quota do LLM
        result = await service.run(request)
    response.status_code = http_status_for(result)
    return result
