"""FastAPI application: JSON API + the single-page web UI."""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings
from app.extract import UnsupportedFile, extract_text
from app.llm.base import LLMError, LLMProvider
from app.llm.providers import get_provider
from app.models import AnalyzeRequest, AnalyzeResponse
from app.pipeline import tailor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"

app = FastAPI(
    title="CV Tailor Agent",
    description="Tailors a CV to a job description with an LLM workflow, ATS scoring and a fact guard.",
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def provider_dep() -> LLMProvider:
    try:
        return get_provider()
    except LLMError as err:
        raise HTTPException(status_code=503, detail=str(err)) from err


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health(settings: Settings = Depends(get_settings)) -> dict:
    return {"status": "ok", "provider": settings.llm_provider}


@app.post("/api/extract")
async def extract(file: UploadFile = File(...), settings: Settings = Depends(get_settings)) -> dict:
    data = await file.read()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(413, f"File larger than {settings.max_upload_mb:g} MB")
    try:
        text = extract_text(file.filename or "", data)
    except UnsupportedFile as err:
        raise HTTPException(415, str(err)) from err
    except Exception as err:  # corrupt PDF/DOCX
        raise HTTPException(422, f"Could not read file: {err}") from err
    if len(text) < 50:
        raise HTTPException(422, "Very little text found. Is this a scanned PDF? Paste the text instead.")
    return {"text": text[: settings.max_text_chars]}


@app.post("/api/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest, llm: LLMProvider = Depends(provider_dep),
            settings: Settings = Depends(get_settings)) -> AnalyzeResponse:
    if len(req.cv_text) > settings.max_text_chars or len(req.jd_text) > settings.max_text_chars:
        raise HTTPException(413, f"Each text must be under {settings.max_text_chars} characters")
    try:
        return tailor.run(req.cv_text, req.jd_text, llm)
    except LLMError as err:
        raise HTTPException(502, str(err)) from err
