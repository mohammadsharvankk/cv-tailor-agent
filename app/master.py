"""
The master CV: one long, private, truthful record of everything you've done.

The tool tailors from this instead of a one-page CV, so skills that never fit on one page can still
be matched. It lives in a git-ignored folder and is only read or written on your own machine.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

CONFIRMED_HEADING = "## Confirmed skills (added in CV Tailor)"


def load(path: str) -> str | None:
    p = Path(path)
    return p.read_text(encoding="utf-8") if p.exists() else None


def save(path: str, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text.rstrip() + "\n", encoding="utf-8")


def _clean(s: str, limit: int) -> str:
    return re.sub(r"\s+", " ", s).strip()[:limit]


def add_evidence(path: str, skill: str, evidence: str) -> str:
    """Append '- skill: evidence' under the confirmed-skills heading (created if missing). Returns the new text."""
    skill, evidence = _clean(skill, 80), _clean(evidence, 300)
    if not skill or len(evidence) < 10:
        raise ValueError("Give the skill and at least a short sentence of real evidence")
    text = load(path) or ""
    line = f"- {skill}: {evidence} (added {date.today():%b %Y})"
    if CONFIRMED_HEADING in text:
        head, tail = text.split(CONFIRMED_HEADING, 1)
        # insert at the end of that section (before the next "## " heading, if any)
        m = re.search(r"\n## ", tail)
        cut = m.start() if m else len(tail)
        tail = tail[:cut].rstrip() + "\n" + line + "\n" + tail[cut:]
        text = head + CONFIRMED_HEADING + tail
    else:
        text = text.rstrip() + f"\n\n{CONFIRMED_HEADING}\n{line}\n"
    save(path, text)
    return load(path) or ""
