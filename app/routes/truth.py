"""Runtime truth-report viewer.

Fetches the structured ``truth_snapshot.json`` and window comparison from the
monitor-logs branch. We render each top-level section as JSON for now — a
future iteration can wire dedicated per-section templates (path funnel,
regime distribution, invalidation audit, etc.) once the dashboard has been
used enough to know which views earn their layout cost."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse

router = APIRouter()


@router.get("/truth")
async def truth(request: Request):
    logs = request.app.state.monitor_logs
    snapshot = await logs.truth_snapshot()
    comparison = await logs.window_comparison()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "truth.html",
        {
            "request": request,
            "snapshot": snapshot,
            "comparison": comparison,
            "active": "truth",
        },
    )


@router.get("/truth/raw.md", response_class=PlainTextResponse)
async def truth_raw_md(request: Request):
    logs = request.app.state.monitor_logs
    return await logs.truth_markdown()


@router.get("/truth/raw.json")
async def truth_raw_json(request: Request):
    logs = request.app.state.monitor_logs
    return JSONResponse(await logs.truth_snapshot())
