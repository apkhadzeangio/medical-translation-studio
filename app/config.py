from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://mts:mts@localhost:5432/mts"
    environment: str = "development"
    api_token: str | None = None  # if set, required as "Authorization: Bearer <token>" on /api/*

    storage_dir: str = "./data"  # local file storage for source files and extracted assets
    max_upload_mb: int = 500
    export_font: str = "Sylfaen"   # Georgian-capable font named in exported documents
    # Run the job worker inside the API process. Needed while storage is a local directory,
    # because a separate worker service would not see the files the API saved.
    embedded_worker: bool = False
    worker_threads: int = 2   # embedded workers; each translates one chunk at a time

    # LLM gateway. Any OpenAI-compatible endpoint; several keys are used round-robin.
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_keys: str = ""  # comma-separated
    llm_model: str = "gpt-6-astra"
    # USD per 1M tokens, per model. A model without a price (and no "default") is refused.
    llm_prices: str = '{"gpt-6-astra": {"input": 10, "cached_input": 1, "output": 50}}'
    llm_budget_usd: float | None = None  # hard cap across all projects
    # Upper bound on output tokens (reasoning included) of one model call, sent as
    # max_completion_tokens. Bounds what a single runaway answer can cost. Empty = provider default.
    llm_max_output_tokens: int | None = None
    llm_max_attempts: int = 6
    llm_concurrency_per_key: int = 4
    llm_timeout_seconds: float = 600
    translate_chunk_words: int = 1200   # source words per model call
    # Ask the model to double-check the layout of complex pages while parsing. Off by default:
    # it costs one image request per page with a figure or table, and the rule-based parser
    # alone already scores 100% on the reference chapter.
    pdf_structure_model: bool = False
    llm_review_model: str | None = None   # medical review; defaults to llm_model
    llm_refine_model: str | None = None   # optional Georgian academic editing after medical review
    # Independent QA. Use a model from a different vendor where possible; a model checking
    # its own work misses its own mistakes. Leave llm_qa_model empty to skip the model QA stage.
    llm_qa_model: str | None = None
    llm_qa_base_url: str | None = None    # only if the QA model lives at another endpoint
    llm_qa_api_keys: str = ""

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
