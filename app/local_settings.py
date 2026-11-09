"""
Read and update the .env file from the Settings panel (local mode only).

Keys are written to the .env next to the app on your own machine. They are never sent back to the page:
the page only learns whether a key is set.
"""

from __future__ import annotations

import re
from pathlib import Path

ENV_FILE = Path(".env")
EDITABLE = {"LLM_PROVIDER", "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_MODEL", "OPENAI_MODEL",
            "REVIEW_ENABLED"}


def update_env(values: dict[str, str], path: Path = ENV_FILE) -> None:
    """Set KEY=value lines in .env, keeping every other line (and comments) as they are."""
    bad = set(values) - EDITABLE
    if bad:
        raise ValueError(f"Not editable: {', '.join(sorted(bad))}")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    todo = dict(values)
    for i, line in enumerate(lines):
        m = re.match(r"\s*([A-Z_]+)\s*=", line)
        if m and m.group(1) in todo:
            lines[i] = f"{m.group(1)}={_clean(todo.pop(m.group(1)))}"
    lines += [f"{k}={_clean(v)}" for k, v in todo.items()]
    path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _clean(value: str) -> str:
    return re.sub(r"[\r\n]", "", value).strip()
