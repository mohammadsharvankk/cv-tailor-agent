"""
Turn a tailoring result into a finished, single-column CV (PDF or DOCX) in the style of Jake's Resume.

1. Assemble: every tailored bullet goes back under the role or project it came from (matched on its
   original text). Consecutive roles at the same employer (promotions) share one block. A block with
   no tailored bullet keeps up to two original lines, so the history has no holes. Unfilled
   "[metric: ...]" clauses are removed, so a placeholder can never reach an employer.
2. Fit: render the PDF and measure it. Use the largest font that fits the page limit; if even the
   smallest is too long, cut the lowest-priority bullets one by one (filler lines first).
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

from app.models import CVProfile, EducationItem, ExportReport, JobRequirements, SkillGroup, TailoredCV
from app.pipeline.checks import ats_score

SECTIONS = ["Summary", "Highlights", "Education", "Experience", "Key Projects", "Technical Skills"]
FILLER = 1000  # priority offset for original (untailored) lines: these are cut first
FONT_SIZES = (11.0, 10.5, 10.0, 9.5)  # largest that fits wins
MARGIN_MM = 15


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
    highlights: list[Bullet]
    skills: list[str]
    skill_groups: list[SkillGroup] = field(default_factory=list)

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


def _match(original: str, originals: list[list[str]]) -> int | None:
    """Index of the role/project whose original bullets best match this bullet's source text."""
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


_RANGE = re.compile(
    r"((?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+)?(?:19|20)\d{2})"
    r"(\s*[–—-]\s*((?:(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+)?(?:19|20)\d{2}|Present))?",
    re.I)


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
    degree = parts[0] if parts else line
    institution = ", ".join(parts[1:])
    return Edu(degree, institution, "", dates, after.strip(" ,|;."))


def assemble(cv: CVProfile, tailored: TailoredCV) -> tuple[CVDoc, int]:
    n_roles = len(cv.roles)
    entries = [Entry([(r.title, r.dates or "")], r.organisation, r.location or "") for r in cv.roles + cv.projects]
    originals = [r.bullets for r in cv.roles + cv.projects]
    highlights: list[Bullet] = []
    removed = 0
    for i, b in enumerate(tailored.bullets):
        text, n = clean_placeholders(b.rewritten)
        removed += n
        if not text:
            continue
        idx = _match(b.original, originals)
        (entries[idx].bullets if idx is not None else highlights).append(Bullet(text, i))

    # Promotions: consecutive roles at the same employer become one block with stacked titles.
    roles: list[Entry] = []
    role_originals: list[list[str]] = []
    for e, orig in zip(entries[:n_roles], originals[:n_roles], strict=True):
        if roles and e.org and _squash(e.org) == _squash(roles[-1].org):
            last = roles[-1]
            last.titles += e.titles
            last.bullets += e.bullets
            last.location = last.location or e.location
            role_originals[-1] += orig
        else:
            roles.append(e)
            role_originals.append(list(orig))
    for k, (role, orig) in enumerate(zip(roles, role_originals, strict=True)):
        if not role.bullets:  # keep two original lines, so no job is left empty
            for j, line in enumerate(orig[:2]):
                text, n = clean_placeholders(line)
                removed += n
                if text:
                    role.bullets.append(Bullet(text, FILLER + 10 * (len(roles) - k) + j))

    summary, n = clean_placeholders(tailored.summary)
    headline, m = clean_placeholders(tailored.headline)
    doc = CVDoc(
        name=(cv.name or "Curriculum Vitae").strip(), contact=re.sub(r"[\[\]]", "", cv.contact or "").strip(),
        headline=headline, summary=summary, education=[to_edu(e) for e in cv.education if e.degree.strip()],
        roles=roles, projects=[p for p in entries[n_roles:] if p.bullets], highlights=highlights,
        skills=list(tailored.skills_ordered), skill_groups=list(tailored.skill_groups),
    )
    return doc, removed + n + m


def without(doc: CVDoc, victim: Bullet) -> CVDoc:
    def strip(e: Entry) -> Entry:
        return replace(e, bullets=[b for b in e.bullets if b is not victim])
    projects = [strip(p) for p in doc.projects]
    return replace(doc, highlights=[b for b in doc.highlights if b is not victim],
                   roles=[strip(r) for r in doc.roles], projects=[p for p in projects if p.bullets])


def skill_lines(doc: CVDoc) -> list[tuple[str, str]]:
    """[(label, 'a, b, c')]. Falls back to one unlabelled line when the model gave no groups."""
    if doc.skill_groups:
        return [(g.name, ", ".join(g.skills)) for g in doc.skill_groups if g.skills]
    return [("", ", ".join(doc.skills))] if doc.skills else []


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


# ---------------------------------------------------------------- PDF

