"""SAR exit A/B — the exit-method bake-off's verdict, forward-measured live.

The 102,496-entry bake-off (engine ``scripts/exit_method_backtest.py``,
2026-07-25) ranked Parabolic SAR on 15m the only profitable trailing exit
(PF 1.60 vs SuperTrend 0.93 / ATR 0.72). A backtest verdict is not a promotion,
so the engine now stamps a counterfactual **pair** per post-scoring candidate
(``src/sar_exit_shadow.py``) and forward-measures both arms on real signals:

* ``X@SARBASE`` — the live evaluator geometry (entry / SL / TP1), static
* ``X@SAREXIT`` — the same entry, exited by a trailing 15m Parabolic SAR

This panel exists because those rows are **not** strategies and must not be read
as strategy rows: they are two counterfactuals stamped from one candidate. Mixed
into the Strategy Lab's per-strategy table they would double-count that
candidate, so they are excluded there and shown here on their own.

Two views, because the rollup and the individual trades answer different
questions:

* **Rollup** (from the edge matrix) — per strategy, which arm is ahead in R and
  by how much. A leader is named only when BOTH arms clear the sample floor;
  a thin arm reads MEASURING, never a winner.
* **Per-signal pairs** (from the ledger) — the actual trades, side by side:
  what the live geometry did vs where the trail got out, in R. This is what
  makes a headline number auditable rather than something to take on faith.

Both arms share the live ``sl_distance`` as their R denominator and the same
192-bar (48h) measurement window, so the comparison carries no hold-time
confound — that was the one confounded axis in the backtest and it is fixed at
the source here.

Read-only, like every diagnostic surface: this panel calls nothing that writes.
All reducers are pure module-top functions, unit-testable without the app.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Query, Request

from app.routes.strategy_lab import EDGE_MIN_SAMPLES, reduce_edge_matrix

router = APIRouter()

SARBASE_SUFFIX = "@SARBASE"
SAREXIT_SUFFIX = "@SAREXIT"

# Mirrors src/suppression_audit.py — the ledger's shared classification
# vocabulary. On a trailing arm WOULD_EXPIRE means the trail never fired inside
# the window and the trade was marked to the window's close.
WOULD_WIN = "WOULD_WIN"
WOULD_LOSE = "WOULD_LOSE"
WOULD_EXPIRE = "WOULD_EXPIRE"


def _sar_base(strategy: str) -> str:
    s = str(strategy or "")
    for sfx in (SARBASE_SUFFIX, SAREXIT_SUFFIX):
        if s.endswith(sfx):
            return s[: -len(sfx)]
    return s


def reduce_sar_exit(rows: list[dict], min_sample: int = EDGE_MIN_SAMPLES) -> list[dict]:
    """Per-strategy live-geometry-vs-SAR rollup (port of engine summarize_sar_exit).

    Pools each arm's matrix cells across contexts (sample-weighted). A leader is
    named only when BOTH arms clear ``min_sample`` — "not enough data yet" has
    to be a first-class answer here, because this table is what an activation
    decision would read and an accidental 0-sample "SAR wins" is exactly the
    kind of number that gets acted on.
    """
    pooled: dict[str, dict[str, dict]] = {}
    for row in rows:
        strategy = str(row.get("strategy", ""))
        if strategy.endswith(SAREXIT_SUFFIX):
            arm = "sar"
        elif strategy.endswith(SARBASE_SUFFIX):
            arm = "base"
        else:
            continue
        n = int(row.get("n", 0) or 0)
        if n <= 0:
            continue
        agg = pooled.setdefault(_sar_base(strategy), {}).setdefault(
            arm, {"n": 0, "wins": 0.0, "r_sum": 0.0, "cells": 0}
        )
        agg["n"] += n
        agg["wins"] += float(row.get("win_rate", 0.0) or 0.0) * n
        agg["r_sum"] += float(row.get("avg_r", 0.0) or 0.0) * n
        agg["cells"] += 1

    out: list[dict] = []
    for base, arms in pooled.items():

        def _arm(name: str) -> dict:
            a = arms.get(name, {"n": 0, "wins": 0.0, "r_sum": 0.0, "cells": 0})
            n = a["n"]
            return {
                "n": n,
                "cells": a["cells"],
                "win_rate": (a["wins"] / n) if n else 0.0,
                "avg_r": (a["r_sum"] / n) if n else 0.0,
            }

        base_arm = _arm("base")
        sar_arm = _arm("sar")
        measured = base_arm["n"] >= min_sample and sar_arm["n"] >= min_sample
        delta = sar_arm["avg_r"] - base_arm["avg_r"]
        out.append({
            "strategy": base,
            "base": base_arm,
            "sar": sar_arm,
            "delta_r": delta if measured else None,
            "leader": (
                ("SAR" if delta > 0 else "LIVE" if delta < 0 else "TIE")
                if measured
                else "MEASURING"
            ),
        })
    out.sort(
        key=lambda r: abs(r["delta_r"]) if r["delta_r"] is not None else -1.0,
        reverse=True,
    )
    return out


def _record_r(rec: dict) -> Optional[float]:
    """Gross R for one classified record, from the arm's own exit.

    Static arm: the TP1-vs-SL race (+R_to_TP1 / −1 / mark-to-close). Trailing
    arm: continuous, wherever the trail got out. Both divide by the same live
    ``sl_distance``, which is what makes the two comparable at all. Mirrors
    ``suppression_audit.candidate_outcome`` — gross only, since ops has no
    view of the engine's cost-model flag and a silently-netted number here
    would not match the engine's.
    """
    cls = rec.get("classification")
    if not cls or cls == "INSUFFICIENT_DATA":
        return None
    entry = float(rec.get("entry") or 0.0)
    sl_distance = float(rec.get("sl_distance") or 0.0)
    if entry <= 0 or sl_distance <= 0:
        return None
    side = str(rec.get("side") or "").upper()

    if str(rec.get("exit_model") or "static") == "trailing":
        exit_price = float(rec.get("trail_exit_price") or 0.0)
        if exit_price <= 0:
            return None
        move = (exit_price - entry) if side == "LONG" else (entry - exit_price)
        return move / sl_distance

    if cls == WOULD_WIN:
        tp1 = float(rec.get("tp1") or 0.0)
        return abs(tp1 - entry) / sl_distance if tp1 > 0 else None
    if cls == WOULD_LOSE:
        return -1.0
    final = float(rec.get("post_price_final") or 0.0)
    if final <= 0:
        return 0.0
    move = (final - entry) if side == "LONG" else (entry - final)
    return move / sl_distance


def reduce_sar_pairs(records: Any, limit: int = 100) -> list[dict]:
    """Pair each candidate's two ledger arms into one comparable row.

    The arms are matched **in stamp order** within each
    (symbol, side, strategy) group rather than by timestamp: the engine stamps
    both arms in one call under a both-or-neither invariant, so the k-th base
    record belongs to the k-th trail record. Timestamps differ by microseconds
    and are not a safe join key.

    A pair is emitted only once BOTH arms are classified — half a pair is not a
    comparison, and showing one would invite reading the resolved arm alone.
    Newest first.
    """
    out: list[dict] = []
    for (symbol, side, strategy), arms in _group_arms(records).items():
        for base_rec, sar_rec in zip(arms["base"], arms["sar"]):
            if base_rec.get("classification") is None or sar_rec.get("classification") is None:
                continue
            base_r = _record_r(base_rec)
            sar_r = _record_r(sar_rec)
            if base_r is None or sar_r is None:
                continue
            out.append({
                "symbol": symbol,
                "side": side,
                "strategy": strategy,
                "stamped_at": float(base_rec.get("suppress_timestamp") or 0.0),
                "context_key": str(base_rec.get("context_key", "")),
                "entry": float(base_rec.get("entry") or 0.0),
                "base_class": base_rec.get("classification"),
                "base_r": base_r,
                "sar_class": sar_rec.get("classification"),
                "sar_r": sar_r,
                "sar_exit_price": sar_rec.get("trail_exit_price"),
                "sar_exit_reason": sar_rec.get("trail_exit_reason"),
                "sar_hold_min": sar_rec.get("trail_hold_min"),
                "delta_r": sar_r - base_r,
            })
    out.sort(key=lambda r: r["stamped_at"], reverse=True)
    return out[:limit]


def _group_arms(records: Any) -> dict[tuple, dict[str, list[dict]]]:
    """Group ledger records into (symbol, side, strategy) → {base:[…], sar:[…]}.

    Sorted by stamp time within each arm so the k-th base record lines up with
    the k-th trail record — the engine stamps both in one call under a
    both-or-neither invariant, so ordinal position is the join key. Timestamps
    differ by microseconds and are not safe to match on.
    """
    groups: dict[tuple, dict[str, list[dict]]] = {}
    if not isinstance(records, list):
        return groups
    for rec in records:
        if not isinstance(rec, dict):
            continue
        strategy = str(rec.get("setup_class", ""))
        if strategy.endswith(SAREXIT_SUFFIX):
            arm = "sar"
        elif strategy.endswith(SARBASE_SUFFIX):
            arm = "base"
        else:
            continue
        key = (
            str(rec.get("symbol", "")),
            str(rec.get("side", "")).upper(),
            _sar_base(strategy),
        )
        groups.setdefault(key, {"base": [], "sar": []})[arm].append(rec)
    for arms in groups.values():
        for arm in ("base", "sar"):
            arms[arm].sort(key=lambda r: float(r.get("suppress_timestamp") or 0.0))
    return groups


# Status vocabulary for a SAR trade. A trailing exit has **no TP and no SL** —
# the trail is the only way out — so the live signal statuses (TP1/TP2/SL) have
# no analogue here and are deliberately absent. What a SAR trade can be:
SAR_RUNNING = "RUNNING"          # stamped; its 48h window has not elapsed yet
SAR_CLOSED_TRAIL = "CLOSED_TRAIL"   # the moving stop caught price
SAR_CLOSED_WINDOW = "CLOSED_WINDOW"  # 48h passed untouched; marked to the close
SAR_NO_DATA = "NO_DATA"          # window elapsed but candles couldn't resolve it

SAR_STATUSES = (SAR_RUNNING, SAR_CLOSED_TRAIL, SAR_CLOSED_WINDOW, SAR_NO_DATA)

# Provenance (engine: src/suppression_audit.py). The arm stamps from both
# scanner call sites, so the ledger mixes candidates that reached subscribers
# with candidates a gate killed. Only the EMITTED half can justify changing
# what users receive — "would this exit have improved the signals we sent" is a
# different question from "…every candidate we considered", and they can have
# different answers. Records stamped before the engine recorded this read
# UNKNOWN and are excluded from both filters rather than guessed at.
PROV_EMITTED = "emitted"
PROV_SUPPRESSED = "suppressed"
PROV_UNKNOWN = ""


def _sar_status(rec: dict) -> str:
    cls = rec.get("classification")
    if cls is None:
        return SAR_RUNNING
    if cls == "INSUFFICIENT_DATA":
        return SAR_NO_DATA
    reason = str(rec.get("trail_exit_reason") or "")
    if reason == "window":
        return SAR_CLOSED_WINDOW
    if reason == "trail":
        return SAR_CLOSED_TRAIL
    return SAR_NO_DATA


def _sar_status_class(status: str) -> str:
    """Badge colour, mirroring the Feed's convention."""
    if status == SAR_RUNNING:
        return "st-active"
    if status == SAR_CLOSED_TRAIL:
        return "st-tp"
    if status == SAR_CLOSED_WINDOW:
        return "st-expired"
    return "st-closed"


