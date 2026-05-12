"""Per-signal drill-down.

The live API gives us the in-flight view; the mounted JSON files give us the
durable confidence / geometry / invalidation history. We join everything
available rather than pre-deciding the canonical source — the template
renders whichever sections returned data."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

router = APIRouter()


def _find_record(records: Any, signal_id: str) -> dict | None:
    if isinstance(records, list):
        for r in records:
            if not isinstance(r, dict):
                continue
            if r.get("signal_id") == signal_id or r.get("id") == signal_id:
                return r
    elif isinstance(records, dict):
        if signal_id in records and isinstance(records[signal_id], dict):
            return records[signal_id]
    return None


@router.get("/signals/{signal_id}")
async def signal_detail(request: Request, signal_id: str):
    api = request.app.state.engine_api
    vol = request.app.state.data_volume

    live = await api.signal(signal_id)
    performance = vol.signal_performance()
    invalidation = vol.invalidation_records()
    history = vol.signal_history()

    perf_row = _find_record(performance, signal_id)
    inval_row = _find_record(invalidation, signal_id)
    hist_row = _find_record(history, signal_id)

    live_ok = isinstance(live, dict) and not live.get("error")
    if not (live_ok or perf_row or inval_row or hist_row):
        raise HTTPException(status_code=404, detail=f"signal not found: {signal_id}")

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "signal_detail.html",
        {
            "request": request,
            "signal_id": signal_id,
            "live": live,
            "performance": perf_row,
            "invalidation": inval_row,
            "history": hist_row,
            "active": "signals",
        },
    )
