import io
import json
from pathlib import Path

import docx
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.export import assemble, clean_placeholders, export, to_edu
from app.main import app
from app.models import BulletRewrite, CVProfile, CVRole, EducationItem, JobRequirements, TailoredCV

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
    # tailored line first, topped up to two lines with the role's own original text
    assert [b.text[:12] for b in doc.roles[2].bullets] == ["Tailored 2.1", "Original bul"]
    assert [b.text[:12] for b in doc.roles[0].bullets] == ["Tailored 0.3", "Original bul"]
    assert len(doc.roles[1].bullets) == 2 and doc.roles[1].bullets[0].text.startswith("Original")  # filler
    assert doc.projects[0].bullets[0].text.startswith("Tailored -1.0")
    assert removed == 4 and "[" not in doc.summary


def test_education_line_split():
    e = to_edu(EducationItem(degree="MSc Data Science, University of Birmingham Dubai, Sep 2025 – Sep 2026, "
                                    "Distinction, overall 83.7%"))
    assert (e.degree, e.institution, e.dates, e.details) == (
        "MSc Data Science", "University of Birmingham Dubai", "Sep 2025 – Sep 2026", "Distinction, overall 83.7%")
    e = to_edu(EducationItem(degree="BSc Mathematics, University of Leeds, 2020"))
    assert (e.degree, e.institution, e.dates) == ("BSc Mathematics", "University of Leeds", "2020")


def test_promotions_at_one_employer_share_a_block():
    cv = CVProfile(name="S", roles=[
        CVRole(title="Senior Engineer", organisation="Resideo", location="Bangalore, India", dates="2023 – 2025",
               bullets=["Built Python ETL pipelines feeding Tableau dashboards"]),
        CVRole(title="Engineer", organisation="Resideo", dates="2019 – 2023", bullets=[]),
        CVRole(title="Intern", organisation="Infosys", dates="2019", bullets=["Trained in Python"])])
    doc, _ = assemble(cv, TailoredCV(headline="h", summary="s", skills_ordered=[], bullets=[]))
    assert [r.titles for r in doc.roles] == [[("Senior Engineer", "2023 – 2025"), ("Engineer", "2019 – 2023")],
                                             [("Intern", "2019")]]
    assert doc.roles[0].location == "Bangalore, India" and doc.roles[0].bullets  # filler from the block


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


def test_short_cv_asked_for_two_pages_stays_on_one():
    cv = _cv(n_roles=1, n_bullets=2)
    data, _, rep = export(cv, JOB, _tailored(cv, [(0, 0)]), "pdf", pages=2)
    assert rep.pages == 1 and any("kept to one page" in w for w in rep.warnings)


def test_two_pages_are_filled_with_real_lines():
    from app.export import assemble, fit
    cv = _cv(n_roles=6, n_bullets=8)
    doc, _ = assemble(cv, _tailored(cv, [(r, 0) for r in range(6)]))
    f = fit(doc, 2, "classic", JOB)
    assert f.pages == 2 and f.added > 0
    from app.export import render_pdf
    _, n, fill = render_pdf(f.doc, f.size, f.stretch, "classic")
    assert n == 2 and fill > 0.85  # the second page ends near the bottom, not halfway
    texts = [b.text for b in f.doc.all_bullets()]
    assert all(t.startswith(("Tailored", "Original")) for t in texts)  # nothing invented


def test_one_page_gap_is_closed_with_spacing():
    from app.export import assemble, fit, render_pdf
    cv = _cv(n_roles=2, n_bullets=2)
    doc, _ = assemble(cv, _tailored(cv, [(0, 0)]))
    f = fit(doc, 1, "harvard", JOB)
    _, n, fill = render_pdf(f.doc, f.size, f.stretch, "harvard")
    assert n == 1 and f.stretch > 1.0


def test_harvard_style_pdf_and_docx():
    cv = _cv()
    cv.activities = [CVRole(title="Volunteer", organisation="MDC Qatar", location="Remote", dates="2022 – Present",
                            bullets=["Provide occasional IT and sales support"])]
    cv.languages, cv.interests = ["English (fluent)", "Arabic (reads and writes)"], ["Chess"]
    data, name, rep = export(cv, JOB, _tailored(cv, [(0, 0), (-1, 0)]), "pdf", pages=2, style="harvard")
    _, text = _pdf_text(data)
    for h in ["Education", "Experience", "Key Projects", "Leadership & Activities", "Skills & Interests",
              "Languages:", "Interests:"]:
        assert h in text
    assert all(rep.text_checks.values())
    one = export(cv, JOB, _tailored(cv, [(0, 0)]), "pdf", pages=1, style="harvard")[0]
    assert "Interests:" not in _pdf_text(one)[1]  # interests only on two pages
    d = docx.Document(io.BytesIO(export(cv, JOB, _tailored(cv, [(0, 0)]), "docx", 2, "harvard")[0]))
    assert d.paragraphs[0].style.name == "Heading 1" and d.styles["Normal"].font.name == "Calibri"
    assert any(p.style.name == "Heading 2" and p.text == "Experience" for p in d.paragraphs)


def test_cutting_never_leaves_a_job_empty():
    cv = _cv(n_roles=8, n_bullets=6)
    picks = [(r, b) for b in range(5) for r in range(8)]
    doc_pdf, _, rep = export(cv, JOB, _tailored(cv, picks), "pdf", pages=1)
    _, text = _pdf_text(doc_pdf)
    for r in range(8):  # every role keeps at least its most relevant bullet
        assert f"Tailored {r}.0" in text
