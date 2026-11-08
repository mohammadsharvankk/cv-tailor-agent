"""Typed data contracts shared by the LLM steps, the scoring code and the API."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ---------- Step 1: job description ----------

class JobRequirements(BaseModel):
    title: str = Field(description="Job title as written in the posting")
    company: str | None = None
    seniority: Literal["intern", "junior", "mid", "senior", "lead", "unknown"] = "unknown"
    must_have_skills: list[str] = Field(default_factory=list, description="Required hard skills: tools, languages, methods")
    nice_to_have_skills: list[str] = Field(default_factory=list, description="Preferred hard skills")
    qualifications: list[str] = Field(default_factory=list,
                                      description="Degrees, years of experience, certifications, licences")
    soft_skills: list[str] = Field(default_factory=list, description="e.g. 'communication', 'problem-solving skills'")
    responsibilities: list[str] = Field(default_factory=list)
    domain_keywords: list[str] = Field(default_factory=list, description="Industry or domain terms, e.g. 'fintech', 'CI/CD'")
    years_experience: float | None = None


# ---------- Step 2: CV ----------

class CVRole(BaseModel):
    title: str
    organisation: str = Field(description="Employer or context only, without the city/country")
    location: str | None = Field(default=None, description="City, country (or 'Remote') if written")
    dates: str | None = None
    bullets: list[str] = Field(default_factory=list)


class EducationItem(BaseModel):
    degree: str = Field(description="e.g. 'MSc Data Science'")
    institution: str | None = None
    location: str | None = None
    dates: str | None = None
    details: str | None = Field(default=None, description="Grade, honours, thesis, conferral date, verbatim")


class CVProfile(BaseModel):
    name: str | None = None
    headline: str | None = None
    contact: str | None = Field(default=None, description="Email, phone, location and links, verbatim, joined with ' · '")
    summary: str | None = None
    skills: list[str] = Field(default_factory=list)
    roles: list[CVRole] = Field(default_factory=list)
    projects: list[CVRole] = Field(default_factory=list)
    activities: list[CVRole] = Field(default_factory=list, description="Volunteering, leadership, societies")
    education: list[EducationItem] = Field(default_factory=list)
    languages: list[str] = Field(default_factory=list, description="Spoken languages with level, verbatim")
    interests: list[str] = Field(default_factory=list)

    @field_validator("education", mode="before")
    @classmethod
    def _plain_lines(cls, v):  # accept one string per qualification too
        return [{"degree": x} if isinstance(x, str) else x for x in (v or [])]


# ---------- Step 3: gap analysis ----------

class SkillEvidence(BaseModel):
    skill: str
    status: Literal["strong", "partial", "missing"]
    evidence: str | None = Field(default=None, description="Short quote from the CV that supports the skill, if any")


class GapAnalysis(BaseModel):
    fit_summary: str = Field(description="2-3 sentence honest summary of fit")
    skills: list[SkillEvidence] = Field(default_factory=list)
    transferable_strengths: list[str] = Field(default_factory=list)
    honest_gaps: list[str] = Field(default_factory=list, description="Requirements the CV genuinely does not show")


# ---------- Step 4: rewrite ----------

class BulletRewrite(BaseModel):
    original: str
    rewritten: str = Field(description="X-Y-Z format: Accomplished X, as measured by Y, by doing Z")
    targets: list[str] = Field(default_factory=list, description="Job requirements this bullet now speaks to")
    needs_metric: bool = Field(default=False, description="True if a [metric] placeholder was left for the candidate to fill")


class SkillGroup(BaseModel):
    name: str = Field(description="Short label, e.g. 'Languages & Databases'")
    skills: list[str] = Field(default_factory=list)


class TailoredCV(BaseModel):
    headline: str
    summary: str
    skills_ordered: list[str] = Field(description="CV skills reordered by relevance; never add skills the CV does not show")
    skill_groups: list[SkillGroup] = Field(default_factory=list,
                                           description="skills_ordered split into 3-5 labelled lines for the CV")
    bullets: list[BulletRewrite] = Field(default_factory=list)


# ---------- Deterministic checks ----------

class ATSScore(BaseModel):
    score: float = Field(ge=0, le=100)
    must_have_hit: list[str]
    must_have_missing: list[str]
    nice_to_have_hit: list[str]
    nice_to_have_missing: list[str]


class FactFlag(BaseModel):
    bullet_index: int = Field(description="0-based bullet number; -1 = skills list, -2 = headline or summary")
    rewritten: str
    issue: Literal["new_number", "new_skill", "gap_mention"]
    detail: str


# ---------- Step 5: independent review ----------

class ReviewIssue(BaseModel):
    location: str = Field(description="Where: 'headline', 'summary', 'skills' or 'bullet N' (1-based)")
    kind: Literal["unsupported_claim", "gap_mention", "keyword_stuffing", "weak_bullet", "irrelevant",
                  "generic", "other"]
    problem: str
    fix: str = Field(description="Concrete instruction for the writer, using only facts in the original CV")


class Review(BaseModel):
    verdict: Literal["ready", "revise"]
    score: int = Field(ge=1, le=10, description="How ready this CV content is to send for THIS job")
    strengths: list[str] = Field(default_factory=list)
    issues: list[ReviewIssue] = Field(default_factory=list)


# ---------- API payloads ----------

class AnalyzeRequest(BaseModel):
    cv_text: str = Field(min_length=50)
    jd_text: str = Field(min_length=50)


class AnalyzeResponse(BaseModel):
    provider: str
    job: JobRequirements
    cv: CVProfile
    gaps: GapAnalysis
    tailored: TailoredCV
    ats_before: ATSScore
    ats_after: ATSScore
    fact_flags: list[FactFlag]
    tailored_markdown: str
    review: Review | None = None
    revision: Literal["off", "not_needed", "accepted", "rejected", "failed"] = "off"
    revision_note: str | None = None
    unconfirmed_lines_skipped: int = 0


class MasterCV(BaseModel):
    exists: bool = True
    text: str


class EvidenceRequest(BaseModel):
    skill: str = Field(min_length=1, max_length=80)
    evidence: str = Field(min_length=10, max_length=300, description="One true sentence showing where you used it")


class ExportRequest(BaseModel):
    cv: CVProfile
    job: JobRequirements
    tailored: TailoredCV
    format: Literal["pdf", "docx"] = "pdf"
    pages: Literal[1, 2] = 1
    style: Literal["classic", "harvard"] = "classic"


class ExportReport(BaseModel):
    pages: int
    bullets_kept: int
    bullets_dropped: int = Field(description="Lower-priority bullets cut to fit the page limit")
    bullets_added: int = Field(default=0, description="Extra original lines from your CV added to fill the pages")
    placeholders_removed: int = Field(description="[metric: ...] clauses taken out because no real figure was given")
    text_checks: dict[str, bool] = Field(default_factory=dict, description="What an ATS can read from the PDF text layer")
    ats_score: float | None = None
    warnings: list[str] = Field(default_factory=list)
