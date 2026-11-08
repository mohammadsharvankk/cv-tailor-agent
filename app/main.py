"""FastAPI application: JSON API + the single-page web UI."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app import export as exporter
from app import master
from app.config import Settings, get_settings
from app.extract import UnsupportedFile, extract_text
from app.llm.base import LLMError, LLMProvider
from app.llm.providers import get_provider
from app.models import AnalyzeRequest, AnalyzeResponse, EvidenceRequest, ExportRequest, MasterCV
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
    return {"status": "ok", "provider": settings.llm_provider, "master_cv": settings.master_cv_enabled,
            "review": settings.review_enabled}


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
        return tailor.run(req.cv_text, req.jd_text, llm, review=settings.review_enabled)
    except LLMError as err:
        raise HTTPException(502, str(err)) from err


MEDIA = {"pdf": "application/pdf",
         "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}


@app.post("/api/export")
def export_cv(req: ExportRequest) -> Response:
    """Build the finished CV. The ATS read-back report comes in the X-Export-Report header (JSON)."""
    data, name, report = exporter.export(req.cv, req.job, req.tailored, req.format, req.pages)
    return Response(data, media_type=MEDIA[req.format], headers={
        "Content-Disposition": f'attachment; filename="{name}"',
        "X-Export-Report": json.dumps(report.model_dump(), ensure_ascii=True),
        "Access-Control-Expose-Headers": "X-Export-Report, Content-Disposition",
    })


def _master_settings(settings: Settings = Depends(get_settings)) -> Settings:
    if not settings.master_cv_enabled:
        raise HTTPException(404, "Master CV is disabled on this server")
    return settings


@app.get("/api/master", response_model=MasterCV)
def get_master(settings: Settings = Depends(_master_settings)) -> MasterCV:
    text = master.load(settings.master_cv_path)
    return MasterCV(exists=text is not None, text=text or "")


@app.put("/api/master", response_model=MasterCV)
def put_master(body: MasterCV, settings: Settings = Depends(_master_settings)) -> MasterCV:
    if len(body.text) < 50 or len(body.text) > settings.max_text_chars:
        raise HTTPException(422, f"Master CV must be 50-{settings.max_text_chars} characters")
    master.save(settings.master_cv_path, body.text)
    return MasterCV(exists=True, text=master.load(settings.master_cv_path) or "")


@app.post("/api/master/evidence", response_model=MasterCV)
def add_evidence(body: EvidenceRequest, settings: Settings = Depends(_master_settings)) -> MasterCV:
    try:
        text = master.add_evidence(settings.master_cv_path, body.skill, body.evidence)
    except ValueError as err:
        raise HTTPException(422, str(err)) from err
    return MasterCV(exists=True, text=text)
