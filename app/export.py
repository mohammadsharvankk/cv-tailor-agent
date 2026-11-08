"""
Turn a tailoring result into a finished, single-column CV (PDF or DOCX) in the style of Jake's Resume.

1. Assemble: every tailored bullet goes back under the role or project it came from (matched on its
   original text). Roles with no tailored bullet keep up to two original lines, so the work history has
   no holes. Unfilled "[metric: ...]" clauses are removed, so a placeholder can never reach an employer.
2. Fit: render the PDF and measure it. If it is over the page limit, step the font down, then cut the
   lowest-priority bullets one by one (original filler lines first, then the least relevant tailored ones).
3. Verify: read the PDF's text layer back, as an ATS would, and check name, email, phone, section order,
   leftover placeholders and keyword coverage.
"""

from __future__ import annotations

import io
import re
import unicodedata
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from xml.sax.saxutils import escape

from app.models import CVProfile, ExportReport, JobRequirements, TailoredCV
from app.pipeline.checks import ats_score

SECTIONS = ["Summary", "Highlights", "Education", "Experience", "Key Projects", "Technical Skills"]
FILLER = 1000  # priority offset for original (untailored) lines: these are cut first
FONT_SIZES = (10.5, 10.0, 9.5)


# ---------------------------------------------------------------- assembling

@dataclass
class Bullet:
    text: str
    priority: int  # lower = more important; tailored bullets keep the model's relevance order


@dataclass
class Entry:
    title: str
    org: str
    dates: str
    bullets: list[Bullet] = field(default_factory=list)


@dataclass
class CVDoc:
    name: str
    contact: str
    headline: str
    summary: str
    education: list[str]
    roles: list[Entry]
    projects: list[Entry]
    highlights: list[Bullet]
    skills: list[str]

    def all_bullets(self) -> list[Bullet]:
        out = list(self.highlights)
        for e in self.roles + self.projects:
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


def _match(original: str, entries: list[Entry], originals: list[list[str]]) -> int | None:
    """Index of the entry whose original bullets best match this bullet's source text."""
    target, best, best_i = _squash(original), 0.0, None
    if not target:
        return None
    for i, bullets in enumerate(originals):
        for b in bullets:
            cand = _squash(b)
            r = 1.0 if cand == target or (len(target) > 25 and (target in cand or cand in target)) else \
                SequenceMatcher(None, cand, target).ratio()
            if r > best:
                best, best_i = r, i
    return best_i if best >= 0.75 else None


def assemble(cv: CVProfile, tailored: TailoredCV) -> tuple[CVDoc, int]:
    roles = [Entry(r.title, r.organisation, r.dates or "") for r in cv.roles]
    projects = [Entry(p.title, p.organisation, p.dates or "") for p in cv.projects]
    entries = roles + projects
    originals = [r.bullets for r in cv.roles] + [p.bullets for p in cv.projects]
    highlights: list[Bullet] = []
    removed = 0
    for i, b in enumerate(tailored.bullets):
        text, n = clean_placeholders(b.rewritten)
        removed += n
        if not text:
            continue
        idx = _match(b.original, entries, originals)
        (entries[idx].bullets if idx is not None else highlights).append(Bullet(text, i))

    # A role with no tailored line keeps up to two original ones, so the history has no empty jobs.
    for role, orig in zip(roles, [r.bullets for r in cv.roles], strict=True):
        if not role.bullets:
            for k, line in enumerate(orig[:2]):
                text, n = clean_placeholders(line)
                removed += n
                if text:
                    role.bullets.append(Bullet(text, FILLER + 10 * len(roles) + k))
    summary, n = clean_placeholders(tailored.summary)
    headline, m = clean_placeholders(tailored.headline)
    contact = re.sub(r"[\[\]]", "", cv.contact or "").strip()
    doc = CVDoc(
        name=(cv.name or "Curriculum Vitae").strip(), contact=contact, headline=headline, summary=summary,
        education=[e for e in cv.education if e.strip()], roles=roles,
        projects=[p for p in projects if p.bullets], highlights=highlights, skills=list(tailored.skills_ordered),
    )
    return doc, removed + n + m