def reduce_sar_signals(records: Any, limit: int = 300) -> list[dict]:
    """The SAR arm as a signal-shaped feed — one row per stamped SAR trade.

    Deliberately **not** the live-signal shape: a SAR trade carries no TP and no
    SL, because the trail is its only exit. The stop/TP levels sitting on the
    record are the live arm's geometry, kept solely as the shared R denominator,
    and rendering them here would invite reading a level the trail never
    consults. So the columns are entry, exit, status — what a trailing trade
    actually has.

    Still-running trades are included (they are the majority for the first 48h
    after the flag goes live); their exit fields are ``None`` and the template
    renders an em-dash rather than a zero, so "not yet" never reads as "flat".
    """
    out: list[dict] = []
    for (symbol, side, strategy), arms in _group_arms(records).items():
        base_recs, sar_recs = arms["base"], arms["sar"]
        for i, rec in enumerate(sar_recs):
            base_rec = base_recs[i] if i < len(base_recs) else None
            status = _sar_status(rec)
            entry = float(rec.get("entry") or 0.0)
            exit_price = rec.get("trail_exit_price")
            exit_price = float(exit_price) if exit_price else None
            r_mult = _record_r(rec) if status != SAR_RUNNING else None
            pnl_pct = None
            if exit_price and entry > 0:
                move = (exit_price - entry) if side == "LONG" else (entry - exit_price)
                pnl_pct = move / entry * 100.0
            # Δ vs the live arm — the reason this arm exists. Only when BOTH
            # sides have resolved; a delta against an unresolved control is
            # not a comparison.
            delta_r = None
            if r_mult is not None and base_rec is not None:
                base_r = _record_r(base_rec)
                if base_r is not None:
                    delta_r = r_mult - base_r
            stamped = float(rec.get("suppress_timestamp") or 0.0)
            out.append({
                "symbol": symbol,
                "side": side,
                "strategy": strategy,
                "stamped_at": stamped,
                "stamped_iso": (
                    datetime.fromtimestamp(stamped, tz=timezone.utc).isoformat()
                    if stamped > 0 else ""
                ),
                "context_key": str(rec.get("context_key", "")),
                "regime": str(rec.get("regime", "")),
                "provenance": str(rec.get("provenance", "") or PROV_UNKNOWN),
                "entry": entry,
                "exit_price": exit_price,
                "exit_reason": rec.get("trail_exit_reason"),
                "hold_min": rec.get("trail_hold_min"),
                "mfe_pct": rec.get("trail_mfe_pct"),
                "status": status,
                "status_class": _sar_status_class(status),
                "r_multiple": r_mult,
                "pnl_pct": pnl_pct,
                "delta_r": delta_r,
            })
    out.sort(key=lambda r: r["stamped_at"], reverse=True)
    return out[:limit]


