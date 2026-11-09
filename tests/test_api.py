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
def mock_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("MASTER_CV_PATH", str(tmp_path / "my_cv" / "master_cv.md"))
    from app.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


client = TestClient(app)


def test_health_and_index():
    assert client.get("/api/health").json() == {"status": "ok", "provider": "mock", "master_cv": True,
                                                 "review": True, "mode": "local"}
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
    # the reviewer ran, flagged the [metric] placeholders, and the (unchanged) mock revision was accepted
    assert d["review"]["verdict"] == "revise" and d["review"]["issues"][0]["kind"] == "weak_bullet"
    assert d["revision"] == "accepted"


def test_review_can_be_turned_off(monkeypatch):
    monkeypatch.setenv("REVIEW_ENABLED", "false")
    from app.config import get_settings

    get_settings.cache_clear()
    d = client.post("/api/analyze", json={"cv_text": CV, "jd_text": JD}).json()
    assert d["review"] is None and d["revision"] == "off"


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


def test_master_cv_save_load_and_evidence():
    assert client.get("/api/master").json() == {"exists": False, "text": ""}
    assert client.put("/api/master", json={"text": CV}).status_code == 200
    assert client.get("/api/master").json()["text"].startswith("Alex Morgan")

    r = client.post("/api/master/evidence", json={"skill": "Kubernetes", "evidence": "Deployed two services on a k8s cluster"})
    text = r.json()["text"]
    assert "## Confirmed skills (added in CV Tailor)" in text and "- Kubernetes: Deployed two services" in text
    client.post("/api/master/evidence", json={"skill": "AWS", "evidence": "Hosted the churn API on AWS EC2"})
    text = client.get("/api/master").json()["text"]
    assert text.count("## Confirmed skills") == 1 and text.index("Kubernetes") < text.index("AWS")

    # confirmed skills now count as evidence: Kubernetes is no longer an honest gap
    d = client.post("/api/analyze", json={"cv_text": text, "jd_text": JD}).json()
    assert "Kubernetes" not in d["ats_after"]["must_have_missing"] + d["ats_after"]["nice_to_have_missing"]


def test_master_cv_rejects_empty_evidence():
    assert client.post("/api/master/evidence", json={"skill": "AWS", "evidence": "yes"}).status_code == 422