def without(doc: CVDoc, victim: Bullet) -> CVDoc:
    def strip(e: Entry) -> Entry:
        return replace(e, bullets=[b for b in e.bullets if b is not victim])
    projects = [strip(p) for p in doc.projects]
    return replace(doc, highlights=[b for b in doc.highlights if b is not victim],
                   roles=[strip(r) for r in doc.roles], projects=[p for p in projects if p.bullets])


# ---------------------------------------------------------------- text safety

_SUBS = {"→": "->", "←": "<-", "≥": ">=", "≤": "<=", "✓": "", "★": "", " ": " ", "−": "-"}


def safe(text: str) -> str:
    """Keep text inside the PDF base fonts' character set (cp1252), so nothing renders as a black box."""
    out = []
    for ch in text:
        ch = _SUBS.get(ch, ch)
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            out.append(unicodedata.normalize("NFKD", ch).encode("ascii", "ignore").decode())
    return "".join(out)


_YEAR = re.compile(r"(19|20)\d{2}|present", re.I)


def split_education(line: str) -> tuple[str, str, str]:
    """'MSc X | Uni | Sep 2025 - Sep 2026' -> ('MSc X', 'Uni', 'Sep 2025 - Sep 2026')."""
    parts = [p.strip() for p in re.split(r"\s+\|\s+|\s+·\s+", line) if p.strip()]
    if len(parts) == 1:
        parts = [p.strip() for p in line.split(",") if p.strip()]
    dates = parts.pop() if len(parts) > 1 and _YEAR.search(parts[-1]) and len(parts[-1]) < 40 else ""
    head = parts[0] if parts else line
    return head, ", ".join(parts[1:]), dates


# ---------------------------------------------------------------- PDF

def render_pdf(doc: CVDoc, size: float = FONT_SIZES[0]) -> tuple[bytes, int]:
    from reportlab.lib.enums import TA_CENTER
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    def p(text: str) -> str:
        return escape(safe(text))

    base = ParagraphStyle("base", fontName="Times-Roman", fontSize=size, leading=size * 1.22)
    name_st = ParagraphStyle("name", base, fontName="Times-Bold", fontSize=size + 10, leading=size + 13,
                             alignment=TA_CENTER)
    center = ParagraphStyle("c", base, alignment=TA_CENTER, fontSize=size - 0.5)
    head_st = ParagraphStyle("h", base, fontName="Times-Bold", fontSize=size + 1, spaceBefore=size * 0.7)
    right = ParagraphStyle("r", base, alignment=2)
    bullet_st = ParagraphStyle("b", base, leftIndent=12, bulletIndent=3, spaceBefore=0.6)

    page_w = A4[0] - 2 * 15 * mm
    grid = TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0), ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                       ("TOPPADDING", (0, 0), (-1, -1), 0), ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                       ("VALIGN", (0, 0), (-1, -1), "TOP")])

    def two_col(left: str, rt: str, left_style=base) -> Table:
        t = Table([[Paragraph(left, left_style), Paragraph(rt, right)]], colWidths=[page_w * 0.74, page_w * 0.26])
        t.setStyle(grid)
        return t

    def heading(title: str) -> list:
        return [Paragraph(title.upper(), head_st),
                HRFlowable(width="100%", thickness=0.6, color="black", spaceBefore=1, spaceAfter=3)]

    def bullets(items: list[Bullet]) -> list:
        return [Paragraph(p(b.text), bullet_st, bulletText="•") for b in items]

    def entry(e: Entry, is_role: bool) -> list:
        org = f"<i>{p(e.org)}</i>" if e.org else ""
        if is_role:
            top = [two_col(f"<b>{p(e.title)}</b>", p(e.dates))]
            if org:
                top.append(Paragraph(org, base))
        else:
            top = [two_col(f"<b>{p(e.title)}</b>" + (f" | {org}" if org else ""), p(e.dates))]
        rest = bullets(e.bullets)
        # keep the title with its first bullet, so it never sits alone at the bottom of a page
        return [KeepTogether(top + rest[:1]), *rest[1:], Spacer(1, size * 0.35)]

    story: list = [Paragraph(p(doc.name), name_st)]
    if doc.contact:
        story.append(Paragraph(p(doc.contact), center))
    if doc.headline:
        story.append(Paragraph(f"<b>{p(doc.headline)}</b>", center))
    if doc.summary:
        story += heading("Summary") + [Paragraph(p(doc.summary), base)]
    if doc.highlights:
        story += heading("Highlights") + bullets(doc.highlights)
    if doc.education:
        story += heading("Education")
        for line in doc.education:
            head, rest, dates = split_education(line)
            story.append(two_col(f"<b>{p(head)}</b>" + (f", {p(rest)}" if rest else ""), p(dates)))
    if doc.roles:
        story += heading("Experience")
        for e in doc.roles:
            story += entry(e, True)
    if doc.projects:
        story += heading("Key Projects")
        for e in doc.projects:
            story += entry(e, False)
    if doc.skills:
        story += heading("Technical Skills") + [Paragraph(p(", ".join(doc.skills)), base)]

    buf = io.BytesIO()
    pdf = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm, topMargin=12 * mm,
                            bottomMargin=12 * mm, title=safe(f"{doc.name} CV"), author=safe(doc.name))
    pdf.build(story)
    return buf.getvalue(), pdf.page


