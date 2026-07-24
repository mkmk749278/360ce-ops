"""Dark Signals — trailing-exit bake-off (Performance tab).

A **parallel observation only**: every real signal is replayed keeping only its
entry price + direction, with its TPs discarded, under three trailing-only exit
methods (ATR-trail, SuperTrend flip, Parabolic SAR). It answers the owner's
question — *are our signals directionally right but leaking on the exit?* — with
realised P/L per method, per market regime, on real candles. Nothing here runs on
the live money path; the engine's real exits are shown alongside as the baseline
to beat.

The heavy lifting (indicators, no-TP trailing sim, fee+funding accounting,
candle fetch/cache) lives in ``app/data_sources/dark_signals.py``. This route
fetches the signal cohort, runs the (cheap, pure) sim under the selected knobs,
and rolls the results up per method / regime / setup.

Read the numbers as **expectancy + profit factor + capture**, not win-rate:
dropping TPs deliberately lowers hit-rate and raises payoff (fewer, bigger
winners; more small trailed losers), so a lower win-rate is the design, not a
regression.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Query, Request

from app.data_sources.dark_signals import (
    METHOD_LABELS,
    METHODS,
    DarkSignalResult,
    SimParams,
)
from app.reports import csv_response

router = APIRouter()

_ACTIVE_STATUSES = frozenset({"ACTIVE", "OPEN", "RUNNING"})
_WINDOW_DAYS: dict[str, float | None] = {
    "live": None, "24h": 1.0, "3d": 3.0, "7d": 7.0, "30d": 30.0, "all": None,
}
_PAGE_SIZE = 50


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch_to_iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _format_relative(value: Any) -> str | None:
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


def _perf_to_signal(r: dict) -> dict:
    """Map a signal_performance.json record to the tracker's expected shape."""
    ts_iso = _epoch_to_iso(
        r.get("dispatch_timestamp") or r.get("create_timestamp") or r.get("timestamp")
    )
    return {
        "signal_id": r.get("signal_id") or "",
        "id": r.get("signal_id") or "",
        "symbol": r.get("symbol") or "",
        "direction": (r.get("direction") or "").upper(),
        "entry": r.get("entry"),
        "entry_price": r.get("entry"),
        "status": (r.get("outcome_label") or "").upper(),
        "pnl_pct": r.get("pnl_pct"),
        "setup_class": r.get("setup_class") or "UNKNOWN",
        "market_phase": r.get("market_phase"),
        "entry_regime": r.get("entry_regime") or r.get("regime"),
        "confidence": r.get("confidence"),
        "timestamp": ts_iso,
        "minutes_ago": None,
    }


async def _fetch_signals(request: Request, window: str) -> tuple[list[dict], str | None]:
    """The signal cohort for ``window`` (live snapshot or perf-record history)."""
    error: str | None = None
    if window == "live":
        api = request.app.state.engine_api
        payload = await api.signals(status="all", limit=500)
        if isinstance(payload, dict) and payload.get("error"):
            return [], str(payload.get("error"))
        items: list[dict] = []
        if isinstance(payload, list):
            items = [e for e in payload if isinstance(e, dict)]
        elif isinstance(payload, dict):
            for key in ("items", "signals"):
                if isinstance(payload.get(key), list):
                    items = [e for e in payload[key] if isinstance(e, dict)]
                    break
        return [e for e in items if (e.get("signal_id") or e.get("id"))], error

    vol = request.app.state.data_volume
    raw = vol.signal_performance()
    if isinstance(raw, dict) and raw.get("error"):
        return [], str(raw.get("error"))
    if not isinstance(raw, list):
        return [], None
    window_days = _WINDOW_DAYS.get(window)
    now = datetime.now(timezone.utc)
    out: list[dict] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        if window_days is not None:
            ts_raw = (
                r.get("terminal_outcome_timestamp")
                or r.get("dispatch_timestamp")
                or r.get("create_timestamp")
                or r.get("timestamp")
            )
            if ts_raw is not None:
                try:
                    ts = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
                    if (now - ts).total_seconds() > window_days * 86400:
                        continue
                except (TypeError, ValueError, OSError):
                    pass
        out.append(_perf_to_signal(r))
    return out, None


# --------------------------------------------------------------------------- #
# Row shaping + aggregation
# --------------------------------------------------------------------------- #
def _row(res: DarkSignalResult) -> dict:
    methods = {}
    for m in METHODS:
        tr = res.results.get(m)
        if tr is None:
            methods[m] = None
            continue
        capture = None
        if tr.mfe_pct not in (None, 0.0) and tr.result_pct is not None:
            capture = tr.result_pct / tr.mfe_pct * 100.0
        methods[m] = {
            "result_pct": tr.result_pct,
            "gross_pct": tr.gross_pct,
            "mfe": tr.mfe_pct,
            "exit_price": tr.exit_price,
            "hold_mins": tr.hold_mins,
            "exited": tr.exited,
            "reason": tr.reason,
            "capture": capture,
        }
    return {
        "id": res.signal_id,
        "symbol": res.symbol,
        "side": res.side,
        "entry": res.entry,
        "setup_class": res.setup_class,
        "regime": res.regime,
        "confidence": res.confidence,
        "real_pnl_pct": res.real_pnl_pct,
        "real_is_active": res.real_is_active,
        "degraded": res.degraded,
        "source": res.source,
        "methods": methods,
        "timestamp": res.timestamp,
        "created_relative": _format_relative(res.timestamp),
        "minutes_ago": res.minutes_ago,
    }


