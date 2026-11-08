"""
The workflow: parse JD -> parse CV -> gap analysis -> X-Y-Z rewrite -> fact guard
-> independent review -> one revision (kept only if it is not worse) -> scores.
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from app.llm.base import LLMError, LLMProvider
from app.models import (
    AnalyzeResponse,
    ATSScore,
    CVProfile,
    FactFlag,
    GapAnalysis,
    JobRequirements,
    Review,
    TailoredCV,
)
from app.pipeline import prompts as P
from app.pipeline.checks import (
    ats_score,
    evidence_backed,
    fact_guard,
    keywords_to_use,
    render_markdown,
    strip_unconfirmed,
    tailored_text_for_scoring,
)

log = logging.getLogger(__name__)

# A revision may lose a few ATS points when it removes stuffed keywords; more than this and the draft wins.
ATS_TOLERANCE = 5.0


def _score(cv_name: str | None, tailored: TailoredCV, job: JobRequirements, cv_text: str) -> tuple[str, ATSScore]:
    md = render_markdown(cv_name, tailored, job)
    return md, ats_score(tailored_text_for_scoring(md, job) + "\n" + cv_text, job)


def _serious(flags: list[FactFlag]) -> int:
    """Flags that still need the candidate's attention. Gap sentences the guard already removed don't count."""
    return sum(1 for f in flags if not f.detail.startswith("Removed"))


def run(cv_text: str, jd_text: str, llm: LLMProvider, review: bool = True) -> AnalyzeResponse:
    t0 = time.perf_counter()
    cv_text, skipped = strip_unconfirmed(cv_text)
    if skipped:
        log.info("skipped %d CV lines marked as unconfirmed", skipped)

    # Steps 1 and 2 are independent, so run them in parallel.
    with ThreadPoolExecutor(max_workers=2) as pool:
        f_job = pool.submit(llm.complete_json, P.PARSE_JD_SYSTEM, P.jd_user(jd_text), JobRequirements, "parse_jd")
        f_cv = pool.submit(llm.complete_json, P.PARSE_CV_SYSTEM, P.cv_user(cv_text), CVProfile, "parse_cv")
        job, cv = f_job.result(), f_cv.result()
    log.info("parsed JD (%d must-have) and CV (%d roles) in %.1fs",
             len(job.must_have_skills), len(cv.roles), time.perf_counter() - t0)

    job_json, cv_json = job.model_dump_json(), cv.model_dump_json()
    gaps = llm.complete_json(P.GAPS_SYSTEM, P.gaps_user(job_json, cv_json, cv_text), GapAnalysis, "gaps")
    backed = evidence_backed(cv_text, gaps, job)
    keywords = keywords_to_use(cv_text, job, backed)
    before = ats_score(cv_text, job)
    log.info("job skills: must=%s nice=%s", job.must_have_skills, job.nice_to_have_skills)
    log.info("asking rewrite to use %d missing-but-supported keywords: %s", len(keywords), keywords)
    draft = llm.complete_json(P.REWRITE_SYSTEM, P.rewrite_user(job_json, cv_json, gaps.model_dump_json(), keywords),
                              TailoredCV, "rewrite")
    tailored, flags = fact_guard(cv_text, job, draft, backed)
    md, after = _score(cv.name, tailored, job, cv_text)

    rev: Review | None = None
    revision, note = "off", None
    if review:
        try:
            # The reviewer sees the raw posting and original CV, not the writer's prompts or gap analysis.
            flags_json = json.dumps([f.model_dump() for f in flags])
            rev = llm.complete_json(P.REVIEW_SYSTEM, P.review_user(jd_text, cv_text, tailored.model_dump_json(),
                                                                   flags_json), Review, "review")
            log.info("review: verdict=%s score=%d issues=%d", rev.verdict, rev.score, len(rev.issues))
            if rev.verdict == "ready" and _serious(flags) == 0:
                revision = "not_needed"
            else:
                revised = llm.complete_json(
                    P.REVISE_SYSTEM,
                    P.revise_user(job_json, cv_json, gaps.model_dump_json(), keywords,
                                  tailored.model_dump_json(), rev.model_dump_json()),
                    TailoredCV, "revise")
                r_tailored, r_flags = fact_guard(cv_text, job, revised, backed)
                r_md, r_after = _score(cv.name, r_tailored, job, cv_text)
                worse_facts = _serious(r_flags) > _serious(flags)
                worse_ats = r_after.score < after.score - ATS_TOLERANCE
                if worse_facts or worse_ats:
                    revision = "rejected"
                    note = ("Kept the first draft: the revision " +
                            ("added unsupported facts" if worse_facts else
                             f"dropped the ATS match from {after.score:.0f} to {r_after.score:.0f}") + ".")
                else:
                    revision = "accepted"
                    note = (f"Revised after review ({len(rev.issues)} issue(s) raised). Open fact flags "
                            f"{_serious(flags)} → {_serious(r_flags)}, ATS {after.score:.0f} → {r_after.score:.0f}.")
                    tailored, flags, md, after = r_tailored, r_flags, r_md, r_after
                log.info("revision %s: %s", revision, note)
        except LLMError as err:  # the review is a bonus: never lose the draft because it failed
            log.warning("review step failed, keeping the draft: %s", err)
            revision, note = "failed", f"The review or revision call failed, so this is the unreviewed draft. ({err})"

    log.info("pipeline finished in %.1fs with %d fact flags", time.perf_counter() - t0, len(flags))
    return AnalyzeResponse(
        provider=llm.name,
        job=job,
        cv=cv,
        gaps=gaps,
        tailored=tailored,
        ats_before=before,
        ats_after=after,
        fact_flags=flags,
        tailored_markdown=md,
        review=rev,
        revision=revision,
        revision_note=note,
        unconfirmed_lines_skipped=skipped,
    )
