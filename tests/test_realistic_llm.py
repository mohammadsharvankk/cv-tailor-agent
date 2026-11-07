"""Pipeline test with replies shaped like a real LLM's: multi-word phrases, reworded names, trimmed quotes."""

import json
from pathlib import Path

from app.llm.base import LLMProvider
from app.pipeline import tailor

SAMPLES = Path(__file__).parent.parent / "app" / "static" / "samples"
CV = (SAMPLES / "sample_cv.txt").read_text()
JD = (SAMPLES / "sample_jd.txt").read_text()

JOB = {
    "title": "Machine Learning Engineer – Fintech Platform",
    "seniority": "mid",
    "must_have_skills": ["Python", "pandas", "scikit-learn", "SQL", "Docker", "CI/CD", "classification models",
                         "data pipelines", "REST APIs", "machine learning evaluation"],
    "nice_to_have_skills": ["Kubernetes", "AWS", "XGBoost", "LightGBM", "MLflow"],
}
CVJ = {"name": "Alex Morgan", "skills": ["Python", "pandas", "scikit-learn", "SQL"], "roles": [], "projects": []}
GAPS = {
    "fit_summary": "Solid analyst moving into ML engineering.",
    "skills": [
        # reworded name + quote trimmed with an ellipsis: should still count
        {"skill": "Classification modelling", "status": "strong",
         "evidence": "Built a churn classifier in Python … reaching 0.84 ROC-AUC"},
        # fabricated quote: must NOT count
        {"skill": "machine learning evaluation", "status": "partial",
         "evidence": "Evaluated models with precision and recall dashboards"},
        {"skill": "Kubernetes", "status": "missing", "evidence": None},
    ],
}


class ClaudeLike(LLMProvider):
    name = "claude-like"

    def __init__(self):
        self.rewrite_prompt = ""

    def _complete(self, system, user):
        if "<job_description>" in user:
            return json.dumps(JOB)
        if "<keywords_to_use>" in user:
            self.rewrite_prompt = user
            return json.dumps({
                "headline": "Data Analyst moving into ML engineering",
                "summary": "Builds classification models and data pipelines in Python, served as REST APIs.",
                "skills_ordered": ["Python", "scikit-learn", "classification models", "data pipelines", "REST APIs",
                                   "machine learning evaluation", "Kubernetes"],
                "bullets": [],
            })
        if "<gaps_json>" not in user and "<job_json>" in user:
            return json.dumps(GAPS)
        return json.dumps(CVJ)


def test_real_llm_shapes_raise_ats_honestly():
    llm = ClaudeLike()
    r = tailor.run(CV, JD, llm)
    sent = llm.rewrite_prompt.split("<keywords_to_use>")[1]
    for kw in ["classification models", "data pipelines", "REST APIs"]:
        assert kw in sent
    assert "machine learning evaluation" not in sent  # its quote was invented
    assert "Kubernetes" not in sent

    assert r.ats_after.score > r.ats_before.score
    assert "machine learning evaluation" not in r.tailored.skills_ordered
    assert "Kubernetes" not in r.tailored.skills_ordered
    assert {f.issue for f in r.fact_flags} == {"new_skill"}
