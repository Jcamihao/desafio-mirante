"""Fronteira com o LLM. O no de geracao depende de `CodeGenerator`, nunca de um SDK."""

import contextlib
import json
import logging
import re
import threading
import time
from collections.abc import Callable
from typing import Protocol

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from mirante.config import Settings, get_settings
from mirante.domain.generation import GeneratedModule, LlmUsage

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"^```(?:python)?\s*\n(.*?)\n```\s*$", re.S)


class CodeGenerator(Protocol):
    def generate(self, system: str, user: str) -> tuple[GeneratedModule, LlmUsage]: ...


def _is_collapsed(code: str) -> bool:
    """Modulo inteiro numa linha so, com `\\n` literais (JSON duplamente escapado pelo modelo)."""
    return code.count("\n") <= 3 and code.count("\\n") >= 10


def _unescape(code: str) -> str:
    """Desfaz o escape duplo: escapa so as aspas/controles crus e deixa o JSON decodificar."""
    body = re.sub(r'(?<!\\)((?:\\\\)*)"', r'\1\\"', code)
    body = body.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    return str(json.loads(f'"{body}"'))


def clean_code(code: str) -> str:
    """Normaliza a saida do modelo: remove cerca markdown e conserta codigo colapsado em 1 linha."""
    code = code.strip()
    if match := _FENCE.match(code):
        code = str(match.group(1))
    if _is_collapsed(code):
        # se nao decodificar, fica como esta: a camada de sintaxe reporta e o reparo age
        with contextlib.suppress(json.JSONDecodeError):
            code = _unescape(code)
    return code.strip() + "\n"


def get_chat_model(settings: Settings | None = None, model: str | None = None) -> BaseChatModel:
    s = settings or get_settings()
    name = model or s.resolved_model
    match s.llm_provider:
        case "gemini":
            from langchain_google_genai import ChatGoogleGenerativeAI

            return ChatGoogleGenerativeAI(
                model=name,
                google_api_key=s.google_api_key,
                temperature=0,
                max_retries=s.llm_max_retries,
                timeout=s.llm_timeout_s,
            )
        case "anthropic":
            from langchain_anthropic import ChatAnthropic

            return ChatAnthropic(
                model=name,
                api_key=s.anthropic_api_key,
                temperature=0,
                max_retries=s.llm_max_retries,
                timeout=s.llm_timeout_s,
                max_tokens=16000,
            )
        case other:
            raise ValueError(f"Provedor de LLM desconhecido: '{other}' (use gemini ou anthropic)")


_RETRY_IN = re.compile(r"retry in ([\d.]+)s|retryDelay['\"]?: ?['\"]?([\d.]+)s", re.I)


def is_daily_quota(exc: Exception) -> bool:
    """429 por cota DIARIA (ex.: free tier de 20 req/dia). Esperar nao adianta ate o dia virar."""
    message = str(exc)
    return "PerDay" in message and ("429" in message or "RESOURCE_EXHAUSTED" in message)


# Disjuntor por modelo, compartilhado entre requisicoes: o gerador e recriado a cada chamada,
# entao o estado "este modelo esta sem cota" precisa viver no modulo.
_EXHAUSTED: dict[
    str, float
] = {}  # nome do modelo -> instante (monotonic) em que volta a ser tentado
_EXHAUSTED_LOCK = threading.Lock()


def rate_limit_delay(exc: Exception) -> float | None:
    """Segundos pedidos pelo provedor num 429 ("retry in 37.7s"); None se nao for rate limit."""
    message = str(exc)
    if "429" not in message and "RESOURCE_EXHAUSTED" not in message:
        return None
    match = _RETRY_IN.search(message)
    return float(match.group(1) or match.group(2)) if match else 0.0


