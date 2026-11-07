from app.models import BulletRewrite, JobRequirements, TailoredCV
from app.pipeline.checks import ats_score, fact_guard
from app.pipeline.skills import find_skills, literal_hit, supported

CV = """Jane Doe
- Trained a sklearn model that cut review time by 30%
- Built ETL jobs in Python and SQL for 12 teams
Skills: Python, SQL, Docker"""


def test_literal_vs_supported():
    assert supported("used sklearn", "scikit-learn")
    assert not literal_hit("used sklearn", "scikit-learn")  # ATS wants the employer's exact wording
    assert literal_hit("Built CI/CD pipelines", "ci/cd")
    assert not supported("R&D budget", "R")  # one-letter skills don't match by accident


def test_find_skills_handles_symbols():
    found = find_skills("C++ and C# services, ML in sklearn")
    assert {"C++", "C#", "scikit-learn"} <= set(found)


def test_ats_score_weights_must_haves():
    job = JobRequirements(title="DS", must_have_skills=["Python", "Kubernetes"], nice_to_have_skills=["AWS"])
    s = ats_score(CV, job)
    assert s.must_have_hit == ["Python"]
    assert s.must_have_missing == ["Kubernetes"]
    assert s.score == 35.0  # 0.7 * 1/2 + 0.3 * 0/1


def test_ats_score_empty_job():
    assert ats_score(CV, JobRequirements(title="x")).score == 0.0


def test_fact_guard_flags_invented_number_and_skill():
    job = JobRequirements(title="MLE")
    tailored = TailoredCV(
        headline="h", summary="s",
        skills_ordered=["Python", "Kubernetes", "scikit-learn"],
        bullets=[
            BulletRewrite(original="a", rewritten="Cut review time by 30% by training a scikit-learn model"),
            BulletRewrite(original="b", rewritten="Scaled ETL to 40 teams on Kubernetes"),
            BulletRewrite(original="c", rewritten="Improved pipelines by [metric: % faster] using Python"),
        ],
    )
    cleaned, flags = fact_guard(CV, job, tailored)
    issues = {(f.bullet_index, f.issue) for f in flags}
    assert (0, "new_number") not in issues  # 30% is in the CV
    assert (1, "new_number") in issues  # 40 is invented
    assert (1, "new_skill") in issues  # Kubernetes is invented
    assert not any(f.bullet_index == 2 for f in flags)  # placeholders are fine
    assert "Kubernetes" not in cleaned.skills_ordered
    assert "scikit-learn" in cleaned.skills_ordered  # supported via the alias "sklearn"