def filter_sar_signals(
    rows: list[dict], status: str = "", strategy: str = "", source: str = ""
) -> list[dict]:
    """Apply the status / setup / source filters (pure).

    ``source`` empty means **all** — every stamped candidate, emitted or not.
    Selecting ``emitted`` narrows to the signals that actually went out, which
    is the only population whose answer can justify changing live behaviour.
    An unknown-provenance record matches neither named filter: it is excluded
    rather than assumed, because guessing it into ``emitted`` would inflate
    exactly the number an adoption decision reads.
    """
    out = rows
    if status:
        out = [r for r in out if r["status"] == status]
    if strategy:
        out = [r for r in out if r["strategy"] == strategy]
    if source:
        out = [r for r in out if r["provenance"] == source]
    return out


def summarize_rows(rows: list[dict]) -> dict:
    """Headline for whatever the current filter selected.

    Averages are over **resolved** trades only — a running trade has no R, and
    counting it as 0R would drag every average toward zero and make the arm
    look flat while it is simply still measuring.
    """
    closed = [r for r in rows if r["r_multiple"] is not None]
    compared = [r for r in rows if r["delta_r"] is not None]
    wins = sum(1 for r in closed if r["r_multiple"] > 0)
    return {
        "n": len(rows),
        "running": sum(1 for r in rows if r["status"] == SAR_RUNNING),
        "closed": len(closed),
        "avg_r": (sum(r["r_multiple"] for r in closed) / len(closed)) if closed else None,
        "total_r": sum(r["r_multiple"] for r in closed) if closed else None,
        "win_rate": (wins / len(closed)) if closed else None,
        "compared": len(compared),
        "avg_delta_r": (
            sum(r["delta_r"] for r in compared) / len(compared)
        ) if compared else None,
    }


