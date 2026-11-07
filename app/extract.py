"""Turn uploaded CV files (PDF, DOCX, TXT/MD) into plain text."""

from __future__ import annotations

import io

from pypdf import PdfReader


class UnsupportedFile(ValueError):
    pass


def extract_text(filename: str, data: bytes) -> str:
    name = filename.lower()
    if name.endswith(".pdf"):
        reader = PdfReader(io.BytesIO(data))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    if name.endswith(".docx"):
        import docx

        d = docx.Document(io.BytesIO(data))
        parts = [p.text for p in d.paragraphs]
        for table in d.tables:
            for row in table.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts).strip()
    if name.endswith((".txt", ".md")):
        return data.decode("utf-8", errors="replace").strip()
    raise UnsupportedFile("Upload a .pdf, .docx, .txt or .md file")
