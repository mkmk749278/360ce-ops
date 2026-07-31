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

What an open row is worth right now (2026-07-31)
------------------------------------------------
The first cut of this page showed an open row's entry and a dash under PnL and
R, so the page's whole subject — what these paths would have produced — was
unreadable until a row closed, up to six hours later and never at all if its
candles stopped arriving. Open rows now carry a live mark, the unrealized move
it implies, and the distance left to each level.

Three rules the SAR live tab paid for and this one inherits:

* **The mark is ours; the currency of the row is the engine's.** Ops fetches the
  whole futures ticker book in one call, so a mark costs one request regardless
  of how many rows are open. But *whether the row is still being measured* is
  graded on the engine's stamps (``last_resolved_at`` / ``bars_behind`` /
  ``resolve_misses``), never on the clock ops supplies — a surface that grades
  its own liveness on its own clock is #108, and printing a live price beside a
  two-hour-old row under the words "right now" is precisely how it read.
* **A missing stamp is not a pass.** Rows written before the engine stamped
  freshness cannot be graded, so they are ``unverified`` — their own bucket, not
  folded into "current".
* **Unrealized never sits in a realized column.** These numbers are marks, not
  results: they are kept out of the per-path Avg R and win rate entirely, and
  the panel that shows them says so.
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
#: Terminal, and deliberately unscored: past the horizon with candles that never
#: arrived, so no verdict was earned. Counted apart from a resolved row — an
#: expiry is a walked window in which nothing happened, this is the absence of a
#: measurement, and pooling them would divide a rate by rows nobody scored.
STATUS_INSUFFICIENT = "INSUFFICIENT"

#: The engine flushes this file on its 5-min resolve cycle, so "stale" is a much
#: looser bound than the SAR live tab's 60s heartbeat. Two missed cycles.
STALE_SEC = 900.0

#: A row the engine has not advanced for this long is not being measured, even
#: when the file around it is current. Two resolve cycles, same bound as the
#: file — but a different clock, which is the entire point (#108).
ROW_STALE_SEC = 900.0

#: Grace before an *unstamped* open row counts as a fault rather than as a row
#: the resolver simply has not reached yet. Mirrors the engine's
#: ``FIRST_RESOLVE_GRACE_SEC``.
FIRST_RESOLVE_GRACE_SEC = 900.0

FRESH_CURRENT = "current"
FRESH_STALLED = "stalled"
FRESH_UNVERIFIED = "unverified"


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


def mark_freshness(rows: list[dict], *, now: float) -> list[dict]:
    """Grade each open row on whether the ENGINE is still advancing it.

    ``resolve_age_sec`` is derived here because only the reader knows "now", but
    every input to the verdict is the engine's: it consumed the bar, it counted
    the miss. The ordering matters — freshness is stamped before any number
    derived from the row's status is rendered, because a stalled row's "open" is
    a claim about bars that stopped arriving.

    ``None`` — not 0, not False — wherever the engine said nothing. A row from
    before the stamps existed is ``unverified``: an unknown, and unknowns are
    counted in their own bucket rather than quietly passing.
    """
    for row in rows:
        if str(row.get("status") or "") != STATUS_OPEN:
            continue
        resolved_at = _f(row.get("last_resolved_at"))
        age = None if resolved_at is None else max(0.0, now - resolved_at)
        row["resolve_age_sec"] = age
        misses = int(_f(row.get("resolve_misses")) or 0)
        emitted = _f(row.get("emitted_at"))
        row_age = None if emitted is None else max(0.0, now - emitted)

        if resolved_at is None:
            stamped = "last_resolved_at" in row or "resolve_misses" in row
            if misses > 0:
                verdict = FRESH_STALLED
            elif not stamped:
                # Written before the engine stamped anything — cannot be graded.
                verdict = FRESH_UNVERIFIED
            elif row_age is not None and row_age > FIRST_RESOLVE_GRACE_SEC:
                # Stamps exist on this schema, and the resolver still has not
                # touched it in two cycles: that is a fault, not a young row.
                verdict = FRESH_STALLED
            else:
                verdict = FRESH_UNVERIFIED
        elif misses > 0 or row.get("stalled") is True or age > ROW_STALE_SEC:
            verdict = FRESH_STALLED
        else:
            verdict = FRESH_CURRENT

        row["freshness"] = verdict
        row["is_stalled"] = verdict == FRESH_STALLED
        row["stall_reason"] = (
            str(row.get("resolve_miss_reason") or "")
            or ("not_advanced" if verdict == FRESH_STALLED else "")
        )
    return rows


