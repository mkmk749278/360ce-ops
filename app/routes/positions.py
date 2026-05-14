"""Live position-state X-ray — operator view of ``TradeMonitor`` internals.

Consumes the engine's ``/internal/diag/positions`` endpoint (shipped in
360-v2 PR #385) which exposes, per active signal, the inputs that
``TradeMonitor._evaluate_signal`` reads: stored geometry, the 1m candle
wick the monitor is comparing against, candle-feed age, and the
direction-aware ``sl_breach_distance_pct``.

Designed to resolve the 2026-05-13 / 14 position-state desync class of
incident in seconds rather than the SSH+grep cycle it took yesterday.
A row with ``status == "ACTIVE"`` and ``sl_breach_distance_pct <= 0`` is
the smoking gun for monitor-evaluation failure; ``candle_1m_age_sec``
above ~180 s separates "WS feed stale for this symbol" from the
above bug.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()


def _classify_row(item: dict) -> str:
    """Map a diag row to a coarse risk class for the template's row tint.

    - ``sl_breached`` — wick has crossed SL but status is still ACTIVE.
      Almost certainly a monitor-evaluation gap.
    - ``feed_stale`` — candle data older than 180 s, regardless of SL.
      The engine doesn't have fresh data to evaluate against.
    - ``ok`` — everything looks healthy.
    """
    status = (item.get("status") or "").upper()
    sl_breach = item.get("sl_breach_distance_pct")
    candle_age = item.get("candle_1m_age_sec")
    if status == "ACTIVE" and sl_breach is not None and sl_breach <= 0:
        return "sl_breached"
    if candle_age is None or (isinstance(candle_age, (int, float)) and candle_age > 180):
        return "feed_stale"
    return "ok"


def _format_relative(value: Any) -> str | None:
    """Compact "Xm ago" / "Xh ago" for an ISO timestamp string."""
    if value is None or value == "":
        return None
    try:
        ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    secs = int((now - ts).total_seconds())
    if secs < 0:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def _enrich_row(item: dict) -> dict:
    """Add classification + relative-time fields the template renders."""
    return {
        **item,
        "row_class": _classify_row(item),
        "timestamp_relative": _format_relative(item.get("timestamp")),
        "dispatch_relative": _format_relative(item.get("dispatch_timestamp")),
    }


@router.get("/positions")
async def positions_diag(request: Request):
    api = request.app.state.engine_api
    payload = await api.positions_diag()

    error: str | None = None
    items: list[dict] = []
    monitor_running = False
    generated_at: str | None = None

    if isinstance(payload, dict):
        if payload.get("error"):
            error = str(payload.get("error"))
        else:
            raw_items = payload.get("items") or []
            if isinstance(raw_items, list):
                items = [_enrich_row(it) for it in raw_items if isinstance(it, dict)]
            monitor_running = bool(payload.get("monitor_running", False))
            generated_at = payload.get("generated_at")

    # Sort: sl_breached first, then feed_stale, then by age.  The operator
    # cares most about anything that should have closed but didn't.
    _order = {"sl_breached": 0, "feed_stale": 1, "ok": 2}
    items.sort(key=lambda r: (_order.get(r.get("row_class", "ok"), 9), -1 * (r.get("minutes_open") or 0)))

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "positions.html",
        {
            "request": request,
            "rows": items,
            "error": error,
            "monitor_running": monitor_running,
            "generated_at": generated_at,
            "active": "positions",
        },
    )
