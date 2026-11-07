"""Deterministic checks that don't trust the LLM: ATS keyword score and the fact guard."""

from __future__ import annotations

import re

from app.models import ATSScore, FactFlag, JobRequirements, TailoredCV
from app.pipeline.skills import find_skills, literal_hit, supported

MUST_WEIGHT = 0.7
NICE_WEIGHT = 0.3


def ats_score(text: str, job: JobRequirements) -> ATSScore:
    """Share of the employer's exact skill phrases found in the text (must-haves weigh 70%)."""
    must_hit = [s for s in job.must_have_skills if literal_hit(text, s)]
    nice_hit = [s for s in job.nice_to_have_skills if literal_hit(text, s)]
    parts, weights = [], []
    if job.must_have_skills:
        parts.append(len(must_hit) / len(job.must_have_skills))
        weights.append(MUST_WEIGHT)
    if job.nice_to_have_skills:
        parts.append(len(nice_hit) / len(job.nice_to_have_skills))
        weights.append(NICE_WEIGHT)
    score = 100 * sum(p * w for p, w in zip(parts, weights, strict=True)) / sum(weights) if weights else 0.0
    return ATSScore(
        score=round(score, 1),
        must_have_hit=must_hit,
        must_have_missing=[s for s in job.must_have_skills if s not in must_hit],
        nice_to_have_hit=nice_hit,
        nice_to_have_missing=[s for s in job.nice_to_have_skills if s not in nice_hit],
    )


_PLACEHOLDER = re.compile(r"\[[^\]]*\]")
# 40%, 3.5x, $1.2M, 500+, 12k, 2019 ... ; ignores digits glued to letters like "S3", "GPT-4", "v2"
_NUMBER = re.compile(r"(?<![A-Za-z0-9\-])[$£€]?\d[\d,]*(?:\.\d+)?\s?(?:%|x|k|K|m|M|bn|\+)?(?![A-Za-z0-9])")


def _numbers(text: str) -> set[str]:
    out = set()
    for m in _NUMBER.findall(_PLACEHOLDER.sub(" ", text)):
        n = m.replace(",", "").replace(" ", "").lstrip("$£€").rstrip("+").lower()
        if n:
            out.add(n)
    return out


def fact_guard(cv_text: str, job: JobRequirements, tailored: TailoredCV) -> tuple[TailoredCV, list[FactFlag]]:
    """
    Flag rewritten bullets that contain numbers or skills not present in the original CV, and
    remove unsupported skills from the skills list outright (that list goes straight onto the CV).
    """
    cv_numbers = _numbers(cv_text)
    flags: list[FactFlag] = []

    for i, b in enumerate(tailored.bullets):
        source = cv_text  # a figure may legitimately move between bullets of the same CV
        for n in sorted(_numbers(b.rewritten) - cv_numbers):
            flags.append(FactFlag(bullet_index=i, rewritten=b.rewritten, issue="new_number",
                                  detail=f"'{n}' does not appear anywhere in your CV"))
        clean = _PLACEHOLDER.sub(" ", b.rewritten)
        for skill in find_skills(clean):
            if not supported(source, skill):
                flags.append(FactFlag(bullet_index=i, rewritten=b.rewritten, issue="new_skill",
                                      detail=f"'{skill}' is not shown anywhere in your CV"))

    kept, dropped = [], []
    for s in tailored.skills_ordered:
        (kept if supported(cv_text, s) else dropped).append(s)
    for s in dropped:
        flags.append(FactFlag(bullet_index=-1, rewritten=s, issue="new_skill",
                              detail=f"Removed '{s}' from the skills list: not shown in your CV"))
    return tailored.model_copy(update={"skills_ordered": kept}), flags


def render_markdown(cv_name: str | None, tailored: TailoredCV, job: JobRequirements) -> str:
    lines = [f"# {cv_name or 'Tailored CV'}", f"**{tailored.headline}**", "", tailored.summary, "",
             "## Skills", ", ".join(tailored.skills_ordered), "", f"## Tailored bullets for: {job.title}"]
    for b in tailored.bullets:
        lines.append(f"- {b.rewritten}")
    return "\n".join(lines) + "\n"
