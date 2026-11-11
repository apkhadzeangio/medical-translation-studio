from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://mts:mts@localhost:5432/mts"
    environment: str = "development"
    api_token: str | None = None  # if set, required as "Authorization: Bearer <token>" on /api/*

    storage_dir: str = "./data"  # local file storage for source files and extracted assets

    # LLM gateway. Any OpenAI-compatible endpoint; several keys are used round-robin.
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_keys: str = ""  # comma-separated
    llm_model: str = "gpt-6-astra"
    # USD per 1M tokens, per model. A model without a price (and no "default") is refused.
    llm_prices: str = '{"gpt-6-astra": {"input": 10, "cached_input": 1, "output": 50}}'
    llm_budget_usd: float | None = None  # hard cap across all projects
    llm_max_attempts: int = 6
    llm_concurrency_per_key: int = 4
    llm_timeout_seconds: float = 600

    @property
    def llm_key_list(self) -> list[str]:
        return [k.strip() for k in self.llm_api_keys.split(",") if k.strip()]

    @field_validator("database_url")
    @classmethod
    def _use_psycopg3(cls, v: str) -> str:
        # Railway and Heroku-style URLs come as postgres:// or postgresql://
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg://" + v[len(prefix):]
        return v


@lru_cache
def get_settings() -> Settings:
    return Settings()
