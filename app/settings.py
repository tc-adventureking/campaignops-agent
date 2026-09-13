from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    agent_mode: Literal["demo", "openai", "youtu"] = "demo"
    model_base_url: str = "https://api.deepseek.com"
    model_name: str = "deepseek-v4-flash"
    model_api_key: SecretStr = SecretStr("")
    model_timeout_seconds: float = Field(default=60, gt=0, le=300)
    model_connect_timeout_seconds: float = Field(default=5, gt=0, le=30)
    model_retries: int = Field(default=2, ge=0, le=4)
    model_retry_base_seconds: float = Field(default=0.5, ge=0, le=10)
    model_fallbacks: list[str] = Field(default_factory=list, max_length=3)
    run_timeout_seconds: float = Field(default=180, gt=0, le=900)
    retrieval_timeout_seconds: float = Field(default=3, gt=0, le=30)
    database_connect_timeout_seconds: int = Field(default=3, ge=1, le=30)
    database_backend: Literal["duckdb", "postgres"] = "duckdb"
    postgres_dsn: SecretStr = SecretStr("")
    postgres_admin_dsn: SecretStr = SecretStr("")
    postgres_reader_password: SecretStr = SecretStr("")
    redis_url: SecretStr = SecretStr("")
    cache_namespace: str = Field(default="campaignops:v2", pattern=r"^[a-zA-Z0-9:_-]{1,64}$")
    cache_ttl_seconds: int = Field(default=300, ge=1, le=3600)
    redis_timeout_seconds: float = Field(default=0.3, gt=0, le=3)
    approval_api_key: SecretStr = SecretStr("")
    approval_actor: str = Field(default="local-operator", min_length=1, max_length=80)
    approval_ttl_seconds: int = Field(default=600, ge=1, le=3600)
    trace_retention_days: int = Field(default=30, ge=1, le=365)
    model_prices_path: Path = Path("configs/model_prices.json")
    prompt_variant: Literal["semantic", "plain"] = "semantic"
    experiment_id: str | None = None
    model_max_tokens: int = Field(default=4096, ge=128, le=16384)
    data_dir: Path = Path("data/generated")
    knowledge_dir: Path = Path("data/knowledge")
    max_query_rows: int = Field(default=200, ge=1, le=1000)
    max_result_bytes: int = Field(default=262144, ge=100, le=2000000)
    query_timeout_seconds: float = Field(default=5, gt=0, le=30)
    max_tool_calls: int = Field(default=8, ge=3, le=20)
    max_concurrent_runs: int = Field(default=4, ge=1, le=32)
    max_queued_runs: int = Field(default=8, ge=0, le=128)

    @property
    def database_path(self) -> Path:
        return self.data_dir / "campaignops.duckdb"

    @property
    def state_path(self) -> Path:
        return self.data_dir / "runs.sqlite3"
