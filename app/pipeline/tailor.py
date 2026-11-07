"""The workflow: parse JD -> parse CV -> gap analysis -> X-Y-Z rewrite -> deterministic checks."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor

from app.llm.base import LLMProvider
from app.models import AnalyzeResponse, CVProfile, GapAnalysis, JobRequirements, TailoredCV
from app.pipeline import prompts as P
from app.pipeline.checks import (
    ats_score,
    evidence_backed,
    fact_guard,
    keywords_to_use,
    render_markdown,
    tailored_text_for_scoring,
)

log = logging.getLogger(__name__)


def run(cv_text: str, jd_text: str, llm: LLMProvider) -> AnalyzeResponse:
    t0 = time.perf_counter()

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
    log.info("missing word-for-word: %s", before.must_have_missing + before.nice_to_have_missing)
    log.info("asking rewrite to use %d missing-but-supported keywords: %s", len(keywords), keywords)
    tailored = llm.complete_json(P.REWRITE_SYSTEM, P.rewrite_user(job_json, cv_json, gaps.model_dump_json(), keywords),
                                 TailoredCV, "rewrite")

    tailored, flags = fact_guard(cv_text, job, tailored, backed)
    md = render_markdown(cv.name, tailored, job)
    log.info("pipeline finished in %.1fs with %d fact flags", time.perf_counter() - t0, len(flags))

    return AnalyzeResponse(
        provider=llm.name,
        job=job,
        cv=cv,
        gaps=gaps,
        tailored=tailored,
        ats_before=before,
        ats_after=ats_score(tailored_text_for_scoring(md, job) + "\n" + cv_text, job),
        fact_flags=flags,
        tailored_markdown=md,
    )
