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

**Window modes**

* ``live`` (default) — live engine API snapshot, shows active positions + recent
  closed history (up to 500 signals from the engine's in-memory cache).
* ``7d`` / ``30d`` / ``all`` — read ``signal_performance.json`` from the mounted
  data volume.  This file has the complete persistent record (potentially
  thousands of signals) and supports date-range filtering.  TP1/TP2 price
  columns are unavailable in performance records (not stored there), but the
  "Reached" column is filled from ``hit_tp`` (which TP level was actually hit).

The replay is ``app/data_sources/free_run.py``; the candles come from Binance
Futures public market data. Read-only — this route never mutates engine state.
When candles can't be fetched the row degrades to the engine's own numbers and
the page says so.

**Exit-method test** (``app/data_sources/exit_sim.py``)

Layered on top: a what-if where each signal is closed *only* at TP or SL — no
pre-TP banking, no invalidation — under a selectable scaled-exit strategy (e.g.
100% at TP1, a flat +1%, or 50% TP1 / 50% TP2). It answers the owner's question
"are the entries fine and the exit method wrong?": the headline card compares
the strategy's net P/L to the engine's real exits over the same signals. This
is computed entirely from the engine's recorded ground truth (hit_tp,
first-touch timestamps, MFE/MAE, TP-based PnL), so it needs **no** Binance
candles and fills every row — including the ones the held-to-stop replay can't.

The closed-signal history is paginated (50/page); active signals are pinned and
always shown.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone, timedelta
from statistics import median as _median
from typing import Any

from fastapi import APIRouter, Query, Request

from app.data_sources.exit_sim import (
    SignalInputs,
    build_catalog,
    direction_filters,
    evaluate,
    evaluate_be,
    get_strategy,
    passes_direction_filter,
)
from app.data_sources.free_run import FreeRunResult
from app.reports import csv_response

router = APIRouter()

# Engine statuses that mean the signal is still live (used only to label the
# "Real exit" column — the held-to-stop status comes from the replay).
_ACTIVE_STATUSES = frozenset({"ACTIVE", "OPEN", "RUNNING"})

_WINDOW_DAYS: dict[str, float | None] = {"24h": 1.0, "3d": 3.0, "7d": 7, "30d": 30, "all": None}

# Closed-signal history is paginated; active signals are always shown (pinned)
# regardless of page, per the owner's "default to last 50 + keep active" ask.
_PAGE_SIZE = 50


