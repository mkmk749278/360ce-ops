"""Per-symbol / per-setup / per-regime rolling stats.

Reducing the mounted ``signal_performance.json`` rather than calling into
``performance_tracker.py`` directly — vendoring that module would couple the
ops repo to the engine's import surface, and the reducer logic here is
straightforward enough that keeping it local is cheaper than the coupling.
If the engine ever publishes pre-reduced rolling stats via the API, this
route should pivot to consume those."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()


def _parse_dt(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _bucket():
    return {"wins": 0, "losses": 0, "neutral": 0, "pnl_sum": 0.0, "n": 0}


def _classify_outcome(label: str) -> str:
    label = (label or "").upper()
    if "TP" in label or "WIN" in label:
        return "win"
    if "SL" in label or "LOSS" in label or "STOP" in label:
        return "loss"
    return "neutral"


def _aggregate(records: Any, window_days: int | None) -> dict:
    by_symbol: dict[str, dict] = defaultdict(_bucket)
    by_setup: dict[str, dict] = defaultdict(_bucket)
    by_regime: dict[str, dict] = defaultdict(_bucket)
    now = datetime.now(tz=timezone.utc)

    rows = records if isinstance(records, list) else []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if window_days is not None:
            ts = _parse_dt(r.get("closed_at") or r.get("terminal_at") or r.get("created_at"))
            if ts is None:
                continue
            if (now - ts).days > window_days:
                continue
        symbol = r.get("symbol", "UNKNOWN")
        setup = r.get("setup_class", "UNKNOWN")
        regime = r.get("regime", "UNKNOWN")
        outcome = _classify_outcome(r.get("outcome_label") or r.get("status") or "")
        try:
            pnl = float(r.get("pnl_pct") or r.get("pnlPct") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        for bucket in (by_symbol[symbol], by_setup[setup], by_regime[regime]):
            bucket["n"] += 1
            bucket["pnl_sum"] += pnl
            bucket["wins" if outcome == "win" else "losses" if outcome == "loss" else "neutral"] += 1

    def _finalize(d: dict[str, dict]) -> list[dict]:
        out: list[dict] = []
        for key, v in d.items():
            n = v["n"] or 1
            out.append({
                "key": key,
                "n": v["n"],
                "wins": v["wins"],
                "losses": v["losses"],
                "neutral": v["neutral"],
                "win_rate": v["wins"] / n if v["n"] else 0.0,
                "avg_pnl": v["pnl_sum"] / n if v["n"] else 0.0,
            })
        out.sort(key=lambda r: r["n"], reverse=True)
        return out

    return {
        "by_symbol": _finalize(by_symbol),
        "by_setup": _finalize(by_setup),
        "by_regime": _finalize(by_regime),
    }


@router.get("/performance")
async def performance(request: Request, window: str = "all"):
    vol = request.app.state.data_volume
    perf = vol.signal_performance()
    if window == "7d":
        days: int | None = 7
    elif window == "30d":
        days = 30
    else:
        days = None
        window = "all"
    agg = _aggregate(perf, days)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "performance.html",
        {"request": request, "agg": agg, "window": window, "active": "performance"},
    )
