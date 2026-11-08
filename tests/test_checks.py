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


def test_rewrite_keywords_raise_ats_and_fake_evidence_is_rejected():
    from app.models import GapAnalysis, SkillEvidence
    from app.pipeline.checks import evidence_backed, keywords_to_use

    cv = "Built a churn classifier in Python with pandas, reaching 0.84 ROC-AUC\nSkills: Python, pandas"
    job = JobRequirements(title="MLE", must_have_skills=["Python", "classification models", "Kubernetes"])
    gaps = GapAnalysis(fit_summary="x", skills=[
        SkillEvidence(skill="classification models", status="strong", evidence="Built a churn classifier in Python"),
        SkillEvidence(skill="Kubernetes", status="partial", evidence="Ran services on Kubernetes clusters"),  # fake quote
    ])
    backed = evidence_backed(cv, gaps)
    assert "classification models" in backed
    assert "kubernetes" not in backed  # its 'evidence' isn't in the CV

    kws = keywords_to_use(cv, job, backed)
    assert kws == ["classification models"]

    tailored = TailoredCV(headline="h", summary="Python developer building classification models",
                          skills_ordered=["Python", "classification models", "Kubernetes"])
    cleaned, flags = fact_guard(cv, job, tailored, backed)
    assert cleaned.skills_ordered == ["Python", "classification models"]
    assert any("Kubernetes" in f.detail for f in flags)

    before = ats_score(cv, job).score
    after = ats_score(cleaned.summary + " " + ", ".join(cleaned.skills_ordered) + "\n" + cv, job).score
    assert after > before


def test_sibling_products_do_not_support_each_other():
    cv = "Pulled data from three SQL Server databases and stored exports in S3"
    assert supported(cv, "SQL") and supported(cv, "AWS")  # a child proves its parent
    assert not supported(cv, "PostgreSQL")  # a sibling does not
    assert not supported("Worked on AWS", "SageMaker")  # a parent does not prove a child


def test_fact_guard_checks_summary_and_headline():
    cv = "Built Python ETL scripts on SQL Server"
    job = JobRequirements(title="Data Engineer", must_have_skills=["PostgreSQL"])
    tailored = TailoredCV(headline="Data Engineer | Python, SQL",
                          summary="SQL Server experience that carries over to PostgreSQL.",
                          skills_ordered=["Python", "PostgreSQL"])
    cleaned, flags = fact_guard(cv, job, tailored)
    assert "PostgreSQL" not in cleaned.skills_ordered
    assert any(f.bullet_index == -2 and "PostgreSQL" in f.detail for f in flags)


def test_verb_embedding_is_not_the_embeddings_skill():
    assert "Embeddings" not in find_skills("Embedding anomaly detection into Jenkins CI/CD pipelines")
    assert "Embeddings" in find_skills("Built a search index with text embeddings")


def test_gap_sentences_are_removed_from_summary():
    job = JobRequirements(title="DA", must_have_skills=["Python", "Apache Spark", "R"])
    t = TailoredCV(headline="Data Analyst", summary="Builds ETL in Python and SQL. No banking, R or Spark experience "
                   "is shown.", skills_ordered=["Python"], bullets=[])
    out, flags = fact_guard(CV, job, t)
    assert out.summary == "Builds ETL in Python and SQL."
    assert [f.issue for f in flags] == ["gap_mention"]  # and no false 'Apache Spark' new_skill flag
    # the removed sentence can't earn ATS points for R or Spark
    assert ats_score(out.summary + " " + CV, job).must_have_missing == ["Apache Spark", "R"]


def test_skills_list_drops_degrees_and_soft_skills_and_is_capped():
    job = JobRequirements(title="DA", must_have_skills=["Python"], qualifications=["Advanced degree", "MS/Ph.D"],
                          soft_skills=["analytical skills"])
    many = [f"Python{i}" for i in range(40)]
    cv = CV + "\nSkills: " + ", ".join(many)
    t = TailoredCV(headline="h", summary="s", bullets=[],
                   skills_ordered=["Python", "Advanced degree", "MS/Ph.D", "analytical skills", "University degree",
                                   *many])
    out, flags = fact_guard(cv, job, t)
    assert out.skills_ordered[0] == "Python" and len(out.skills_ordered) == 25
    assert not {"Advanced degree", "MS/Ph.D", "analytical skills", "University degree"} & set(out.skills_ordered)
    assert flags == []


def test_unconfirmed_lines_are_skipped():
    from app.pipeline.checks import strip_unconfirmed

    text, n = strip_unconfirmed("A\n- Built flaky-test ML model [CONFIRM scope]\n- Ran A/B tests [verify]\nB")
    assert text == "A\nB" and n == 2
