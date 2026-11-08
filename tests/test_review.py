"""Reviewer + revision loop, with replies shaped like the problems seen in a real run."""

import json

import pytest

from app.llm.base import LLMProvider
from app.pipeline import tailor

CV = """Sam Lee
Data Scientist
Experience
Senior Engineer | Acme | 2019 - 2025
- Built Python ETL pipelines feeding Tableau dashboards for 40 stakeholders
- Maintained a shared SQL analytics repository for release reporting
- Trained a flaky-test classifier in production [CONFIRM scope]
Education
- MSc Data Science, Distinction
Skills: Python, SQL, Tableau, pandas"""

JD = "Data Analyst\nRequired: Python, SQL, Spark, R. Advanced degree (MS/Ph.D). Strong analytical skills."

JOB = {"title": "Data Analyst", "must_have_skills": ["Python", "SQL", "Spark", "R"],
       "qualifications": ["Advanced degree", "MS/Ph.D"], "soft_skills": ["analytical skills"]}
CVJ = {"name": "Sam Lee", "skills": ["Python", "SQL", "Tableau", "pandas"], "roles": [], "projects": []}
GAPS = {"fit_summary": "Good core, no big-data stack.", "skills": [], "honest_gaps": ["Spark", "R"]}

BULLET = {"original": "Built Python ETL pipelines feeding Tableau dashboards for 40 stakeholders",
          "rewritten": "Gave 40 stakeholders release visibility by building Python ETL pipelines feeding Tableau "
                       "dashboards, applying analytical skills.", "targets": ["Python"]}
# The first draft repeats what the real run did: degrees as skills and a sentence admitting gaps.
DRAFT = {"headline": "Data Analyst | Python, SQL",
         "summary": "Data scientist with an Advanced degree at the MS/Ph.D tier. No R or Spark experience is shown.",
         "skills_ordered": ["Python", "SQL", "Advanced degree", "MS/Ph.D", "analytical skills", "Tableau"],
         "bullets": [BULLET]}
CLEAN = {"headline": "Data Analyst | Python, SQL, Tableau",
         "summary": "Data scientist (MSc, Distinction) who turns release data into dashboards with Python and SQL.",
         "skills_ordered": ["Python", "SQL", "Tableau", "pandas"],
         "bullets": [{**BULLET, "rewritten": "Gave 40 stakeholders release visibility by building Python ETL "
                                             "pipelines feeding Tableau dashboards."}]}
INVENTED = {**CLEAN, "bullets": [{**BULLET, "rewritten": "Cut reporting time by 35% for 40 stakeholders with "
                                                         "Python ETL pipelines feeding Tableau dashboards."}]}
REVIEW = {"verdict": "revise", "score": 5, "strengths": ["Relevant ETL work chosen"], "issues": [
    {"location": "summary", "kind": "keyword_stuffing", "problem": "Degree written as requirement wording",
     "fix": "State 'MSc Data Science, Distinction' plainly"},
    {"location": "bullet 1", "kind": "keyword_stuffing", "problem": "'applying analytical skills' is bolted on",
     "fix": "Delete the clause"}]}


class Scripted(LLMProvider):
    name = "scripted"

    def __init__(self, revision=CLEAN, review=REVIEW, draft=DRAFT):
        self.revision, self.review, self.draft, self.calls, self.prompts = revision, review, draft, [], {}

    def _complete(self, system, user):
        if "<draft_json>" in user:
            self.calls.append("revise")
            self.prompts["revise"] = user
            return json.dumps(self.revision)
        if "<tailored_json>" in user:
            self.calls.append("review")
            self.prompts["review"] = user
            return json.dumps(self.review)
        if "<keywords_to_use>" in user:
            self.calls.append("rewrite")
            return json.dumps(self.draft)
        if "<job_description>" in user:
            return json.dumps(JOB)
        if "<gaps_json>" not in user and "<job_json>" in user:
            return json.dumps(GAPS)
        self.prompts["parse_cv"] = user
        return json.dumps(CVJ)


def test_draft_problems_are_caught_and_revision_accepted():
    llm = Scripted()
    r = tailor.run(CV, JD, llm)
    assert llm.calls == ["rewrite", "review", "revise"]
    assert "[CONFIRM" not in llm.prompts["parse_cv"] and r.unconfirmed_lines_skipped == 1
    # the reviewer works from the raw posting and original CV, plus the guard's findings on the draft
    assert "<job_posting>" in llm.prompts["review"] and "gap_mention" in llm.prompts["review"]
    assert r.revision == "accepted" and r.review.score == 5
    assert r.tailored.summary == CLEAN["summary"]
    assert r.fact_flags == []
    assert r.ats_after.must_have_missing == ["Spark", "R"]  # gaps stay gaps


def test_first_draft_is_cleaned_even_without_review():
    r = tailor.run(CV, JD, Scripted(), review=False)
    assert "No R or Spark" not in r.tailored.summary and "Spark" not in r.tailored_markdown
    assert r.tailored.skills_ordered == ["Python", "SQL", "Tableau"]  # degrees and soft skills dropped
    assert [f.issue for f in r.fact_flags] == ["gap_mention"]
    assert r.revision == "off"


def test_revision_that_invents_a_number_is_rejected():
    r = tailor.run(CV, JD, Scripted(revision=INVENTED))
    assert r.revision == "rejected" and "unsupported" in r.revision_note
    assert "35%" not in r.tailored_markdown


def test_ready_verdict_skips_revision():
    llm = Scripted(review={"verdict": "ready", "score": 9, "issues": []}, draft=CLEAN)
    r = tailor.run(CV, JD, llm)
    assert llm.calls == ["rewrite", "review"] and r.revision == "not_needed"


@pytest.mark.parametrize("bad_review", ["not json at all", {"verdict": "maybe"}])
def test_broken_review_keeps_the_draft(bad_review):
    class Broken(Scripted):
        def _complete(self, system, user):
            if "<tailored_json>" in user:
                return bad_review if isinstance(bad_review, str) else json.dumps(bad_review)
            return super()._complete(system, user)

    r = tailor.run(CV, JD, Broken())
    assert r.revision == "failed" and r.review is None
    assert r.tailored.skills_ordered == ["Python", "SQL", "Tableau"]
