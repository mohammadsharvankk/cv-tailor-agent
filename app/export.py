"""
Turn a tailoring result into a finished CV (PDF or DOCX) in one of two single-column styles:

- "classic": Jake's Resume look, Times, job title first, upper-case section headings.
- "harvard": Harvard careers-office (MCS) look, Calibri/Carlito, organisation first, title-case headings.

1. Assemble: every tailored bullet goes back under the role, project or activity it came from (matched on
   its original text). Promotions at one employer share a block. Unfilled "[metric: ...]" clauses are
   removed, so a placeholder can never reach an employer.
2. Fit: the largest font that fits the page limit; if even the smallest is too long, cut the least
   relevant bullets (never a job's last one).
3. Fill: if there's room, add more of the candidate's own original lines (most job-relevant first), then
   spread the spacing so the last page ends near the bottom. Nothing is invented: extra lines are verbatim.
4. Verify: read the PDF text layer back like an ATS and check name, email, phone, section order,
   leftover placeholders and keyword coverage.
"""

from __future__ import annotations

import copy
import io
import re
import unicodedata
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from pathlib import Path
from xml.sax.saxutils import escape

from app.models import CVProfile, EducationItem, ExportReport, JobRequirements, SkillGroup, TailoredCV
from app.pipeline.checks import ats_score
from app.pipeline.skills import supported

FILLER = 1000  # priority offset for original (untailored) lines: these are cut first
FONT_SIZES = (11.0, 10.5, 10.0, 9.5)  # largest that fits wins
GROW_SIZES = (12.0, 11.5)  # only to fill space when the CV has run out of lines
MAX_STRETCH = 1.8  # spacing may grow up to this factor to fill the last page
TARGET_FILL = 0.97  # stop filling when the last page is this full
MARGIN_MM = 15
FONTS = Path(__file__).parent / "fonts"


# ---------------------------------------------------------------- assembling

@dataclass
class Bullet:
    text: str
    priority: int  # lower = more important; tailored bullets keep the model's relevance order


@dataclass
class Entry:
    titles: list[tuple[str, str]]  # (title, dates); several when promoted within one employer
    org: str
    location: str
    bullets: list[Bullet] = field(default_factory=list)
    unused: list[str] = field(default_factory=list)  # original lines not shown yet (used to fill pages)
    continued: bool = False  # same employer as the role above (a promotion): the company line isn't repeated


@dataclass
class Edu:
    degree: str
    institution: str
    location: str
    dates: str
    details: str


@dataclass
class CVDoc:
    name: str
    contact: str
    headline: str
    summary: str
    education: list[Edu]
    roles: list[Entry]
    projects: list[Entry]
    activities: list[Entry]
    highlights: list[Bullet]
    skills: list[str]
    skill_groups: list[SkillGroup] = field(default_factory=list)
    languages: list[str] = field(default_factory=list)
    interests: list[str] = field(default_factory=list)

    def entries(self) -> list[Entry]:
        return self.roles + self.projects + self.activities

    def all_bullets(self) -> list[Bullet]:
        out = list(self.highlights)
        for e in self.entries():
            out += e.bullets
        return out


_MEASURED = re.compile(r",?\s*as measured by\s*\[[^\]]*\]\s*,?", re.I)
_VERB_PH = re.compile(r",?\s*\b(?:improving|reducing|increasing|saving|cutting|raising)\s*\[[^\]]*\]", re.I)
_ANY_PH = re.compile(r"\s*\[[^\]]*\]")