class ChatModelGenerator:
    """Implementa `CodeGenerator` sobre chat models do LangChain.

    Politica de falha, nesta ordem, para cada modelo (principal, depois fallbacks):
      1. 429 por cota DIARIA: nao espera; marca o modelo esgotado (disjuntor), passa ao proximo;
      2. 429 por minuto com "retry in Ns" (N <= limite): espera N s e repete no MESMO modelo;
      3. outro erro (503, timeout, ...) ou 429 persistente: passa ao proximo modelo.
    Se a cadeia inteira falhar por motivo transitorio (nao cota diaria), espera e repete a cadeia
    (`rounds` vezes no total): picos de 503 costumam durar segundos a minutos.
    Modelos esgotados sao pulados por `quota_cooldown_s` (se TODOS estiverem, tenta mesmo assim).
    Se todos falharem, propaga o erro do primeiro (o do modelo principal).
    """

    def __init__(
        self,
        chat_model: BaseChatModel,
        *fallbacks: BaseChatModel,
        max_rate_limit_wait_s: float = 60,
        quota_cooldown_s: float = 3600,
        rounds: int = 1,
        round_backoff_s: float = 20,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        models = (chat_model, *fallbacks)
        self._names = [
            str(getattr(m, "model", None) or getattr(m, "name", None) or f"model-{i}")
            for i, m in enumerate(models)
        ]
        self._chains = [m.with_structured_output(GeneratedModule, include_raw=True) for m in models]
        self._max_wait = max_rate_limit_wait_s
        self._cooldown = quota_cooldown_s
        self._rounds = max(1, rounds)
        self._round_backoff = round_backoff_s
        self._sleep = sleep
        self._clock = clock

    def _cooling(self, name: str) -> bool:
        with _EXHAUSTED_LOCK:
            return _EXHAUSTED.get(name, 0.0) > self._clock()

    def _mark_exhausted(self, name: str) -> None:
        with _EXHAUSTED_LOCK:
            _EXHAUSTED[name] = self._clock() + self._cooldown
        logger.warning(
            "%s sem cota diaria: pulado pelos proximos %.0f min", name, self._cooldown / 60
        )

    def _invoke(self, chain, messages):
        try:
            return chain.invoke(messages)
        except Exception as exc:  # noqa: BLE001 - provedor externo: qualquer falha e tratavel
            delay = rate_limit_delay(exc)
            if delay is None or delay > self._max_wait or is_daily_quota(exc):
                raise  # sem rate limit, espera longa demais ou cota diaria: esperar nao ajuda
            logger.warning("429 do provedor: aguardando %.0fs e repetindo no mesmo modelo", delay)
            self._sleep(delay + 1)
            return chain.invoke(messages)

    def generate(self, system: str, user: str) -> tuple[GeneratedModule, LlmUsage]:
        messages = [SystemMessage(system), HumanMessage(user)]
        first_error: Exception | None = None
        candidates = list(zip(self._names, self._chains, strict=True))
        for round_no in range(self._rounds):
            available = [(n, c) for n, c in candidates if not self._cooling(n)]
            transient = False
            for name, chain in available or candidates:  # todos esgotados: tenta mesmo assim
                try:
                    result = self._invoke(chain, messages)
                except Exception as exc:  # noqa: BLE001
                    first_error = first_error or exc
                    if is_daily_quota(exc):
                        self._mark_exhausted(name)
                    else:
                        transient = True
                    logger.warning("Modelo falhou (%s); tentando o proximo", type(exc).__name__)
                    continue
                return self._parse(result)
            if not transient or round_no == self._rounds - 1:
                break  # so cota diaria (esperar nao ajuda) ou rodadas esgotadas
            wait = self._round_backoff * (round_no + 1)
            logger.warning("Todos os modelos falharam; nova rodada em %.0fs", wait)
            self._sleep(wait)
        raise first_error  # type: ignore[misc]  # `_chains` nunca e vazio

    @staticmethod
    def _parse(result: dict) -> tuple[GeneratedModule, LlmUsage]:
        parsed = result.get("parsed")
        if parsed is None:
            raise ValueError(f"Resposta do LLM fora do schema: {result.get('parsing_error')}")
        raw = result["raw"]
        usage = getattr(raw, "usage_metadata", None) or {}
        metadata = getattr(raw, "response_metadata", None) or {}
        parsed.code = clean_code(parsed.code)
        return parsed, LlmUsage(
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            model=metadata.get("model_name", ""),
        )


def default_generator() -> CodeGenerator:
    settings = get_settings()
    primary = get_chat_model(settings)
    fallbacks = [get_chat_model(settings, m) for m in settings.fallback_models]
    return ChatModelGenerator(
        primary,
        *fallbacks,
        max_rate_limit_wait_s=settings.llm_max_rate_limit_wait_s,
        quota_cooldown_s=settings.llm_quota_cooldown_s,
        rounds=settings.llm_retry_rounds,
        round_backoff_s=settings.llm_round_backoff_s,
    )