def render_pdf(doc: CVDoc, size: float = FONT_SIZES[0]) -> tuple[bytes, int]:
    from reportlab.lib.colors import HexColor
    from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.units import mm
    from reportlab.pdfbase.pdfmetrics import stringWidth
    from reportlab.platypus import HRFlowable, KeepTogether, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    def p(text: str) -> str:
        return escape(safe(text))

    ink, soft = HexColor("#111111"), HexColor("#444444")
    lead = size * 1.2
    base = ParagraphStyle("base", fontName="Times-Roman", fontSize=size, leading=lead, textColor=ink)
    just = ParagraphStyle("just", base, alignment=TA_JUSTIFY)
    italic = ParagraphStyle("it", base, fontName="Times-Italic", textColor=soft)
    right = ParagraphStyle("r", base, alignment=TA_RIGHT)
    right_it = ParagraphStyle("ri", italic, alignment=TA_RIGHT)
    name_st = ParagraphStyle("name", base, fontName="Times-Bold", fontSize=size * 2.1, leading=size * 2.4,
                             alignment=TA_CENTER)
    contact_st = ParagraphStyle("c", base, alignment=TA_CENTER, fontSize=size - 0.5, leading=lead, textColor=soft)
    headline_st = ParagraphStyle("hl", base, alignment=TA_CENTER, fontName="Times-Bold", spaceBefore=1)
    head_st = ParagraphStyle("h", base, fontName="Times-Bold", fontSize=size + 1.5, leading=size * 1.35,
                             spaceBefore=size * 0.7)
    bullet_st = ParagraphStyle("b", just, leftIndent=13, bulletIndent=3, spaceBefore=size * 0.1)

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

    def heading(title: str) -> list:
        return [Paragraph(title.upper(), head_st),
                HRFlowable(width="100%", thickness=0.7, color=ink, spaceBefore=1.5, spaceAfter=size * 0.35)]

    def bullets(items: list[Bullet]) -> list:
        return [Paragraph(p(b.text), bullet_st, bulletText="•") for b in items]

    def gap() -> Spacer:
        return Spacer(1, size * 0.35)

    def entry(e: Entry, is_role: bool) -> list:
        if is_role:
            top = [row(f"<b>{p(t)}</b>", p(d)) for t, d in e.titles]
            if e.org or e.location:
                top.append(row(p(e.org), p(e.location), italic, right_it))
        else:
            title, dates = e.titles[0]
            ctx = f" <font color='#444444'>| <i>{p(e.org)}</i></font>" if e.org else ""
            top = [row(f"<b>{p(title)}</b>{ctx}", p(dates))]
        rest = bullets(e.bullets)
        # the header stays with its first bullet, so it never sits alone at the bottom of a page
        return [KeepTogether(top + rest[:1]), *rest[1:], gap()]

    def one_line(text: str, st: ParagraphStyle, floor: float) -> ParagraphStyle:
        """Shrink a centred header line (down to `floor` pt) so it doesn't wrap."""
        fs = st.fontSize
        while fs > floor and stringWidth(safe(text), st.fontName, fs) > width:
            fs -= 0.25
        return ParagraphStyle(st.name + "1", st, fontSize=fs)

    story: list = [Paragraph(p(doc.name), name_st)]
    if doc.contact:
        story.append(Paragraph(p(doc.contact), one_line(doc.contact, contact_st, size - 2.5)))
    if doc.headline:
        story.append(Paragraph(p(doc.headline), one_line(doc.headline, headline_st, size - 1.5)))
    if doc.summary:
        story += heading("Summary") + [Paragraph(p(doc.summary), just)]
    if doc.highlights:
        story += heading("Highlights") + bullets(doc.highlights)
    if doc.education:
        story += heading("Education")
        for ed in doc.education:
            block = [row(f"<b>{p(ed.degree)}</b>", p(ed.dates))]
            if ed.institution or ed.location:
                block.append(row(p(ed.institution), p(ed.location), italic, right_it))
            if ed.details:
                block.append(Paragraph(p(ed.details), base))
            story += [KeepTogether(block), gap()]
    if doc.roles:
        story += heading("Experience")
        for e in doc.roles:
            story += entry(e, True)
    if doc.projects:
        story += heading("Key Projects")
        for e in doc.projects:
            story += entry(e, False)
    lines = skill_lines(doc)
    if lines:
        story += heading("Technical Skills")
        for label, items in lines:
            story.append(Paragraph((f"<b>{p(label)}:</b> " if label else "") + p(items),
                                   ParagraphStyle("s", base, leftIndent=12, firstLineIndent=-12,
                                                  spaceBefore=size * 0.1)))
    # drop a trailing spacer so it can't push an empty last page
    while story and isinstance(story[-1], Spacer):
        story.pop()

    buf = io.BytesIO()
    pdf.filename = buf
    pdf.title, pdf.author = safe(f"{doc.name} CV"), safe(doc.name)
    pdf.build(story)
    return buf.getvalue(), pdf.page


