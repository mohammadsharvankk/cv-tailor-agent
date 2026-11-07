"""Provider-agnostic interface for getting validated, typed JSON out of an LLM."""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import TypeVar

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """Raised when the model cannot produce valid output after retries."""


class TruncatedOutput(LLMError):
    """The model hit its output-token limit, so the JSON is incomplete. Retrying won't help."""


def extract_json(text: str) -> str:
    """Pull the first JSON object out of a model reply (handles ```json fences and chatter)."""
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        return fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object found in model output")
    return text[start : end + 1]


class LLMProvider(ABC):
    name: str = "base"

    @abstractmethod
    def _complete(self, system: str, user: str) -> str:
        """Return the raw text completion for one system + user message."""

    def complete_json(self, system: str, user: str, schema: type[T], task: str, retries: int = 1) -> T:
        """Ask for JSON matching `schema`; on bad output, send the validation error back once to self-repair."""
        schema_json = json.dumps(schema.model_json_schema(), indent=None)
        sys_prompt = (
            f"{system}\n\nRespond with ONE JSON object only, no prose, matching this JSON Schema:\n{schema_json}"
        )
        prompt = user
        last_err: Exception | None = None
        for attempt in range(retries + 1):
            try:
                raw = self._complete(sys_prompt, prompt) if self.name != "mock" else self._mock(task, user)
            except TruncatedOutput as err:
                raise TruncatedOutput(f"{err} (during '{task}')") from err
            except Exception as err:  # network, auth, rate limit, unknown model ... from the vendor SDK
                raise LLMError(f"{self.name} API call failed during '{task}': {type(err).__name__}: {err}") from err
            try:
                return schema.model_validate_json(extract_json(raw))
            except (ValueError, ValidationError) as err:
                last_err = err
                log.warning("task=%s attempt=%d invalid JSON: %s", task, attempt, str(err)[:300])
                prompt = (
                    f"{user}\n\nYour previous reply was not valid for the schema. Error:\n{str(err)[:1500]}\n"
                    "Return the corrected JSON object only."
                )
        raise LLMError(f"{self.name} failed to return valid {schema.__name__} for task '{task}': {last_err}")

    # Only the mock provider overrides this.
    def _mock(self, task: str, user: str) -> str:  # pragma: no cover
        raise NotImplementedError
