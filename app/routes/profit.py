"""Signal profit tracker — every signal replayed under "held to the stop".

This page is a **parallel observation only**. Real trading is untouched: the
engine's exits (pre-TP, invalidation, expiry, TP) still fire for real, and the
live Signals tab / the app keep showing the real outcomes. Here we ignore those
exits and replay Binance 1m candles from each signal's dispatch forward under a
single rule — *exit only at the original stop* — to surface what the live table
cannot:

* **Max profit** — the best the trade ever showed (peak-to-entry) before the
  stop was touched. "How much was on the table."
* **Result (held)** — the SL loss once price first touches the stop, or the
  live P/L while it's still running. The held-to-stop outcome, not the real one.
* **Real exit** — what the engine *actually* did, shown muted alongside, so the
  give-back (max profit vs what the real exit captured) is visible at a glance.

The replay is ``app/data_sources/free_run.py``; the candles come from Binance
Futures public market data. Read-only — this route never mutates engine state.
When candles can't be fetched the row degrades to the engine's own numbers and
the page says so.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, Request

from app.data_sources.free_run import FreeRunResult
from app.reports import csv_response

router = APIRouter()

# Engine statuses that mean the signal is still live (used only to label the
# "Real exit" column — the held-to-stop status comes from the replay).
_ACTIVE_STATUSES = frozenset({"ACTIVE", "OPEN", "RUNNING"})


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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


def _tp_reach(
    mfe_price: float | None,
    tp1: float | None,
    tp2: float | None,
    tp3: float | None,
    side: str,
) -> str | None:
    """Highest TP level the max-profit price touched, or None if none reached."""
    if mfe_price is None:
        return None
    is_long = side == "LONG"
    for label, tp in (("TP3", tp3), ("TP2", tp2), ("TP1", tp1)):
        if tp is None or tp <= 0:
            continue
        if (is_long and mfe_price >= tp) or (not is_long and mfe_price <= tp):
            return label
    return None


def _row(entry: dict, fr: FreeRunResult) -> dict:
    """Combine an engine signal with its held-to-stop replay into a view row."""
    side = str(entry.get("direction") or entry.get("side") or "").upper()
    entry_px = _f(entry.get("entry") if entry.get("entry") is not None else entry.get("entry_price"))
    sl_px = _f(entry.get("stop_loss") if entry.get("stop_loss") is not None else entry.get("sl"))
    tp1 = _f(entry.get("tp1"))
    tp2 = _f(entry.get("tp2"))
    tp3 = _f(entry.get("tp3"))

    raw_status = str(entry.get("status") or "").upper()
    real_is_active = raw_status in _ACTIVE_STATUSES or raw_status == ""
    # Give-back: max profit the held-to-stop run showed vs what the engine's
    # real exit actually realised. Only meaningful once the engine has closed.
    real_pnl = _f(entry.get("pnl_pct"))
    giveback = None
    if not real_is_active and fr.mfe_pct is not None and real_pnl is not None:
        giveback = fr.mfe_pct - real_pnl

    return {
        "id": entry.get("signal_id") or entry.get("id") or "",
        "symbol": entry.get("symbol", ""),
        "side": side,
        "entry": entry_px,
        "sl": sl_px,
        "tp1": tp1,
        "tp2": tp2,
        "tp3": tp3,
        # --- held-to-stop (free run) ---
        "current": fr.current_price,
        "result_pct": fr.result_pct,
        "mfe_pct": fr.mfe_pct,
        "max_price": fr.mfe_price,
        "tp_reach": _tp_reach(fr.mfe_price, tp1, tp2, tp3, side),
        "is_active": fr.is_active,
        "status": "Active" if fr.is_active else "Stopped",
        "capped": fr.capped,
        "degraded": fr.degraded,
        "hold_mins": fr.hold_mins,
        # --- engine's real exit (muted comparison) ---
        "real_is_active": real_is_active,
        "real_status": "" if real_is_active else raw_status,
        "real_pnl_pct": real_pnl,
        "giveback_pct": giveback,
        "setup_class": entry.get("setup_class") or "UNKNOWN",
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
    """Fetch all signals, replay each held-to-stop, reshape into rows.

    ``view`` filters to active / stopped / all on the *held-to-stop* status
    (not the engine's real status). Active rows sort first, then by recency.
    """
    api = request.app.state.engine_api
    tracker = request.app.state.free_run
    payload = await api.signals(status="all", limit=500)

    error: str | None = None
    if isinstance(payload, dict) and payload.get("error"):
        error = str(payload.get("error"))

    items = [e for e in _extract_items(payload) if (e.get("signal_id") or e.get("id"))]
    results = await tracker.compute_many(items)

    rows = []
    for e in items:
        sid = str(e.get("signal_id") or e.get("id") or "")
        fr = results.get(sid)
        if fr is None:
            continue
        rows.append(_row(e, fr))

    if view == "active":
        rows = [r for r in rows if r["is_active"]]
    elif view == "closed":
        rows = [r for r in rows if not r["is_active"]]

    rows.sort(key=lambda r: (0 if r["is_active"] else 1, r.get("minutes_ago") if r.get("minutes_ago") is not None else 10**9))
    return rows, error


def _summary(rows: list[dict]) -> dict:
    active = [r for r in rows if r["is_active"]]
    stopped = [r for r in rows if not r["is_active"]]
    mfes = [r["mfe_pct"] for r in rows if r["mfe_pct"] is not None]
    givebacks = [r["giveback_pct"] for r in rows if r["giveback_pct"] is not None]
    return {
        "active": len(active),
        "closed": len(stopped),
        "best_mfe": max(mfes) if mfes else None,
        "avg_giveback": (sum(givebacks) / len(givebacks)) if givebacks else None,
        "degraded": sum(1 for r in rows if r["degraded"]),
    }


def _group_giveback(rows: list[dict], key: str) -> list[dict]:
    """Aggregate give-back over the engine-closed rows, grouped by ``key``.

    Give-back is held-to-stop max profit minus what the engine's real exit
    realised, so it only exists on rows the engine has actually closed. We sum
    and average it per group, alongside the average max profit and realised P/L
    that produced it, then sort by *total* give-back so the cohort leaving the
    most on the table sits first. A positive total means the held-to-stop run
    would have shown more than the real exit captured (winners cut); a negative
    total means the real exit beat holding (e.g. pre-TP banking a move that a
    pure stop would have surrendered).
    """
    buckets: dict[str, dict] = {}
    for r in rows:
        if r["real_is_active"] or r.get("giveback_pct") is None:
            continue
        name = str(r.get(key) or "UNKNOWN")
        b = buckets.setdefault(name, {"n": 0, "gb": 0.0, "mfe": 0.0, "real": 0.0})
        b["n"] += 1
        b["gb"] += r["giveback_pct"]
        b["mfe"] += r["mfe_pct"] or 0.0
        b["real"] += r["real_pnl_pct"] or 0.0
    out = []
    for name, b in buckets.items():
        n = b["n"]
        out.append({
            "key": name,
            "n": n,
            "total_giveback": b["gb"],
            "avg_giveback": b["gb"] / n,
            "avg_mfe": b["mfe"] / n,
            "avg_real": b["real"] / n,
        })
    out.sort(key=lambda x: x["total_giveback"], reverse=True)
    return out


def _aggregates(rows: list[dict]) -> dict:
    """Give-back rolled up by the engine's real exit type and by setup_class."""
    closed = [r for r in rows if not r["real_is_active"] and r.get("giveback_pct") is not None]
    return {
        "n": len(closed),
        "total_giveback": sum(r["giveback_pct"] for r in closed),
        "by_exit": _group_giveback(rows, "real_status"),
        "by_setup": _group_giveback(rows, "setup_class"),
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
            "aggregates": _aggregates(rows),
            "view": view,
            "error": error,
            "active": "profit",
        },
    )


_EXPORT_COLS = [
    "id", "symbol", "side", "setup_class", "entry", "sl", "tp1", "tp2", "tp3",
    "current", "result_pct", "mfe_pct", "max_price", "tp_reach", "status", "hold_mins",
    "real_status", "real_pnl_pct", "giveback_pct", "degraded",
]


@router.get("/profit/export.csv")
async def profit_export(request: Request, view: str = Query("all", pattern="^(all|active|closed)$")):
    """Download the (filtered) held-to-stop table as CSV — same shape as the page."""
    rows, _ = await _build_rows(request, view)
    data = [[r.get(col) for col in _EXPORT_COLS] for r in rows]
    return csv_response(f"signal_profit_{view}", _EXPORT_COLS, data)
