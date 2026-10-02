"""Cadastra no Langfuse o preco dos modelos Gemini usados (idempotente).

Rode apos `docker compose up`.

O Langfuse calcula custo a partir de uma tabela de modelos; a versao self-hosted traz so Gemini 1.x.
Sem este passo o custo das chamadas de LLM aparece como 0. Precos de lista (USD por 1M de tokens) de
https://ai.google.dev/gemini-api/docs/pricing (paid tier, conferidos em 2026-10-01). Tokens de
raciocinio ("thinking") sao cobrados como saida. No free tier o custo real e zero: o valor aqui e o
equivalente pago, util para projetar gasto.

    uv run python -m mirante.langfuse_setup [--force]
"""

import sys
from typing import Any

import httpx

from mirante.config import get_settings

PER_M = 1e-6
MODELS: dict[str, tuple[float, float]] = {  # nome -> (entrada, saida) por 1M de tokens
    "gemini-2.5-flash": (0.30, 2.50),
    "gemini-3.5-flash": (1.50, 9.00),
    "gemini-3.6-flash": (0.75, 3.75),  # promocional ate 2026-12-31; depois 1.50 / 7.50
    "gemini-3-flash-preview": (0.50, 3.00),
    "gemini-3.8-flash": (0.75, 3.75),  # promocional ate 2026-12-31; depois 1.50 / 7.50
    "gemini-3.1-flash-lite": (0.25, 1.50),
    # gemini-3.5-flash-lite: SEM preco cadastrado de proposito (nao confirmado na pagina oficial).
}


def definition(name: str, price_in: float, price_out: float) -> dict[str, Any]:
    escaped = name.replace(".", r"\.")
    return {
        "modelName": name,
        "matchPattern": rf"(?i)^(models/)?({escaped})$",
        "unit": "TOKENS",
        # So `pricingTiers` aceita tipos de uso alem de input/output (`prices` e descartado).
        "pricingTiers": [
            {
                "name": "Standard",
                "isDefault": True,
                "priority": 0,
                "conditions": [],
                "prices": {
                    "input": price_in * PER_M,
                    "output": price_out * PER_M,
                    "output_reasoning": price_out * PER_M,  # thinking e cobrado como saida
                },
            }
        ],
    }


def apply(client: httpx.Client, *, force: bool = False) -> list[str]:
    """Devolve as linhas de log do que foi criado/ignorado."""
    existing: dict[str, Any] = {}
    page = 1
    while True:
        response = client.get("/api/public/models", params={"limit": 100, "page": page})
        data = response.raise_for_status().json()
        existing |= {m["modelName"]: m for m in data["data"] if not m.get("isLangfuseManaged")}
        if page >= data["meta"]["totalPages"]:
            break
        page += 1
    log = []
    for name, (price_in, price_out) in MODELS.items():
        if name in existing and not force:
            log.append(f"= {name}: ja cadastrado (use --force para recriar)")
            continue
        if name in existing:
            client.delete(f"/api/public/models/{existing[name]['id']}").raise_for_status()
        client.post(
            "/api/public/models", json=definition(name, price_in, price_out)
        ).raise_for_status()
        log.append(f"+ {name}: entrada ${price_in}/1M, saida ${price_out}/1M")
    return log


if __name__ == "__main__":
    s = get_settings()
    http = httpx.Client(
        base_url=s.langfuse_host, auth=(s.langfuse_public_key, s.langfuse_secret_key), timeout=15
    )
    print("\n".join(apply(http, force="--force" in sys.argv)))