def clean_placeholders(text: str) -> tuple[str, int]:
    """Remove unfilled [metric: ...] clauses and tidy the sentence. Returns (text, number removed)."""
    n = len(_ANY_PH.findall(text))
    if not n:
        return text, 0
    t = _MEASURED.sub(", ", text)
    t = _VERB_PH.sub("", t)
    t = _ANY_PH.sub("", t)
    t = re.sub(r"\s+,", ",", t)
    t = re.sub(r",\s*,", ",", t)
    t = re.sub(r",\s*([.;])", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,")
    if t and t[-1] not in ".!?":
        t += "."
    return t, n


def _squash(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def _similar(a: str, b: str) -> float:
    a, b = _squash(a), _squash(b)
    if not a or not b:
        return 0.0
    if a == b or (len(a) > 25 and (a in b or b in a)):
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _match(original: str, originals: list[list[str]]) -> tuple[int, int] | None:
    """(entry index, bullet index) of the original line this tailored bullet came from."""
    best, where = 0.0, None
    for i, bullets in enumerate(originals):
        for j, b in enumerate(bullets):
            r = _similar(original, b)
            if r > best:
                best, where = r, (i, j)
    return where if best >= 0.75 else None


_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+"
_RANGE = re.compile(rf"((?:{_MONTH})?(?:19|20)\d{{2}})(\s*[–—-]\s*((?:{_MONTH})?(?:19|20)\d{{2}}|Present))?", re.I)


def to_edu(item: EducationItem) -> Edu:
    """Structured item -> Edu. A plain one-line item ('MSc X, Uni, Sep 2025 – Sep 2026, Distinction') is split."""
    if item.institution or item.dates or item.details:
        return Edu(item.degree, item.institution or "", item.location or "", item.dates or "", item.details or "")
    line = item.degree.strip()
    m = _RANGE.search(line)
    dates, before, after = "", line, ""
    if m:
        dates, before, after = m.group(0).strip(), line[: m.start()], line[m.end():]
    parts = [x.strip() for x in re.split(r"\s+\|\s+|,\s+", before.strip(" ,|")) if x.strip()]
    return Edu(parts[0] if parts else line, ", ".join(parts[1:]), "", dates, after.strip(" ,|;."))


def relevant_activity(e: Entry, job: JobRequirements | None) -> bool:
    """Show a volunteer/activity block only when the job asks for what it shows (or a tailored line came from it)."""
    if any(b.priority < FILLER for b in e.bullets):  # the tailoring step picked one of its lines
        return True
    if job is None:
        return False
    text = " ".join([e.org, *[t for t, _ in e.titles], *[b.text for b in e.bullets], *e.unused])
    terms = job.must_have_skills + job.nice_to_have_skills + job.domain_keywords
    return any(supported(text, t) for t in terms)


def assemble(cv: CVProfile, tailored: TailoredCV, job: JobRequirements | None = None) -> tuple[CVDoc, int]:
    sources = [("role", r) for r in cv.roles] + [("project", p) for p in cv.projects] + \
              [("activity", a) for a in cv.activities]
    entries = [Entry([(r.title, r.dates or "")], r.organisation, r.location or "") for _, r in sources]
    originals = [list(r.bullets) for _, r in sources]
    used = [set() for _ in sources]
    highlights: list[Bullet] = []
    removed = 0
    for i, b in enumerate(tailored.bullets):
        text, n = clean_placeholders(b.rewritten)
        removed += n
        if not text:
            continue
        where = _match(b.original, originals)
        if where is None:
            highlights.append(Bullet(text, i))
        else:
            entries[where[0]].bullets.append(Bullet(text, i))
            used[where[0]].add(where[1])
    for e, orig, u in zip(entries, originals, used, strict=True):
        e.unused = [line for j, line in enumerate(orig) if j not in u and not _ANY_PH.search(line)]

    def collect(kind: str) -> list[Entry]:
        """Entries of one kind, in CV order. A role at the same employer as the one above is marked as continued,
        so promotions read as separate roles, each with its own lines, under one company line."""
        out: list[Entry] = []
        for (k, _), e in zip(sources, entries, strict=True):
            if k != kind:
                continue
            if kind == "role" and out and e.org and _squash(e.org) == _squash(out[-1].org):
                e.continued = True
                e.location = e.location or out[-1].location
            out.append(e)
        return out

    roles, projects, activities = collect("role"), collect("project"), collect("activity")
    # Activities (e.g. occasional pro bono work) only appear when this job calls for them.
    activities = [a for a in activities if relevant_activity(a, job)]
    # Every job and activity shows at least one line, so nothing looks empty.
    for k, e in enumerate(roles + activities):
        while len(e.bullets) < (2 if e in roles else 1) and e.unused:
            e.bullets.append(Bullet(e.unused.pop(0), FILLER + 10 * (len(roles) - k) + len(e.bullets)))

    summary, n = clean_placeholders(tailored.summary)
    headline, m = clean_placeholders(tailored.headline)
    doc = CVDoc(
        name=(cv.name or "Curriculum Vitae").strip(), contact=re.sub(r"[\[\]]", "", cv.contact or "").strip(),
        headline=headline, summary=summary, education=[to_edu(e) for e in cv.education if e.degree.strip()],
        roles=roles, projects=[p for p in projects if p.bullets], activities=activities, highlights=highlights,
        skills=list(tailored.skills_ordered), skill_groups=list(tailored.skill_groups),
        languages=[x for x in cv.languages if x.strip()], interests=[x for x in cv.interests if x.strip()],
    )
    return doc, removed + n + m


def without(doc: CVDoc, victim: Bullet) -> CVDoc:
    def strip(e: Entry) -> Entry:
        return replace(e, bullets=[b for b in e.bullets if b is not victim])
    projects = [strip(p) for p in doc.projects]
    return replace(doc, highlights=[b for b in doc.highlights if b is not victim], roles=[strip(r) for r in doc.roles],
                   projects=[p for p in projects if p.bullets], activities=[strip(a) for a in doc.activities])


def reserve_lines(doc: CVDoc, job: JobRequirements) -> list[tuple[int, str]]:
    """Unused original lines as (entry index in doc.entries(), text), most job-relevant first, then most recent."""
    skills = job.must_have_skills + job.nice_to_have_skills + job.domain_keywords
    out = []
    for i, e in enumerate(doc.entries()):
        for j, line in enumerate(e.unused):
            hits = sum(1 for s in skills if supported(line, s))
            out.append((-hits, i, j, line))
    out.sort()
    return [(i, line) for _, i, _, line in out]


def skill_lines(doc: CVDoc) -> list[tuple[str, str]]:
    """[(label, 'a, b, c')], plus languages and (two-page CVs only) interests."""
    lines = [(g.name, ", ".join(g.skills)) for g in doc.skill_groups if g.skills]
    if not lines and doc.skills:
        lines = [("Technical", ", ".join(doc.skills))]
    if doc.languages:
        lines.append(("Languages", ", ".join(doc.languages)))
    if doc.interests:
        lines.append(("Interests", ", ".join(doc.interests)))
    return lines


# ---------------------------------------------------------------- text safety

_SUBS = {"→": "->", "←": "<-", "≥": ">=", "≤": "<=", "✓": "", "★": "", " ": " ", "−": "-"}


def safe(text: str) -> str:
    """Keep text inside a Latin character set, so nothing renders as a black box."""
    out = []
    for ch in text:
        ch = _SUBS.get(ch, ch)
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            out.append(unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode())
    return "".join(out)


# ---------------------------------------------------------------- styles

@dataclass(frozen=True)
class Style:
    regular: str
    bold: str
    italic: str
    docx_font: str
    upper_headings: bool
    org_first: bool  # Harvard: organisation line first, then title; Classic: title first
    name_scale: float
    rule_under_name: bool
    skills_heading: str
    skills_as_bullets: bool


def _register_carlito() -> None:
    from reportlab.lib.fonts import addMapping
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    if "Carlito" in pdfmetrics.getRegisteredFontNames():
        return
    for name, file in [("Carlito", "Carlito-Regular.ttf"), ("Carlito-Bold", "Carlito-Bold.ttf"),
                       ("Carlito-Italic", "Carlito-Italic.ttf")]:
        pdfmetrics.registerFont(TTFont(name, str(FONTS / file)))
    addMapping("Carlito", 0, 0, "Carlito")
    addMapping("Carlito", 1, 0, "Carlito-Bold")
    addMapping("Carlito", 0, 1, "Carlito-Italic")
    addMapping("Carlito", 1, 1, "Carlito-Bold")


STYLES = {
    "classic": Style("Times-Roman", "Times-Bold", "Times-Italic", "Times New Roman", True, False, 2.1, False,
                     "Technical Skills", False),
    "harvard": Style("Carlito", "Carlito-Bold", "Carlito-Italic", "Calibri", False, True, 1.55, True,
                     "Skills & Interests", True),
}


def headings_for(doc: CVDoc, st: Style) -> list[str]:
    names = []
    if doc.summary:
        names.append("Summary")
    if doc.highlights:
        names.append("Highlights")
    if doc.education:
        names.append("Education")
    if doc.roles:
        names.append("Experience")
    if doc.projects:
        names.append("Key Projects")
    if doc.activities:
        names.append("Leadership & Activities")
    if skill_lines(doc):
        names.append(st.skills_heading if doc.interests or not st.skills_as_bullets else "Skills")
    return [n.upper() if st.upper_headings else n for n in names]


# ---------------------------------------------------------------- PDF

def render_pdf(doc: CVDoc, size: float = FONT_SIZES[0], stretch: float = 1.0,
               style: str = "classic") -> tuple[bytes, int, float]:
    """Returns (pdf bytes, page count, how full the last page is from 0 to 1)."""
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import (
        Flowable,
        HRFlowable,
        KeepTogether,
        Paragraph,
        SimpleDocTemplate,
        Spacer,
        Table,
        TableStyle,
    )

    st = STYLES[style]
    if st.regular == "Carlito":
        _register_carlito()

    def p(text: str) -> str:
        return escape(safe(text))

    ink, soft = HexColor("#111111"), HexColor("#444444")
    lead = size * (1.2 + 0.08 * (stretch - 1))
    base = ParagraphStyle("base", fontName=st.regular, fontSize=size, leading=lead, textColor=ink)
    just = ParagraphStyle("just", base, alignment=TA_JUSTIFY)
    second = ParagraphStyle("it", base, fontName=st.italic if not st.org_first else st.regular,
                            textColor=soft if not st.org_first else ink)
    right = ParagraphStyle("r", base, alignment=TA_RIGHT)
    right2 = ParagraphStyle("r2", second, alignment=TA_RIGHT)
    name_st = ParagraphStyle("name", base, fontName=st.bold, fontSize=size * st.name_scale,
                             leading=size * st.name_scale * 1.18, alignment=TA_CENTER)
    contact_st = ParagraphStyle("c", base, alignment=TA_CENTER, fontSize=size - 0.5, leading=lead,
                                textColor=soft if not st.org_first else ink)
    headline_st = ParagraphStyle("hl", base, alignment=TA_CENTER, fontName=st.bold, spaceBefore=1)
    head_st = ParagraphStyle("h", base, fontName=st.bold, fontSize=size + (1.5 if st.upper_headings else 1),
                             leading=size * 1.35, spaceBefore=size * 0.7 * stretch)
    bullet_st = ParagraphStyle("b", just, leftIndent=14 if st.org_first else 13, bulletIndent=3,
                               spaceBefore=size * 0.1 * stretch)

    pdf = SimpleDocTemplate(io.BytesIO(), pagesize=A4, leftMargin=MARGIN_MM * mm, rightMargin=MARGIN_MM * mm,
                            topMargin=12 * mm, bottomMargin=12 * mm)
    width = pdf.width - 12  # the frame keeps 6 pt padding on each side; rows must match the text width
    grid = TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                       ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                       ("VALIGN", (0, 0), (-1, -1), "BOTTOM")])

    def row(left: str, rt: str, ls=base, rs=right) -> Table:
        # the right column is exactly as wide as its text, so the left side gets all the remaining room
        rw = stringWidth(re.sub(r"<[^>]+>", "", rt).replace("&amp;", "&"), rs.fontName, rs.fontSize) + 2 if rt else 0
        t = Table([[Paragraph(left, ls), Paragraph(rt, rs)]], colWidths=[width - rw, rw], hAlign="LEFT")
        t.setStyle(grid)
        return t

    def rule(after: float) -> HRFlowable:
        return HRFlowable(width="100%", thickness=0.7, color=ink, spaceBefore=1.5, spaceAfter=after)

    titles = iter(headings_for(doc, st))

    def heading() -> list:
        return [Paragraph(p(next(titles)), head_st), rule(size * 0.35 * stretch)]

    def bullets(items: list[Bullet]) -> list:
        return [Paragraph(p(b.text), bullet_st, bulletText="•") for b in items]

    def gap() -> Spacer:
        return Spacer(1, size * 0.35 * stretch)

    def entry(e: Entry, kind: str) -> list:
        if kind == "project":
            title, dates = e.titles[0]
            if st.org_first:
                top = [row(f"<b>{p(title)}</b>", p(dates))]
                if e.org:
                    top.append(Paragraph(p(e.org), second))
            else:
                ctx = f" <font color='#444444'>| <i>{p(e.org)}</i></font>" if e.org else ""
                top = [row(f"<b>{p(title)}</b>{ctx}", p(dates))]
        elif st.org_first:  # Harvard: **Organisation** ... location / Title ... dates
            top = [] if e.continued else [row(f"<b>{p(e.org)}</b>", p(e.location))]
            top += [row(p(t), p(d)) for t, d in e.titles]
        else:  # Classic: **Title** ... dates / *Organisation* ... *location*
            top = [row(f"<b>{p(t)}</b>", p(d)) for t, d in e.titles]
            if (e.org or e.location) and not e.continued:
                top.append(row(p(e.org), p(e.location), second, right2))
        rest = bullets(e.bullets)
        # the header stays with its first bullet, so it never sits alone at the bottom of a page
        return [KeepTogether(top + rest[:1]), *rest[1:], gap()]

    def one_line(text: str, ps: ParagraphStyle, floor: float) -> ParagraphStyle:
        """Shrink a centred header line (down to `floor` pt) so it doesn't wrap."""
        fs = ps.fontSize
        while fs > floor and stringWidth(safe(text), ps.fontName, fs) > width:
            fs -= 0.25
        return ParagraphStyle(ps.name + "1", ps, fontSize=fs)

    class End(Flowable):
        """Zero-size marker at the end of the story: records how far down the last page the text reaches."""
        y = 0.0

        def wrap(self, *_):
            return 0, 0

        def draw(self):
            pass

        def drawOn(self, canvas, x, y, _sW=0):  # noqa: N802 - reportlab API
            End.y = y

    story: list = [Paragraph(p(doc.name), name_st)]
    if st.rule_under_name:
        story.append(rule(3))
    if doc.contact:
        story.append(Paragraph(p(doc.contact), one_line(doc.contact, contact_st, 8.0)))
    if doc.headline:
        story.append(Paragraph(p(doc.headline), one_line(doc.headline, headline_st, 9.0)))
    if doc.summary:
        story += heading() + [Paragraph(p(doc.summary), just)]
    if doc.highlights:
        story += heading() + bullets(doc.highlights)
    if doc.education:
        story += heading()
        for ed in doc.education:
            if st.org_first:
                block = [row(f"<b>{p(ed.institution or ed.degree)}</b>", p(ed.location))]
                block.append(row(p(ed.degree if ed.institution else ""), p(ed.dates)))
            else:
                block = [row(f"<b>{p(ed.degree)}</b>", p(ed.dates))]
                if ed.institution or ed.location:
                    block.append(row(p(ed.institution), p(ed.location), second, right2))
            if ed.details:
                block.append(Paragraph(p(ed.details), base))
            story += [KeepTogether(block), gap()]
    for kind, items in (("role", doc.roles), ("project", doc.projects), ("activity", doc.activities)):
        if items:
            story += heading()
            for e in items:
                story += entry(e, kind)
    lines = skill_lines(doc)
    if lines:
        story += heading()
        for label, items in lines:
            text = f"<b>{p(label)}:</b> {p(items)}"
            if st.skills_as_bullets:
                story.append(Paragraph(text, bullet_st, bulletText="•"))
            else:
                story.append(Paragraph(text, ParagraphStyle("s", base, leftIndent=12, firstLineIndent=-12,
                                                            spaceBefore=size * 0.1 * stretch)))
    while story and isinstance(story[-1], Spacer):  # a trailing spacer must not push an empty last page
        story.pop()
    story.append(End())

    buf = io.BytesIO()
    pdf.filename = buf
    pdf.title, pdf.author = safe(f"{doc.name} CV"), safe(doc.name)
    pdf.build(story)
    top, bottom = A4[1] - pdf.topMargin - 6, pdf.bottomMargin + 6
    fill = min(1.0, max(0.0, (top - End.y) / (top - bottom)))
    return buf.getvalue(), pdf.page, fill


