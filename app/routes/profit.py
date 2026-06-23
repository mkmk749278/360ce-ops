"""Signal profit tracker — every signal's running and peak profit.

Operator view built around the "let it run" model: a signal is treated as
*Active* (still in flight) until it terminates, and its terminal exit is the
stop. While a signal runs we surface two numbers the raw Signals table does
not put side-by-side:

* **Current profit** — ``pnl_pct`` from the engine (mark-to-entry, live for
  active signals; the realised result for closed ones).
* **Max profit hit** — ``max_favorable_excursion_pct`` (MFE): the best the
  trade ever showed, peak-to-entry, before it gave any of it back. This is the
  number that tells the operator "how much was on the table" versus what the
  exit actually captured.

Both fields are sourced from the engine's ``/api/signals`` (exposed there in
360-v2 alongside this page). Read-only: this route only reshapes the engine's
own signal payload — it never mutates engine state.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, Request

from app.reports import csv_response

router = APIRouter()

# Statuses the engine stamps on a signal that is still in flight. Anything
# else (SL_HIT, INVALIDATED, BREAKEVEN_EXIT, TP*_HIT, EXPIRED, CANCELLED, …)
# is a terminal outcome → the signal is Closed.
_ACTIVE_STATUSES = frozenset({"ACTIVE", "OPEN", "RUNNING"})


def _f(value: Any) -> float | None:
    """Best-effort float; ``None`` when the engine omitted / nulled a field."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _peak_price(entry: float | None, side: str, mfe_pct: float | None) -> float | None:
    """Reconstruct the price at max favorable excursion from entry + MFE.

    The engine tracks MFE as a percentage of entry, not an absolute price, so
    derive the peak so it sits in the same column units as entry / SL /
    current. LONG peaks above entry, SHORT below.
    """
    if entry is None or mfe_pct is None or entry <= 0:
        return None
    frac = mfe_pct / 100.0
    return entry * (1 + frac) if side == "LONG" else entry * (1 - frac)


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
    secs = int((datetime.now(timezone.utc) - ts).total_seconds())
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
    return f"{hours // 24}d ago"


def _normalize(entry: dict) -> dict:
    side = str(entry.get("direction") or entry.get("side") or "").upper()
    raw_status = str(entry.get("status") or "").upper()
    is_active = raw_status in _ACTIVE_STATUSES or raw_status == ""
    entry_px = _f(entry.get("entry") if entry.get("entry") is not None else entry.get("entry_price"))
    sl_px = _f(entry.get("stop_loss") if entry.get("stop_loss") is not None else entry.get("sl"))
    current_px = _f(entry.get("current_price"))
    pnl_pct = _f(entry.get("pnl_pct"))
    mfe_pct = _f(entry.get("max_favorable_excursion_pct"))
    mae_pct = _f(entry.get("max_adverse_excursion_pct"))
    return {
        "id": entry.get("signal_id") or entry.get("id") or "",
        "symbol": entry.get("symbol", ""),
        "side": side,
        "entry": entry_px,
        "sl": sl_px,
        "current": current_px,
        "pnl_pct": pnl_pct,
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "max_price": _peak_price(entry_px, side, mfe_pct),
        "is_active": is_active,
        "status": "Active" if is_active else "Closed",
        # The actual terminal reason (SL_HIT, INVALIDATED, …) for the tooltip,
        # so "Closed" doesn't hide *how* it closed.
        "close_reason": "" if is_active else raw_status,
        "minutes_ago": entry.get("minutes_ago"),
        "created_relative": _format_relative(entry.get("timestamp")),
    }


def _extract_items(payload: Any) -> list[dict]:
    if isinstance(payload, list):
        return [e for e in payload if isinstance(e, dict)]
    if isinstance(payload, dict):
        for key in ("items", "signals"):
            if isinstance(payload.get(key), list):
                return [e for e in payload[key] if isinstance(e, dict)]
    return []


async def _build_rows(request: Request, view: str) -> tuple[list[dict], str | None]:
    """Fetch all signals from the engine and reshape into profit rows.

    ``view`` filters to active / closed / all. Active rows sort first (the
    operator is watching the live book), then closed, each newest-first.
    """
    api = request.app.state.engine_api
    payload = await api.signals(status="all")

    error: str | None = None
    if isinstance(payload, dict) and payload.get("error"):
        error = str(payload.get("error"))

    rows = [_normalize(e) for e in _extract_items(payload)]
    rows = [r for r in rows if r["id"]]

    if view == "active":
        rows = [r for r in rows if r["is_active"]]
    elif view == "closed":
        rows = [r for r in rows if not r["is_active"]]

    # Active first, then by recency (smaller minutes_ago = more recent).
    rows.sort(key=lambda r: (0 if r["is_active"] else 1, r.get("minutes_ago") if r.get("minutes_ago") is not None else 10**9))
    return rows, error


def _summary(rows: list[dict]) -> dict:
    active = [r for r in rows if r["is_active"]]
    closed = [r for r in rows if not r["is_active"]]
    mfes = [r["mfe_pct"] for r in rows if r["mfe_pct"] is not None]
    return {
        "active": len(active),
        "closed": len(closed),
        "best_mfe": max(mfes) if mfes else None,
    }


@router.get("/profit")
async def profit(request: Request, view: str = Query("all", pattern="^(all|active|closed)$")):
    rows, error = await _build_rows(request, view)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "profit.html",
        {
            "request": request,
            "rows": rows,
            "summary": _summary(rows),
            "view": view,
            "error": error,
            "active": "profit",
        },
    )


_EXPORT_COLS = [
    "id", "symbol", "side", "entry", "sl", "current",
    "pnl_pct", "mfe_pct", "mae_pct", "max_price", "status", "close_reason",
]


@router.get("/profit/export.csv")
async def profit_export(request: Request, view: str = Query("all", pattern="^(all|active|closed)$")):
    """Download the (filtered) profit table as CSV — same shape as the page."""
    rows, _ = await _build_rows(request, view)
    data = [[r.get(col) for col in _EXPORT_COLS] for r in rows]
    return csv_response(f"signal_profit_{view}", _EXPORT_COLS, data)
