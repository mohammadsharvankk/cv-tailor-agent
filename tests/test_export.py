import io
import json
from pathlib import Path

import docx
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.export import assemble, clean_placeholders, export, split_education
from app.main import app
from app.models import BulletRewrite, CVProfile, CVRole, JobRequirements, TailoredCV

JOB = JobRequirements(title="Data Analyst – Banking", must_have_skills=["Python", "SQL", "Tableau"])


def _cv(n_roles=3, n_bullets=6):
    roles = [CVRole(title=f"Engineer {r}", organisation=f"Company {r}", dates=f"Jan 20{10 + r} – Dec 20{11 + r}",
                    bullets=[f"Original bullet {r}.{b} about Python ETL work for team {r}{b} with Tableau"
                             for b in range(n_bullets)]) for r in range(n_roles)]
    return CVProfile(name="Sam Lee", contact="Dubai, UAE · sam.lee@example.com · +971 50 000 0000",
                     roles=roles, projects=[CVRole(title="Churn Model", organisation="MSc", dates="2026",
                                                   bullets=["Built a churn model in Python"])],
                     education=["MSc Data Science | University of Birmingham Dubai | Sep 2025 – Sep 2026"])


def _tailored(cv, picks):
    bullets = [BulletRewrite(original=cv.roles[r].bullets[b] if r >= 0 else cv.projects[0].bullets[0],
                             rewritten=f"Tailored {r}.{b}: shipped Python and SQL reporting, as measured by "
                                       f"[metric: % time saved], by building Tableau dashboards and automated data-quality "
                                       f"checks for the regional operations team {r}{b} across three markets.")
               for r, b in picks]
    return TailoredCV(headline="Data Analyst", summary="Builds Python and SQL reporting [metric: x].",
                      skills_ordered=["Python", "SQL", "Tableau"], bullets=bullets)


def test_placeholder_clauses_are_removed_cleanly():
    t, n = clean_placeholders("Cut review time, as measured by [metric: % saved], by automating checks.")
    assert (t, n) == ("Cut review time, by automating checks.", 1)
    t, n = clean_placeholders("Wrote SQL queries, improving [metric: e.g. error rate].")
    assert (t, n) == ("Wrote SQL queries.", 1)
    assert clean_placeholders("Cut costs by 30%.") == ("Cut costs by 30%.", 0)


def test_bullets_go_back_under_their_own_role():
    cv = _cv()
    doc, removed = assemble(cv, _tailored(cv, [(2, 1), (0, 3), (-1, 0)]))
    assert [b.text[:12] for b in doc.roles[2].bullets] == ["Tailored 2.1"]
    assert [b.text[:12] for b in doc.roles[0].bullets] == ["Tailored 0.3"]
    assert len(doc.roles[1].bullets) == 2 and doc.roles[1].bullets[0].text.startswith("Original")  # filler
    assert doc.projects[0].bullets[0].text.startswith("Tailored -1.0")
    assert removed == 4 and "[" not in doc.summary


def test_education_line_split():
    assert split_education("MSc Data Science | University of Birmingham Dubai | Sep 2025 – Sep 2026") == (
        "MSc Data Science", "University of Birmingham Dubai", "Sep 2025 – Sep 2026")
    assert split_education("BSc Mathematics, University of Leeds, 2020") == ("BSc Mathematics", "University of Leeds", "2020")


def _pdf_text(data):
    r = PdfReader(io.BytesIO(data))
    return len(r.pages), "\n".join(p.extract_text() for p in r.pages)


def test_long_cv_is_cut_to_one_page_least_relevant_first():
    cv = _cv(n_roles=8, n_bullets=6)
    picks = [(r, b) for b in range(5) for r in range(8)]  # 40 tailored bullets, priority = list order
    data, name, rep = export(cv, JOB, _tailored(cv, picks), "pdf", pages=1)
    n, text = _pdf_text(data)
    assert n == rep.pages == 1 and rep.bullets_dropped > 0
    assert "Tailored 0.0" in text and "Tailored 7.4" not in text  # first picks survive, last are cut
    assert all(rep.text_checks.values()) and "[" not in text
    assert name == "Sam_Lee_Data_Analyst.pdf"


def test_two_page_limit_keeps_more():
    cv = _cv(n_roles=8, n_bullets=6)
    picks = [(r, b) for b in range(5) for r in range(8)]
    one = export(cv, JOB, _tailored(cv, picks), "pdf", pages=1)[2]
    two = export(cv, JOB, _tailored(cv, picks), "pdf", pages=2)[2]
    assert two.pages <= 2 and two.bullets_kept > one.bullets_kept


def test_docx_export_has_all_sections():
    cv = _cv()
    data, name, rep = export(cv, JOB, _tailored(cv, [(0, 0), (-1, 0)]), "docx", pages=2)
    text = "\n".join(p.text for p in docx.Document(io.BytesIO(data)).paragraphs)
    for s in ["Sam Lee", "sam.lee@example.com", "EDUCATION", "EXPERIENCE", "KEY PROJECTS", "TECHNICAL SKILLS"]:
        assert s in text
    assert name.endswith(".docx") and "[" not in text


def test_export_endpoint_end_to_end(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    from app.config import get_settings

    get_settings.cache_clear()
    samples = Path(__file__).parent.parent / "app" / "static" / "samples"
    client = TestClient(app)
    d = client.post("/api/analyze", json={"cv_text": (samples / "sample_cv.txt").read_text(),
                                          "jd_text": (samples / "sample_jd.txt").read_text()}).json()
    assert d["cv"]["contact"].startswith("London, UK")
    r = client.post("/api/export", json={"cv": d["cv"], "job": d["job"], "tailored": d["tailored"]})
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert 'filename="Alex_Morgan_Machine_Learning_Engineer.pdf"' in r.headers["content-disposition"]
    rep = json.loads(r.headers["x-export-report"])
    assert rep["pages"] == 1 and all(rep["text_checks"].values())
    n, text = _pdf_text(r.content)
    assert "Jan 2022 – Present" in text and "Northwind Logistics" in text
    get_settings.cache_clear()
