from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

DEFAULT_MODELS = {"gemini": "gemini-3.6-flash", "anthropic": "claude-sonnet-5-5"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    llm_provider: str = "gemini"  # gemini | anthropic
    llm_model: str = ""  # vazio => padrao do provedor
    # Verificado em 2026-10-01: a familia 2.5 deu 404 num projeto novo (indisponivel p/ novos
    # usuarios); os Flash 3.x responderam.
    llm_fallback_models: str = "gemini-3.5-flash,gemini-3-flash-preview"  # separados por virgula
    llm_timeout_s: int = 120  # por chamada; falhar rapido e melhor que prender a requisicao
    llm_max_retries: int = 1  # retries internos do SDK (503 transitorio); alem disso: fallback
    llm_retry_rounds: int = 2  # rodadas completas pela cadeia de modelos (picos de 503)
    llm_round_backoff_s: int = 20  # espera entre rodadas (cresce linearmente)
    llm_quota_cooldown_s: int = 3600  # modelo sem cota DIARIA e pulado por este tempo
    llm_max_rate_limit_wait_s: int = 60  # espera maxima honrando o "retry in Ns" de um 429
    google_api_key: str = ""
    anthropic_api_key: str = ""
    max_generation_attempts: int = 3  # 1 geracao + ate 2 reparos guiados pela validacao
    auto_migrate: bool = True  # aplica db/init/*.sql no startup da API
    max_concurrent_runs: int = 4  # protege a quota do LLM (cada execucao leva 20-120 s)
    database_url: str = "postgresql+psycopg://mirante:mirante@localhost:5432/mirante"
    langfuse_enabled: bool = True  # so liga de fato se as duas chaves existirem
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""

    @property
    def fallback_models(self) -> list[str]:
        return [m.strip() for m in self.llm_fallback_models.split(",") if m.strip()]

    @property
    def resolved_model(self) -> str:
        return self.llm_model or DEFAULT_MODELS.get(self.llm_provider, "")


@lru_cache
def get_settings() -> Settings:
    return Settings()
