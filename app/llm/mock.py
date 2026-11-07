"""
Deterministic, rule-based stand-in for an LLM.

It lets the whole app run with no API key (tests, CI, offline demos). Its output is
plausible but basic; the real value comes from the Anthropic/OpenAI providers.
"""

from __future__ import annotations

import json
import re

from app.llm.base import LLMProvider
from app.pipeline.skills import SKILL_ALIASES, canonical, find_skills, literal_hit, supported

_NICE_HEADING = re.compile(r"(nice to have|nice-to-have|preferred|desirable|bonus|plus)", re.I)
_SECTION = re.compile(r"^(summary|profile|experience|work experience|employment|projects|education|skills|"
                      r"technical skills|certifications)\s*:?\s*$", re.I)
_BULLET = re.compile(r"^\s*[-•*▪–]\s+")


def _tag(text: str, tag: str) -> str:
    m = re.search(rf"<{tag}>\n?(.*?)\n?</{tag}>", text, re.DOTALL)
    return m.group(1) if m else ""


def _employer_wording(jd_text: str, canon: str) -> str:
    """Return the spelling the JD actually used for a canonical skill."""
    for form in [canon, *SKILL_ALIASES.get(canon, [])]:
        m = re.search(r"(?<![A-Za-z0-9])" + re.escape(form) + r"(?![A-Za-z0-9])", jd_text, re.I)
        if m:
            return m.group(0)
    return canon


