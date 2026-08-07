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

Max profit, and the exit methods (2026-08-03, engine #869)
-----------------------------------------------------------
Owner: *"implement same like live features in dark feed — max PnL before hitting
SL, and same exit strategies like Held to stop in dark feed too."*

Neither was answerable from this ledger, and not for want of a column. The
engine's dark walk **stopped at the first TP1-or-SL touch**, so ``mfe_pct`` on a
row that closed at TP1 is bounded by TP1 by construction — it says how far the
trade ran before its own exit and nothing at all about how far it was going to
run. Everything after that touch was never looked at, which is also why no
held-to-stop or laddered exit could be priced.

The engine now walks a second arm over the same bars with TP1 removed
(``dark_emission._walk_hold``): it exits only at the original stop or at the
six-hour horizon, and it records the peak before the stop, the drawdown on the
way to that peak, and the highest ladder level reached before it. This page
prices the Profit tab's strategy catalog off those stamps
(``app/data_sources/dark_exit_sim.py``) and leads with the peak.

Three things the panels must keep saying:

* **The two arms are different mechanisms and are never blended.** The row's own
  status is its SL/TP1 geometry; the held arm is what holding would have done.
  Same rule as the SAR panel below, same reason.
* **The comparison population is the rows where BOTH decided.** A strategy
  measured on the subset it happens to be able to price would win on selection.
* **Rows the arm cannot speak for are counted and named**, never scored zero:
  still running, retired unmeasured, or written before the arm shipped are three
  different states with three different next moves.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Query, Request

from app.data_sources.dark_exit_sim import (
    HOLD_OPEN,
    DarkRowInputs,
    build_catalog,
    compare_strategies,
    evaluate,
    get_strategy,
    hold_coverage,
    summarize_max_profit,
)

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

#: Why a row retired unmeasured. Mirrors the engine's own constants — a row
#: whose candles never arrived and one whose walk had holes are different
#: faults, and the second was silently scored 0R as an expiry until 2026-08-01.
INSUFFICIENT_PARTIAL_WINDOW = "partial_window"
INSUFFICIENT_NO_WALK = "no_walk"

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


#: The engine's `price_action_lane.DARK_GATE`. Mirrored deliberately and pinned
#: by a test that reads the engine constant, because an unpinned mirror is what
#: drifted MEASUREMENT_SUFFIXES for a week.
LANE_DARK_GATE = "price_action_lane"


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
    # Phase 5 rows share this ledger for its resolver and are NOT the same kind
    # of thing. Every row on this page is a candidate the scanner was willing to
    # send with one gate loosened — it cleared the full scoring engine, the MTF
    # policy, the confidence floor and every other gate. A price-action lane row
    # has been through NONE of that; it never entered the scanner's chain.
    # Pooling them would make this page's own first sentence false, and it is
    # how 15 rows disappear into 2,418. They are read on /signals/price-action.
    out = [r for r in out if str(r.get("dark_gate") or "") != LANE_DARK_GATE]
    out.sort(key=lambda r: _f(r.get("emitted_at")) or 0.0, reverse=True)
    return out


def reduce_lane_rows(payload: Any) -> list[dict]:
    """The complement: only the price-action lane's rows.

    One reader, one writer — the two populations are split here rather than in
    two places that could drift apart.
    """
    if not isinstance(payload, dict):
        return []
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return []
    out = [
        r for r in rows
        if isinstance(r, dict) and str(r.get("dark_gate") or "") == LANE_DARK_GATE
    ]
    out.sort(key=lambda r: _f(r.get("emitted_at")) or 0.0, reverse=True)
    return out


#: Same 90 minutes `/signals/price-action` groups a run by, so a reader moving
#: between the two pages compares like with like.
#:
#: There it is *derived* — three times the engine's 30-minute per-symbol emit
#: throttle. Here nothing derives it, and that difference is the finding rather
#: than an oversight: a dark candidate is diverted at the `signal_queue.put`
#: site, **before** `SignalRouter._process`, so `GLOBAL_SYMBOL_COOLDOWN_SECONDS`
#: never applies to it. This lane has no per-symbol throttle at all. The window
#: is therefore a chosen grouping, it is named as one on screen, and the panel
#: publishes the campaign view beside it precisely because no single window can
#: be the right one.
RUN_GAP_S = 5_400.0


def _net_pct(rows: list[dict], fee_pct: float) -> Optional[float]:
    """Net PnL over the rows that carry one, fee charged per row.

    A row with no readable `pnl_pct` is excluded rather than scored zero — an
    open row is not a flat one, and `EXPIRED` already *is* a real 0.00%.
    """
    scored = [r for r in rows if _f(r.get("pnl_pct")) is not None]
    if not scored:
        return None
    return sum(_f(r.get("pnl_pct")) or 0.0 for r in scored) - fee_pct * len(scored)


def _group_stats(
    groups: list[tuple[str, list[dict]]], rows: list[dict], fee_pct: float
) -> dict:
    """Shared shape for both groupings, so neither can quietly measure differently."""
    book = _net_pct(rows, fee_pct)
    # Groups carrying at least one scored row. NOT "losing groups" — a group
    # whose rows are all still open has no net at all, and is a different thing
    # from one that netted zero.
    scored = [
        (label, g, net) for label, g, net in
        ((label, g, _net_pct(g, fee_pct)) for label, g in groups)
        if net is not None
    ]

    worst = None
    if scored:
        label, g, net = min(scored, key=lambda x: x[2])
        stamps = [s for s in (_f(r.get("emitted_at")) for r in g) if s is not None]
        worst = {
            "label": label,
            "n_rows": len(g),
            "hours": (max(stamps) - min(stamps)) / 3600.0 if len(stamps) >= 2 else 0.0,
            "net_pct": net,
            # Only meaningful while the book is losing: "this one group is N% of
            # everything the selection lost". Over 100% means the rest is
            # positive without it.
            "share_of_book": (net / book * 100.0) if (book is not None and book < 0) else None,
            # Exactly this group's rows — removing every row that merely shares
            # a ticker would overstate what one group cost.
            "book_without": _net_pct([r for r in rows if id(r) not in {id(x) for x in g}], fee_pct),
        }

    # How few groups it takes to change the sign. This is a CONCENTRATION
    # measure, not a filter anyone could have run: the groups are chosen by
    # already knowing their outcome, which is why the row share is printed
    # beside it. A small k over a small row share is the finding — it says the
    # verdict rests on a handful of symbols rather than on the paths.
    to_flip = None
    if book is not None and book < 0 and scored:
        ordered = sorted(scored, key=lambda x: x[2])
        running, removed = book, 0
        for i, (_, g, net) in enumerate(ordered, start=1):
            running -= net
            removed += len(g)
            if running >= 0:
                to_flip = {
                    "k": i,
                    "n_rows": removed,
                    "row_share": removed / len(rows) * 100.0 if rows else 0.0,
                    "book_without": running,
                }
                break

    multi = sum(len(g) for _, g in groups if len(g) > 1)
    return {
        "n_groups": len(groups),
        "rows_per_group": (len(rows) / len(groups)) if groups else 0.0,
        "multi_row_share": (multi / len(rows) * 100.0) if rows else 0.0,
        "worst": worst,
        "to_flip": to_flip,
    }


def concentration(rows: list[dict], *, fee_pct: float) -> dict:
    """Two groupings of the same rows, because they answer different questions.

    `/signals/price-action` learned (2026-08-07) that a move key of
    ``symbol · side · entry`` is structurally blind to a trending symbol — a
    trend hands out a new entry every time — and shipped a **run** panel keyed
    on time-clustered re-entries. Porting that panel here alone would have been
    wrong, and measuring it first is the only reason we know:

    * **Runs** (this symbol, this side, re-stamped inside 90 minutes) find
      almost nothing on this book. Median gap between consecutive stamps on one
      symbol·side is ~12 hours and only ~15% of gaps fall inside the window, so
      the worst run on the 2026-08-07 book was 3 rows and 8% of the loss.
    * **Campaigns** (this symbol, this side, over the whole window) carry it:
      183 campaigns over 445 closed rows, and the worst **eight** — 50 rows,
      11% of the book — take it from −119.85% to positive.

    That is the price-action lesson arriving with its own key wrong: a run panel
    here would have read *"concentration is not a problem"* over a book whose
    verdict is eleven percent of its rows. The dark lane spans days and has no
    per-symbol throttle, so its repeats are spread out rather than bunched, and
    a window narrow enough to mean "one move" cannot see them.

    Both render, neither is "the" number, and **nothing de-duplicates** — that
    judgement belongs to the reader, and counting silently is what this panel
    exists to stop (#816 at the display side).
    """
    if not rows:
        return {
            "n_rows": 0, "book_net": None, "gap_minutes": RUN_GAP_S / 60.0,
            "runs": None, "campaigns": None,
        }

    by_key: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        key = (str(row.get("symbol") or "?"), str(row.get("side") or "?"))
        by_key.setdefault(key, []).append(row)

    campaigns = [(f"{sym} {side}", g) for (sym, side), g in by_key.items()]

    runs: list[tuple[str, list[dict]]] = []
    for (sym, side), group in by_key.items():
        ordered = sorted(group, key=lambda r: _f(r.get("emitted_at")) or 0.0)
        run = [ordered[0]]
        for prev, cur in zip(ordered, ordered[1:]):
            a, b = _f(prev.get("emitted_at")), _f(cur.get("emitted_at"))
            if a is not None and b is not None and (b - a) < RUN_GAP_S:
                run.append(cur)
            else:
                runs.append((f"{sym} {side}", run))
                run = [cur]
        runs.append((f"{sym} {side}", run))

    return {
        "n_rows": len(rows),
        "book_net": _net_pct(rows, fee_pct),
        "gap_minutes": RUN_GAP_S / 60.0,
        "runs": _group_stats(runs, rows, fee_pct),
        "campaigns": _group_stats(campaigns, rows, fee_pct),
    }


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

    **A closed row is graded too when its held-to-stop arm is still running.**
    That arm exits at the stop rather than at TP1, so it routinely outlives the
    row's own verdict — and while it runs the page prints a mark beside it. A
    mark with no currency verdict is exactly the thing this function exists to
    prevent, and "the row is closed" stopped meaning "nothing here is still
    being measured" the moment the second arm shipped.
    """
    for row in rows:
        hold_running = str(row.get("hold_status") or "") == HOLD_OPEN
        if str(row.get("status") or "") != STATUS_OPEN and not hold_running:
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
        elif row.get("window_undated_reason"):
            # Advanced — so not stalled — but on bars the engine could not date,
            # which means nothing can say whether they are current. Printing
            # "2m ago" here would answer a question nobody asked: that is when
            # the resolver ran, not how recent the candles it read were. Its own
            # bucket, because an unknown is not a pass.
            verdict = FRESH_UNVERIFIED
        else:
            verdict = FRESH_CURRENT

        row["freshness"] = verdict
        row["is_stalled"] = verdict == FRESH_STALLED
        row["stall_reason"] = (
            str(row.get("resolve_miss_reason") or "")
            or ("not_advanced" if verdict == FRESH_STALLED else "")
        )
        # Why it is unverified: written before the stamps existed, or advanced
        # on an undatable window. Different causes, different fixes, and the
        # second one is a live fault rather than a legacy row.
        row["unverified_reason"] = (
            str(row.get("window_undated_reason") or "")
            if verdict == FRESH_UNVERIFIED else ""
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
        # Present-but-None on EVERY row, not only the open ones: the column
        # means "we could not compute this", and the template must not have to
        # tell a missing key from an unknown value.
        #
        # This block used to sit BELOW the status check, so it described only
        # the rows that were going to be marked anyway — the promise above was
        # kept for exactly the population that did not need it. A terminal row
        # is normally covered by `pnl_pct`, so the gap was invisible until a
        # status that is terminal AND deliberately unscored appeared:
        # `INSUFFICIENT` (#839 — the absence of a measurement, never 0R). Two
        # such rows took `/signals/price-action` down with a 500, because a
        # Jinja `Undefined` is not `None` and sails straight through an
        # `is not none` guard into `format()`. The seam is here, not in the
        # template that trusted the docstring.
        for key in (
            "current_price", "unrealized_pct", "unrealized_r",
            "room_to_sl_pct", "room_to_tp1_pct",
        ):
            row.setdefault(key, None)
        row.setdefault("level_crossed", "")

        if str(row.get("status") or "") != STATUS_OPEN:
            continue

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

    **PnL % leads and R is subordinate** (owner, 2026-08-02: *"that R is purely
    confusing — 3% SL still only 1R"*). ``signal_dispatch`` sizes every position
    at a fixed notional, so the stop distance is absent from the sizing formula;
    R divides each outcome by its own stop, which equalises trades only when size
    scales inversely to that stop. It does not.

    This misranks paths, measurably. On the 2026-08-02 window MEAN_REVERT reads
    worst-in-book by R (−0.481R) while losing $2.02 per trade, and
    MA_CROSS_TREND_SHIFT reads mid-table (−0.293R) while losing $11.05 — five
    times more money from the path R called healthier, because its median stop is
    5.15% against MEAN_REVERT's 1.09%. R is kept as the bridge to the Strategy
    Lab and the edge matrix, which are keyed in R, and is never the headline.

    Open rows are counted and excluded from every rate. An expiry is counted
    apart from a loss and scores 0R: the mechanism did nothing, it did not lose,
    and folding the two together is the #685 fabrication class. The win rate
    divides by **decided** rows (TP1 + SL) for the same reason.

    ``INSUFFICIENT`` is terminal but unmeasured, and it now has **two** causes
    that are counted apart (engine 2026-08-01):

    * ``no_walk`` — the candles never arrived, so the row was never walkable.
    * ``partial_window`` — the row *was* walked, but the walk did not cover its
      window, so "nothing happened" would be a claim about bars nobody looked at.
      In the owner's window ROBOUSDT expired on 309 bars of a 362-minute window
      and ARBUSDT on 329 of 365; a touch inside the missing 89 minutes would have
      been booked as a 0R expiry.

    Pooling them would report one fault while the other is what is happening —
    this repo's own "blank needs a cause before it gets a caption" rule. Both are
    kept out of ``resolved``, so no rate here is ever divided by a population
    that includes rows nobody scored. Unrealized marks live in ``summarize_open``
    and never enter this table.
    """
    by_setup: dict[str, dict] = {}
    for row in rows:
        setup = str(row.get("setup_class") or "UNKNOWN")
        agg = by_setup.setdefault(setup, {
            "setup_class": setup, "n": 0, "open": 0, "tp1": 0, "sl": 0,
            "expired": 0, "insufficient": 0, "insufficient_no_walk": 0,
            "insufficient_partial_window": 0, "sum_r": 0.0, "n_r": 0,
            "sum_pnl": 0.0, "n_pnl": 0, "gates": {},
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
            # Split by the engine's stamped reason. A missing series and a
            # series with holes are different faults with different fixes.
            if str(row.get("insufficient_reason") or "") == INSUFFICIENT_PARTIAL_WINDOW:
                agg["insufficient_partial_window"] += 1
            else:
                agg["insufficient_no_walk"] += 1
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
        # The money. Primary since 2026-08-02 — see the module docstring for why
        # R misranks this book. Needs no denominator, so its population is every
        # scored row rather than the subset carrying an entry-risk stamp.
        pnl = _f(row.get("pnl_pct"))
        if pnl is not None:
            agg["sum_pnl"] += pnl
            agg["n_pnl"] += 1
    out = []
    for agg in by_setup.values():
        decided = agg["tp1"] + agg["sl"]
        agg["resolved"] = agg["n"] - agg["open"] - agg["insufficient"]
        agg["decided"] = decided
        agg["avg_r"] = (agg["sum_r"] / agg["n_r"]) if agg["n_r"] else None
        agg["avg_pnl_pct"] = (agg["sum_pnl"] / agg["n_pnl"]) if agg["n_pnl"] else None
        agg["total_pnl_pct"] = agg["sum_pnl"] if agg["n_pnl"] else None
        agg["win_rate"] = (agg["tp1"] / decided) if decided else None
        agg["avg_confidence"] = (
            (agg["sum_conf"] / agg["n_conf"]) if agg["n_conf"] else None
        )
        top = sorted(agg["gates"].items(), key=lambda kv: -kv[1])
        agg["top_gate"] = top[0][0] if top else "—"
        out.append(agg)
    out.sort(key=lambda a: -a["n"])
    return {"by_setup": out, "n": len(rows)}


def _resolve_target_pct(raw: Optional[float]) -> float:
    """Clamp the fixed-target / break-even percent to 0.1–20%, default 1%.

    Same band and default as the Profit tab's, so the two pages sweep the same
    range and a figure copied between them means the same thing.
    """
    if raw is None:
        return 1.0
    return max(0.1, min(20.0, raw))


def _resolve_fee_pct(raw: Optional[float]) -> float:
    """Round-trip fee, 0–2%, default 0.07 (Binance USD-M maker in + taker out).

    Charged because the cost of trading has run ~10× this book's edge, so a
    gross-only comparison answers the wrong question — `/track-record`'s rule,
    applied to a page that compares exit methods, where the fee count differs
    between a one-leg exit and a three-leg ladder in the reader's head but not
    in this number. (It is one round trip either way here: the ladder's legs
    close one position, and modelling a per-leg taker fee would need a fill
    model this ledger does not carry. Stated rather than silently assumed.)
    """
    if raw is None:
        return 0.07
    return max(0.0, min(2.0, raw))


#: An arm's terminal states, mirrored from the engine's ``sar_live_shadow``.
#: ``INSUFFICIENT`` is terminal but deliberately unscored — the absence of a
#: measurement, not a 0R outcome.
ARM_RUNNING = "RUNNING"
ARM_INSUFFICIENT = "INSUFFICIENT"


def reduce_arms(payload: Any) -> dict[str, list[dict]]:
    """SAR arms from the dark lane's own file, indexed by ``signal_id``.

    One dark row can hold several arms — one per timeframe — and they are kept
    as a list rather than collapsed. Pooling 5m and 15m into one number would
    make the headline move with the timeframe mix instead of the mechanism,
    which is the rule ``/signals/sar-live`` already carries.
    """
    out: dict[str, list[dict]] = {}
    if not isinstance(payload, dict):
        return out
    for key in ("open", "resolved"):
        arms = payload.get(key)
        if not isinstance(arms, list):
            continue
        for arm in arms:
            if not isinstance(arm, dict):
                continue
            sid = str(arm.get("signal_id") or "")
            if sid:
                out.setdefault(sid, []).append(arm)
    return out


def attach_arms(rows: list[dict], arms_by_signal: dict[str, list[dict]]) -> list[dict]:
    """Hang each dark row's SAR arms off the row, without merging the outcomes.

    Two outcomes per row and **neither is "the" result**: ``status``/
    ``r_multiple`` are what the row's own SL/TP1 geometry produced, and the arm
    carries what a SAR handover would have produced from the same entry. There
    is deliberately no combined field — collapsing them before the difference is
    known is choosing the answer, exactly as with the two fills and the two
    denominators on the live SAR page.

    A row with no arm gets an empty list, which is a real state and not a zero:
    the engine refuses to open an arm whose anchor bar is stale (#836), so
    "no arm" usually means the guard fired, never that SAR did nothing.
    """
    for row in rows:
        sid = str(row.get("signal_id") or "")
        found = arms_by_signal.get(sid) or []
        row["sar_arms"] = found
        row["sar_n"] = len(found)
        # The single most-advanced arm, for the row-level column. Chosen by bars
        # consumed rather than by timeframe so the column never silently prefers
        # a series that stopped arriving.
        best = None
        for arm in found:
            if best is None or int(arm.get("bars_seen") or 0) > int(best.get("bars_seen") or 0):
                best = arm
        row["sar_best"] = best
        row["sar_status"] = str(best.get("status") or "") if best else ""
    return rows


def summarize_sar(rows: list[dict]) -> dict:
    """The two outcomes side by side, over the rows where BOTH are decided.

    The comparison population is deliberately narrow. A dark row that resolved
    while its arm is still running, or an arm that closed while the row is still
    open, describes one mechanism and not the other — averaging over the union
    would compare two different sets of trades and call the difference a result.
    So the panel reports the paired population and says how many rows fell out
    of it, rather than quietly widening the denominator.

    ``@level`` and ``@confirm`` stay apart for the reason the live SAR page
    keeps them apart: their difference is the cost of confirmation, and it is
    never zero.
    """
    paired: list[tuple[dict, dict]] = []
    row_only = arm_only = 0
    for row in rows:
        arm = row.get("sar_best")
        row_done = str(row.get("status") or "") not in ("", STATUS_OPEN)
        row_scored = row_done and str(row.get("status")) != STATUS_INSUFFICIENT
        arm_done = bool(arm) and str(arm.get("status") or "") not in ("", ARM_RUNNING)
        arm_scored = arm_done and str(arm.get("status")) != ARM_INSUFFICIENT
        if row_scored and arm_scored:
            paired.append((row, arm))
        elif row_scored and arm is not None:
            row_only += 1
        elif arm_scored:
            arm_only += 1

    def _mean(vals: list[float]) -> Optional[float]:
        return (sum(vals) / len(vals)) if vals else None

    regular = [v for v in (_f(r.get("r_multiple")) for r, _ in paired) if v is not None]
    lvl = [v for v in (_f(a.get("r_level")) for _, a in paired) if v is not None]
    cfm = [v for v in (_f(a.get("r_confirm")) for _, a in paired) if v is not None]
    lvl_risk = [v for v in (_f(a.get("r_level_risk")) for _, a in paired) if v is not None]
    cfm_risk = [v for v in (_f(a.get("r_confirm_risk")) for _, a in paired) if v is not None]
    wins = sum(1 for r, _ in paired if str(r.get("status")) == STATUS_TP1)
    arm_wins = sum(1 for _, a in paired if (_f(a.get("r_level")) or 0.0) > 0)
    return {
        "n": len(paired),
        "row_only": row_only,
        "arm_only": arm_only,
        "regular": {"avg_r": _mean(regular), "n_r": len(regular),
                    "win_rate": (wins / len(paired)) if paired else None},
        "sar_level": {"avg_r": _mean(lvl), "n_r": len(lvl),
                      "avg_r_risk": _mean(lvl_risk), "n_r_risk": len(lvl_risk),
                      "win_rate": (arm_wins / len(paired)) if paired else None},
        "sar_confirm": {"avg_r": _mean(cfm), "n_r": len(cfm),
                        "avg_r_risk": _mean(cfm_risk), "n_r_risk": len(cfm_risk)},
    }


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


def attach_strategy(rows: list[dict], strategy_key: str, target_pct: float,
                    fee_pct: float) -> list[dict]:
    """Layer the selected exit strategy onto each row, in keys of its own.

    Never into ``pnl_pct``: that column is the row's own SL/TP1 outcome and a
    what-if written over it would be indistinguishable from a result the engine
    recorded. A row the strategy cannot price carries the reason rather than a
    number, so the table can say *why* a cell is blank instead of leaving the
    reader to assume it is a zero.
    """
    strat = get_strategy(strategy_key, target_pct)
    for row in rows:
        inp = DarkRowInputs.from_row(row)
        res = evaluate(inp, strat, be_pct=target_pct) if inp is not None else None
        if res is None or res.result_pct is None:
            row["strategy_pct"] = None
            row["strategy_gross_pct"] = None
            row["strategy_labels"] = ""
            row["strategy_skipped"] = res.skipped if res is not None else "no_geometry"
            continue
        row["strategy_gross_pct"] = res.result_pct
        row["strategy_pct"] = res.result_pct - fee_pct
        row["strategy_labels"] = "+".join(res.filled_labels)
        row["strategy_skipped"] = ""
    return rows


@router.get("/signals/dark-live")
async def dark_signals_live(
    request: Request,
    setup: str = Query(""),
    gate: str = Query(""),
    status: str = Query(""),
    strategy: str = Query("hold"),
    target_pct: float = Query(1.0),
    fee_pct: float = Query(0.07),
):
    vol = request.app.state.data_volume
    payload = vol.dark_signals()
    provenance = vol.dark_signals_provenance()
    rows = reduce_rows(payload)
    # The second outcome per row. Its own file, because the live SAR ledger is
    # the population an adoption decision reads and every arm in it belongs to a
    # delivered signal — these belong to signals nobody received.
    attach_arms(rows, reduce_arms(vol.dark_sar_arms()))

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

    # Measured on the rows the page is showing, after every filter — a summary
    # over the whole ledger beside a filtered table summarises nothing the
    # reader is looking at (#90/#91).
    sar_panel = summarize_sar(selected)

    # The held-to-stop arm and the exit-method what-ifs, on the same filtered
    # selection for the same reason.
    target = _resolve_target_pct(target_pct)
    fee = _resolve_fee_pct(fee_pct)
    catalog = build_catalog(target)
    if strategy not in catalog:
        strategy = "hold"
    max_profit = summarize_max_profit(selected)
    exits = compare_strategies(selected, target_pct=target, fee_pct=fee)
    coverage = hold_coverage(selected)
    # On the filtered selection like every other panel (#90/#91) — a
    # concentration figure over the whole ledger beside a one-path table would
    # describe symbols the reader has filtered out.
    conc = concentration(selected, fee_pct=fee)
    attach_strategy(selected, strategy, target, fee)

    # Truncate after filtering, never before (#97). The page says when it bit.
    shown = selected[:TABLE_ROW_CAP]

    return request.app.state.templates.TemplateResponse("dark_signals_live.html", {
        "request": request,
        "active": "dark_live",
        "rows": shown,
        "max_profit": max_profit,
        "exits": exits,
        "hold_coverage": coverage,
        "concentration": conc,
        "strategies": [(k, s.label) for k, s in catalog.items()],
        "strategy": strategy,
        "strategy_label": catalog[strategy].label,
        "target_pct": target,
        "fee_pct": fee,
        "matched": len(selected),
        "shown": len(shown),
        "row_cap": TABLE_ROW_CAP,
        # Summaries measured on the full selection, never the truncated table.
        "summary": summarize(selected),
        "live": summarize_open(selected),
        "sar": sar_panel,
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
    # The rest of the ladder. Without it a spreadsheet cannot reprice a leg, and
    # re-deriving one from an R multiple is exactly the invented denominator
    # this repo keeps refusing.
    "tp2", "tp3",
    "sl_distance_pct",
    "emitted_at", "status", "closed_at", "exit_price", "pnl_pct", "r_multiple",
    # Both halves of the excursion. MFE alone cannot answer any question about
    # stop distance — "would a tighter stop have helped" is exactly "did the
    # winners survive it", and only MAE counts that instead of assuming it.
    "mfe_pct", "mae_pct", "bars_seen", "ambiguous_bar",
    "window_coverage", "insufficient_reason",
    # The mark and what it implies. An export is a surface and inherits the
    # page's rules — the SAR export that first showed a 2h-old stop had no
    # column that could have said so, so it read as healthy too.
    "current_price", "unrealized_pct", "unrealized_r",
    "room_to_sl_pct", "room_to_tp1_pct", "level_crossed",
    # …and whether the row beside those numbers is still being measured.
    "last_resolved_at", "resolve_age_sec", "last_bar_ms", "bars_behind",
    "resolve_misses", "resolve_miss_reason", "stalled", "freshness",
    "window_undated_reason", "unverified_reason",
    # The held-to-stop arm: TP1 removed, exits only at the original stop or the
    # horizon. `hold_mfe_pct` is "max PnL before hitting SL" and is the ONLY one
    # of the two peaks not bounded by TP1 — `mfe_pct` above stops where the row
    # did, so the two are not the same measurement and a sheet that pooled them
    # would average a truncated series with a complete one.
    "hold_status", "hold_result_pct", "hold_mark_pct", "hold_exit_price",
    "hold_mfe_pct", "hold_mfe_incl_pct", "hold_mae_pre_peak_pct",
    "hold_peak_bars", "hold_bars", "hold_hit_tp", "hold_ambiguous_bar",
    "hold_window_coverage", "hold_insufficient_reason", "hold_last_bar_ms",
    # …and the selected strategy priced on it. Named columns rather than a
    # bare number, because a what-if in a sheet beside a recorded outcome is
    # precisely where the two get read as one.
    "strategy_pct", "strategy_gross_pct", "strategy_labels", "strategy_skipped",
]


@router.get("/signals/dark-live/export.csv")
async def dark_signals_export(
    request: Request,
    setup: str = Query(""),
    gate: str = Query(""),
    status: str = Query(""),
    strategy: str = Query("hold"),
    target_pct: float = Query(1.0),
    fee_pct: float = Query(0.07),
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
    # The export is a surface and inherits the page's rules — including which
    # strategy the numbers beside each row were priced under.
    target = _resolve_target_pct(target_pct)
    attach_strategy(rows, strategy, target, _resolve_fee_pct(fee_pct))
    data = [[r.get(c) for c in _DARK_COLS] for r in rows]
    return csv_response("dark_signals_live", _DARK_COLS, data)