@dataclass
class Fit:
    doc: CVDoc
    pdf: bytes
    size: float
    stretch: float
    pages: int
    dropped: int = 0
    added: int = 0


def fit(doc: CVDoc, pages: int, style: str = "classic", job: JobRequirements | None = None) -> Fit:
    """Largest font that fits; cut if it must; then fill the remaining space with real lines and spacing."""
    size, squeeze, dropped, n = FONT_SIZES[-1], 1.0, 0, pages + 1
    for size, squeeze in [(fs, 1.0) for fs in FONT_SIZES] + [(FONT_SIZES[-1], 0.8)]:
        pdf, n, fill = render_pdf(doc, size, squeeze, style)
        if n <= pages:
            break

    def floor(e: Entry) -> int:  # the main (most recent) job keeps three lines, every other block one
        return 3 if doc.roles and e is doc.roles[0] else 1

    while n > pages:  # cut the least relevant line, but never below a block's floor
        spare = list(doc.highlights) + [b for e in doc.entries() if len(e.bullets) > floor(e) for b in e.bullets]
        pool = spare or doc.all_bullets()
        if not pool:
            break
        doc = without(doc, max(pool, key=lambda b: b.priority))
        dropped += 1
        pdf, n, fill = render_pdf(doc, size, squeeze, style)

    # Fill: add the candidate's own unused lines, most relevant first, while they still fit.
    added = 0
    if not dropped and job is not None and fill < TARGET_FILL:
        doc = copy.deepcopy(doc)
        misses = 0
        for k, (idx, line) in enumerate(reserve_lines(doc, job)):
            entry = doc.entries()[idx]
            entry.bullets.append(Bullet(line, 2 * FILLER + k))
            trial, tn, tfill = render_pdf(doc, size, squeeze, style)
            if tn <= pages and (tn > n or tfill <= TARGET_FILL or n < pages):
                pdf, n, fill, added, misses = trial, tn, tfill, added + 1, 0
                if n == pages and fill >= TARGET_FILL - 0.05:
                    break
            else:
                entry.bullets.pop()
                misses += 1
                if misses >= 3:
                    break

    # Out of lines and still short? A slightly larger font, if it keeps the same page count.
    if not dropped and fill < TARGET_FILL - 0.15:
        for big in GROW_SIZES:
            trial, tn, tfill = render_pdf(doc, big, 1.0, style)
            if tn == n and tfill <= TARGET_FILL:
                size, pdf, fill = big, trial, tfill
                break

    # Spread: grow the spacing so the last page ends near the bottom (binary search on the stretch factor).
    best = (squeeze, pdf, fill)
    if fill < TARGET_FILL - 0.03:
        lo, hi = squeeze, MAX_STRETCH
        for _ in range(7):
            mid = (lo + hi) / 2
            trial, tn, tfill = render_pdf(doc, size, mid, style)
            if tn == n and tfill <= TARGET_FILL:
                lo, best = mid, (mid, trial, tfill)
            else:
                hi = mid
    return Fit(doc, best[1], size, best[0], n, dropped, added)


