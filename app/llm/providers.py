"""Concrete LLM providers and a factory that picks one from settings."""

from __future__ import annotations

from app.config import Settings, get_settings
from app.llm.base import LLMError, LLMProvider, TruncatedOutput


def _truncated_msg(limit: int) -> str:
    return (f"The model's reply was cut off at the {limit}-token output limit, usually because the CV is very long. "
            f"Raise MAX_OUTPUT_TOKENS in .env (e.g. to {limit * 2}) and restart the server.")


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, settings: Settings):
        if not settings.anthropic_api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set")
        import anthropic

        self.client = anthropic.Anthropic(api_key=settings.anthropic_api_key, timeout=settings.request_timeout_s)
        self.model = settings.anthropic_model
        self.s = settings

    def _complete(self, system: str, user: str) -> str:
        msg = self.client.messages.create(
            model=self.model,
            system=system,
            max_tokens=self.s.max_output_tokens,
            messages=[{"role": "user", "content": user}],
        )
        if getattr(msg, "stop_reason", None) == "max_tokens":
            raise TruncatedOutput(_truncated_msg(self.s.max_output_tokens))
        return "".join(block.text for block in msg.content if getattr(block, "type", "") == "text")


class OpenAIProvider(LLMProvider):
    name = "openai"

    def __init__(self, settings: Settings):
        if not settings.openai_api_key:
            raise LLMError("OPENAI_API_KEY is not set")
        import openai

        self.client = openai.OpenAI(api_key=settings.openai_api_key, timeout=settings.request_timeout_s)
        self.model = settings.openai_model
        self.s = settings

    def _complete(self, system: str, user: str) -> str:
        extra = {"temperature": self.s.temperature} if self.s.temperature is not None else {}
        resp = self.client.chat.completions.create(
            model=self.model,
            max_completion_tokens=self.s.max_output_tokens,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            **extra,
        )
        if resp.choices[0].finish_reason == "length":
            raise TruncatedOutput(_truncated_msg(self.s.max_output_tokens))
        return resp.choices[0].message.content or ""


def get_provider(settings: Settings | None = None) -> LLMProvider:
    s = settings or get_settings()
    if s.llm_provider == "anthropic":
        return AnthropicProvider(s)
    if s.llm_provider == "openai":
        return OpenAIProvider(s)
    from app.llm.mock import MockProvider

    return MockProvider()