def reduce_ledger_status(records: Any, pairs: list[dict]) -> dict:
    """Is the arm actually producing? — the honest state of the measurement.

    The arm ships DARK, so an empty ledger is the expected state until the
    owner enables it, not a fault. This distinguishes the two so the panel
    never reads as broken when it is merely off, and never reads as off when
    it is genuinely stuck.
    """
    if isinstance(records, dict) and records.get("error"):
        return {
            "state": "unavailable",
            "detail": str(records.get("error")),
            "stamped": 0, "classified": 0, "pending": 0, "pairs": 0,
        }
    if not isinstance(records, list):
        return {
            "state": "unavailable", "detail": "unexpected ledger shape",
            "stamped": 0, "classified": 0, "pending": 0, "pairs": 0,
        }
    stamped = len(records)
    classified = sum(
        1 for r in records
        if isinstance(r, dict) and r.get("classification") is not None
    )
    pending = stamped - classified
    if stamped == 0:
        state, detail = "dark", (
            "No pairs stamped. Expected while SAR_EXIT_SHADOW_ENABLED / "
            "sar_exit_shadow_enabled is off — the arm is dark by design."
        )
    elif classified == 0:
        state, detail = "measuring", (
            "Pairs are stamping but none have resolved yet — each needs a "
            "48h forward window of real candles before it classifies."
        )
    else:
        state, detail = "live", "Pairs are stamping and resolving."
    return {
        "state": state, "detail": detail,
        "stamped": stamped, "classified": classified,
        "pending": pending, "pairs": len(pairs),
    }