def _f(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _epoch_to_iso(value: Any) -> str | None:
    """Convert a float Unix epoch to an ISO-8601 string for _format_relative / _dispatch_ms."""
    if value is None:
        return None
    try:
        dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        return dt.isoformat()
    except (TypeError, ValueError, OSError):
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


def _parse_regime(raw: Any) -> str | None:
    """Strip the pipe-delimited detail suffix from market_phase values.

    market_phase may be stored as "RANGING | ATR%ile=10 | Vol=DISTRIBUTION";
    keep only the leading token so the regime table stays low-cardinality.
    Returns None when the value is absent or empty.
    """
    if not raw:
        return None
    return str(raw).split("|")[0].strip() or None


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


def _hit_tp_label(hit_tp: Any) -> str | None:
    """Map the performance record's integer hit_tp (0/1/2/3) to a label."""
    try:
        n = int(hit_tp or 0)
    except (TypeError, ValueError):
        return None
    if n >= 3:
        return "TP3"
    if n == 2:
        return "TP2"
    if n == 1:
        return "TP1"
    return None


def _row(entry: dict, fr: FreeRunResult) -> dict:
    """Combine an engine signal with its held-to-stop replay into a view row."""
    side = str(entry.get("direction") or entry.get("side") or "").upper()
    entry_px = _f(entry.get("entry") if entry.get("entry") is not None else entry.get("entry_price"))
    sl_px = _f(entry.get("original_stop_loss") or entry.get("stop_loss") or entry.get("sl"))
    tp1 = _f(entry.get("tp1"))
    tp2 = _f(entry.get("tp2"))
    tp3 = _f(entry.get("tp3"))

    raw_status = str(entry.get("status") or "").upper()
    real_is_active = raw_status in _ACTIVE_STATUSES or raw_status == ""
    real_pnl = _f(entry.get("pnl_pct"))
    giveback = None
    if not real_is_active and fr.mfe_pct is not None and real_pnl is not None:
        giveback = fr.mfe_pct - real_pnl

    # TP reach: prefer price-based calculation (live API has TP prices); fall
    # back to the performance record's hit_tp integer when prices aren't stored.
    tp_reach = _tp_reach(fr.mfe_price, tp1, tp2, tp3, side)
    if tp_reach is None:
        tp_reach = _hit_tp_label(entry.get("hit_tp"))

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
        "tp_reach": tp_reach,
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
        "regime": _parse_regime(
            entry.get("market_phase") or entry.get("entry_regime") or entry.get("regime")
        ) or "UNKNOWN",
        "confidence": _f(entry.get("confidence")),
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


def _from_perf_record(r: dict) -> dict:
    """Map a signal_performance.json SignalRecord to the shape _row() + FreeRunTracker expect.

    Performance records don't store tp1/tp2/tp3 price levels (those aren't in
    SignalRecord), so the TP price columns will show —. The "Reached" column is
    filled from hit_tp (0=none, 1=TP1, 2=TP2, 3=TP3) which IS recorded.

    The timestamp field in SignalRecord is a float Unix epoch; convert to ISO-8601
    so _format_relative() and the free-run _dispatch_ms() can parse it.
    """
    # Prefer dispatch_timestamp as the signal-fired time; fall back to create or generic.
    ts_raw = (
        r.get("dispatch_timestamp")
        or r.get("create_timestamp")
        or r.get("timestamp")
    )
    ts_iso = _epoch_to_iso(ts_raw)

    # outcome_label is the terminal status (PROFIT_LOCKED / INVALIDATED / SL_HIT / EXPIRED).
    status = (r.get("outcome_label") or "").upper()

    return {
        "signal_id": r.get("signal_id") or "",
        "id": r.get("signal_id") or "",
        "symbol": r.get("symbol") or "",
        "direction": (r.get("direction") or "").upper(),
        "side": (r.get("direction") or "").upper(),
        "entry": r.get("entry"),
        "entry_price": r.get("entry"),
        # original_stop_loss not separately stored in perf records; stop_loss is
        # the price at signal creation (pre-BE shift) for completed signals.
        "stop_loss": r.get("stop_loss"),
        "original_stop_loss": r.get("stop_loss"),
        "sl": r.get("stop_loss"),
        # TP price levels not in SignalRecord — columns show —.
        "tp1": None,
        "tp2": None,
        "tp3": None,
        # hit_tp: which TP was reached (0=none,1=TP1,2=TP2,3=TP3); used as
        # tp_reach fallback in _row() when TP prices are absent.
        "hit_tp": r.get("hit_tp") or r.get("signal_quality_hit_tp"),
        "hit_sl": r.get("hit_sl"),
        "status": status,
        "pnl_pct": r.get("pnl_pct"),
        "max_favorable_excursion_pct": r.get("max_favorable_excursion_pct", 0.0),
        "max_adverse_excursion_pct": r.get("max_adverse_excursion_pct", 0.0),
        # Engine's TP-based PnL + first-touch ordering — the ground truth the
        # exit-strategy simulator uses to price TP legs and resolve TP-vs-SL
        # order without needing Binance candles (covers every row).
        "signal_quality_pnl_pct": r.get("signal_quality_pnl_pct"),
        "signal_quality_hit_tp": r.get("signal_quality_hit_tp"),
        "first_tp_touch_timestamp": r.get("first_tp_touch_timestamp"),
        "first_sl_touch_timestamp": r.get("first_sl_touch_timestamp"),
        "setup_class": r.get("setup_class") or "UNKNOWN",
        # market_phase may carry "RANGING | ATR%ile=…" — keep the leading token.
        "entry_regime": _parse_regime(
            r.get("market_phase") or r.get("entry_regime") or r.get("regime")
        ),
        "confidence": r.get("confidence"),
        "timestamp": ts_iso,
        "minutes_ago": None,
    }


async def _build_rows(
    request: Request,
    view: str,
    window: str = "live",
    strategy_key: str = "tp1",
    target_pct: float = 1.0,
    fee_pct: float = 0.07,
    direction: str = "all",
    date_from: str = "",
    date_to: str = "",
) -> tuple[list[dict], str | None]:
    """Fetch signals, replay each held-to-stop, reshape into rows.

    ``window`` controls the data source:
    * ``live`` — live engine API (up to 500 signals from in-memory snapshot).
    * ``24h`` / ``3d`` / ``7d`` / ``30d`` / ``all`` — signal_performance.json
      on the data volume, filtered to the given window.  Supports thousands
      of records.
    * ``range`` — same data-volume source, filtered to the inclusive
      ``date_from``→``date_to`` UTC day range (ISO ``YYYY-MM-DD``; either
      bound may be blank for an open end).

    ``view`` filters to active / stopped / all on the *held-to-stop* status.
    Active rows sort first, then by recency.

    ``strategy_key`` + ``target_pct`` select the scaled-exit what-if strategy
    (``app/data_sources/exit_sim.py``) layered onto each row — the "what if we
    only exited at TP/SL, no pre-TP, no invalidation" analysis. Computed from
    the engine's own recorded fields, so it covers every row (no Binance dep).

    ``fee_pct`` is the round-trip trading fee (maker + taker, default 0.07%).
    Deducted from closed-signal strategy P/L only (active signals haven't paid
    the exit leg yet).
    """
    tracker = request.app.state.free_run
    strategy = get_strategy(strategy_key, target_pct)
    error: str | None = None

    if window == "live":
        api = request.app.state.engine_api
        payload = await api.signals(status="all", limit=500)
        if isinstance(payload, dict) and payload.get("error"):
            error = str(payload.get("error"))
        items = [e for e in _extract_items(payload) if (e.get("signal_id") or e.get("id"))]
    else:
        # Data-volume path: signal_performance.json has the full persistent record.
        vol = request.app.state.data_volume
        raw = vol.signal_performance()
        if isinstance(raw, dict) and raw.get("error"):
            error = str(raw.get("error"))
            raw = []
        if not isinstance(raw, list):
            raw = []

        window_days = _WINDOW_DAYS.get(window)
        now = datetime.now(timezone.utc)
        # Custom From→To range (inclusive whole UTC days). A malformed or
        # missing bound leaves that side open rather than erroring.
        range_start = range_end = None
        if window == "range":
            try:
                if date_from:
                    range_start = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
            except ValueError:
                range_start = None
            try:
                if date_to:
                    range_end = datetime.fromisoformat(date_to).replace(
                        tzinfo=timezone.utc
                    ) + timedelta(days=1)
            except ValueError:
                range_end = None
        items = []
        for r in raw:
            if not isinstance(r, dict):
                continue
            if window_days is not None or window == "range":
                ts_raw = (
                    r.get("terminal_outcome_timestamp")
                    or r.get("dispatch_timestamp")
                    or r.get("create_timestamp")
                    or r.get("timestamp")
                )
                if ts_raw is not None:
                    try:
                        ts = datetime.fromtimestamp(float(ts_raw), tz=timezone.utc)
                        if window_days is not None and (now - ts).total_seconds() > window_days * 86400:
                            continue
                        if range_start is not None and ts < range_start:
                            continue
                        if range_end is not None and ts >= range_end:
                            continue
                    except (TypeError, ValueError, OSError):
                        pass
            items.append(_from_perf_record(r))

    results = await tracker.compute_many(items)

    rows = []
    for e in items:
        sid = str(e.get("signal_id") or e.get("id") or "")
        fr = results.get(sid)
        if fr is None:
            continue
        row = _row(e, fr)
        # Layer the scaled-exit what-if result onto the row.
        inp = SignalInputs.from_dict(e)
        if inp is not None:
            if strategy_key == "be_tp1":
                # BE stop: if MFE >= target_pct and stop subsequently hit without
                # TP1 filling first, simulate a 0% exit instead of the SL loss.
                ex = evaluate_be(inp, get_strategy("tp1", target_pct), target_pct)
            else:
                ex = evaluate(inp, strategy)
            gross = ex.result_pct
            # Deduct round-trip fee for closed signals (entry + exit both paid).
            # Active signals haven't closed yet — don't pre-charge the exit leg.
            net = (gross - fee_pct) if (gross is not None and not ex.is_active) else gross
            row["strategy_pct"] = net
            row["strategy_pct_gross"] = gross
            row["strategy_labels"] = "+".join(ex.filled_labels) if ex.filled_labels else ""
            row["strategy_approx"] = ex.approx
            row["strategy_active"] = ex.is_active
            row["strategy_sl_frac"] = ex.sl_frac
        else:
            row["strategy_pct"] = None
            row["strategy_pct_gross"] = None
            row["strategy_labels"] = ""
            row["strategy_approx"] = False
            row["strategy_active"] = False
            row["strategy_sl_frac"] = 0.0
        rows.append(row)

    if view == "active":
        rows = [r for r in rows if r["is_active"]]
    elif view == "closed":
        rows = [r for r in rows if not r["is_active"]]

    # Directional what-if filter — applied here so every downstream aggregation
    # (summary, strategy_summary, breakdowns, combos, give-back) sees the same cohort.
    if direction and direction != "all":
        rows = [
            r for r in rows
            if passes_direction_filter(r.get("side"), r.get("setup_class"), direction)
        ]

    rows.sort(key=lambda r: (0 if r["is_active"] else 1, r.get("minutes_ago") if r.get("minutes_ago") is not None else 10**9))
    return rows, error


def _summary(rows: list[dict]) -> dict:
    active = [r for r in rows if r["is_active"]]
    stopped = [r for r in rows if not r["is_active"]]
    mfes = [r["mfe_pct"] for r in rows if r["mfe_pct"] is not None]
    givebacks = [r["giveback_pct"] for r in rows if r["giveback_pct"] is not None]
    # Signals where the held-to-stop replay shows MFE touched TP1+ but the engine's
    # real exit was SL_HIT — price moved through TP1 and came all the way back.
    # In the new TP1/SL-only regime this should be near-zero; a non-zero count may
    # indicate a TP1 order placement/fill issue or extreme market-reversal events.
    tp1_miss = sum(
        1 for r in rows
        if not r["real_is_active"]
        and r.get("tp_reach") in ("TP1", "TP2", "TP3")
        and r.get("real_status") == "SL_HIT"
    )
    return {
        "active": len(active),
        "closed": len(stopped),
        "best_mfe": max(mfes) if mfes else None,
        "avg_giveback": (sum(givebacks) / len(givebacks)) if givebacks else None,
        "degraded": sum(1 for r in rows if r["degraded"]),
        "tp1_miss": tp1_miss,
    }


def _group_giveback(rows: list[dict], key: str) -> list[dict]:
    """Aggregate give-back over the engine-closed rows, grouped by ``key``."""
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
    """Give-back rolled up by the engine's real exit type, setup_class, and regime."""
    closed = [r for r in rows if not r["real_is_active"] and r.get("giveback_pct") is not None]
    return {
        "n": len(closed),
        "total_giveback": sum(r["giveback_pct"] for r in closed),
        "by_exit": _group_giveback(rows, "real_status"),
        "by_setup": _group_giveback(rows, "setup_class"),
        "by_regime": _group_giveback(rows, "regime"),
    }


def _strategy_summary(rows: list[dict], fee_pct: float = 0.0) -> dict:
    """Net read-out for the selected what-if exit strategy vs the engine's real
    exits, over the closed rows in this view.

    Both sides are net of ``fee_pct`` (round-trip: maker entry + taker close)
    so the comparison is apples-to-apples. ``strategy_pct`` on each row is
    already net of fee (applied in ``_build_rows``); ``real_pnl_pct`` comes
    from the engine's price-based P/L (no fee deducted), so we deduct here.

    This is the headline that answers "is the exit method the problem?": if the
    strategy's average/total beats the engine's real average/total on the same
    signals, the entries were fine and the engine's exits gave the edge back.
    """
    closed = [r for r in rows if not r["is_active"]]
    strat = [r["strategy_pct"] for r in closed if r.get("strategy_pct") is not None]
    real = [
        r["real_pnl_pct"] - fee_pct
        for r in closed
        if r.get("real_pnl_pct") is not None
    ]

    def _stat(vals: list[float]) -> dict:
        if not vals:
            return {"n": 0, "avg": None, "total": None, "win_rate": None}
        wins = sum(1 for v in vals if v > 1e-9)
        return {
            "n": len(vals),
            "avg": sum(vals) / len(vals),
            "total": sum(vals),
            "win_rate": wins / len(vals) * 100.0,
        }

    s = _stat(strat)
    r = _stat(real)
    edge = None
    if s["total"] is not None and r["total"] is not None:
        edge = s["total"] - r["total"]
    return {
        "strategy": s,
        "real": r,
        "edge_total": edge,
        "approx": sum(1 for row in closed if row.get("strategy_approx")),
    }


# Score bands in the same 0–100 scale used by performance.py / raw_edge.py so
# historical signal_performance.json records (which store confidence as e.g.
# 72.5) bucket correctly.  Live API signals carry confidence on a 0–1 scale
# (e.g. 0.725); _conf_band() normalises both by multiplying values ≤ 1 by 100.
_SCORE_BANDS_100: list[tuple[str, float, float]] = [
    ("< 65",  0.0,  65.0),
    ("65–70", 65.0, 70.0),
    ("70–75", 70.0, 75.0),
    ("75–80", 75.0, 80.0),
    ("80+",   80.0, 1e9),
]
_BAND_ORDER = {label: i for i, (label, _, _) in enumerate(_SCORE_BANDS_100)}
_BAND_ORDER["unknown"] = len(_SCORE_BANDS_100)


def _conf_band(conf: float | None) -> str:
    if conf is None:
        return "unknown"
    try:
        c = float(conf)
    except (TypeError, ValueError):
        return "unknown"
    # Normalise 0–1 scale to 0–100 (historical JSON already uses 0–100).
    if c <= 1.0:
        c = c * 100.0
    for label, lo, hi in _SCORE_BANDS_100:
        if lo <= c < hi:
            return label
    return "unknown"


def _stat_block(pcts: list[float]) -> dict:
    if not pcts:
        return {"n": 0, "avg": None, "total": None, "win_rate": None}
    wins = sum(1 for v in pcts if v > 1e-9)
    return {
        "n": len(pcts),
        "avg": sum(pcts) / len(pcts),
        "total": sum(pcts),
        "win_rate": wins / len(pcts) * 100.0,
    }


def _group_by(
    rows: list[dict],
    key_fn,
    fee_pct: float,
) -> list[dict]:
    """Aggregate strategy vs engine real exits by an arbitrary grouping key.

    Both sides are net of ``fee_pct`` (strategy_pct is already net; real_pnl_pct
    is price-based from the engine so we deduct here for a fair comparison).
    Only closed (non-active) rows participate — active signals haven't resolved.
    """
    buckets: dict[str, tuple[list[float], list[float]]] = {}
    for r in rows:
        if r["is_active"]:
            continue
        key = key_fn(r)
        strat_list, real_list = buckets.setdefault(key, ([], []))
        if r.get("strategy_pct") is not None:
            strat_list.append(r["strategy_pct"])
        if r.get("real_pnl_pct") is not None:
            real_list.append(r["real_pnl_pct"] - fee_pct)
    out = []
    for key, (strat_list, real_list) in buckets.items():
        s = _stat_block(strat_list)
        rv = _stat_block(real_list)
        edge = (
            s["total"] - rv["total"]
            if s["total"] is not None and rv["total"] is not None
            else None
        )
        out.append({"key": key, "strategy": s, "real": rv, "edge": edge})
    return out


def _breakdown_scoreband(rows: list[dict], fee_pct: float) -> list[dict]:
    """Strategy vs real exits broken down by confidence score band."""
    result = _group_by(rows, lambda r: _conf_band(r.get("confidence")), fee_pct)
    result.sort(key=lambda x: _BAND_ORDER.get(x["key"], 99))
    return result


def _breakdown_path(rows: list[dict], fee_pct: float) -> list[dict]:
    """Strategy vs real exits broken down by setup_class (evaluator path)."""
    result = _group_by(rows, lambda r: r.get("setup_class") or "UNKNOWN", fee_pct)
    # Most-fired path first so the dominant setups surface at the top.
    result.sort(key=lambda x: x["strategy"]["n"], reverse=True)
    return result


def _breakdown_regime(rows: list[dict], fee_pct: float) -> list[dict]:
    """Strategy vs real exits broken down by market regime at signal entry."""
    result = _group_by(rows, lambda r: r.get("regime") or "UNKNOWN", fee_pct)
    result.sort(key=lambda x: x["strategy"]["n"], reverse=True)
    return result


# ---------------------------------------------------------------------------
# Combination analyzer — score band × path × regime cross-tab
#
# The single-dimension breakdowns above marginalise away interaction: "B-tier
# is breakeven" and "SR_FLIP is breakeven" can both be true while
# "B-tier SR_FLIP in TRENDING_DOWN" is strongly +EV. This finds the
# *combinations* that actually print money so we can tighten generation toward
# them (and starve the combos that bleed). Closed rows only — active signals
# haven't resolved. Optional band/path/regime filters narrow the population
# first; ``min_n`` drops thin, statistically-meaningless cells.
# ---------------------------------------------------------------------------
def _combo_options(rows: list[dict]) -> dict:
    """Distinct band / path / regime values across closed rows — drives the
    filter dropdowns so they only ever offer values that exist in this view."""
    bands: set[str] = set()
    paths: set[str] = set()
    regimes: set[str] = set()
    for r in rows:
        if r["is_active"]:
            continue
        bands.add(_conf_band(r.get("confidence")))
        paths.add(r.get("setup_class") or "UNKNOWN")
        regimes.add(r.get("regime") or "UNKNOWN")
    return {
        "bands": sorted(bands, key=lambda b: _BAND_ORDER.get(b, 99)),
        "paths": sorted(paths),
        "regimes": sorted(regimes),
    }


def _combos(
    rows: list[dict],
    fee_pct: float,
    *,
    band: str | None = None,
    path: str | None = None,
    regime: str | None = None,
    min_n: int = 1,
) -> list[dict]:
    """Profit by (score band × path × regime) combination, best total first.

    Each cell carries engine-real and TP1-full-strategy stats (both net of
    ``fee_pct``) plus MFE / give-back / capture so the owner can see not just
    *which* combos win but *why* (real edge vs given-back move). Sorted by
    engine-real total P/L descending — the top rows are where the money is.
    """
    buckets: dict[tuple[str, str, str], dict] = {}
    for r in rows:
        if r["is_active"]:
            continue
        b = _conf_band(r.get("confidence"))
        p = r.get("setup_class") or "UNKNOWN"
        g = r.get("regime") or "UNKNOWN"
        if band and b != band:
            continue
        if path and p != path:
            continue
        if regime and g != regime:
            continue
        bk = buckets.setdefault(
            (b, p, g), {"real": [], "strat": [], "mfe": [], "gb": []}
        )
        if r.get("real_pnl_pct") is not None:
            bk["real"].append(r["real_pnl_pct"] - fee_pct)
        if r.get("strategy_pct") is not None:
            bk["strat"].append(r["strategy_pct"])
        if r.get("mfe_pct") is not None:
            bk["mfe"].append(r["mfe_pct"])
        if r.get("giveback_pct") is not None:
            bk["gb"].append(r["giveback_pct"])

    out: list[dict] = []
    for (b, p, g), bk in buckets.items():
        rstat = _stat_block(bk["real"])
        if rstat["n"] < max(1, min_n):
            continue
        sstat = _stat_block(bk["strat"])
        edge = (
            sstat["total"] - rstat["total"]
            if sstat["total"] is not None and rstat["total"] is not None
            else None
        )
        avg_mfe = (sum(bk["mfe"]) / len(bk["mfe"])) if bk["mfe"] else None
        avg_gb = (sum(bk["gb"]) / len(bk["gb"])) if bk["gb"] else None
        capture = (
            (rstat["avg"] / avg_mfe * 100.0)
            if avg_mfe not in (None, 0.0) and rstat["avg"] is not None
            else None
        )
        out.append({
            "band": b,
            "path": p,
            "regime": g,
            "n": rstat["n"],
            "real": rstat,
            "strategy": sstat,
            "edge": edge,
            "avg_mfe": avg_mfe,
            "avg_giveback": avg_gb,
            "capture": capture,
        })
    out.sort(
        key=lambda x: (x["real"]["total"] if x["real"]["total"] is not None else -1e18),
        reverse=True,
    )
    return out


_MFE_RUNNER_THRESHOLD = 1.0  # % — a signal that cleared 1% MFE is a "runner"


def _raw_edge_agg(rows: list[dict]) -> dict:
    """Raw-edge-style MFE / give-back / capture tables by score band, setup, regime.

    Mirrors the Raw Edge tab's per-cohort analysis but computed directly from the
    profit-tab rows (which carry mfe_pct from the held-to-stop replay and
    real_pnl_pct from the engine's actual exit), so it works across all windows
    including live.  Active signals are excluded — they haven't resolved.
    """
    def _make_bucket() -> dict:
        return {"n": 0, "mfe_list": [], "mfe_sum": 0.0, "realized_sum": 0.0,
                "giveback_sum": 0.0, "runners": 0}

    def _add(b: dict, mfe: float, realized: float) -> None:
        b["n"] += 1
        b["mfe_list"].append(mfe)
        b["mfe_sum"] += mfe
        b["realized_sum"] += realized
        if mfe - realized > 0:
            b["giveback_sum"] += mfe - realized
        if mfe >= _MFE_RUNNER_THRESHOLD:
            b["runners"] += 1

    def _fin(d: dict, order: list[str] | None = None) -> list[dict]:
        out: list[dict] = []
        keys = order if order is not None else list(d.keys())
        for key in keys:
            b = d.get(key)
            if not b or b["n"] == 0:
                continue
            n = b["n"]
            mfe_sum = b["mfe_sum"]
            out.append({
                "key": key,
                "n": n,
                "avg_mfe": mfe_sum / n,
                "median_mfe": _median(b["mfe_list"]) if b["mfe_list"] else 0.0,
                "runner_rate": b["runners"] / n,
                "avg_realized": b["realized_sum"] / n,
                "giveback_avg": b["giveback_sum"] / n,
                "capture": (b["realized_sum"] / mfe_sum) if mfe_sum > 0 else None,
            })
        if order is None:
            out.sort(key=lambda r: r["n"], reverse=True)
        return out

    by_band: dict[str, dict] = defaultdict(_make_bucket)
    by_setup: dict[str, dict] = defaultdict(_make_bucket)
    by_regime: dict[str, dict] = defaultdict(_make_bucket)

    for r in rows:
        if r.get("is_active"):
            continue
        mfe = r.get("mfe_pct")
        realized = r.get("real_pnl_pct")
        if mfe is None or realized is None:
            continue
        mfe = max(0.0, float(mfe))

        _add(by_regime[r.get("regime") or "UNKNOWN"], mfe, float(realized))
        _add(by_setup[r.get("setup_class") or "UNKNOWN"], mfe, float(realized))
        band = _conf_band(r.get("confidence"))
        if band != "unknown":
            _add(by_band[band], mfe, float(realized))

    band_order = [label for label, _, _ in _SCORE_BANDS_100]
    return {
        "by_band": _fin(dict(by_band), band_order),
        "by_setup": _fin(dict(by_setup)),
        "by_regime": _fin(dict(by_regime)),
    }


def _paginate(rows: list[dict], page: int) -> dict:
    """Split rows into the active head (always shown) + one page of closed rows.

    Active signals are never paged out — they're the live state the owner wants
    in view at all times. Closed signals fill the rest of the page (50 each).
    """
    active = [r for r in rows if r["is_active"]]
    closed = [r for r in rows if not r["is_active"]]
    total_pages = max(1, (len(closed) + _PAGE_SIZE - 1) // _PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start = (page - 1) * _PAGE_SIZE
    page_closed = closed[start:start + _PAGE_SIZE]
    # Active rows pin to the top of page 1; later pages show closed only.
    page_rows = (active + page_closed) if page == 1 else page_closed
    return {
        "rows": page_rows,
        "page": page,
        "total_pages": total_pages,
        "n_active": len(active),
        "n_closed": len(closed),
        "showing": len(page_rows),
    }


def _resolve_target_pct(raw: float | None) -> float:
    """Clamp the fixed-target percent to a sane band (0.1%..20%), default 1%."""
    if raw is None:
        return 1.0
    return max(0.1, min(20.0, raw))


def _resolve_fee_pct(raw: float | None) -> float:
    """Clamp round-trip fee to 0..2% band, default 0.07 (Binance std maker+taker)."""
    if raw is None:
        return 0.07
    return max(0.0, min(2.0, raw))


@router.get("/profit")
async def profit(
    request: Request,
    view: str = Query("all", pattern="^(all|active|closed)$"),
    window: str = Query("live", pattern="^(live|24h|3d|7d|30d|all|range)$"),
    date_from: str = Query("", max_length=10),
    date_to: str = Query("", max_length=10),
    strategy: str = Query("tp1"),
    target_pct: float = Query(1.0),
    fee_pct: float = Query(0.07),
    direction: str = Query("all"),
    page: int = Query(1, ge=1),
    combo_band: str = Query(""),
    combo_path: str = Query(""),
    combo_regime: str = Query(""),
    combo_min_n: int = Query(1, ge=1, le=50),
):
    target = _resolve_target_pct(target_pct)
    fee = _resolve_fee_pct(fee_pct)
    catalog = build_catalog(target)
    if strategy not in catalog:
        strategy = "tp1"
    dir_filters = direction_filters()
    if direction not in dir_filters:
        direction = "all"
    rows, error = await _build_rows(
        request, view, window, strategy, target, fee, direction, date_from, date_to,
    )
    pagination = _paginate(rows, page)
    combos = _combos(
        rows,
        fee,
        band=combo_band or None,
        path=combo_path or None,
        regime=combo_regime or None,
        min_n=combo_min_n,
    )
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "profit.html",
        {
            "request": request,
            "rows": pagination["rows"],
            "summary": _summary(rows),
            "aggregates": _aggregates(rows),
            "strategy_summary": _strategy_summary(rows, fee),
            "breakdown_scoreband": _breakdown_scoreband(rows, fee),
            "breakdown_path": _breakdown_path(rows, fee),
            "breakdown_regime": _breakdown_regime(rows, fee),
            "raw_edge_agg": _raw_edge_agg(rows),
            "combos": combos,
            "combo_options": _combo_options(rows),
            "combo_filters": {
                "band": combo_band,
                "path": combo_path,
                "regime": combo_regime,
                "min_n": combo_min_n,
            },
            "runner_threshold": _MFE_RUNNER_THRESHOLD,
            "strategies": [(k, s.label) for k, s in catalog.items()],
            "strategy": strategy,
            "strategy_label": catalog[strategy].label,
            "direction_filters": list(dir_filters.items()),
            "direction": direction,
            "direction_label": dir_filters[direction],
            "target_pct": target,
            "fee_pct": fee,
            "pagination": pagination,
            "view": view,
            "window": window,
            "date_from": date_from,
            "date_to": date_to,
            "error": error,
            "active": "profit",
        },
    )


_EXPORT_COLS = [
    "id", "symbol", "side", "setup_class", "regime", "entry", "sl", "tp1", "tp2", "tp3",
    "current", "result_pct", "mfe_pct", "max_price", "tp_reach", "status", "hold_mins",
    "real_status", "real_pnl_pct", "giveback_pct",
    "strategy_pct", "strategy_pct_gross", "strategy_labels", "strategy_approx", "degraded",
]


@router.get("/profit/export.csv")
async def profit_export(
    request: Request,
    view: str = Query("all", pattern="^(all|active|closed)$"),
    window: str = Query("live", pattern="^(live|24h|3d|7d|30d|all|range)$"),
    date_from: str = Query("", max_length=10),
    date_to: str = Query("", max_length=10),
    strategy: str = Query("tp1"),
    target_pct: float = Query(1.0),
    fee_pct: float = Query(0.07),
    direction: str = Query("all"),
):
    """Download the full (filtered) table as CSV — every row, not paginated."""
    target = _resolve_target_pct(target_pct)
    fee = _resolve_fee_pct(fee_pct)
    if strategy not in build_catalog(target):
        strategy = "tp1"
    if direction not in direction_filters():
        direction = "all"
    rows, _ = await _build_rows(
        request, view, window, strategy, target, fee, direction, date_from, date_to,
    )
    data = [[r.get(col) for col in _EXPORT_COLS] for r in rows]
    window_tag = window if window != "range" else f"range_{date_from or 'open'}_{date_to or 'open'}"
    return csv_response(f"signal_profit_{window_tag}_{view}_{strategy}_{direction}", _EXPORT_COLS, data)
