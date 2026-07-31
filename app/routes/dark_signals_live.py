"""Dark emission lane — the silenced paths, emitting, owner-only.

Engine ``src/dark_emission.py``. Added 2026-07-31 on the owner's instruction:
*"Loosening gates per path to emit some signals — those signals we can see and
analyse, not for users."*

What makes these rows different from everything else in this repo
-----------------------------------------------------------------
Every other suppression surface here is a **counterfactual**: a gate killed a
candidate, and a resolver scored it later. That is optimistic (~0.38R measured)
and it cannot describe a feed.

A dark row went through the **whole scanner chain** — scoring, MTF,
min_confidence, the context floors, level_still_in_play, staleness — with one
gate overridden. It is a signal the engine was *willing to send*, on a path
that normally sends nothing: ``SR_FLIP_RETEST`` shipped one signal in nine days,
``RANGE_FADE`` and ``MEAN_REVERT`` zero, while ``MOVER_TREND_PULLBACK`` took 64%
of the delivered book.

The two sentences this page must never merge
---------------------------------------------
**"The scanner was willing to send this"** is what a dark row means.
**"A user would have seen this"** is what it does not mean.

``SignalRouter._process`` applies a second layer the engine's dark lane does not
— correlation lock, per-symbol and per-channel cooldown, concurrency caps,
same-direction throttle — and drops most of what it dequeues. So the count here
over-reports a feed size, and every number is captioned accordingly. Labelling a
pre-router population as a delivered one is exactly #816, where ``emitted``
stamped at the enqueue site inflated the only population allowed to justify a
live change by ~30x and this dashboard read "Emitted to live (98)" for a window
with 3 real signals.

Safety, because the page should state it rather than imply it
--------------------------------------------------------------
No row here reached a channel, a push, the app feed, or an order. The engine
diverts a dark candidate at the single ``signal_queue.put`` site, and the queue
is the only route to the router — so the property is structural rather than a
set of skips that could each be forgotten.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Query, Request

router = APIRouter()

#: Render bound, applied in the route AFTER filtering — never in a reducer (#97).
TABLE_ROW_CAP = 400

STATUS_OPEN = "OPEN"
STATUS_TP1 = "CLOSED_TP1"
STATUS_SL = "CLOSED_SL"
STATUS_EXPIRED = "EXPIRED"

#: The engine flushes this file on its 5-min resolve cycle, so "stale" is a much
#: looser bound than the SAR live tab's 60s heartbeat. Two missed cycles.
STALE_SEC = 900.0


def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def reduce_rows(payload: Any) -> list[dict]:
    """Rows newest first. A bad or missing payload yields [] rather than raising —
    the page then says the file is unavailable, which is a different statement
    from "no dark signals" and the template distinguishes them."""
    if not isinstance(payload, dict):
        return []
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return []
    out = [r for r in rows if isinstance(r, dict)]
    out.sort(key=lambda r: _f(r.get("emitted_at")) or 0.0, reverse=True)
    return out


def filter_rows(
    rows: list[dict], *, setup: str = "", gate: str = "", status: str = ""
) -> list[dict]:
    """Each selector independent, so a caller can omit one when counting that
    selector's own options (#90/#91)."""
    out = rows
    if setup:
        out = [r for r in out if str(r.get("setup_class") or "") == setup]
    if gate:
        out = [r for r in out if str(r.get("dark_gate") or "").startswith(gate)]
    if status:
        out = [r for r in out if str(r.get("status") or "") == status]
    return out


def summarize(rows: list[dict]) -> dict:
    """Per-path verdict on the rows that actually resolved.

    Open rows are counted and excluded from every rate. An expiry is counted
    apart from a loss and scores 0R: the mechanism did nothing, it did not lose,
    and folding the two together is the #685 fabrication class. The win rate
    divides by **decided** rows (TP1 + SL) for the same reason.
    """
    by_setup: dict[str, dict] = {}
    for row in rows:
        setup = str(row.get("setup_class") or "UNKNOWN")
        agg = by_setup.setdefault(setup, {
            "setup_class": setup, "n": 0, "open": 0, "tp1": 0, "sl": 0,
            "expired": 0, "sum_r": 0.0, "n_r": 0, "gates": {},
            "sum_conf": 0.0, "n_conf": 0,
        })
        agg["n"] += 1
        gate = str(row.get("dark_gate") or "")
        agg["gates"][gate] = agg["gates"].get(gate, 0) + 1
        conf = _f(row.get("confidence"))
        if conf is not None:
            agg["sum_conf"] += conf
            agg["n_conf"] += 1
        status = str(row.get("status") or "")
        if status == STATUS_OPEN:
            agg["open"] += 1
            continue
        if status == STATUS_TP1:
            agg["tp1"] += 1
        elif status == STATUS_SL:
            agg["sl"] += 1
        elif status == STATUS_EXPIRED:
            agg["expired"] += 1
        r = _f(row.get("r_multiple"))
        if r is not None:
            agg["sum_r"] += r
            agg["n_r"] += 1
    out = []
    for agg in by_setup.values():
        decided = agg["tp1"] + agg["sl"]
        agg["resolved"] = agg["n"] - agg["open"]
        agg["decided"] = decided
        agg["avg_r"] = (agg["sum_r"] / agg["n_r"]) if agg["n_r"] else None
        agg["win_rate"] = (agg["tp1"] / decided) if decided else None
        agg["avg_confidence"] = (
            (agg["sum_conf"] / agg["n_conf"]) if agg["n_conf"] else None
        )
        top = sorted(agg["gates"].items(), key=lambda kv: -kv[1])
        agg["top_gate"] = top[0][0] if top else "—"
        out.append(agg)
    out.sort(key=lambda a: -a["n"])
    return {"by_setup": out, "n": len(rows)}


