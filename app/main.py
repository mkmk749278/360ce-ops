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

from app.app_tokens import AppTokenStore
from app.auth_mw import AuthRedirectMiddleware
from app.totp import TotpGate
from app.device_registry import DeviceRegistry
from app.config import load_settings
from app.data_sources.agent_alerts import AgentAlertsReader
from app.data_sources.binance_klines import BinanceKlinesClient
from app.data_sources.dark_signals import DarkSignalTracker
from app.data_sources.data_volume import DataVolumeReader
from app.data_sources.diag_runner import DiagRunner
from app.data_sources.engine_api import EngineApiClient
from app.data_sources.exit_backtest import ExitBacktestRunner
from app.data_sources.free_run import FreeRunTracker
from app.data_sources.monitor_logs import MonitorLogsReader
from app.routes import (
    alerts,
    api_v1,
    audit_status,
    auth,
    control,
    dark_signals,
    data_export,
    diag,
    exit_backtest,
    invalidations,
    pairs,
    performance,
    positions,
    profit,
    pulse,
    raw_edge,
    referrals,
    signal_detail,
    signals,
    strategy_lab,
    truth,
    users,
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
    # TOTP second factor on both login paths (audit F-08). Enabled when
    # OPS_TOTP_SECRET is set; otherwise password-only as before.
    app.state.totp_gate = TotpGate(settings.totp_secret)
    app.state.engine_api = EngineApiClient(settings)
    app.state.app_tokens = AppTokenStore(settings.app_tokens_path)
    app.state.device_registry = DeviceRegistry(settings.device_tokens_path)
    app.state.data_volume = DataVolumeReader(settings)
    app.state.monitor_logs = MonitorLogsReader(settings)
    app.state.diag_runner = DiagRunner(settings)
    app.state.agent_alerts = AgentAlertsReader(settings)
    app.state.binance_klines = BinanceKlinesClient(settings)
    app.state.free_run = FreeRunTracker(settings, app.state.binance_klines)
    app.state.dark_signals = DarkSignalTracker(settings, app.state.binance_klines)
    app.state.exit_backtest = ExitBacktestRunner(settings)
    logger.info("ops up — engine_api=%s data_dir=%s", settings.engine_api_base, settings.engine_data_dir)
    try:
        yield
    finally:
        await app.state.engine_api.aclose()
        await app.state.monitor_logs.aclose()
        await app.state.agent_alerts.aclose()
        await app.state.binance_klines.aclose()


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
app.include_router(api_v1.router)
app.include_router(pulse.router)
app.include_router(truth.router)
app.include_router(signals.router)
app.include_router(signal_detail.router)
app.include_router(pairs.router)
app.include_router(diag.router)
app.include_router(invalidations.router)
app.include_router(performance.router)
app.include_router(raw_edge.router)
app.include_router(strategy_lab.router)
app.include_router(positions.router)
app.include_router(profit.router)
app.include_router(dark_signals.router)
app.include_router(exit_backtest.router)
app.include_router(control.router)
app.include_router(users.router)
app.include_router(referrals.router)
app.include_router(alerts.router)
app.include_router(data_export.router)
app.include_router(audit_status.router)


@app.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok"}
