"""Exit-method backtest — run the engine's large-sample bake-off from ops.

A long-running (minutes) engine-side job, so the page is a **trigger + poll**:
POST starts a background ``docker exec`` run (PRG so a refresh can't re-fire it),
an HTMX status partial polls every few seconds, and when the run finishes the
produced ``signals.csv`` and ``summary.md`` are offered as downloads and the
summary is rendered inline. Owner-only, read-only on the engine (the script only
reads public Binance klines). See ``app/data_sources/exit_backtest.py``.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Form, Request
from starlette.responses import RedirectResponse, StreamingResponse

from app.data_sources.exit_backtest import ExitBacktestParams

router = APIRouter()


def _attachment(text: str, stem: str, ext: str, media: str) -> StreamingResponse:
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    return StreamingResponse(
        iter([text]),
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="{stem}_{stamp}.{ext}"',
            "Cache-Control": "no-store",
        },
    )


@router.get("/exit-backtest")
async def exit_backtest_page(request: Request):
    runner = request.app.state.exit_backtest
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "exit_backtest.html",
        {
            "request": request,
            "active": "exit_backtest",
            "runner": runner,
            "state": runner.snapshot(),
            "default_pairs": runner.default_pairs,
        },
        # Never let a browser serve a cached idle page over a fresh run's state.
        headers={"Cache-Control": "no-store"},
    )


@router.post("/exit-backtest/run")
async def exit_backtest_run(
    request: Request,
    months: float = Form(6.0),
    pairs: str = Form(""),
    entry_tf: str = Form("5m"),
    exit_tf: str = Form("15m"),
    period: int = Form(10),
    mult: float = Form(3.0),
    sar_step: float = Form(0.02),
    sar_max: float = Form(0.2),
    fee_pct: float = Form(0.07),
    funding_bps: float = Form(1.0),
    lookahead: int = Form(20),
    max_forward_bars: int = Form(192),
):
    runner = request.app.state.exit_backtest
    params = ExitBacktestParams.clamped(
        months=months, pairs=pairs, entry_tf=entry_tf, exit_tf=exit_tf,
        period=period, mult=mult, sar_step=sar_step, sar_max=sar_max,
        fee_pct=fee_pct, funding_bps=funding_bps, lookahead=lookahead,
        max_forward_bars=max_forward_bars,
    )
    runner.start(params)
    # PRG: a refresh lands on GET, never re-fires the run.
    return RedirectResponse("/exit-backtest", status_code=303)


@router.get("/_partial/exit-backtest/status")
async def exit_backtest_status(request: Request):
    runner = request.app.state.exit_backtest
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "_exit_backtest_status.html",
        {"request": request, "state": runner.snapshot()},
    )


@router.get("/exit-backtest/download.csv")
async def exit_backtest_csv(request: Request):
    csv_text = request.app.state.exit_backtest.read_csv()
    if not csv_text:
        return RedirectResponse("/exit-backtest", status_code=303)
    return _attachment(csv_text, "exit_backtest_signals", "csv", "text/csv")


@router.get("/exit-backtest/download.md")
async def exit_backtest_summary(request: Request):
    summary_md = request.app.state.exit_backtest.read_summary()
    if not summary_md:
        return RedirectResponse("/exit-backtest", status_code=303)
    return _attachment(summary_md, "exit_backtest_summary", "md", "text/markdown")