def reduce_totals(pairs: list[dict]) -> dict:
    """Aggregate the paired trades — the headline both arms are judged on."""
    n = len(pairs)
    if n == 0:
        return {"n": 0}
    base_rs = [p["base_r"] for p in pairs]
    sar_rs = [p["sar_r"] for p in pairs]

    def _profit_factor(values: list[float]) -> Optional[float]:
        gain = sum(v for v in values if v > 0)
        loss = -sum(v for v in values if v < 0)
        if loss <= 0:
            return None            # no losers yet — a PF here would be a lie
        return gain / loss

    def _median(values: list[float]) -> float:
        s = sorted(values)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0

    return {
        "n": n,
        "base_total_r": sum(base_rs),
        "sar_total_r": sum(sar_rs),
        "base_avg_r": sum(base_rs) / n,
        "sar_avg_r": sum(sar_rs) / n,
        # The bake-off's contested criterion (the owner deferred ruling on it
        # 2026-07-25): a trend-following exit's signature is a negative median
        # with a positive mean, so both are shown rather than one.
        "base_median_r": _median(base_rs),
        "sar_median_r": _median(sar_rs),
        "base_win_rate": sum(1 for v in base_rs if v > 0) / n,
        "sar_win_rate": sum(1 for v in sar_rs if v > 0) / n,
        "base_pf": _profit_factor(base_rs),
        "sar_pf": _profit_factor(sar_rs),
        "delta_total_r": sum(sar_rs) - sum(base_rs),
    }


def _build_view(vol) -> dict:
    mc_raw = vol.market_context()
    affinity = mc_raw.get("strategy_affinity") if isinstance(mc_raw, dict) else None
    matrix_rows = reduce_edge_matrix(vol.strategy_edge(), affinity)
    ledger = vol.sar_exit_candidates()
    pairs = reduce_sar_pairs(ledger)
    return {
        "rollup": reduce_sar_exit(matrix_rows),
        "pairs": pairs,
        "totals": reduce_totals(pairs),
        "status": reduce_ledger_status(ledger, pairs),
        "min_sample": EDGE_MIN_SAMPLES,
    }


