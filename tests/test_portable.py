"""Settings panel (local mode) and the bring-your-own-key public demo mode."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import local_settings
from app.config import get_settings
from app.main import app, limiter

SAMPLES = Path(__file__).parent.parent / "app" / "static" / "samples"
BODY = {"cv_text": (SAMPLES / "sample_cv.txt").read_text(), "jd_text": (SAMPLES / "sample_jd.txt").read_text()}
client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)  # .env is read and written in the working folder
    for k in ("LLM_PROVIDER", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "MODE", "REVIEW_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(local_settings, "ENV_FILE", tmp_path / ".env")
    limiter.hits.clear()
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_env_update_keeps_other_lines(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# my notes\nLLM_PROVIDER=mock\nMAX_OUTPUT_TOKENS=16000\n")
    local_settings.update_env({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-x\n"}, env)
    assert env.read_text() == "# my notes\nLLM_PROVIDER=anthropic\nMAX_OUTPUT_TOKENS=16000\nANTHROPIC_API_KEY=sk-ant-x\n"
    with pytest.raises(ValueError):
        local_settings.update_env({"MODE": "demo"}, env)  # can't switch off safety settings from the page


def test_local_settings_saved_and_key_never_returned(tmp_path):
    assert client.get("/api/settings").json()["anthropic_key_set"] is False
    r = client.put("/api/settings", json={"provider": "anthropic"})
    assert r.status_code == 422  # no key: refused, and nothing written
    assert not (tmp_path / ".env").exists()
    r = client.put("/api/settings", json={"provider": "anthropic", "anthropic_api_key": "sk-ant-secret"})
    assert r.status_code == 200
    view = r.json()
    assert view["provider"] == "anthropic" and view["anthropic_key_set"] is True
    assert "sk-ant-secret" not in r.text and "sk-ant-secret" not in client.get("/api/settings").text
    assert "ANTHROPIC_API_KEY=sk-ant-secret" in (tmp_path / ".env").read_text()
    assert client.get("/api/health").json()["provider"] == "anthropic"


def test_demo_mode_is_stateless_and_uses_the_visitors_key(monkeypatch):
    monkeypatch.setenv("MODE", "demo")
    monkeypatch.setenv("DEMO_RUNS_PER_HOUR", "2")
    get_settings.cache_clear()
    h = client.get("/api/health").json()
    assert h["mode"] == "demo" and h["master_cv"] is False
    assert client.get("/api/master").status_code == 404  # no CV storage on a shared server
    assert client.put("/api/settings", json={"provider": "mock"}).status_code == 403
    # no key: free rule-based mode works without limits
    for _ in range(3):
        assert client.post("/api/analyze", json=BODY, headers={"X-LLM-Provider": "mock"}).status_code == 200
    # a real provider needs the visitor's key
    r = client.post("/api/analyze", json=BODY, headers={"X-LLM-Provider": "anthropic"})
    assert r.status_code == 400 and "API key" in r.json()["detail"]


def test_demo_rate_limit_and_key_used_per_request(monkeypatch):
    monkeypatch.setenv("MODE", "demo")
    monkeypatch.setenv("DEMO_RUNS_PER_HOUR", "2")
    get_settings.cache_clear()
    seen = []

    def fake_provider(settings):
        seen.append((settings.llm_provider, settings.anthropic_api_key))
        from app.llm.mock import MockProvider
        return MockProvider()

    monkeypatch.setattr("app.main.get_provider", fake_provider)
    hdr = {"X-LLM-Provider": "anthropic", "X-API-Key": "sk-ant-visitor"}
    assert client.post("/api/analyze", json=BODY, headers=hdr).status_code == 200
    assert client.post("/api/analyze", json=BODY, headers=hdr).status_code == 200
    assert client.post("/api/analyze", json=BODY, headers=hdr).status_code == 429
    assert seen == [("anthropic", "sk-ant-visitor")] * 2
    assert get_settings().anthropic_api_key is None  # the server's own settings never hold it


def test_desktop_launcher_picks_a_free_local_port():
    import socket

    from app.desktop import app_folder, free_port

    busy = socket.socket()
    busy.bind(("127.0.0.1", 0))
    port = busy.getsockname()[1]
    try:
        assert free_port(port) != port  # skips a port that's taken
    finally:
        busy.close()
    assert app_folder().exists()