def mark_live_pnl(rows: list[dict], prices: dict) -> list[dict]:
    """Mark every open row and say what it is worth right now.

    Written into keys of its own. An unrealized number in a realized column is
    how a still-open trade gets read as a finished one, so ``pnl_pct`` /
    ``r_multiple`` — the engine's, written only at a terminal transition — are
    never touched here.

    ``unrealized_r`` divides by the engine's stamped ``sl_distance_pct``, the
    same denominator its realized R uses, so a mark on this page is comparable
    with a result on it. A row without that stamp gets a percentage and no R:
    refuse, don't clamp — an invented denominator would make a missing field
    read as a modest result.

    A level already crossed while the row still says OPEN is a **contradiction**,
    not a near miss: the resolver walks bars in order and would have closed the
    row, so price beyond the stop means those bars never arrived. Flagged rather
    than printed as one more negative percentage, which is what made a −5.45%
    KORUUSDT row unremarkable on the SAR tab.
    """
    for row in rows:
        if str(row.get("status") or "") != STATUS_OPEN:
            continue
        # Present-but-None: the column means "we could not compute this", and
        # the template must not have to tell a missing key from an unknown value.
        for key in (
            "current_price", "unrealized_pct", "unrealized_r",
            "room_to_sl_pct", "room_to_tp1_pct",
        ):
            row.setdefault(key, None)
        row.setdefault("level_crossed", "")

        price = _f(prices.get(str(row.get("symbol") or "")))
        entry = _f(row.get("entry"))
        if price is None or price <= 0 or not entry or entry <= 0:
            continue
        is_long = str(row.get("side") or "").upper() == "LONG"
        row["current_price"] = price
        move = (price - entry) if is_long else (entry - price)
        row["unrealized_pct"] = move / entry * 100.0
        sl_dist = _f(row.get("sl_distance_pct"))
        if sl_dist and sl_dist > 0:
            row["unrealized_r"] = row["unrealized_pct"] / sl_dist

        stop, tp1 = _f(row.get("stop_loss")), _f(row.get("tp1"))
        if stop and stop > 0:
            room = (price - stop) if is_long else (stop - price)
            row["room_to_sl_pct"] = room / price * 100.0
        if tp1 and tp1 > 0:
            room = (tp1 - price) if is_long else (price - tp1)
            row["room_to_tp1_pct"] = room / price * 100.0
        if row["room_to_sl_pct"] is not None and row["room_to_sl_pct"] < 0:
            row["level_crossed"] = "SL"
        elif row["room_to_tp1_pct"] is not None and row["room_to_tp1_pct"] < 0:
            row["level_crossed"] = "TP1"
    return rows


def summarize_open(rows: list[dict]) -> dict:
    """What the open book is worth right now, and how much of it can be trusted.

    Two denominators, both published, neither called "the" number: every marked
    open row, and only the rows the engine is demonstrably still advancing. They
    are the same figure while the lane is healthy and they diverge exactly when
    it is not — and the second one is the one an adoption decision may read,
    because a stalled row's status is a claim about bars that stopped arriving
    even though its mark is arithmetically fine.
    """
    open_rows = [r for r in rows if str(r.get("status") or "") == STATUS_OPEN]
    marked = [r for r in open_rows if r.get("unrealized_pct") is not None]
    current = [r for r in marked if r.get("freshness") == FRESH_CURRENT]

    def _agg(bucket: list[dict]) -> dict:
        r_vals = [_f(r.get("unrealized_r")) for r in bucket]
        r_vals = [v for v in r_vals if v is not None]
        pct = [_f(r.get("unrealized_pct")) or 0.0 for r in bucket]
        return {
            "n": len(bucket),
            "n_r": len(r_vals),
            "avg_r": (sum(r_vals) / len(r_vals)) if r_vals else None,
            "avg_pct": (sum(pct) / len(pct)) if pct else None,
            "n_up": sum(1 for v in pct if v > 0),
        }

    return {
        "open": len(open_rows),
        "unmarked": len(open_rows) - len(marked),
        "stalled": sum(1 for r in open_rows if r.get("freshness") == FRESH_STALLED),
        "unverified": sum(
            1 for r in open_rows if r.get("freshness") == FRESH_UNVERIFIED
        ),
        "crossed": sum(1 for r in open_rows if r.get("level_crossed")),
        "all_marked": _agg(marked),
        "still_measured": _agg(current),
    }