def fit(doc: CVDoc, pages: int) -> tuple[CVDoc, bytes, float, int]:
    """Largest font that fits; otherwise cut least relevant bullets. Returns (doc, pdf, size, dropped)."""
    if pages > 1:  # a half-empty second page looks unfinished: use one page if everything fits there
        for size in FONT_SIZES:
            pdf, n = render_pdf(doc, size)
            if n == 1:
                return doc, pdf, size, 0
    for size in FONT_SIZES:
        pdf, n = render_pdf(doc, size)
        if n <= pages:
            return doc, pdf, size, 0
    dropped = 0
    while n > pages:
        # cut from blocks that have more than one bullet, so no job is left looking empty
        spare = list(doc.highlights) + [b for e in doc.roles + doc.projects if len(e.bullets) > 1 for b in e.bullets]
        pool = spare or doc.all_bullets()
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
    from docx.shared import Mm, Pt, RGBColor

    soft = RGBColor(0x44, 0x44, 0x44)
    d = docx.Document()
    sec = d.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    sec.left_margin = sec.right_margin = Mm(MARGIN_MM)
    sec.top_margin = sec.bottom_margin = Mm(12)
    text_w = sec.page_width - sec.left_margin - sec.right_margin
    normal = d.styles["Normal"]
    normal.font.name, normal.font.size = "Times New Roman", Pt(size)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    fmt = normal.paragraph_format
    fmt.space_after = fmt.space_before = Pt(0)
    fmt.line_spacing = 1.05

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

    def heading(title: str) -> None:
        par = para(title.upper(), bold=True, pt=size + 1.5)
        par.paragraph_format.space_before = Pt(size * 0.85)
        par.paragraph_format.space_after = Pt(size * 0.35)
        border = OxmlElement("w:pBdr")
        bottom = OxmlElement("w:bottom")
        for k, v in {"w:val": "single", "w:sz": "6", "w:space": "1", "w:color": "111111"}.items():
            bottom.set(qn(k), v)
        border.append(bottom)
        par._p.get_or_add_pPr().append(border)

    def row(left: str, right: str, bold=True, italic=False, suffix: str = "") -> None:
        par = d.add_paragraph()
        par.paragraph_format.tab_stops.add_tab_stop(text_w, WD_TAB_ALIGNMENT.RIGHT)
        r = par.add_run(left)
        r.bold, r.italic = bold, italic
        if italic:
            r.font.color.rgb = soft
        if suffix:
            s = par.add_run(suffix)
            s.italic, s.font.color.rgb = True, soft
        if right:
            r2 = par.add_run("\t" + right)
            r2.italic = italic
            if italic:
                r2.font.color.rgb = soft

    def bullets(items: list[Bullet]) -> None:
        for b in items:
            par = d.add_paragraph(b.text, style="List Bullet")
            par.alignment = WD_ALIGN_PARAGRAPH.JUSTIFY
            pf = par.paragraph_format
            pf.space_before, pf.left_indent, pf.first_line_indent = Pt(size * 0.1), Pt(13), Pt(-10)

    def gap() -> None:
        para().paragraph_format.line_spacing = Pt(size * 0.45)

    para(doc.name, bold=True, pt=size * 2.1, align=WD_ALIGN_PARAGRAPH.CENTER)
    if doc.contact:
        para(doc.contact, pt=size - 0.5, align=WD_ALIGN_PARAGRAPH.CENTER, color=soft)
    if doc.headline:
        para(doc.headline, bold=True, align=WD_ALIGN_PARAGRAPH.CENTER)
    if doc.summary:
        heading("Summary")
        para(doc.summary, align=WD_ALIGN_PARAGRAPH.JUSTIFY)
    if doc.highlights:
        heading("Highlights")
        bullets(doc.highlights)
    if doc.education:
        heading("Education")
        for ed in doc.education:
            row(ed.degree, ed.dates)
            if ed.institution or ed.location:
                row(ed.institution, ed.location, bold=False, italic=True)
            if ed.details:
                para(ed.details)
            gap()
    if doc.roles:
        heading("Experience")
        for e in doc.roles:
            for t, dts in e.titles:
                row(t, dts)
            if e.org or e.location:
                row(e.org, e.location, bold=False, italic=True)
            bullets(e.bullets)
            gap()
    if doc.projects:
        heading("Key Projects")
        for e in doc.projects:
            title, dates = e.titles[0]
            row(title, dates, suffix=f" | {e.org}" if e.org else "")
            bullets(e.bullets)
            gap()
    lines = skill_lines(doc)
    if lines:
        heading("Technical Skills")
        for label, items in lines:
            par = d.add_paragraph()
            par.paragraph_format.left_indent, par.paragraph_format.first_line_indent = Pt(12), Pt(-12)
            if label:
                par.add_run(f"{label}: ").bold = True
            par.add_run(items)
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
    if pages > 1 and n_pages == 1:
        warnings.append("Everything fits on one page, so the CV was kept to one page.")
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
