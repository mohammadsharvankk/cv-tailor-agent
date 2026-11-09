"""FastAPI application: JSON API + the single-page web UI."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app import export as exporter
from app import local_settings, master
from app.config import Settings, get_settings
from app.extract import UnsupportedFile, extract_text
from app.llm.base import LLMError, LLMProvider
from app.llm.providers import get_provider
from app.models import (
    AnalyzeRequest,
    AnalyzeResponse,
    EvidenceRequest,
    ExportRequest,
    MasterCV,
    SettingsUpdate,
    SettingsView,
)
from app.pipeline import tailor

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
STATIC = Path(__file__).parent / "static"

app = FastAPI(
    title="CV Tailor Agent",
    description="Tailors a CV to a job description with an LLM workflow, ATS scoring and a fact guard.",
    version="1.0.0",
)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


class RateLimit:
    """Sliding one-hour window per visitor, in memory (enough for a single small demo server)."""

    def __init__(self) -> None:
        self.hits: dict[str, deque] = {}
        self.lock = threading.Lock()

    def allow(self, who: str, per_hour: int) -> bool:
        now = time.monotonic()
        with self.lock:
            q = self.hits.setdefault(who, deque())
            while q and now - q[0] > 3600:
                q.popleft()
            if len(q) >= per_hour:
                return False
            q.append(now)
            return True


limiter = RateLimit()


def _visitor(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() or (request.client.host if request.client else "unknown")


def provider_dep(request: Request, settings: Settings = Depends(get_settings)) -> LLMProvider:
    """Local: the provider from .env. Demo: the visitor's own provider and key, sent with this request only."""
    try:
        if settings.mode != "demo":
            return get_provider(settings)
        name = request.headers.get("x-llm-provider", "mock").lower()
        if name not in {"anthropic", "openai", "mock"}:
            raise HTTPException(400, "Unknown provider")
        if name == "mock":
            return get_provider(settings.model_copy(update={"llm_provider": "mock"}))
        key = request.headers.get("x-api-key", "").strip()
        if not key:
            raise HTTPException(400, "Add your API key in Settings, or choose the free rule-based mode.")
        if not limiter.allow(_visitor(request), settings.demo_runs_per_hour):
            raise HTTPException(429, f"Demo limit reached ({settings.demo_runs_per_hour} runs per hour). "
                                     "Please try again later.")
        return get_provider(settings.model_copy(update={"llm_provider": name, f"{name}_api_key": key}))
    except LLMError as err:
        raise HTTPException(status_code=503, detail=str(err)) from err


def _local_only(request: Request, settings: Settings = Depends(get_settings)) -> Settings:
    """Settings can only be changed in local mode, from this machine."""
    host = request.client.host if request.client else ""
    if settings.mode != "local" or host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
        raise HTTPException(403, "Settings can only be changed on your own computer")
    return settings


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health(settings: Settings = Depends(get_settings)) -> dict:
    return {"status": "ok", "provider": settings.llm_provider, "master_cv": settings.master_cv_enabled,
            "review": settings.review_enabled, "mode": settings.mode}


def _view(s: Settings) -> SettingsView:
    return SettingsView(mode=s.mode, provider=s.llm_provider, anthropic_key_set=bool(s.anthropic_api_key),
                        openai_key_set=bool(s.openai_api_key), anthropic_model=s.anthropic_model,
                        openai_model=s.openai_model, review_enabled=s.review_enabled)


@app.get("/api/settings", response_model=SettingsView)
def read_settings(settings: Settings = Depends(get_settings)) -> SettingsView:
    if settings.mode == "demo":
        return SettingsView(mode="demo", provider="mock", review_enabled=settings.review_enabled)
    return _view(settings)


@app.put("/api/settings", response_model=SettingsView)
def write_settings(body: SettingsUpdate, settings: Settings = Depends(_local_only)) -> SettingsView:
    values: dict[str, str] = {}
    if body.provider:
        values["LLM_PROVIDER"] = body.provider
    if body.anthropic_api_key is not None:
        values["ANTHROPIC_API_KEY"] = body.anthropic_api_key
    if body.openai_api_key is not None:
        values["OPENAI_API_KEY"] = body.openai_api_key
    if body.review_enabled is not None:
        values["REVIEW_ENABLED"] = "true" if body.review_enabled else "false"
    # check before writing, so a bad save never leaves the app without a working provider
    provider = body.provider or settings.llm_provider
    if provider != "mock":
        typed = getattr(body, f"{provider}_api_key")
        if not (typed if typed is not None else getattr(settings, f"{provider}_api_key")):
            raise HTTPException(422, f"Add your {provider.title()} API key, or choose the rule-based mode.")
    if values:
        local_settings.update_env(values)
        get_settings.cache_clear()
    return _view(get_settings())


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
    data, name, report = exporter.export(req.cv, req.job, req.tailored, req.format, req.pages, req.style)
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