def _method_stat(rows: list[dict], method: str) -> dict:
    """Per-method read-out over the given rows.

    Reports **expectancy (avg), total, profit factor, capture, avg hold** and
    exited-rate — not just win-rate — because the no-TP trail is expected to
    trade hit-rate for payoff.
    """
    vals: list[float] = []
    mfes: list[float] = []
    holds: list[int] = []
    exited = 0
    gross_win = 0.0
    gross_loss = 0.0
    for r in rows:
        mr = r["methods"].get(method)
        if not mr or mr["result_pct"] is None:
            continue
        v = mr["result_pct"]
        vals.append(v)
        if mr["mfe"] is not None:
            mfes.append(mr["mfe"])
        if mr["hold_mins"] is not None:
            holds.append(mr["hold_mins"])
        if mr["exited"]:
            exited += 1
        if v > 1e-9:
            gross_win += v
        elif v < -1e-9:
            gross_loss += -v
    n = len(vals)
    if n == 0:
        return {"n": 0, "avg": None, "total": None, "win_rate": None,
                "profit_factor": None, "avg_mfe": None, "capture": None,
                "avg_hold": None, "exited_rate": None}
    wins = sum(1 for v in vals if v > 1e-9)
    avg = sum(vals) / n
    avg_mfe = (sum(mfes) / len(mfes)) if mfes else None
    return {
        "n": n,
        "avg": avg,
        "total": sum(vals),
        "win_rate": wins / n * 100.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 1e-9 else None,
        "avg_mfe": avg_mfe,
        "capture": (avg / avg_mfe * 100.0) if avg_mfe not in (None, 0.0) else None,
        "avg_hold": (sum(holds) / len(holds)) if holds else None,
        "exited_rate": exited / n * 100.0,
    }


def _real_stat(rows: list[dict]) -> dict:
    """Engine's real-exit baseline over the same rows (closed only)."""
    vals = [r["real_pnl_pct"] for r in rows if r.get("real_pnl_pct") is not None]
    if not vals:
        return {"n": 0, "avg": None, "total": None, "win_rate": None}
    wins = sum(1 for v in vals if v > 1e-9)
    return {"n": len(vals), "avg": sum(vals) / len(vals), "total": sum(vals),
            "win_rate": wins / len(vals) * 100.0}


def _bake_off(rows: list[dict]) -> list[dict]:
    """The headline: one stats block per method, best expectancy first."""
    out = [
        {"method": m, "label": METHOD_LABELS[m], "stats": _method_stat(rows, m)}
        for m in METHODS
    ]
    out.sort(key=lambda x: (x["stats"]["total"] if x["stats"]["total"] is not None
                            else -1e18), reverse=True)
    return out


def _grouped(rows: list[dict], key: str) -> list[dict]:
    """Per-``key`` (regime / setup) × method matrix, most-populated group first."""
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(str(r.get(key) or "UNKNOWN"), []).append(r)
    out = []
    for name, grp in groups.items():
        out.append({
            "key": name,
            "n": len(grp),
            "methods": {m: _method_stat(grp, m) for m in METHODS},
            "real": _real_stat(grp),
        })
    out.sort(key=lambda x: x["n"], reverse=True)
    return out


