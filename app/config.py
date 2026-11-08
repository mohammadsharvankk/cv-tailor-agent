"""Runtime configuration, read from environment variables or a .env file."""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Which LLM backend to use. "mock" needs no API key and is used for tests and offline demos.
    llm_provider: Literal["anthropic", "openai", "mock"] = "mock"

    anthropic_api_key: str | None = None
    anthropic_model: str = "claude-sonnet-5-5"

    openai_api_key: str | None = None
    openai_model: str = "gpt-4.1-mini"

    # Optional, OpenAI only (some reasoning models reject it). Anthropic's current API has no such setting.
    temperature: float | None = None
    # A long master CV parses to a long JSON reply; too low a limit truncates it.
    max_output_tokens: int = 16000
    request_timeout_s: float = 90.0

    # Independent reviewer pass after the rewrite (+1 call, +1 more if it asks for a revision).
    review_enabled: bool = True

    # Private master CV (git-ignored). Turn off if you ever deploy this app for other people.
    master_cv_enabled: bool = True
    master_cv_path: str = "my_cv/master_cv.md"

    # Upload guard-rails
    max_upload_mb: float = 5.0
    max_text_chars: int = 30_000


@lru_cache
def get_settings() -> Settings:
    return Settings()