def fit(doc: CVDoc, pages: int) -> tuple[CVDoc, bytes, float, int]:
    """Smallest change that fits the page limit. Returns (doc, pdf, font size, bullets dropped)."""
    for size in FONT_SIZES:
        pdf, n = render_pdf(doc, size)
        if n <= pages:
            return doc, pdf, size, 0
    dropped = 0
    while n > pages:
        pool = doc.all_bullets()
        if not pool:
            break
        doc = without(doc, max(pool, key=lambda b: b.priority))
        dropped += 1
        pdf, n = render_pdf(doc, size)
    return doc, pdf, size, dropped


# ---------------------------------------------------------------- DOCX

def render_docx(doc: CVDoc, size: float) -> bytes:
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Mm, Pt

    d = docx.Document()
    sec = d.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    sec.left_margin = sec.right_margin = Mm(15)
    sec.top_margin = sec.bottom_margin = Mm(12)
    text_w = sec.page_width - sec.left_margin - sec.right_margin
    normal = d.styles["Normal"]
    normal.font.name, normal.font.size = "Times New Roman", Pt(size)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.space_before = Pt(0)

    def para(text: str = "", bold=False, italic=False, pt: float | None = None, center=False):
        par = d.add_paragraph()
        if text:
            run = par.add_run(text)
            run.bold, run.italic = bold, italic
            if pt:
                run.font.size = Pt(pt)
        if center:
            par.alignment = WD_ALIGN_PARAGRAPH.CENTER
        return par

    def heading(title: str) -> None:
        par = para(title.upper(), bold=True, pt=size + 1)
        par.paragraph_format.space_before = Pt(size * 0.7)
        par.paragraph_format.space_after = Pt(2)
        border = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        for k, v in {"w:val": "single", "w:sz": "6", "w:space": "1", "w:color": "000000"}.items():
            bottom.set(qn(k), v)
        border.append(bottom)
        par._p.get_or_add_pPr().append(border)

    def row(left: str, right: str, bold_left=True, suffix: str = "") -> None:
        par = d.add_paragraph()
        par.paragraph_format.tab_stops.add_tab_stop(text_w, WD_TAB_ALIGNMENT.RIGHT)
        par.add_run(left).bold = bold_left
        if suffix:
            par.add_run(suffix)
        if right:
            par.add_run("\t" + right)

    def bullets(items: list[Bullet]) -> None:
        for b in items:
            par = d.add_paragraph(b.text, style="List Bullet")
            par.paragraph_format.space_after = Pt(0.6)

    para(doc.name, bold=True, pt=size + 10, center=True)
    if doc.contact:
        para(doc.contact, pt=size - 0.5, center=True)
    if doc.headline:
        para(doc.headline, bold=True, center=True)
    if doc.summary:
        heading("Summary")
        para(doc.summary)
    if doc.highlights:
        heading("Highlights")
        bullets(doc.highlights)
    if doc.education:
        heading("Education")
        for line in doc.education:
            head, rest, dates = split_education(line)
            row(head, dates, suffix=f", {rest}" if rest else "")
    if doc.roles:
        heading("Experience")
        for e in doc.roles:
            row(e.title, e.dates)
            if e.org:
                para(e.org, italic=True)
            bullets(e.bullets)
    if doc.projects:
        heading("Key Projects")
        for e in doc.projects:
            row(e.title, e.dates, suffix=f" | {e.org}" if e.org else "")
            bullets(e.bullets)
    if doc.skills:
        heading("Technical Skills")
        para(", ".join(doc.skills))
    d.core_properties.title, d.core_properties.author = f"{doc.name} CV", doc.name
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------- ATS read-back

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"\+?\d[\d ()-]{7,}\d")


