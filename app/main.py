"""360 CE Ops — FastAPI entrypoint.

Middleware ordering matters here. starlette evaluates middleware outside-in in
the reverse order of ``add_middleware`` calls — the LAST middleware added is
the outermost on the request. We therefore register AuthRedirectMiddleware
first (innermost) and SessionMiddleware second (outermost) so the session is
populated by the time the auth check runs.
"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.auth_mw import AuthRedirectMiddleware
from app.config import load_settings
from app.data_sources.data_volume import DataVolumeReader
from app.data_sources.diag_runner import DiagRunner
from app.data_sources.engine_api import EngineApiClient
from app.data_sources.monitor_logs import MonitorLogsReader
from app.routes import (
    auth,
    diag,
    invalidations,
    performance,
    pulse,
    signal_detail,
    signals,
    truth,
)

settings = load_settings()
logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))
logger = logging.getLogger("ops")

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.settings = settings
    app.state.templates = templates
    app.state.engine_api = EngineApiClient(settings)
    app.state.data_volume = DataVolumeReader(settings)
    app.state.monitor_logs = MonitorLogsReader(settings)
    app.state.diag_runner = DiagRunner(settings)
    logger.info("ops up — engine_api=%s data_dir=%s", settings.engine_api_base, settings.engine_data_dir)
    try:
        yield
    finally:
        await app.state.engine_api.aclose()
        await app.state.monitor_logs.aclose()


app = FastAPI(title="360 CE Ops", docs_url=None, redoc_url=None, lifespan=lifespan)

app.add_middleware(AuthRedirectMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.session_secret,
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")

app.include_router(auth.router)
app.include_router(pulse.router)
app.include_router(truth.router)
app.include_router(signals.router)
app.include_router(signal_detail.router)
app.include_router(diag.router)
app.include_router(invalidations.router)
app.include_router(performance.router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}