def _paginate(rows: list[dict], page: int) -> dict:
    active = [r for r in rows if r["real_is_active"]]
    closed = [r for r in rows if not r["real_is_active"]]
    total_pages = max(1, (len(closed) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * _PAGE_SIZE
    page_closed = closed[start:start + _PAGE_SIZE]
    page_rows = (active + page_closed) if page == 1 else page_closed
    return {"rows": page_rows, "page": page, "total_pages": total_pages,
            "n_active": len(active), "n_closed": len(closed)}


def _params_from_query(
    defaults: SimParams,
    tf_min: int | None,
    period: int | None,
    mult: float | None,
    sar_step: float | None,
    sar_max: float | None,
    funding: float | None,
    fee: float | None,
) -> SimParams:
    """Override defaults with any provided query knobs, then clamp to sane bands."""
    return SimParams(
        tf_min=tf_min if tf_min is not None else defaults.tf_min,
        period=period if period is not None else defaults.period,
        mult=mult if mult is not None else defaults.mult,
        sar_step=sar_step if sar_step is not None else defaults.sar_step,
        sar_max=sar_max if sar_max is not None else defaults.sar_max,
        funding_bps_per_8h=funding if funding is not None else defaults.funding_bps_per_8h,
        fee_pct=fee if fee is not None else defaults.fee_pct,
    ).clamped()


async def _build(
    request: Request, window: str, view: str, params: SimParams
) -> tuple[list[dict], str | None]:
    signals, error = await _fetch_signals(request, window)
    tracker = request.app.state.dark_signals
    results = await tracker.compute_many(signals, params)
    rows = [_row(results[str(s.get("signal_id") or s.get("id") or "")])
            for s in signals
            if str(s.get("signal_id") or s.get("id") or "") in results]
    if view == "active":
        rows = [r for r in rows if r["real_is_active"]]
    elif view == "closed":
        rows = [r for r in rows if not r["real_is_active"]]
    rows.sort(key=lambda r: (0 if r["real_is_active"] else 1,
                             r.get("minutes_ago") if r.get("minutes_ago") is not None
                             else 10**9))
    return rows, error


@router.get("/dark-signals")
async def dark_signals(
    request: Request,
    window: str = Query("live", pattern="^(live|24h|3d|7d|30d|all)$"),
    view: str = Query("all", pattern="^(all|active|closed)$"),
    tf_min: int | None = Query(None, ge=1, le=60),
    period: int | None = Query(None, ge=2, le=100),
    mult: float | None = Query(None, ge=0.1, le=20.0),
    sar_step: float | None = Query(None, ge=0.001, le=0.2),
    sar_max: float | None = Query(None, ge=0.001, le=1.0),
    funding: float | None = Query(None, ge=0.0, le=100.0),
    fee: float | None = Query(None, ge=0.0, le=2.0),
    page: int = Query(1, ge=1),
):
    tracker = request.app.state.dark_signals
    params = _params_from_query(
        tracker.defaults, tf_min, period, mult, sar_step, sar_max, funding, fee,
    )
    rows, error = await _build(request, window, view, params)
    # Aggregations run over resolved (real-closed) rows so the baseline is fair.
    closed = [r for r in rows if not r["real_is_active"]]
    pagination = _paginate(rows, page)
    klines = request.app.state.binance_klines
    ban_seconds = klines.ban_seconds_remaining if klines.circuit_open else 0
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "dark_signals.html",
        {
            "request": request,
            "rows": pagination["rows"],
            "pagination": pagination,
            "bake_off": _bake_off(closed),
            "real_baseline": _real_stat(closed),
            "by_regime": _grouped(closed, "regime"),
            "by_setup": _grouped(closed, "setup_class"),
            "methods": [(m, METHOD_LABELS[m]) for m in METHODS],
            "params": params,
            "degraded": sum(1 for r in rows if r["degraded"]),
            "ban_seconds": int(ban_seconds),
            "n_closed": len(closed),
            "window": window,
            "view": view,
            "error": error,
            "active": "dark_signals",
        },
    )


_EXPORT_COLS = [
    "id", "timestamp", "symbol", "side", "setup_class", "regime", "entry", "confidence",
    "real_is_active", "real_pnl_pct",
    "atr_result", "atr_mfe", "atr_hold", "atr_exited",
    "supertrend_result", "supertrend_mfe", "supertrend_hold", "supertrend_exited",
    "sar_result", "sar_mfe", "sar_hold", "sar_exited",
]


@router.get("/dark-signals/export.csv")
async def dark_signals_export(
    request: Request,
    window: str = Query("live", pattern="^(live|24h|3d|7d|30d|all)$"),
    view: str = Query("all", pattern="^(all|active|closed)$"),
    tf_min: int | None = Query(None, ge=1, le=60),
    period: int | None = Query(None, ge=2, le=100),
    mult: float | None = Query(None, ge=0.1, le=20.0),
    sar_step: float | None = Query(None, ge=0.001, le=0.2),
    sar_max: float | None = Query(None, ge=0.001, le=1.0),
    funding: float | None = Query(None, ge=0.0, le=100.0),
    fee: float | None = Query(None, ge=0.0, le=2.0),
):
    tracker = request.app.state.dark_signals
    params = _params_from_query(
        tracker.defaults, tf_min, period, mult, sar_step, sar_max, funding, fee,
    )
    rows, _ = await _build(request, window, view, params)
    data = [
        [
            r["id"], r.get("timestamp"), r["symbol"], r["side"], r["setup_class"], r["regime"],
            r["entry"], r["confidence"], r["real_is_active"], r["real_pnl_pct"],
            _cell(r, "atr", "result_pct"), _cell(r, "atr", "mfe"), _cell(r, "atr", "hold_mins"), _cell(r, "atr", "exited"),
            _cell(r, "supertrend", "result_pct"), _cell(r, "supertrend", "mfe"), _cell(r, "supertrend", "hold_mins"), _cell(r, "supertrend", "exited"),
            _cell(r, "sar", "result_pct"), _cell(r, "sar", "mfe"), _cell(r, "sar", "hold_mins"), _cell(r, "sar", "exited"),
        ]
        for r in rows
    ]
    return csv_response(f"dark_signals_{window}_{view}", _EXPORT_COLS, data)


def _cell(row: dict, method: str, field: str) -> Any:
    mr = row["methods"].get(method)
    return mr.get(field) if mr else None
