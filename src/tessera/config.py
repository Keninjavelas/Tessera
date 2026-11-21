"""Application configuration via Pydantic Settings."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Centralized runtime settings loaded from environment variables and `.env`."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- LLM Providers (Tier 1 -> Tier 2 -> Tier 3) ---
    gemini_api_key: str | None = Field(default=None, alias="GEMINI_API_KEY")
    gemini_model: str = Field(default="gemini-2.5-flash", alias="GEMINI_MODEL")

    groq_api_key: str | None = Field(default=None, alias="GROQ_API_KEY")
    groq_model: str = Field(default="openai/gpt-oss-20b", alias="GROQ_MODEL")

    deepseek_api_key: str | None = Field(default=None, alias="DEEPSEEK_API_KEY")
    deepseek_model: str = Field(default="deepseek-chat", alias="DEEPSEEK_MODEL")

    # --- GitHub API ---
    github_token: str | None = Field(default=None, alias="GITHUB_TOKEN")

    # --- Optional Google Sheets ---
    google_sheets_credentials_path: str | None = Field(
        default=None, alias="GOOGLE_SHEETS_CREDENTIALS_PATH"
    )
    google_sheets_spreadsheet_id: str | None = Field(
        default=None, alias="GOOGLE_SHEETS_SPREADSHEET_ID"
    )

    # --- Paths ---
    data_dir: Path = Field(default=Path("data"), alias="TESSERA_DATA_DIR")
    export_dir: Path = Field(default=Path("exports"), alias="TESSERA_EXPORT_DIR")
    db_path: Path = Field(default=Path("data/tessera_state.db"), alias="TESSERA_DB_PATH")

    # --- Logging ---
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Field(
        default="INFO", alias="TESSERA_LOG_LEVEL"
    )

    # --- Fetcher Concurrency & Resilience ---
    http_max_concurrency: int = Field(default=20, ge=1, le=200, alias="TESSERA_HTTP_MAX_CONCURRENCY")
    per_domain_concurrency: int = Field(
        default=4, ge=1, le=50, alias="TESSERA_PER_DOMAIN_CONCURRENCY"
    )
    http_timeout_seconds: float = Field(
        default=25.0, ge=1.0, le=120.0, alias="TESSERA_HTTP_TIMEOUT_SECONDS"
    )

    # --- LLM Budgets & Retries ---
    llm_max_input_tokens: int = Field(
        default=6000, ge=500, le=128000, alias="TESSERA_LLM_MAX_INPUT_TOKENS"
    )
    llm_max_retries: int = Field(default=3, ge=1, le=10, alias="TESSERA_LLM_MAX_RETRIES")

    # --- Pipeline Targets ---
    phase1_target_count: int = Field(default=1060, ge=100, alias="TESSERA_PHASE1_TARGET")
    freshness_hours: int = Field(default=24, ge=1, le=168, alias="TESSERA_FRESHNESS_HOURS")

    def ensure_directories(self) -> None:
        """Create required runtime directories if they do not exist."""
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.export_dir.mkdir(parents=True, exist_ok=True)
        (self.export_dir / "csv").mkdir(parents=True, exist_ok=True)
        (self.export_dir / "jsonl").mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)


def get_settings() -> Settings:
    """Instantiate and return validated settings."""
    return Settings()