@router.get("/sar-exit")
async def sar_exit(request: Request):
    templates = request.app.state.templates
    ctx = _build_view(request.app.state.data_volume)
    ctx.update({"request": request, "active": "sar_exit"})
    return templates.TemplateResponse("sar_exit.html", ctx)


@router.get("/signals/sar")
async def sar_signals(
    request: Request,
    status: str = Query("", alias="status"),
    strategy: str = Query("", alias="strategy"),
    source: str = Query("", alias="source"),
):
    """The SAR arm as a signal feed — lives under Signals, beside the live Feed.

    Kept a **separate page** rather than merged into the live Feed on purpose:
    these are counterfactual shadow trades, not dispatched signals. Nothing here
    was ever sent to a subscriber or opened on anyone's capital, and the Feed's
    row actions (force-close) are meaningless against a measurement record.
    Mixing them would put shadow rows in the operator's live signal book.
    """
    templates = request.app.state.templates
    ledger = request.app.state.data_volume.sar_exit_candidates()
    all_rows = reduce_sar_signals(ledger)
    rows = filter_sar_signals(all_rows, status=status, strategy=strategy, source=source)
    return templates.TemplateResponse("sar_signals.html", {
        "request": request,
        "active": "sar_signals",
        "rows": rows,
        "total": len(all_rows),
        "summary": summarize_rows(rows),
        "statuses": [s for s in SAR_STATUSES if any(r["status"] == s for r in all_rows)],
        "strategies": sorted({r["strategy"] for r in all_rows}),
        "n_emitted": sum(1 for r in all_rows if r["provenance"] == PROV_EMITTED),
        "n_suppressed": sum(1 for r in all_rows if r["provenance"] == PROV_SUPPRESSED),
        "n_unknown": sum(1 for r in all_rows if r["provenance"] == PROV_UNKNOWN),
        "filter_status": status,
        "filter_strategy": strategy,
        "filter_source": source,
        "ledger_status": reduce_ledger_status(ledger, []),
    })


_SIGNAL_COLS = [
    "stamped_iso", "symbol", "side", "strategy", "provenance", "entry",
    "exit_price", "status", "exit_reason", "hold_min", "r_multiple", "pnl_pct",
    "delta_r", "mfe_pct", "context_key",
]


@router.get("/signals/sar/export.csv")
async def sar_signals_export_csv(
    request: Request,
    status: str = Query("", alias="status"),
    strategy: str = Query("", alias="strategy"),
    source: str = Query("", alias="source"),
):
    """The SAR signal feed as CSV, honouring the current filter."""
    from app.reports import csv_response

    ledger = request.app.state.data_volume.sar_exit_candidates()
    rows = filter_sar_signals(
        reduce_sar_signals(ledger), status=status, strategy=strategy, source=source
    )
    data = [[r.get(c) for c in _SIGNAL_COLS] for r in rows]
    return csv_response("sar_signals", _SIGNAL_COLS, data)


_PAIR_COLS = [
    "stamped_at", "symbol", "side", "strategy", "context_key", "entry",
    "base_class", "base_r", "sar_class", "sar_r", "sar_exit_price",
    "sar_exit_reason", "sar_hold_min", "delta_r",
]


@router.get("/sar-exit/export.csv")
async def sar_exit_export_csv(request: Request):
    """Every paired trade as a flat CSV — one row per candidate, both arms."""
    from app.reports import csv_response

    pairs = _build_view(request.app.state.data_volume).get("pairs", [])
    data = [[p.get(c) for c in _PAIR_COLS] for p in pairs]
    return csv_response("sar_exit_pairs", _PAIR_COLS, data)


@router.get("/sar-exit/export.json")
async def sar_exit_export_json(request: Request):
    """The full SAR exit A/B view (rollup + pairs + totals + status) as JSON."""
    from app.reports import json_response

    return json_response("sar_exit", _build_view(request.app.state.data_volume))
