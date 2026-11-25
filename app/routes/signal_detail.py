"""Per-signal drill-down.

The live API gives us the in-flight view; the mounted JSON files give us the
durable confidence / geometry / invalidation history. We join everything
available rather than pre-deciding the canonical source — the template
renders whichever sections returned data."""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.ladder import ladder

router = APIRouter()


def _num(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def dossier(live: dict) -> dict[str, Any]:
    """The structured header the page leads with (outside audit, 2026-11-25:
    "Signal Detail is a raw scrollable JSON dump").

    The ladder's axis is the stop the signal was ISSUED with
    (``original_stop_loss``); ``stop_loss`` is mutated in place by BE shifts
    and trails (360-v2 CLAUDE.md, #848), so it is drawn as its own "Stop now"
    mark rather than silently moving the axis under the reader."""
    orig = _num(live.get("original_stop_loss")) or None
    return {
        "ladder": ladder(
            live.get("direction"), orig or live.get("stop_loss"), live.get("entry"),
            live.get("current_price"), live.get("tp1"), live.get("tp2"),
            live.get("tp3"), stop_now=live.get("stop_loss") if orig else None,
        ),
        "pnl": _num(live.get("pnl_pct")),
        "mfe": _num(live.get("max_favorable_excursion_pct")),
        "mae": _num(live.get("max_adverse_excursion_pct")),
        "confidence": _num(live.get("confidence")),
    }


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

    # Key presence, never truthiness: an unmessaged timeout arrives as
    # {"endpoint": ..., "error": ""}, and `not live.get("error")` read that as
    # a successful read (2026-09-03 class).
    live_ok = isinstance(live, dict) and not ("error" in live and "ok" not in live)
    if not (live_ok or perf_row or inval_row or hist_row):
        raise HTTPException(status_code=404, detail=f"signal not found: {signal_id}")

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "signal_detail.html",
        {
            "request": request,
            "signal_id": signal_id,
            "live": live,
            "live_ok": live_ok,
            "d": dossier(live) if live_ok else None,
            "performance": perf_row,
            "invalidation": inval_row,
            "history": hist_row,
            "active": "signals",
        },
    )
