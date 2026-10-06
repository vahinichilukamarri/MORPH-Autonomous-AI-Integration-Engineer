from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr
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
    # Spec files given to POST /systems/ingest must live under this directory.
    spec_root: Path = ENV_FILE.parent


@lru_cache
def get_settings() -> Settings:
    return Settings()