def reduce_state(provenance: dict, rows: list[dict], now: Optional[float] = None) -> dict:
    """Is this page live? Read before any number on it.

    Three states the reader needs kept apart, the same distinction the SAR live
    tab had to learn: **missing** = the lane is off or the resolve loop is not
    running; **current and empty** = the lane is on and the loosened paths have
    produced nothing, which is a finding rather than a fault; **stale** = the
    engine stopped writing.
    """
    _now = time.time() if now is None else now
    age = provenance.get("age_sec")
    if not provenance.get("exists"):
        return {
            "state": "unavailable",
            "detail": (
                f"{provenance.get('file')} is not on the data volume. The engine "
                "writes it on the 5-minute resolve cycle whenever the lane is "
                "on, so this means DARK_EMISSION_ENABLED is off or that loop is "
                "not running — not that the paths are quiet."
            ),
        }
    if age is not None and age > STALE_SEC:
        return {
            "state": "stale",
            "detail": (
                f"The dark ledger has not been written for {age / 60.0:.0f} "
                "minutes. The engine flushes it every resolve cycle (~5 min), so "
                "open rows below carry outcomes that stopped being updated then."
            ),
        }
    if not rows:
        return {
            "state": "empty",
            "detail": (
                "The lane is running and has emitted nothing yet. That is a "
                "finding, not a fault: the loosened gates are not what is "
                "stopping these paths — something upstream of them is, most "
                "likely the detector itself."
            ),
        }
    open_n = sum(1 for r in rows if str(r.get("status") or "") == STATUS_OPEN)
    return {
        "state": "live",
        "detail": (
            f"{len(rows)} dark signals, {open_n} still open. Every one cleared "
            "the full scanner chain with one gate overridden, and none of them "
            "reached a channel, a push, the app feed, or an order."
        ),
    }


@router.get("/signals/dark-live")
async def dark_signals_live(
    request: Request,
    setup: str = Query(""),
    gate: str = Query(""),
    status: str = Query(""),
):
    vol = request.app.state.data_volume
    payload = vol.dark_signals()
    provenance = vol.dark_signals_provenance()
    rows = reduce_rows(payload)

    # Every count measured with every filter applied EXCEPT its own (#90/#91).
    scoped_setup = filter_rows(rows, gate=gate, status=status)
    scoped_gate = filter_rows(rows, setup=setup, status=status)
    scoped_status = filter_rows(rows, setup=setup, gate=gate)
    selected = filter_rows(rows, setup=setup, gate=gate, status=status)

    # Truncate after filtering, never before (#97). The page says when it bit.
    shown = selected[:TABLE_ROW_CAP]

    return request.app.state.templates.TemplateResponse("dark_signals_live.html", {
        "request": request,
        "active": "dark_live",
        "rows": shown,
        "matched": len(selected),
        "shown": len(shown),
        "row_cap": TABLE_ROW_CAP,
        # Summary measured on the full selection, never the truncated table.
        "summary": summarize(selected),
        "state": reduce_state(provenance, rows),
        "provenance": provenance,
        "setups": sorted({str(r.get("setup_class") or "") for r in rows if r.get("setup_class")}),
        "gates": sorted({str(r.get("dark_gate") or "") for r in rows if r.get("dark_gate")}),
        "statuses": sorted({str(r.get("status") or "") for r in rows if r.get("status")}),
        "n_setup": {
            s: sum(1 for r in scoped_setup if r.get("setup_class") == s)
            for s in sorted({str(r.get("setup_class") or "") for r in rows if r.get("setup_class")})
        },
        "n_gate": {
            g: sum(1 for r in scoped_gate if str(r.get("dark_gate") or "").startswith(g))
            for g in sorted({str(r.get("dark_gate") or "") for r in rows if r.get("dark_gate")})
        },
        "n_status": {
            st: sum(1 for r in scoped_status if r.get("status") == st)
            for st in sorted({str(r.get("status") or "") for r in rows if r.get("status")})
        },
        "filter_setup": setup,
        "filter_gate": gate,
        "filter_status": status,
    })


_DARK_COLS = [
    "signal_id", "symbol", "side", "setup_class", "dark_gate", "confidence",
    "regime", "context_key", "pair_admission", "entry", "stop_loss", "tp1",
    "emitted_at", "status", "closed_at", "exit_price", "pnl_pct", "r_multiple",
    "mfe_pct", "bars_seen", "ambiguous_bar",
]


@router.get("/signals/dark-live/export.csv")
async def dark_signals_export(
    request: Request,
    setup: str = Query(""),
    gate: str = Query(""),
    status: str = Query(""),
):
    """The current selection as CSV — uncapped, unlike the rendered table."""
    from app.reports import csv_response

    vol = request.app.state.data_volume
    rows = filter_rows(reduce_rows(vol.dark_signals()), setup=setup, gate=gate, status=status)
    data = [[r.get(c) for c in _DARK_COLS] for r in rows]
    return csv_response("dark_signals_live", _DARK_COLS, data)
