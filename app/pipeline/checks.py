"""Deterministic checks that don't trust the LLM: ATS keyword score and the fact guard."""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from app.models import ATSScore, FactFlag, GapAnalysis, JobRequirements, TailoredCV
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


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9%+#/.]+", " ", text.lower()).strip()


def phrase_supported(cv_text: str, phrase: str) -> bool:
    """
    Alias-aware support for whole employer phrases. "classification models" is supported if every known
    skill inside it is supported ("classification" <- "churn classifier"). A phrase with no known skill
    inside it falls back to a literal match.
    """
    if supported(cv_text, phrase):
        return True
    parts = find_skills(phrase)
    return bool(parts) and all(supported(cv_text, p) for p in parts)


def _quote_in_cv(quote: str | None, cv_squashed: str) -> bool:
    """True if the LLM's evidence quote really comes from the CV (tolerates '...' cuts and small edits)."""
    if not quote:
        return False
    frags = [_squash(f) for f in re.split(r"\.{3}|…", quote)]
    frags = [f for f in frags if len(f) >= 8]
    if not frags:
        return False
    if all(f in cv_squashed for f in frags):
        return True
    words = _squash(quote).split()
    grams = [" ".join(words[i : i + 3]) for i in range(len(words) - 2)]
    return bool(grams) and sum(g in cv_squashed for g in grams) / len(grams) >= 0.8


def _match_gap(skill: str, gaps: GapAnalysis):
    """Find the gap-analysis item for a job skill, even if the model reworded the name slightly."""
    target = _squash(skill)
    best, best_ratio = None, 0.0
    for item in gaps.skills:
        name = _squash(item.skill)
        if name == target:
            return item
        ratio = SequenceMatcher(None, name, target).ratio()
        if target in name or name in target:
            ratio = max(ratio, 0.9)
        if ratio > best_ratio:
            best, best_ratio = item, ratio
    return best if best_ratio >= 0.75 else None


def evidence_backed(cv_text: str, gaps: GapAnalysis, job: JobRequirements | None = None) -> set[str]:
    """
    Employer phrases (lower-case) the CV supports: alias-aware match, or a strong/partial rating whose
    evidence quote is verified in code to come from the CV, so a made-up citation can't justify a skill.
    """
    cv = _squash(cv_text)
    names = (job.must_have_skills + job.nice_to_have_skills) if job else [g.skill for g in gaps.skills]
    backed = set()
    for name in names:
        if phrase_supported(cv_text, name):
            backed.add(name.lower())
            continue
        item = _match_gap(name, gaps)
        if item and item.status != "missing" and _quote_in_cv(item.evidence, cv):
            backed.add(name.lower())
    return backed


def keywords_to_use(cv_text: str, job: JobRequirements, backed: set[str]) -> list[str]:
    """Employer phrases missing word-for-word from the CV, but which the CV demonstrably supports."""
    before = ats_score(cv_text, job)
    missing = before.must_have_missing + before.nice_to_have_missing
    return [k for k in missing if k.lower() in backed]


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


def fact_guard(cv_text: str, job: JobRequirements, tailored: TailoredCV,
               backed: set[str] | None = None) -> tuple[TailoredCV, list[FactFlag]]:
    """
    Flag rewritten bullets that contain numbers or skills not present in the original CV, and
    remove unsupported skills from the skills list outright (that list goes straight onto the CV).
    `backed` holds employer phrases whose support was verified by `evidence_backed`.
    """
    backed = backed or set()
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

    # Headline and summary go straight onto the CV too, so check them for new skills (index -2).
    for field in (tailored.headline, tailored.summary):
        for skill in find_skills(_PLACEHOLDER.sub(" ", field)):
            if not supported(cv_text, skill) and skill.lower() not in backed:
                flags.append(FactFlag(bullet_index=-2, rewritten=field, issue="new_skill",
                                      detail=f"'{skill}' is not shown anywhere in your CV"))

    kept, dropped = [], []
    for s in tailored.skills_ordered:
        (kept if phrase_supported(cv_text, s) or s.lower() in backed else dropped).append(s)
    for s in dropped:
        flags.append(FactFlag(bullet_index=-1, rewritten=s, issue="new_skill",
                              detail=f"Removed '{s}' from the skills list: not shown in your CV"))
    return tailored.model_copy(update={"skills_ordered": kept}), flags


def tailored_text_for_scoring(md: str, job: JobRequirements) -> str:
    """Remove the job title so that echoing it ("Machine Learning Engineer") can't count as a skill match."""
    return re.sub(re.escape(job.title), " ", md, flags=re.I) if job.title else md


def render_markdown(cv_name: str | None, tailored: TailoredCV, job: JobRequirements) -> str:
    lines = [f"# {cv_name or 'Tailored CV'}", f"**{tailored.headline}**", "", tailored.summary, "",
             "## Skills", ", ".join(tailored.skills_ordered), "", "## Experience highlights"]
    for b in tailored.bullets:
        lines.append(f"- {b.rewritten}")
    return "\n".join(lines) + "\n"