def verify(pdf: bytes, doc: CVDoc, job: JobRequirements) -> tuple[dict[str, bool], float]:
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
    present = [s for s in SECTIONS if s.upper() in flat]
    checks["sections_in_order"] = present == sorted(present, key=lambda s: flat.index(s.upper()))
    checks["no_placeholders"] = "[" not in flat
    return checks, ats_score(flat, job).score


# ---------------------------------------------------------------- entry point

def filename(doc: CVDoc, job: JobRequirements, ext: str) -> str:
    role = re.split(r"\s+[–—-]\s+|,|\(", job.title)[0] if job.title else "CV"
    stem = re.sub(r"[^A-Za-z0-9]+", "_", safe(f"{doc.name} {role}")).strip("_")[:90]
    return f"{stem or 'CV'}.{ext}"


def export(cv: CVProfile, job: JobRequirements, tailored: TailoredCV, fmt: str = "pdf",
           pages: int = 1) -> tuple[bytes, str, ExportReport]:
    doc, removed = assemble(cv, tailored)
    doc, pdf, size, dropped = fit(doc, pages)
    checks, score = verify(pdf, doc, job)
    n_pages = render_pdf(doc, size)[1]
    warnings = []
    if removed:
        warnings.append(f"Took out {removed} unfilled [metric] placeholder(s). Add real figures to your master "
                        "CV to make those bullets stronger.")
    if dropped:
        warnings.append(f"Cut {dropped} lower-priority bullet(s) to fit {pages} page(s).")
    if n_pages > pages:
        warnings.append(f"Still {n_pages} pages: the fixed sections alone are longer than {pages} page(s).")
    if not doc.contact:
        warnings.append("No contact details found in your CV. Add email and phone to the top of your master CV.")
    warnings += [f"ATS check failed: {k.replace('_', ' ')}" for k, ok in checks.items() if not ok]
    if fmt == "docx":
        warnings.append("Word layout can differ slightly from the PDF; check the page count before sending.")
    report = ExportReport(pages=n_pages, bullets_kept=len(doc.all_bullets()), bullets_dropped=dropped,
                          placeholders_removed=removed, text_checks=checks, ats_score=score, warnings=warnings)
    data = render_docx(doc, size) if fmt == "docx" else pdf
    return data, filename(doc, job, fmt), report