def summarize(rows: list[dict]) -> dict:
    """Per-path verdict on the rows that actually resolved.

    Open rows are counted and excluded from every rate. An expiry is counted
    apart from a loss and scores 0R: the mechanism did nothing, it did not lose,
    and folding the two together is the #685 fabrication class. The win rate
    divides by **decided** rows (TP1 + SL) for the same reason.

    ``INSUFFICIENT`` is terminal but unmeasured — the engine retires a row whose
    candles never arrived rather than leaving it open forever. It is counted in
    its own column and kept out of ``resolved``, so no rate here is ever divided
    by a population that includes rows nobody scored. Unrealized marks live in
    ``summarize_open`` and never enter this table.
    """
    by_setup: dict[str, dict] = {}
    for row in rows:
        setup = str(row.get("setup_class") or "UNKNOWN")
        agg = by_setup.setdefault(setup, {
            "setup_class": setup, "n": 0, "open": 0, "tp1": 0, "sl": 0,
            "expired": 0, "insufficient": 0, "sum_r": 0.0, "n_r": 0, "gates": {},
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
        if status == STATUS_INSUFFICIENT:
            agg["insufficient"] += 1
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
        agg["resolved"] = agg["n"] - agg["open"] - agg["insufficient"]
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
    open_rows = [r for r in rows if str(r.get("status") or "") == STATUS_OPEN]
    stalled = sum(1 for r in open_rows if r.get("freshness") == FRESH_STALLED)
    # The file's age says the engine is writing; it says nothing about whether
    # the rows inside it are moving. "3 arms running" was true and useless on
    # 2026-07-30 while two of the three had consumed zero bars (#108), so the
    # headline carries the second number — the one that invalidates the first.
    detail = (
        f"{len(rows)} dark signals, {len(open_rows)} still open"
        + (
            f", {stalled} of them no longer being advanced"
            if stalled
            else (" and being advanced" if open_rows else "")
        )
        + ". Every one cleared the full scanner chain with one gate overridden, "
        "and none of them reached a channel, a push, the app feed, or an order."
    )
    return {"state": "live", "detail": detail, "stalled": stalled}


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

    # One request for the whole futures book, TTL-cached: marking N open rows
    # costs one call rather than N, so the page's cost does not scale with how
    # many trades are running. A missing price blanks a column, never breaks the
    # page — which is why the failure is swallowed here and shown as an unmarked
    # row rather than raised.
    try:
        prices = await request.app.state.binance_klines.fetch_all_prices()
    except Exception:
        prices = {}

    # Freshness first, on every row: the verdict below reads it, and the state
    # banner counts it. Marks second — a mark is only meaningful once the reader
    # knows whether the row it sits beside is still being measured.
    now = time.time()
    mark_freshness(rows, now=now)
    mark_live_pnl(rows, prices)

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
        # Summaries measured on the full selection, never the truncated table.
        "summary": summarize(selected),
        "live": summarize_open(selected),
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
    "sl_distance_pct",
    "emitted_at", "status", "closed_at", "exit_price", "pnl_pct", "r_multiple",
    "mfe_pct", "bars_seen", "ambiguous_bar",
    # The mark and what it implies. An export is a surface and inherits the
    # page's rules — the SAR export that first showed a 2h-old stop had no
    # column that could have said so, so it read as healthy too.
    "current_price", "unrealized_pct", "unrealized_r",
    "room_to_sl_pct", "room_to_tp1_pct", "level_crossed",
    # …and whether the row beside those numbers is still being measured.
    "last_resolved_at", "resolve_age_sec", "last_bar_ms", "bars_behind",
    "resolve_misses", "resolve_miss_reason", "stalled", "freshness",
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
    try:
        prices = await request.app.state.binance_klines.fetch_all_prices()
    except Exception:
        prices = {}
    mark_freshness(rows, now=time.time())
    mark_live_pnl(rows, prices)
    data = [[r.get(c) for c in _DARK_COLS] for r in rows]
    return csv_response("dark_signals_live", _DARK_COLS, data)
