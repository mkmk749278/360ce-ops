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

from app.reports import csv_response

router = APIRouter()


def _resolve_window(window: str) -> tuple[str, int | None]:
    """Map a window token to ``(normalized_token, days_or_None)``.  Anything
    unrecognized collapses to all-time so a hand-edited query never 500s."""
    if window == "7d":
        return "7d", 7
    if window == "30d":
        return "30d", 30
    return "all", None


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


# Fixed confidence-band edges for the score → outcome view (research session
# 19).  Ordered so the table reads low→high regardless of per-band volume.
_SCORE_BANDS: list[tuple[str, float, float]] = [
    ("< 65", 0.0, 65.0),
    ("65–70", 65.0, 70.0),
    ("70–75", 70.0, 75.0),
    ("75–80", 75.0, 80.0),
    ("80+", 80.0, 1e9),
]


def _score_band(conf: float | None) -> str | None:
    """Return the fixed band label for a confidence value, or None when the
    record carries no usable confidence (kept out of the band table)."""
    if conf is None:
        return None
    try:
        c = float(conf)
    except (TypeError, ValueError):
        return None
    for label, lo, hi in _SCORE_BANDS:
        if lo <= c < hi:
            return label
    return None


def _pearson(pairs: list[tuple[float, float]]) -> float | None:
    """Pearson correlation of (confidence, pnl) pairs.  Returns None when
    there is too little data or zero variance (a flat series has no defined
    correlation).  This is the headline number: ~0 means the confidence
    score does not predict outcome."""
    n = len(pairs)
    if n < 5:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    syy = sum((p[1] - my) ** 2 for p in pairs)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    return sxy / (sxx ** 0.5 * syy ** 0.5)


def _classify_outcome(label: str) -> str:
    label = (label or "").upper()
    # PROFIT_LOCKED is the engine's dominant positive terminal status (pre-TP
    # banked + residual closed in profit); without it the win column silently
    # undercounted every table on this page.
    if "TP" in label or "WIN" in label or "PROFIT" in label:
        return "win"
    if "SL" in label or "LOSS" in label or "STOP" in label:
        return "loss"
    return "neutral"


def _aggregate(records: Any, window_days: int | None) -> dict:
    by_symbol: dict[str, dict] = defaultdict(_bucket)
    by_setup: dict[str, dict] = defaultdict(_bucket)
    by_regime: dict[str, dict] = defaultdict(_bucket)
    by_score_band: dict[str, dict] = defaultdict(_bucket)
    corr_pairs: list[tuple[float, float]] = []
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
        regime = r.get("entry_regime") or r.get("regime", "UNKNOWN")
        outcome = _classify_outcome(r.get("outcome_label") or r.get("status") or "")
        try:
            pnl = float(r.get("pnl_pct") or r.get("pnlPct") or 0.0)
        except (TypeError, ValueError):
            pnl = 0.0
        targets = [by_symbol[symbol], by_setup[setup], by_regime[regime]]
        # Score band + correlation only when the record carries a confidence.
        band = _score_band(r.get("confidence"))
        if band is not None:
            targets.append(by_score_band[band])
            try:
                corr_pairs.append((float(r.get("confidence")), pnl))
            except (TypeError, ValueError):
                pass
        for bucket in targets:
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

    def _finalize_bands() -> list[dict]:
        # Fixed low→high order so the score → outcome trend reads naturally;
        # empty bands are dropped so the table only shows bands that fired.
        out: list[dict] = []
        for label, _lo, _hi in _SCORE_BANDS:
            v = by_score_band.get(label)
            if not v or v["n"] == 0:
                continue
            n = v["n"]
            out.append({
                "key": label,
                "n": n,
                "wins": v["wins"],
                "losses": v["losses"],
                "neutral": v["neutral"],
                "win_rate": v["wins"] / n,
                "avg_pnl": v["pnl_sum"] / n,
            })
        return out

    corr = _pearson(corr_pairs)
    return {
        "by_symbol": _finalize(by_symbol),
        "by_setup": _finalize(by_setup),
        "by_regime": _finalize(by_regime),
        "by_score_band": _finalize_bands(),
        "confidence_pnl_corr": corr,
        "confidence_pnl_n": len(corr_pairs),
    }


@router.get("/performance")
async def performance(request: Request, window: str = "all"):
    vol = request.app.state.data_volume
    perf = vol.signal_performance()
    window, days = _resolve_window(window)
    agg = _aggregate(perf, days)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "performance.html",
        {"request": request, "agg": agg, "window": window, "active": "performance"},
    )


# Which aggregation each export view maps to, and the label for its first column.
_EXPORT_VIEWS = {
    "symbol": ("by_symbol", "symbol"),
    "setup": ("by_setup", "setup"),
    "regime": ("by_regime", "regime"),
    "score_band": ("by_score_band", "score_band"),
}


@router.get("/performance/export.csv")
async def performance_export(request: Request, window: str = "all", view: str = "symbol"):
    """Download a performance table as CSV.  ``view`` selects the breakdown
    (symbol / setup / regime / score_band); ``window`` matches the page picker."""
    vol = request.app.state.data_volume
    _window, days = _resolve_window(window)
    agg = _aggregate(vol.signal_performance(), days)

    agg_key, label = _EXPORT_VIEWS.get(view, _EXPORT_VIEWS["symbol"])
    table = agg.get(agg_key, [])

    header = [label, "n", "wins", "losses", "neutral", "win_rate_pct", "avg_pnl_pct"]
    rows = [
        [
            r["key"], r["n"], r["wins"], r["losses"], r["neutral"],
            "%.1f" % (r["win_rate"] * 100), "%.4f" % r["avg_pnl"],
        ]
        for r in table
    ]
    return csv_response(f"performance_{label}_{_window}", header, rows)