# ---------------------------------------------------------------- DOCX

def render_docx(doc: CVDoc, size: float, stretch: float = 1.0, style: str = "classic") -> bytes:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING, WD_TAB_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt, RGBColor

    st = STYLES[style]
    soft = RGBColor(0x44, 0x44, 0x44)
    black = RGBColor(0x11, 0x11, 0x11)
    d = docx.Document()
    sec = d.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    sec.left_margin = sec.right_margin = Mm(MARGIN_MM)
    sec.top_margin = sec.bottom_margin = Mm(10)  # 2 mm of slack: Word and the PDF engine break lines a little differently
    text_w = sec.page_width - sec.left_margin - sec.right_margin

    def font(style_obj, pt: float, bold=False):
        style_obj.font.name, style_obj.font.size, style_obj.font.bold = st.docx_font, Pt(pt), bold
        style_obj.font.color.rgb = black
        rpr = style_obj.element.get_or_add_rPr()
        fonts = rpr.find(qn("w:rFonts"))
        if fonts is None:
            fonts = OxmlElement("w:rFonts")
            rpr.append(fonts)
        for k in ("w:ascii", "w:hAnsi", "w:eastAsia", "w:cs"):
            fonts.set(qn(k), st.docx_font)
        pf = style_obj.paragraph_format
        pf.space_after = pf.space_before = Pt(0)
        # exact line height, identical to the PDF (Word's "single" spacing for Calibri is looser)
        pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        pf.line_spacing = Pt(pt * (1.2 + 0.08 * (stretch - 1)) if not bold else pt * 1.25)

    font(d.styles["Normal"], size)
    font(d.styles["List Bullet"], size)
    # Real heading styles, so screen readers and parsers see the structure (the template's "accessible" part).
    h1, h2 = d.styles["Heading 1"], d.styles["Heading 2"]
    font(h1, size * st.name_scale, bold=True)
    font(h2, size + (1.5 if st.upper_headings else 1), bold=True)
    for h in (h1, h2):
        h.font.italic = False
        h.paragraph_format.keep_with_next = True

    def border_bottom(par) -> None:
        border = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        for k, v in {"w:val": "single", "w:sz": "6", "w:space": "1", "w:color": "111111"}.items():
            bottom.set(qn(k), v)
        border.append(bottom)
        par._p.get_or_add_pPr().append(border)

    def para(text: str = "", bold=False, italic=False, pt: float | None = None, align=None, color=None):
        par = d.add_paragraph()
        if text:
            run = par.add_run(text)
            run.bold, run.italic = bold, italic
            if pt:
                run.font.size = Pt(pt)
            if color:
                run.font.color.rgb = color
        if align is not None:
            par.alignment = align
        return par

    titles = iter(headings_for(doc, st))

    def heading() -> None:
        par = d.add_paragraph(next(titles), style="Heading 2")
        par.paragraph_format.space_before = Pt(size * 0.7 * stretch)
        par.paragraph_format.space_after = Pt(size * 0.35 * stretch)
        border_bottom(par)

    def row(left: str, right: str, bold=True, italic=False, color=None, suffix: str = "") -> None:
        par = d.add_paragraph()
        par.paragraph_format.tab_stops.add_tab_stop(text_w, WD_TAB_ALIGNMENT.RIGHT)
        par.paragraph_format.keep_with_next = True
        r = par.add_run(left)
        r.bold, r.italic = bold, italic
        if color:
            r.font.color.rgb = color
        if suffix:
            sx = par.add_run(suffix)
            sx.italic, sx.font.color.rgb = True, soft
        if right:
            r2 = par.add_run("\t" + right)
            r2.italic = italic
            if color:
                r2.font.color.rgb = color

    def bullet(text: str, label: str = "") -> None:
        par = d.add_paragraph(style="List Bullet")
        if label:
            par.add_run(f"{label}: ").bold = True
        par.add_run(text)
        par.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
        pf = par.paragraph_format
        pf.space_before, pf.left_indent, pf.first_line_indent = Pt(size * 0.1 * stretch), Pt(14), Pt(-10)

    def gap() -> None:
        para().paragraph_format.line_spacing = Pt(size * 0.35 * stretch)

    name = d.add_paragraph(doc.name, style="Heading 1")
    name.alignment = WD_ALIGN_PARAGRAPH.CENTER
    if st.rule_under_name:
        border_bottom(name)
        name.paragraph_format.space_after = Pt(3)
    def fits(text: str, pdf_font: str, start: float, floor: float) -> float:
        """Largest size (down to floor) at which a header line stays on one line; same metrics as the PDF."""
        from reportlab.pdfbase.pdfmetrics import stringWidth
        if pdf_font.startswith("Carlito"):
            _register_carlito()
        fs = start
        while fs > floor and stringWidth(safe(text), pdf_font, fs) > text_w / 12700 - 12:  # EMU -> pt
            fs -= 0.25
        return fs

    def exact(par, pt: float) -> None:
        par.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        par.paragraph_format.line_spacing = Pt(pt * 1.25)

    if doc.contact:
        fs = fits(doc.contact, st.regular, size - 0.5, 8.0)
        exact(para(doc.contact, pt=fs, align=WD_ALIGN_PARAGRAPH.CENTER, color=None if st.org_first else soft), fs)
    if doc.headline:
        fs = fits(doc.headline, st.bold, size, 9.0)
        exact(para(doc.headline, bold=True, pt=fs, align=WD_ALIGN_PARAGRAPH.CENTER), fs)
    if doc.summary:
        heading()
        para(doc.summary, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
    if doc.highlights:
        heading()
        for b in doc.highlights:
            bullet(b.text)
    if doc.education:
        heading()
        for ed in doc.education:
            if st.org_first:
                row(ed.institution or ed.degree, ed.location)
                row(ed.degree if ed.institution else "", ed.dates, bold=False)
            else:
                row(ed.degree, ed.dates)
                if ed.institution or ed.location:
                    row(ed.institution, ed.location, bold=False, italic=True, color=soft)
            if ed.details:
                para(ed.details)
            gap()
    for kind, items in (("role", doc.roles), ("project", doc.projects), ("activity", doc.activities)):
        if not items:
            continue
        heading()
        for e in items:
            if kind == "project":
                title, dates = e.titles[0]
                if st.org_first:
                    row(title, dates)
                    if e.org:
                        para(e.org)
                else:  # Classic keeps the context on the title line, as in the PDF
                    row(title, dates, suffix=f" | {e.org}" if e.org else "")
            elif st.org_first:
                if not e.continued:
                    row(e.org, e.location)
                for t, dts in e.titles:
                    row(t, dts, bold=False)
            else:
                for t, dts in e.titles:
                    row(t, dts)
                if (e.org or e.location) and not e.continued:
                    row(e.org, e.location, bold=False, italic=True, color=soft)
            for b in e.bullets:
                bullet(b.text)
            gap()
    lines = skill_lines(doc)
    if lines:
        heading()
        for label, items in lines:
            if st.skills_as_bullets:
                bullet(items, label)
            else:
                par = d.add_paragraph()
                par.paragraph_format.left_indent, par.paragraph_format.first_line_indent = Pt(12), Pt(-12)
                par.add_run(f"{label}: ").bold = True
                par.add_run(items)
    d.core_properties.title, d.core_properties.author = f"{doc.name} CV", doc.name
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- ATS read-back

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+?\d[\d ()-]{7,}\d")


def verify(pdf: bytes, doc: CVDoc, job: JobRequirements, style: str = "classic") -> tuple[dict[str, bool], float]:
    from pypdf import PdfReader

    text = "\n".join((pg.extract_text() or "") for pg in PdfReader(io.BytesIO(pdf)).pages)
    flat = re.sub(r"\s+", " ", text)
    checks = {"name_readable": safe(doc.name).lower() in flat.lower()}
    email = _EMAIL.search(doc.contact)
    if email:
        checks["email_readable"] = email.group(0).lower() in flat.lower()
    phone = _PHONE.search(doc.contact)
    if phone:
        digits = re.sub(r"\D", "", phone.group(0))
        checks["phone_readable"] = digits in re.sub(r"\D", "", flat)
    pos, ok = 0, True
    for h in headings_for(doc, STYLES[style]):  # each heading must appear after the previous one
        i = flat.find(h, pos)
        ok, pos = ok and i >= 0, (i if i >= 0 else pos)
    checks["sections_in_order"] = ok
    checks["no_placeholders"] = "[" not in flat
    return checks, ats_score(flat, job).score


# ---------------------------------------------------------------- entry point

def filename(doc: CVDoc, job: JobRequirements, ext: str) -> str:
    role = re.split(r"\s+[–—-]\s+|,|\(", job.title)[0] if job.title else "CV"
    stem = re.sub(r"[^A-Za-z0-9]+", "_", safe(f"{doc.name} {role}")).strip("_")[:90]
    return f"{stem or 'CV'}.{ext}"


def export(cv: CVProfile, job: JobRequirements, tailored: TailoredCV, fmt: str = "pdf", pages: int = 1,
           style: str = "classic") -> tuple[bytes, str, ExportReport]:
    doc, removed = assemble(cv, tailored, job)
    if pages < 2:
        doc.interests = []  # interests only on a two-page CV
    f = fit(doc, pages, style, job)
    checks, score = verify(f.pdf, f.doc, job, style)
    warnings = []
    if removed:
        warnings.append(f"Took out {removed} unfilled [metric] placeholder(s). Add real figures to your master "
                        "CV to make those bullets stronger.")
    if f.dropped:
        warnings.append(f"Cut {f.dropped} lower-priority bullet(s) to fit {pages} page(s).")
    if f.added:
        warnings.append(f"Added {f.added} more line(s) from your master CV, unchanged, to fill the page(s).")
    if pages > 1 and f.pages == 1:
        warnings.append("Even with every line from your CV this is one page, so it was kept to one page. "
                        "Add more detail to your master CV for a full two-page version.")
    if f.pages > pages:
        warnings.append(f"Still {f.pages} pages: the fixed sections alone are longer than {pages} page(s).")
    if pages > 1 and not f.doc.interests:
        warnings.append("No interests found. Add an 'Interests' line to your master CV to show them on two pages.")
    for e in f.doc.roles:
        if not e.bullets:
            warnings.append(f"'{e.titles[0][0]}' has no lines in your master CV. Add two or three bullets under "
                            "that role so it doesn't look empty.")
    if not f.doc.contact:
        warnings.append("No contact details found in your CV. Add email and phone to the top of your master CV.")
    warnings += [f"ATS check failed: {k.replace('_', ' ')}" for k, ok in checks.items() if not ok]
    if fmt == "docx":
        warnings.append("Word layout can differ slightly from the PDF; check the page count before sending.")
    report = ExportReport(pages=f.pages, bullets_kept=len(f.doc.all_bullets()), bullets_dropped=f.dropped,
                          bullets_added=f.added, placeholders_removed=removed, text_checks=checks, ats_score=score,
                          warnings=warnings)
    data = render_docx(f.doc, f.size, f.stretch, style) if fmt == "docx" else f.pdf
    return data, filename(f.doc, job, fmt), report
