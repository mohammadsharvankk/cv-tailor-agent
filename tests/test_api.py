import io
from pathlib import Path

import docx
import pytest
from fastapi.testclient import TestClient

from app.main import app

SAMPLES = Path(__file__).parent.parent / "app" / "static" / "samples"
CV = (SAMPLES / "sample_cv.txt").read_text()
JD = (SAMPLES / "sample_jd.txt").read_text()


@pytest.fixture(autouse=True)
def mock_provider(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


client = TestClient(app)


def test_health_and_index():
    assert client.get("/api/health").json() == {"status": "ok", "provider": "mock"}
    assert "CV Tailor Agent" in client.get("/").text


def test_analyze_end_to_end():
    r = client.post("/api/analyze", json={"cv_text": CV, "jd_text": JD})
    assert r.status_code == 200
    d = r.json()
    assert d["provider"] == "mock"
    assert "Kubernetes" in d["gaps"]["honest_gaps"]  # the sample CV genuinely lacks it
    assert d["ats_after"]["score"] >= d["ats_before"]["score"]
    assert d["fact_flags"] == []  # the mock never invents facts
    assert all("Kubernetes" not in s for s in d["tailored"]["skills_ordered"])
    assert any(b["needs_metric"] for b in d["tailored"]["bullets"])
    assert d["tailored_markdown"].startswith("# Alex Morgan") and "Engineer" not in d["tailored_markdown"]


def test_analyze_rejects_short_input():
    assert client.post("/api/analyze", json={"cv_text": "too short", "jd_text": JD}).status_code == 422


def test_extract_txt_and_docx():
    r = client.post("/api/extract", files={"file": ("cv.txt", CV.encode(), "text/plain")})
    assert r.status_code == 200 and "Alex Morgan" in r.json()["text"]

    d = docx.Document()
    for line in CV.splitlines():
        d.add_paragraph(line)
    buf = io.BytesIO()
    d.save(buf)
    r = client.post("/api/extract", files={"file": ("cv.docx", buf.getvalue(), "application/octet-stream")})
    assert r.status_code == 200 and "Northwind Logistics" in r.json()["text"]


def test_extract_rejects_unknown_type():
    r = client.post("/api/extract", files={"file": ("cv.exe", b"x" * 100, "application/octet-stream")})
    assert r.status_code == 415
