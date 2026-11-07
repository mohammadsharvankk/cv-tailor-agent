import pytest

from app.config import Settings
from app.llm.base import LLMError, LLMProvider, extract_json
from app.llm.providers import get_provider
from app.models import JobRequirements


class ScriptedProvider(LLMProvider):
    """Returns canned replies in order, to test the validate-and-repair loop."""

    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []

    def _complete(self, system, user):
        self.prompts.append(user)
        return self.replies.pop(0)


def test_extract_json_from_fenced_reply():
    assert extract_json('Sure!\n```json\n{"a": 1}\n```\nDone') == '{"a": 1}'
    assert extract_json('noise {"a": {"b": 2}} trailing') == '{"a": {"b": 2}}'
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_complete_json_repairs_once():
    p = ScriptedProvider(['{"title": 5, "seniority": "wizard"}', '{"title": "Data Scientist"}'])
    out = p.complete_json("sys", "jd", JobRequirements, task="parse_jd")
    assert out.title == "Data Scientist"
    assert "not valid" in p.prompts[1]  # the validation error was fed back


def test_complete_json_gives_up():
    p = ScriptedProvider(["garbage", "still garbage"])
    with pytest.raises(LLMError):
        p.complete_json("sys", "jd", JobRequirements, task="parse_jd")


def test_factory_requires_keys():
    with pytest.raises(LLMError):
        get_provider(Settings(llm_provider="anthropic", anthropic_api_key=None))
    with pytest.raises(LLMError):
        get_provider(Settings(llm_provider="openai", openai_api_key=None))
    assert get_provider(Settings(llm_provider="mock")).name == "mock"


class BrokenProvider(LLMProvider):
    name = "broken"

    def _complete(self, system, user):
        raise ConnectionError("network down")


def test_vendor_errors_become_llm_errors():
    with pytest.raises(LLMError, match="network down"):
        BrokenProvider().complete_json("sys", "jd", JobRequirements, task="parse_jd")