class MockProvider(LLMProvider):
    name = "mock"

    def _complete(self, system: str, user: str) -> str:  # pragma: no cover - not used
        raise NotImplementedError

    def _mock(self, task: str, user: str) -> str:
        return json.dumps(getattr(self, f"_{task}")(user))

    # ---- step 1 -------------------------------------------------------
    def _parse_jd(self, user: str) -> dict:
        jd = _tag(user, "job_description")
        lines = [ln.strip() for ln in jd.splitlines() if ln.strip()]
        split = next((i for i, ln in enumerate(lines) if _NICE_HEADING.search(ln) and len(ln) < 60), len(lines))
        must_text, nice_text = "\n".join(lines[:split]), "\n".join(lines[split:])
        must = [_employer_wording(jd, s) for s in find_skills(must_text)]
        nice = [_employer_wording(jd, s) for s in find_skills(nice_text) if _employer_wording(jd, s) not in must]
        years = re.search(r"(\d+)\+?\s*years", jd, re.I)
        title = lines[0] if lines else "Unknown role"
        seniority = next((lvl for kw, lvl in [("intern", "intern"), ("junior", "junior"), ("lead", "lead"),
                                              ("senior", "senior")] if kw in title.lower()), "mid")
        return {
            "title": title[:120],
            "company": None,
            "seniority": seniority,
            "must_have_skills": must,
            "nice_to_have_skills": nice,
            "responsibilities": [_BULLET.sub("", ln) for ln in lines if _BULLET.match(ln)][:8],
            "domain_keywords": [],
            "years_experience": float(years.group(1)) if years else None,
        }

    # ---- step 2 -------------------------------------------------------
    def _parse_cv(self, user: str) -> dict:
        cv = _tag(user, "cv")
        lines = [ln.rstrip() for ln in cv.splitlines() if ln.strip()]
        out = {"name": lines[0].strip() if lines else None, "headline": lines[1].strip() if len(lines) > 1 else None,
               "summary": None, "skills": find_skills(cv), "roles": [], "projects": [], "education": []}
        section, current = None, None
        for ln in lines[2:]:
            s = ln.strip()
            if _SECTION.match(s):
                section = s.lower().rstrip(":")
                current = None
                continue
            if section in {"experience", "work experience", "employment", "projects"}:
                bucket = out["projects"] if section == "projects" else out["roles"]
                if _BULLET.match(s):
                    if current is None:
                        current = {"title": "Untitled", "organisation": "", "dates": None, "bullets": []}
                        bucket.append(current)
                    current["bullets"].append(_BULLET.sub("", s))
                else:
                    parts = [p.strip() for p in re.split(r"\s+[|–—]\s+|\s+-\s+", s)]
                    current = {"title": parts[0], "organisation": parts[1] if len(parts) > 1 else "",
                               "dates": parts[2] if len(parts) > 2 else None, "bullets": []}
                    bucket.append(current)
            elif section in {"summary", "profile"}:
                out["summary"] = (out["summary"] + " " + s) if out["summary"] else s
            elif section == "education":
                out["education"].append(_BULLET.sub("", s))
        return out

    # ---- step 3 -------------------------------------------------------
    def _gaps(self, user: str) -> dict:
        job, cv = json.loads(_tag(user, "job_json")), json.loads(_tag(user, "cv_json"))
        cv_text = _tag(user, "cv")
        bullets = [b for r in cv["roles"] + cv["projects"] for b in r["bullets"]]
        skills = []
        for s in job["must_have_skills"] + job["nice_to_have_skills"]:
            ev = next((b for b in bullets if supported(b, s)), None)
            status = "strong" if ev else ("partial" if supported(cv_text, s) else "missing")
            skills.append({"skill": s, "status": status, "evidence": ev[:160] if ev else None})
        missing = [x["skill"] for x in skills if x["status"] == "missing"]
        strong = [x["skill"] for x in skills if x["status"] == "strong"]
        return {
            "fit_summary": (f"The CV demonstrates {len(strong)} of {len(skills)} listed skills in real work. "
                            + (f"Main gaps: {', '.join(missing[:4])}." if missing else "No major skill gaps.")
                            + " (Rule-based mock analysis: connect an LLM provider for a full assessment.)"),
            "skills": skills,
            "transferable_strengths": [r["title"] for r in cv["roles"][:2]],
            "honest_gaps": missing,
        }

    # ---- step 4 -------------------------------------------------------
    def _rewrite(self, user: str) -> dict:
        job, cv = json.loads(_tag(user, "job_json")), json.loads(_tag(user, "cv_json"))
        targets = job["must_have_skills"] + job["nice_to_have_skills"]
        roles = (cv["roles"] + cv["projects"])[:2]
        bullets = []
        for r in roles:
            for b in r["bullets"]:
                hit = [t for t in targets if supported(b, t)]
                text = b.rstrip(".")
                # switch CV wording to the employer's for skills the bullet already shows
                for t in hit:
                    canon = canonical(t)
                    # only swap to the employer's wording when it is the standard name (sklearn -> scikit-learn),
                    # not when it is a looser synonym (ETL -> "data pipelines" would read badly)
                    if not canon or canon.lower() != t.lower():
                        continue
                    for form in ([canon, *SKILL_ALIASES[canon]] if canon else []):
                        if form.lower() != t.lower() and not literal_hit(text, t):
                            text = re.sub(r"(?<![A-Za-z0-9])" + re.escape(form) + r"(?![A-Za-z0-9])", t, text,
                                          count=1, flags=re.I)
                has_metric = bool(re.search(r"\d", text))
                rewritten = text if has_metric else f"{text}, improving [metric: e.g. % time saved or error rate]"
                bullets.append({"original": b, "rewritten": rewritten + ".", "targets": hit,
                                "needs_metric": not has_metric})
        bullets = bullets[:12]
        relevant = [s for s in cv["skills"] if any(canonical(t) == s for t in targets)]
        others = [s for s in cv["skills"] if s not in relevant]
        # use the employer's wording for equivalent skills
        wording = {canonical(t): t for t in targets if canonical(t)}
        ordered = [wording.get(s, s) for s in relevant + others]
        kw = [k.strip() for k in _tag(user, "keywords_to_use").splitlines() if k.strip() and k.strip() != "(none)"]
        ordered = list(dict.fromkeys(kw + ordered))  # employer phrases first, no duplicates
        top = ", ".join(ordered[:3])
        return {
            "headline": cv.get("headline") or "Professional",
            "summary": f"Hands-on experience in {top}, applied to production data problems.",
            "skills_ordered": ordered,
            "bullets": bullets,
        }
