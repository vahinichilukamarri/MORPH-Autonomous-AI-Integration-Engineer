from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ENV_FILE, extra="ignore")

    database_url: str = "postgresql+psycopg://morph:morph@localhost:5432/morph"
    cors_origins: list[str] = ["http://localhost:5173"]
    embedding_provider: Literal["fastembed", "fake"] = "fastembed"
    embedding_model: str = "BAAI/bge-small-en-v1.5"
    embedding_cache_dir: str | None = None
    llm_provider: Literal["groq", "ollama"] = "groq"
    llm_temperature: float = 0.0
    llm_timeout_s: float = 60.0
    llm_max_retries: int = 5
    llm_wall_clock_budget_s: float = 600.0
    llm_max_output_tokens: int = 4000
    llm_cache_dir: Path = ENV_FILE.parent / ".cache" / "llm"
    groq_api_key: SecretStr | None = None
    groq_model: str = "openai/gpt-oss-120b"
    groq_reasoning_effort: Literal["low", "medium", "high"] = "low"
    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1:8b"
    samples_dir: Path = ENV_FILE.parent / "mock_systems" / "samples"
    # The policy layer (v0.6). The approver token is a secret: environment only, never logged.
    approver_token: SecretStr | None = Field(default=None, validation_alias="MORPH_APPROVER_TOKEN")
    mcp_role: Literal["reader", "operator"] = Field(
        default="reader", validation_alias="MORPH_MCP_ROLE"
    )
    policy_dir: Path = Path(__file__).resolve().parents[1] / "policy"

    @field_validator("groq_api_key", "approver_token", mode="before")
    @classmethod
    def _empty_key_is_no_key(cls, value: object) -> object:
        """An empty GROQ_API_KEY (the .env.example placeholder) means no key."""
        return None if isinstance(value, str) and not value.strip() else value

    # Spec files given to POST /systems/ingest must live under this directory.
    spec_root: Path = ENV_FILE.parent


@lru_cache
def get_settings() -> Settings:
    return Settings()
