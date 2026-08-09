"""Live SAR exit mechanism — the arms as they run, not as they are replayed.

This is the owner-facing surface of the engine's ``src/sar_live_shadow.py``,
added 2026-07-30 because the two SAR surfaces that preceded it are both
hindsight and neither can justify enabling a SAR exit for users:

* ``/signals/sar`` renders the ``@SARBASE``/``@SAREXIT`` replay ledger. On the
  owner's 2026-07-30 export 8 of 19 rows were unresolved — including **all
  four** of the window's winners — so its verdict was an artefact of which
  symbols the resolver's refresh budget reached.
* The Performance tab's dark-signals bake-off replays klines *after* the engine
  has already closed the trade. Splitting its rows by whether the trail actually
  exited collapses the apparent edge from +0.375pp to +0.070pp per trade.

Neither had ever computed a stop while a position was open. These arms do:
each one is stepped forward bar by bar inside the monitor loop, so an open row
carries the stop the mechanism *would have parked right now*.

…provided it is actually still being stepped, which the first cut of this page
took on faith (#108). The engine advanced arms only while their signal was in
the router's active set, and only when the store's candles moved — so a closed
signal, or a mover that rotated out of the scan universe, left an arm RUNNING
with a frozen stop, and this page painted a **live Binance price** next to it
under the words "right now". KORUUSDT SHORT read that way for 2h19m with
``bars_seen: 0``, its parked 5m stop already blown through by 5.45%: the exact
sentence this module's own docstring warns about — *a working price feed is not
evidence the measurement is running* — except the price feed was ours.

So freshness is now per-arm, not per-file. The engine stamps ``last_advance_at``
(when the arm last consumed a bar), ``bars_behind`` (how far its newest closed
bar lags the clock) and ``stalled``; this page leads every open row with them and
counts stepping arms apart from stalled ones. The file's mtime says the *loop* is
alive; only the arm can say the *arm* is.

Two tabs, because they answer different questions
-------------------------------------------------
**Live** (default) — arms currently running. Which leg is governing, where the
stop sits, how far price is from it. This is the operability read: it is the
only view in the system that shows the mechanism working rather than the
mechanism scored.

**Resolved** — arms that closed. This is where the verdict lives. You cannot
judge a mechanism from open trades alone, which is why the resolved tab exists
even though the owner asked for a live feed.

Rules this page inherits, all paid for elsewhere in this repo
-------------------------------------------------------------
* **Every count is measured with every filter applied except its own.** A
  selector applied to its own counts makes each option describe only itself
  (#90/#91).
* **Truncate after filtering, never before.** The row cap is a render bound
  applied in the route, and the page says when it bit (#97).
* **Unrealized never sits in a realized column.** Live rows carry marks; the
  realized columns stay blank until the arm actually closes.
* **Ops ports the engine's math, it does not invent it.** Every field here is
  computed engine-side; this module groups and counts, it does not re-derive a
  fill, an R, or a stop.
* **Copy is part of the measurement.** The page states that both fills are
  shown and what the difference means, because a single "SAR result" would be a
  choice this data does not support making yet.

An arm can be born a replay, and this page has to say so (#836)
---------------------------------------------------------------
The sentence at the top of this page is *"this is not a replay"*, and on
2026-07-31 it was false for one of the rows under it. An arm anchors to the
newest closed bar the store holds at creation; nothing checked that the bar was
**current**. For a promoted mover — REST re-seed only, no WS klines — ACHUSDT's
15m series was ~40h stale, so the arm read SAR-at-entry off a 40h-old bar and
its first advance walked 39.5 hours of history in a single pass, stamping a
fresh ``last_advance_at`` on every bar of it. The row published as a
forward-stepped fill, and every freshness column #108 added read healthy,
because by then it *was* fresh.

The engine now refuses to open such an arm and stamps ``anchor_bars_behind`` /
``first_step_bars`` on every one it does. This page grades on those stamps, runs
its own bars-vs-lifetime check on rows written before them, and **excludes a
replayed arm from every R** — counted, named, never averaged in.

Both denominators, for the same reason as both fills (#836)
------------------------------------------------------------
R divides by the SL distance at entry. When SAR governs, that stop is cancelled
and SAR's own was **wider on 14 of 27 handovers** in the owner's window (mean
1.25x, max 2.81x), so the reported R exaggerates a loss taken at 2.7x the
designed risk. ``r_level_risk`` divides by the risk actually parked. Neither is
"the" R, exactly as neither fill is "the" fill.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Query, Request

from app.data_sources import sar_hold

router = APIRouter()

#: Render bound. Applied in the route AFTER filtering — never inside a reducer.
TABLE_ROW_CAP = 400

GOV_SAR = "SAR"
GOV_GEOMETRY = "GEOMETRY"

STATUS_RUNNING = "RUNNING"
STATUS_CLOSED_SAR_FLIP = "CLOSED_SAR_FLIP"
#: The ATR trail's own terminal state.  A separate name engine-side rather than
#: reuse of ``CLOSED_SAR_FLIP``, and the distinction is real: SAR *reverses* —
#: the level it breaches is a direction change — while a chandelier stop is
#: simply touched.  One word for two events is how a page stops being able to
#: say what happened.
STATUS_CLOSED_TRAIL_STOP = "CLOSED_TRAIL_STOP"
STATUS_CLOSED_SL = "CLOSED_SL"
STATUS_CLOSED_TP1 = "CLOSED_TP1"
STATUS_INSUFFICIENT = "INSUFFICIENT"

RESOLVED_STATUSES = (
    STATUS_CLOSED_SAR_FLIP,
    STATUS_CLOSED_TRAIL_STOP,
    STATUS_CLOSED_SL,
    STATUS_CLOSED_TP1,
    STATUS_INSUFFICIENT,
)

#: Exit reasons, in render order.  Both mechanisms' own exits are listed
#: because a lane only ever produces one of them — showing the other's row as a
#: zero would read as "the mechanism never fired" rather than "this mechanism
#: does not have that exit".  The template renders the ones the lane's rows
#: actually carry.
EXIT_REASONS = ("sar_flip", "trail_stop", "static_sl", "static_tp1")

#: **The mechanism block is READ OUT OF THE LEDGER, never mirrored here.**
#: The engine writes ``mechanism`` into every file (label, parameters, whether
#: the mechanism has a direction of its own), for exactly the reason
#: ``strategy_catalog`` is written there: one writer, one reader.  This fallback
#: exists only so a pre-manifest ledger still renders, and the page **says on
#: screen** when it is used — a silent fallback is a mirror nobody knows is a
#: mirror, which is how ``MEASUREMENT_SUFFIXES`` drifted for a week.
MECHANISM_FALLBACK = {
    "sar": {"key": "sar", "label": "Parabolic SAR", "short": "SAR",
            "has_direction": True, "params": {}},
    "chandelier": {"key": "chandelier", "label": "ATR-trail (Chandelier)",
                   "short": "ATR trail", "has_direction": False, "params": {}},
}

#: Short forms for the table headings.  Not a second label list: the engine's
#: manifest carries the full label and this carries the abbreviation a column
#: header needs, keyed off the same ``key``.  A mechanism absent from here
#: renders under its full label rather than under another mechanism's short
#: name — badged, never renamed.
MECHANISM_SHORT = {"sar": "SAR", "chandelier": "ATR trail"}

#: The two lanes each page can show.  ``delivered`` rows reached a subscriber;
#: ``dark`` rows reached nobody.  Never pooled, and never defaulted into one
#: another — the delivered lane is the evidence for changing what subscribers
#: receive, and inflating it with signals that were never sent is the exact
#: mistake #816 cost a session over.
LANE_DELIVERED = "delivered"
LANE_DARK = "dark"

#: How stale a lane's file may be before its live tab stops claiming to be live.
#: The delivered lanes are stepped inside the monitor loop (a 60s heartbeat);
#: the dark lanes ride the maintenance loop's ~5-minute resolve cycle, so the
#: same age means different things and one constant for both would report a
#: fault on a perfectly healthy dark lane.
LANE_STALE_SEC = {LANE_DELIVERED: 120.0, LANE_DARK: 900.0}

#: How stale the arm file may be before the live tab stops claiming to be live.
#:
#: This is only meaningful because the engine writes on a **60s heartbeat**, not
#: merely when an arm changes. The first cut flushed on change alone, so an open
#: trade with no bar close in the window went untouched and this threshold fired
#: on a perfectly healthy engine — FROZEN at 2.1 minutes with two arms running
#: and `bars_seen: 0` (owner-caught 2026-07-30, the same hour UNAVAILABLE was).
#: 2x the heartbeat: one missed write is jitter, two is the loop stopping.
LIVE_STALE_SEC = 120.0

#: How far behind an arm's newest closed bar may sit before the row is called
#: stalled here. Mirrors the engine's ``SAR_LIVE_SHADOW_STALL_BARS`` default and
#: is only a *fallback*: the engine stamps ``stalled`` itself and that stamp
#: wins, because ops ports the engine's math rather than inventing it. This
#: exists so arms persisted before the engine stamped anything still render
#: honestly instead of reading as fresh.
STALL_BARS = 3.0


#: Bar widths, for the reader-side anchor check on rows the engine never
#: stamped. Mirrors the engine's ``_INTERVAL_SECONDS`` for the timeframes this
#: page runs; a timeframe absent here makes the check refuse rather than guess.
_TF_SECONDS = {
    "1m": 60.0, "3m": 180.0, "5m": 300.0, "15m": 900.0,
    "30m": 1800.0, "1h": 3600.0, "4h": 14400.0,
}

#: Did this arm start life on a bar that was current?
#:
#: ``clean``       the arm stepped forward from a current anchor
#: ``replayed``    the engine's own stamps say it walked history at open
#: ``suspect``     no engine stamp, and the reader-side check says it did
#: ``unverified``  neither could be evaluated — an unknown, not a pass
ANCHOR_CLEAN = "clean"
ANCHOR_REPLAYED = "replayed"
ANCHOR_SUSPECT = "suspect"
ANCHOR_UNVERIFIED = "unverified"

#: Verdicts whose rows are excluded from every R figure. They are still counted,
#: exactly as ``INSUFFICIENT`` is: the row measured *something*, but not this
#: mechanism running forward, and averaging it in would let a replay set the
#: number an adoption decision reads.
ANCHOR_EXCLUDED = (ANCHOR_REPLAYED, ANCHOR_SUSPECT)

#: Slack on the reader-side check. An arm cannot consume more bars than have
#: closed since it opened, plus one for the bar it anchored to and one for
#: rounding. Anything beyond that is history, not stepping.
ANCHOR_BAR_SLACK = 2.0


def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def reduce_arms(payload: Any) -> tuple[list[dict], list[dict]]:
    """Split the engine's ledger into (open, resolved). Pure; no truncation.

    A payload that is missing, unparseable, or of an unexpected shape yields two
    empty lists rather than raising — the page then says the file is unavailable,
    which is a different statement from "no arms", and the template distinguishes
    them.

    **The split is on the SAR arm's own status, NOT on which list the engine
    filed the row under**, and that distinction arrived with the held-to-stop
    arm (engine schema 2). The engine's ``open`` set now means *some arm is
    still owed a verdict* — it holds rows whose SAR arm exited hours ago while
    the held arm walks on to the original stop. Reading ``open`` as "running
    SAR arms" would put a resolved fill in the Running table under a live mark
    and a "Dist. to stop" column, i.e. render a finished trade as an open one on
    the page whose entire identity is knowing the difference.

    Every panel on this page is a statement about the SAR mechanism's verdicts,
    so every one of them keeps measuring exactly the population it did before.
    The held arm's own state is a separate column, never a reason to move a row.
    """
    if not isinstance(payload, dict):
        return [], []
    open_rows = payload.get("open")
    resolved_rows = payload.get("resolved")
    filed_open = [r for r in open_rows if isinstance(r, dict)] if isinstance(open_rows, list) else []
    done = (
        [r for r in resolved_rows if isinstance(r, dict)]
        if isinstance(resolved_rows, list)
        else []
    )
    live: list[dict] = []
    for row in filed_open:
        if str(row.get("status") or "") == "RUNNING":
            live.append(row)
        else:
            # SAR has its verdict; only the held arm is still walking. It belongs
            # with the resolved population for every SAR figure on this page.
            done.append(row)
    live.sort(key=lambda r: _f(r.get("opened_at")) or 0.0, reverse=True)
    done.sort(key=lambda r: _f(r.get("closed_at")) or 0.0, reverse=True)
    return live, done


#: How the coverage block reads when the engine has not written one. Its own
#: state, never a zero: "the engine is older than this panel" and "the lane
#: covered nothing" have completely different next moves, and rendering the
#: second for the first is how a blank gets the wrong caption.
COVERAGE_UNREPORTED = "unreported"


def reduce_coverage(payload: Any) -> dict:
    """What fraction of the delivered book this verdict was able to look at.

    **Every other panel on this page grades arms that EXIST.** The anchor panel
    excludes replayed arms, the status filter excludes ``INSUFFICIENT`` ones,
    and both are careful and correct — and none of them can see a delivered
    signal that never became an arm at all. That was the largest exclusion of
    the lot and it had no counter anywhere in either repo.

    Guest-session audit 2026-08-08, joining this ledger to the closed-signal
    record over the arm window: **124 of 152 delivered trades had an arm
    (81.6%), and the 28 without ran −1.643%/trade at 10.7% win, 67.9% SL_HIT,
    against +0.753% and 43.5% for the armed ones.** So "+0.588%/arm" was
    measured on a winner-enriched subset and presented as the mechanism's
    result on our book. That is #832's own rule — *what fraction of the
    population resolved, and is the unresolved part random?* — applied
    rigorously one step too late.

    Three states, never two. ``fully`` / ``partly`` / ``unarmed``: an arm on 5m
    and none on 15m is neither covered nor missing, and the two timeframes are
    reported as independent experiments everywhere else on this page.
    """
    block = payload.get("coverage") if isinstance(payload, dict) else None
    if not isinstance(block, dict):
        return {"state": COVERAGE_UNREPORTED}
    seen = int(_f(block.get("signals_seen")) or 0)
    full = int(_f(block.get("fully_armed")) or 0)
    part = int(_f(block.get("partly_armed")) or 0)
    none = int(_f(block.get("unarmed")) or 0)
    reasons = block.get("reasons")
    misses = block.get("misses")
    evicted = _f(block.get("evicted"))
    return {
        "state": "reported",
        "signals_seen": seen,
        "fully_armed": full,
        "partly_armed": part,
        "unarmed": none,
        # The headline. Partly-armed counts as NOT fully covered, because the
        # timeframe that is missing is the one whose arm would have been in the
        # verdict — rounding it up to "covered" is the flattering direction.
        "covered_pct": (100.0 * full / seen) if seen else None,
        "uncovered": part + none,
        # Iterate the ENGINE's reasons, never a list kept here. A reason ops has
        # never heard of renders under its raw name rather than being dropped —
        # `MEASUREMENT_SUFFIXES` drifted for a week and the fix for a drifting
        # mirror is not a second mirror.
        "reasons": dict(reasons) if isinstance(reasons, dict) else {},
        "misses": [m for m in misses if isinstance(m, dict)] if isinstance(misses, list) else [],
        # Tri-state, like the edge matrix's eviction count: ``None`` means the
        # engine did not say, never "nothing was evicted".
        "evicted": int(evicted) if evicted is not None else None,
        "cap": int(_f(block.get("cap")) or 0) or None,
    }


#: One sentence per refusal reason. COPY, looked up by the engine's key — the
#: page iterates the payload and falls back to the raw name badged
#: `unclassified`, so tomorrow's reason is visible rather than absent.
COVERAGE_REASON_COPY = {
    "no_series": (
        "the candle store held no usable series for that symbol and timeframe "
        "when the signal appeared, so there was no bar to anchor an arm to. "
        "Overwhelmingly promoted movers, which carry no WS kline subscription "
        "and are re-seeded by REST on a throttle."
    ),
    "stale_anchor": (
        "a series existed but its newest closed bar was itself hours old. The "
        "engine refuses rather than anchoring to it — an arm born on a stale "
        "bar walks history on its first advance and publishes a replay (#836)."
    ),
}


def filter_arms(
    rows: list[dict],
    *,
    timeframe: str = "",
    governor: str = "",
    alignment: str = "",
    status: str = "",
) -> list[dict]:
    """Apply the page's selectors. Each is independent so a caller can omit one
    when counting that selector's own options."""
    out = rows
    if timeframe:
        out = [r for r in out if str(r.get("timeframe") or "") == timeframe]
    if governor:
        out = [r for r in out if str(r.get("governor") or "") == governor]
    if alignment == "agreed":
        out = [r for r in out if r.get("aligned_at_entry") is True]
    elif alignment == "opposed":
        out = [r for r in out if r.get("aligned_at_entry") is False]
    elif alignment == "unknown":
        out = [r for r in out if r.get("aligned_at_entry") is None]
    if status:
        out = [r for r in out if str(r.get("status") or "") == status]
    return out


def is_stalled(row: dict) -> bool:
    """Is this arm's stop older than the bar it claims to be parked on?

    The engine's own ``stalled`` stamp is authoritative when present. The
    ``bars_behind`` fallback covers arms written before the engine stamped
    either — those rows are exactly the ones that were rendering as fresh, so
    treating a missing stamp as "healthy" would preserve the bug for the whole
    population that has it.
    """
    if row.get("stalled") is True:
        return True
    behind = _f(row.get("bars_behind"))
    return behind is not None and behind > STALL_BARS


def mark_freshness(rows: list[dict], *, now: float) -> list[dict]:
    """Stamp how long ago each open arm was last *advanced*, in seconds.

    Not the same question as how long ago the file was written, and that
    difference is the whole of #108: the file was 18s old and correct while the
    arms in it had not moved for two hours. ``last_advance_at`` is the engine's,
    written when a bar was actually consumed; the age is derived here because
    only the reader knows "now".

    ``advance_age_sec`` is None — not 0 — when the engine never stamped it. That
    is an unknown, and the template must not be able to print it as "just now".
    """
    for row in rows:
        if row.get("status") != STATUS_RUNNING:
            continue
        row.setdefault("stalled", None)
        advanced_at = _f(row.get("last_advance_at"))
        row["advance_age_sec"] = (
            None if advanced_at is None else max(0.0, now - advanced_at)
        )
        row["is_stalled"] = is_stalled(row)
    return rows


def count_live_freshness(rows: list[dict]) -> dict:
    """Split the running arms into the ones being stepped and the ones frozen.

    "3 arms running" was true of the owner's 2026-07-30 page and told them
    nothing: two of the three had consumed zero bars since entry. The headline
    now carries both numbers because the second one is the one that invalidates
    the first.
    """
    running = [r for r in rows if r.get("status") == STATUS_RUNNING]
    stalled = [r for r in running if is_stalled(r)]
    never = [r for r in running if not int(_f(r.get("bars_seen")) or 0)]
    return {
        "running": len(running),
        "stalled": len(stalled),
        "stepping": len(running) - len(stalled),
        "no_bars_yet": len(never),
    }


def mark_distance_to_stop(rows: list[dict], prices: dict[str, float]) -> list[dict]:
    """Add the live mark and how far price sits from the parked stop.

    Distance-to-stop is the number the live tab exists for: it is the only thing
    on any SAR surface that says "this mechanism is about to act". Written into
    its own keys — never into a realized column, and never into ``sar_stop``,
    which is the engine's value and must stay the engine's value.

    A negative distance is not a near-miss, it is a **contradiction**: for a
    SHORT the parked stop sits above price, so price above the stop means the
    level was crossed and the arm did not act on it. Flagged as
    ``stop_crossed`` rather than printed as a slightly negative percentage —
    that is the state the owner spotted on KORUUSDT (−5.45%, arm still RUNNING),
    and on the old page it was one unremarkable number in a row of numbers.

    A missing price leaves the row alone: the columns blank, the page renders.
    """
    for row in rows:
        if row.get("status") != STATUS_RUNNING:
            continue
        row.setdefault("stop_crossed", False)
        # Present-but-None, not absent: the column has a defined meaning
        # ("we could not compute it") and the template must not have to
        # distinguish a missing key from an unknown value.
        row.setdefault("stop_distance_pct", None)
        price = _f(prices.get(str(row.get("symbol") or "")))
        entry = _f(row.get("entry"))
        if price is None or price <= 0 or not entry:
            continue
        is_long = str(row.get("side") or "").upper() == "LONG"
        row["current_price"] = price
        move = (price - entry) if is_long else (entry - price)
        row["unrealized_pct"] = move / entry * 100.0
        stop = _f(row.get("sar_stop"))
        if stop is None or stop <= 0 or row.get("governor") != GOV_SAR:
            # The geometry leg's stop is the original SL, which the row already
            # carries; distance-to-SAR is meaningless until the handover.
            continue
        gap = (price - stop) if is_long else (stop - price)
        row["stop_distance_pct"] = gap / price * 100.0
        row["stop_crossed"] = gap < 0
    return rows


def anchor_verdict(row: dict, *, now: float) -> tuple[str, bool, Optional[float]]:
    """Was this arm ever actually stepped forward, or was it born a replay?

    Returns ``(verdict, engine_stamped, replay_bars)``.

    An arm anchors to "the newest closed bar the store holds right now" and
    steps from there. Engine #836: when that bar was *itself* hours old — a
    promoted mover on REST re-seed with no WS klines — the arm read its
    SAR-at-entry off a stale bar and its first advance walked the whole gap in
    one pass, stamping a fresh ``last_advance_at`` on every bar of it. ACHUSDT
    15m consumed **158 bars in ten bars of life** and published as a live fill,
    on the page whose first sentence is "this is not a replay". That sentence is
    this page's, so checking it is this page's job.

    The engine's own stamps win where they exist: ``first_step_bars`` is 1 on a
    genuinely live arm and larger only if it walked history, and
    ``anchor_bars_behind`` says how stale the anchor was. Rows written before
    #836 carry neither — and those are precisely the rows that have the bug — so
    a missing stamp must not read as a pass. They get a **reader-side** check
    instead: ``bars_seen`` against the arm's own lifetime in bars. It is a
    second computation of the engine's quantity, never an overwrite of it, and
    it is reported under its own name so the owner can see which arms were
    verified by the producer and which by the reader.
    """
    first_step = _f(row.get("first_step_bars"))
    behind = _f(row.get("anchor_bars_behind"))
    stamped = "first_step_bars" in row or "anchor_bars_behind" in row
    if first_step is not None and first_step > 1:
        return ANCHOR_REPLAYED, True, first_step
    if behind is not None and behind > STALL_BARS:
        return ANCHOR_REPLAYED, True, behind
    if stamped:
        return ANCHOR_CLEAN, True, first_step

    width = _TF_SECONDS.get(str(row.get("timeframe") or ""))
    opened = _f(row.get("opened_at"))
    ended = _f(row.get("closed_at")) or _f(row.get("last_advance_at")) or now
    bars_seen = _f(row.get("bars_seen"))
    if not width or opened is None or bars_seen is None or ended <= opened:
        # Cannot be evaluated either way. That is an unknown, and an unknown
        # reported as "clean" is how the bug this check exists for survived.
        return ANCHOR_UNVERIFIED, False, None
    lifetime_bars = (ended - opened) / width
    if bars_seen > lifetime_bars + ANCHOR_BAR_SLACK:
        return ANCHOR_SUSPECT, False, bars_seen - lifetime_bars
    return ANCHOR_CLEAN, False, None


def mark_anchor_integrity(rows: list[dict], *, now: float) -> list[dict]:
    """Stamp each row's anchor verdict. Must run before any summary reads it."""
    for row in rows:
        verdict, stamped, replay_bars = anchor_verdict(row, now=now)
        row["anchor_verdict"] = verdict
        row["anchor_engine_stamped"] = stamped
        row["anchor_replay_bars"] = replay_bars
    return rows


def count_anchor_verdicts(rows: list[dict]) -> dict:
    """How many arms in this selection can be trusted to have stepped forward.

    Reported on the page whether or not any row failed. A panel that appears
    only when something is wrong teaches the owner that its absence means
    "checked and fine", when it equally means "the check stopped running".
    """
    counts = {
        key: sum(1 for r in rows if r.get("anchor_verdict") == key)
        for key in (ANCHOR_CLEAN, ANCHOR_REPLAYED, ANCHOR_SUSPECT, ANCHOR_UNVERIFIED)
    }
    counts["total"] = len(rows)
    counts["excluded"] = counts[ANCHOR_REPLAYED] + counts[ANCHOR_SUSPECT]
    counts["engine_stamped"] = sum(
        1 for r in rows if r.get("anchor_engine_stamped") is True
    )
    counts["worst_replay_bars"] = max(
        (_f(r.get("anchor_replay_bars")) or 0.0
         for r in rows if r.get("anchor_verdict") in ANCHOR_EXCLUDED),
        default=None,
    )
    return counts


def _risk_denominator(row: dict) -> Optional[float]:
    """The risk this arm actually parked, in percent — or None.

    R on this page divides by the SL distance at entry, which keeps it
    comparable with the edge matrix and Track record. But when SAR governs, that
    stop is **cancelled**, and on the owner's 2026-07-31 window SAR's own stop
    was wider than it on 14 of 27 handovers (mean 1.25x, max 2.81x). Dividing a
    loss taken at 2.7x the designed risk by the designed risk reports −1.90R for
    what was −0.71R of the capital actually exposed.

    Neither denominator is wrong and this page publishes both, for the same
    reason it publishes both fills: where two readings are defensible, choosing
    one before the gap is known is choosing the answer.

    ``handover_risk_pct`` is the entry-to-SAR-stop distance stamped at handover.
    An arm that never handed over ran on its original stop, so its designed SL
    *is* the risk it took — the two R's coincide and that is a fact, not a
    fallback.
    """
    handed_over = row.get("handover_at") is not None
    risk = _f(row.get("handover_risk_pct"))
    if handed_over and risk is not None and risk > 0:
        return risk
    sl = _f(row.get("sl_distance_pct"))
    return sl if sl and sl > 0 else None


def mark_risk_adjusted_r(rows: list[dict]) -> list[dict]:
    """Add R measured against the risk actually parked, beside the reported R.

    Written into its own keys. ``r_level`` / ``r_confirm`` are the engine's and
    stay the engine's — ops ports the engine's math, and a denominator swap that
    overwrote the original would make the two repos disagree about a field name
    they share.
    """
    for row in rows:
        denom = _risk_denominator(row)
        for src, dst in (("pnl_level_pct", "r_level_risk"),
                         ("pnl_confirm_pct", "r_confirm_risk")):
            pnl = _f(row.get(src))
            row[dst] = None if (denom is None or pnl is None) else pnl / denom
        row["risk_denominator_pct"] = denom
    return rows


def summarize_resolved(rows: list[dict]) -> dict:
    """The verdict, on both fills, refusing rather than averaging over blanks.

    Both fills are reported side by side and neither is called *the* result.
    ``fill_level`` is the stop being touched intrabar; ``fill_confirm`` is
    waiting for the bar to close and exiting at market. The gap between them is
    the cost of confirmation, and choosing one before that cost is known would
    be picking the answer.

    **PnL % is the primary measure and R is subordinate to it** (owner,
    2026-08-02: *"that R is purely confusing — 3% SL still only 1R"*).

    ``signal_dispatch`` sizes every position at a **fixed $500 notional**
    (``raw_qty = notional / entry_price``) — the stop distance appears nowhere in
    that formula. R divides each outcome by its own stop, which only equalises
    trades when size is scaled inversely to the stop. It is not. So a trade that
    loses 0.80% and one that loses 6.14% both read exactly −1.00R while costing
    $4.00 and $30.70 of the same $500.

    That is not a presentation quibble; it changes conclusions. On the
    2026-08-02 arms R reads **+0.035R** and PnL reads **−0.041%** — *the sign
    flips* — because the winners sit on tighter stops (mean 3.25%) than the
    losers (3.46%), so dividing by the stop inflates the wins and shrinks the
    losses.

    R is still computed and still shown, because the Strategy Lab and the edge
    matrix speak R and a reader moving between them needs the bridge. It is
    never the headline here.

    Rows with no usable R (no SL distance at entry) are counted in ``n`` and
    excluded from every R figure, with the shortfall stated — scoring them 0R
    would drag the averages toward zero and make missing data read as mediocre
    performance. PnL % has no such shortfall: it needs no denominator.
    """
    closed = [r for r in rows if r.get("status") in RESOLVED_STATUSES]
    scored = [r for r in closed if r.get("status") != STATUS_INSUFFICIENT]
    # An arm that walked history at open measured a replay, not this mechanism
    # running forward (#836). Counted, excluded from every R — the same
    # treatment INSUFFICIENT already gets, and for the same reason: it is "we
    # cannot say", not a result.
    replayed = [r for r in scored if r.get("anchor_verdict") in ANCHOR_EXCLUDED]
    measurable = [r for r in scored if r.get("anchor_verdict") not in ANCHOR_EXCLUDED]
    r_level = [_f(r.get("r_level")) for r in measurable]
    r_confirm = [_f(r.get("r_confirm")) for r in measurable]
    r_level = [x for x in r_level if x is not None]
    r_confirm = [x for x in r_confirm if x is not None]
    r_level_risk = [_f(r.get("r_level_risk")) for r in measurable]
    r_confirm_risk = [_f(r.get("r_confirm_risk")) for r in measurable]
    r_level_risk = [x for x in r_level_risk if x is not None]
    r_confirm_risk = [x for x in r_confirm_risk if x is not None]
    # The money. Needs no denominator, so its population is every measurable
    # arm rather than the subset carrying an entry-risk stamp.
    pnl_level = [_f(r.get("pnl_level_pct")) for r in measurable]
    pnl_confirm = [_f(r.get("pnl_confirm_pct")) for r in measurable]
    pnl_level = [x for x in pnl_level if x is not None]
    pnl_confirm = [x for x in pnl_confirm if x is not None]
    slip = [_f(r.get("confirm_slippage_pct")) for r in measurable]
    slip = [x for x in slip if x is not None]
    # Win rate counts on the MONEY, not on R: they agree per row (a positive
    # denominator cannot change a sign) but the populations differ, because R
    # silently drops rows with no entry-risk stamp and PnL keeps them.
    wins_level = sum(1 for x in pnl_level if x > 0)
    wins_confirm = sum(1 for x in pnl_confirm if x > 0)

    def _avg(xs: list[float]) -> Optional[float]:
        return (sum(xs) / len(xs)) if xs else None

    return {
        "n": len(closed),
        "measurable": len(measurable),
        "insufficient": sum(
            1 for r in closed if r.get("status") == STATUS_INSUFFICIENT
        ),
        "replayed": len(replayed),
        "unverified": sum(
            1 for r in measurable if r.get("anchor_verdict") == ANCHOR_UNVERIFIED
        ),
        "no_r": len(measurable) - len(r_level),
        # Primary. Listed first because it is what the owner reads to decide.
        "n_pnl": len(pnl_level),
        "avg_pnl_level_pct": _avg(pnl_level),
        "avg_pnl_confirm_pct": _avg(pnl_confirm),
        "total_pnl_level_pct": sum(pnl_level) if pnl_level else None,
        "total_pnl_confirm_pct": sum(pnl_confirm) if pnl_confirm else None,
        "n_r": len(r_level),
        "n_r_risk": len(r_level_risk),
        "avg_r_level": _avg(r_level),
        "avg_r_confirm": _avg(r_confirm),
        # Same fills, divided by the risk the arm actually parked rather than
        # the SL it cancelled. Win rates are deliberately not repeated: a
        # positive denominator cannot change a sign, so they are identical and
        # printing them twice would imply two populations.
        "avg_r_level_risk": _avg(r_level_risk),
        "avg_r_confirm_risk": _avg(r_confirm_risk),
        "win_rate_level": (wins_level / len(pnl_level)) if pnl_level else None,
        "win_rate_confirm": (wins_confirm / len(pnl_confirm)) if pnl_confirm else None,
        "avg_confirm_slippage_pct": _avg(slip),
        "by_exit": {
            reason: sum(1 for r in measurable if r.get("exit_reason") == reason)
            for reason in EXIT_REASONS
        },
        "handovers": sum(1 for r in measurable if r.get("handover_at")),
        "ambiguous": sum(1 for r in measurable if r.get("ambiguous_bar")),
    }


def summarize_by_risk(rows: list[dict]) -> dict:
    """Split the verdict by whether SAR's stop was wider than the designed SL.

    When SAR agrees at entry it governs and the signal's own stop is never used,
    so the arm can end up carrying **more** risk than the evaluator sized the
    trade for — the first two live arms did exactly that on one signal (MUUUSDT
    SHORT: 5m at 1.60%, 15m at 3.77%, against a designed 3.00%).

    That matters for reading the R's, not just for safety. R divides by the SL
    distance at entry, so a wider-stop arm mechanically produces larger losses
    per stop-out, and a pooled average would blend "the exit timed badly" with
    "the exit risked more". These are different findings and the page must not
    hand the owner one wearing the other's name.

    ``unknown`` is its own bucket rather than folded into either: an arm that
    never handed over has no SAR risk to compare, which is not the same as
    having had a narrow one.
    """
    buckets: dict[str, list[dict]] = {"wider": [], "inside": [], "unknown": []}
    for r in rows:
        flag = r.get("handover_wider_than_sl")
        key = "wider" if flag is True else "inside" if flag is False else "unknown"
        buckets[key].append(r)
    out = {k: summarize_resolved(v) for k, v in buckets.items()}
    for key, rows_ in buckets.items():
        risks = [_f(r.get("handover_risk_pct")) for r in rows_]
        risks = [x for x in risks if x is not None]
        out[key]["avg_risk_pct"] = (sum(risks) / len(risks)) if risks else None
        out[key]["bucket"] = key
    return out


def summarize_by_timeframe(rows: list[dict]) -> list[dict]:
    """The 5m-vs-15m question, answered on the same population.

    Reported per timeframe rather than pooled because they are different
    experiments on the same signals — pooling them would let the mix of
    timeframes move the headline instead of the mechanism.
    """
    out = []
    for tf in sorted({str(r.get("timeframe") or "") for r in rows if r.get("timeframe")}):
        summary = summarize_resolved([r for r in rows if r.get("timeframe") == tf])
        summary["timeframe"] = tf
        out.append(summary)
    return out


def reduce_mechanism(payload: Any, mechanism: str) -> dict:
    """The mechanism block, from the LEDGER, with the fallback named on screen.

    The engine writes ``mechanism`` into every file — label, parameters, and
    whether the mechanism carries a direction of its own — so this page never
    keeps a second copy of what a mechanism is.  ``source`` is the honest half:
    a reader has to be able to tell "the engine told me this" from "this build
    guessed", because a silent fallback is a mirror nobody knows is a mirror.
    """
    fallback = dict(MECHANISM_FALLBACK.get(str(mechanism), {}))
    block = payload.get("mechanism") if isinstance(payload, dict) else None
    if isinstance(block, dict) and block.get("key"):
        out = dict(block)
        out["source"] = "engine"
    else:
        out = fallback or {"key": str(mechanism), "label": str(mechanism),
                           "has_direction": False, "params": {}}
        out = dict(out)
        out["source"] = "fallback"
    # The short form a column heading needs.  A mechanism this build has never
    # heard of keeps its full label rather than borrowing another's short name —
    # badged, never renamed.
    out.setdefault("short", MECHANISM_SHORT.get(str(out.get("key")), out.get("label")))
    if not out.get("short"):
        out["short"] = out.get("label") or str(mechanism)
    return out


def reduce_live_state(
    provenance: dict, live_rows: list[dict], lane: str = LANE_DELIVERED
) -> dict:
    """Is this tab actually live? — read before any number on it.

    A frozen arm file renders as a page full of open trades with plausible
    stops beside accurate live prices, which is precisely how the replay
    ledger looked healthy on 2026-07-29 while its newest resolution was 11.6h
    old. So the file's own age is stated first, separately from the prices.
    """
    # Age comes from the file's mtime (data_volume), not a clock read here: the
    # question is how long ago the engine wrote, and only the file knows that.
    age = provenance.get("age_sec")
    # The dark lanes ride the maintenance loop's ~5-minute resolve cycle rather
    # than the monitor loop's 60s heartbeat, so one staleness bound for both
    # would report a fault on a perfectly healthy dark lane.
    stale_sec = LANE_STALE_SEC.get(str(lane), LIVE_STALE_SEC)
    if not provenance.get("exists"):
        return {
            "state": "unavailable",
            "detail": (
                f"{provenance.get('file')} is not on the data volume. The engine "
                "writes it on a 60s heartbeat from the monitor loop — even with "
                "no signals open — so it should appear within a minute of an "
                "engine start. Past that, check the lane's enable flag "
                "(SAR_LIVE_SHADOW_ENABLED / ATR_TRAIL_LIVE_ENABLED) and the "
                "engine container, not this page."
            ),
        }
    if provenance.get("newer_version"):
        return {
            "state": "orphan",
            "detail": (
                f"The engine has moved to {provenance.get('newer_file')}. Every "
                "number here describes a population it has discarded — bump "
                "the lane's version constant in data_volume.py."
            ),
        }
    if age is not None and age > stale_sec:
        return {
            "state": "frozen",
            "detail": (
                f"The arm file has not been written for {age / 60.0:.1f} minutes, "
                f"against a {stale_sec / 60.0:.0f}-minute bound for this lane. "
                "The engine writes it on a heartbeat whether or not an arm "
                "changed, so this means the loop is not running — not "
                "that the market is quiet. Live prices below are still real: a "
                "working price feed is not evidence the measurement is running."
            ),
        }
    if not live_rows:
        return {
            "state": "idle",
            "detail": (
                "The file is current and no signals are open, so there is "
                "nothing to run. This is the quiet case, not a fault."
            ),
        }
    # A current file proves the monitor loop is writing. It does not prove any
    # individual arm is advancing, and those are different claims — the whole of
    # #108. So the headline is graded on the arms, not the file.
    counts = count_live_freshness(live_rows)
    if counts["stalled"] and counts["stalled"] >= counts["running"]:
        return {
            "state": "stalled",
            "counts": counts,
            "detail": (
                f"The loop is writing, but all {counts['running']} running arms "
                "are stalled: their newest closed bar is bars behind, so the "
                "parked stops below were computed then and have not moved since. "
                "Prices are live and the stops are not — do not read a "
                "distance-to-stop on this page while that is true. A symbol that "
                "rotated out of the scan universe stops receiving klines; the "
                "engine retires such an arm as INSUFFICIENT once the gap is "
                "unrecoverable rather than scoring it."
            ),
        }
    if counts["stalled"]:
        return {
            "state": "partial",
            "counts": counts,
            "detail": (
                f"{counts['stepping']} of {counts['running']} arms are advancing; "
                f"{counts['stalled']} are stalled and carry a stop from an older "
                "bar. Stalled rows are badged in the table — their "
                "distance-to-stop is not a live number."
            ),
        }
    return {
        "state": "live",
        "counts": counts,
        "detail": (
            f"{counts['running']} arms running and advancing, stepped inside the "
            "monitor loop."
        ),
    }


def _view(
    request: Request,
    tab: str,
    mechanism: str = "sar",
    lane: str = LANE_DELIVERED,
    **selectors: str,
) -> dict:
    vol = request.app.state.data_volume
    dark = str(lane) == LANE_DARK
    payload = vol.trail_arms(mechanism, dark=dark)
    provenance = vol.trail_arms_provenance(mechanism, dark=dark)
    live_rows, resolved_rows = reduce_arms(payload)
    return {
        "payload": payload,
        "provenance": provenance,
        "mechanism": reduce_mechanism(payload, mechanism),
        "lane": LANE_DARK if dark else LANE_DELIVERED,
        "strategy_catalog": (
            payload.get("strategy_catalog") if isinstance(payload, dict) else None
        ),
        "live_rows": live_rows,
        "resolved_rows": resolved_rows,
        # Coverage is a fact about the whole book and is deliberately NOT
        # filtered with the table: a selector narrows which arms you are
        # looking at and cannot change how much of the delivered book the lane
        # was able to arm in the first place. The panel says so on screen, so
        # it is not read as a summary of the current selection (#90 in the
        # other direction — the exception, stated, rather than a silent one).
        "coverage": reduce_coverage(payload),
        "coverage_copy": COVERAGE_REASON_COPY,
        "tab": tab,
        "selectors": selectors,
    }


#: URL per mechanism.  Two paths and ONE handler: the page is the same
#: measurement with a different level function, and two handlers would be two
#: places for a panel to be added to — which is how one surface silently stops
#: showing what the other does.
MECHANISM_PATHS = {"sar": "/signals/sar-live", "chandelier": "/signals/atr-live"}


async def _render(
    request: Request,
    *,
    mechanism: str,
    tab: str,
    lane: str,
    timeframe: str,
    governor: str,
    alignment: str,
    status: str,
    fee: float,
):
    templates = request.app.state.templates
    lane = LANE_DARK if str(lane) == LANE_DARK else LANE_DELIVERED
    ctx = _view(request, tab, mechanism=mechanism, lane=lane)
    live_rows, resolved_rows = ctx["live_rows"], ctx["resolved_rows"]
    base = live_rows if tab != "resolved" else resolved_rows

    # Every count is measured with every filter applied EXCEPT its own, so a
    # selector never makes each of its options describe only itself (#90/#91).
    scoped_tf = filter_arms(base, governor=governor, alignment=alignment, status=status)
    scoped_gov = filter_arms(base, timeframe=timeframe, alignment=alignment, status=status)
    scoped_align = filter_arms(base, timeframe=timeframe, governor=governor, status=status)
    selected = filter_arms(
        base, timeframe=timeframe, governor=governor, alignment=alignment, status=status
    )

    try:
        prices = await request.app.state.binance_klines.fetch_all_prices()
    except Exception:
        prices = {}

    # Stamped on the FULL selection, before any summary reads it — the summaries
    # below are measured on `selected`, not on the truncated table, and a row
    # excluded from the verdict has to be excluded wherever it is counted.
    now = time.time()
    mark_anchor_integrity(selected, now=now)
    mark_risk_adjusted_r(selected)

    # Truncate here, after every filter — never in a reducer. The cap is a
    # render bound and the template says when it bit.
    rows = selected[:TABLE_ROW_CAP]
    mark_freshness(rows, now=now)
    mark_distance_to_stop(rows, prices)

    return templates.TemplateResponse("sar_live.html", {
        "request": request,
        # The nav pill must match the page.  `/signals/price-action` and
        # `/signals/structural-veto` both shipped setting the *Feed* tab's key,
        # so the Feed pill lit up on a page that was not the feed — that is what
        # looked wrong on screen before anyone read a label.
        "active": "sar_live" if mechanism == "sar" else "atr_live",
        "mech": ctx["mechanism"],
        "lane": lane,
        "page_path": MECHANISM_PATHS.get(mechanism, "/signals/sar-live"),
        # The OTHER mechanism, so the comparison is one click away rather than a
        # URL the reader has to know.  The whole point of the second lane is
        # "which one actually suits this setup".
        "other_path": MECHANISM_PATHS.get(
            "chandelier" if mechanism == "sar" else "sar"
        ),
        "other_label": MECHANISM_FALLBACK[
            "chandelier" if mechanism == "sar" else "sar"
        ]["label"],
        "tab": tab,
        "rows": rows,
        "matched": len(selected),
        "shown": len(rows),
        "row_cap": TABLE_ROW_CAP,
        "n_live": len(live_rows),
        "n_resolved": len(resolved_rows),
        # Summaries measured on the full selection, never the truncated table.
        "summary": summarize_resolved(selected),
        "by_timeframe": summarize_by_timeframe(selected),
        "by_risk": summarize_by_risk(selected),
        # Both new panels are measured on `selected` like everything else on
        # this page (#90) — a summary computed over the whole ledger above a
        # filtered table is not a summary of anything the reader is looking at.
        "peak": sar_hold.reduce_peak(selected),
        "strategies": sar_hold.reduce_strategies(
            selected, fee_pct=fee, catalog=ctx.get("strategy_catalog")
        ),
        "fee_pct": fee,
        "n_wider": sum(
            1 for r in scoped_align if r.get("handover_wider_than_sl") is True
        ),
        "anchor": count_anchor_verdicts(selected),
        # Unfiltered by design — see `reduce_coverage` and the panel's own copy.
        "coverage": ctx["coverage"],
        "coverage_copy": ctx["coverage_copy"],
        "timeframes": sorted({r["timeframe"] for r in base if r.get("timeframe")}),
        "governors": sorted({r["governor"] for r in base if r.get("governor")}),
        "statuses": sorted({r["status"] for r in base if r.get("status")}),
        "n_tf": {
            tf: sum(1 for r in scoped_tf if r.get("timeframe") == tf)
            for tf in sorted({r["timeframe"] for r in base if r.get("timeframe")})
        },
        "n_gov": {
            g: sum(1 for r in scoped_gov if r.get("governor") == g)
            for g in sorted({r["governor"] for r in base if r.get("governor")})
        },
        "n_agreed": sum(1 for r in scoped_align if r.get("aligned_at_entry") is True),
        "n_opposed": sum(1 for r in scoped_align if r.get("aligned_at_entry") is False),
        "filter_timeframe": timeframe,
        "filter_governor": governor,
        "filter_alignment": alignment,
        "filter_status": status,
        "provenance": ctx["provenance"],
        "live_state": reduce_live_state(ctx["provenance"], live_rows, lane),
    })


@router.get("/signals/sar-live")
async def sar_live(
    request: Request,
    tab: str = Query("live"),
    lane: str = Query(LANE_DELIVERED),
    timeframe: str = Query(""),
    governor: str = Query(""),
    alignment: str = Query(""),
    status: str = Query(""),
    fee: float = Query(sar_hold.DEFAULT_FEE_PCT),
):
    return await _render(
        request, mechanism="sar", tab=tab, lane=lane, timeframe=timeframe,
        governor=governor, alignment=alignment, status=status, fee=fee,
    )


@router.get("/signals/atr-live")
async def atr_live(
    request: Request,
    tab: str = Query("live"),
    lane: str = Query(LANE_DELIVERED),
    timeframe: str = Query(""),
    governor: str = Query(""),
    alignment: str = Query(""),
    status: str = Query(""),
    fee: float = Query(sar_hold.DEFAULT_FEE_PCT),
):
    """The ATR trail, measured exactly as SAR is — same arms, same guards.

    Owner, 2026-08-09: *"exactly implement same for ATR-trail (Chandelier)"*.
    The engine runs one arm engine with a mechanism parameter, so this page is
    the SAR page with a different level function behind it; a second template
    would be a second place for a panel to be added to.
    """
    return await _render(
        request, mechanism="chandelier", tab=tab, lane=lane, timeframe=timeframe,
        governor=governor, alignment=alignment, status=status, fee=fee,
    )


_ARM_COLS = [
    "arm_id", "signal_id", "symbol", "side", "setup_class", "timeframe",
    "entry", "stop_loss", "tp1", "sl_distance_pct",
    "aligned_at_entry", "governor", "handover_at", "sar_stop", "sar_up",
    "status", "exit_reason", "bars_seen",
    "fill_level", "fill_confirm", "pnl_level_pct", "pnl_confirm_pct",
    "r_level", "r_confirm", "confirm_slippage_pct",
    # Both halves of the excursion — see the dark export for why MFE alone
    # cannot settle a stop-distance question.
    "mfe_pct", "mae_pct", "current_price", "unrealized_pct", "stop_distance_pct",
    "ambiguous_bar",
    "sar_risk_pct", "max_sar_risk_pct", "handover_risk_pct",
    "handover_wider_than_sl",
    # R against the risk actually parked, beside the engine's R against the SL
    # the trade was sized for. Both, never one (#836).
    "risk_denominator_pct", "r_level_risk", "r_confirm_risk",
    # Anchor integrity — whether the arm stepped forward or walked history at
    # open. An export is a surface and inherits the page's rules: the CSV that
    # first showed the 158-bar arm had no column that could have said so.
    "anchor_bars_behind", "first_step_bars",
    "anchor_verdict", "anchor_engine_stamped", "anchor_replay_bars",
    # Freshness of the measurement (#108). The owner's 2026-07-30 export had no
    # column that could have shown a 2h19m-old stop, so the CSV read as healthy
    # too — an export is a surface, and it inherits the same rule.
    "last_advance_at", "advance_age_sec", "bars_behind", "stalled",
    "stall_reason", "is_stalled", "stop_crossed",
]


async def _export(
    request: Request,
    *,
    mechanism: str,
    tab: str,
    lane: str,
    timeframe: str,
    governor: str,
    alignment: str,
    status: str,
):
    """The current selection as CSV — uncapped, unlike the rendered table.

    ``mechanism`` and ``lane`` ride on every row.  A spreadsheet is exactly
    where two populations get averaged into one, and a download that cannot say
    which mechanism or which delivery it describes is the one artifact this
    lane's whole file split exists to prevent.
    """
    from app.reports import csv_response

    lane = LANE_DARK if str(lane) == LANE_DARK else LANE_DELIVERED
    ctx = _view(request, tab, mechanism=mechanism, lane=lane)
    base = ctx["live_rows"] if tab != "resolved" else ctx["resolved_rows"]
    rows = filter_arms(
        base, timeframe=timeframe, governor=governor, alignment=alignment, status=status
    )
    try:
        prices = await request.app.state.binance_klines.fetch_all_prices()
    except Exception:
        prices = {}
    now = time.time()
    mark_anchor_integrity(rows, now=now)
    mark_risk_adjusted_r(rows)
    mark_freshness(rows, now=now)
    mark_distance_to_stop(rows, prices)
    for r in rows:
        r.setdefault("mechanism", ctx["mechanism"].get("key"))
        r["lane"] = lane
    cols = _ARM_COLS + ["mechanism", "lane"]
    data = [[r.get(c) for c in cols] for r in rows]
    name = f"{ctx['mechanism'].get('key', mechanism)}_{lane}_arms"
    return csv_response(name, cols, data)


@router.get("/signals/sar-live/export.csv")
async def sar_live_export_csv(
    request: Request,
    tab: str = Query("live"),
    lane: str = Query(LANE_DELIVERED),
    timeframe: str = Query(""),
    governor: str = Query(""),
    alignment: str = Query(""),
    status: str = Query(""),
):
    return await _export(
        request, mechanism="sar", tab=tab, lane=lane, timeframe=timeframe,
        governor=governor, alignment=alignment, status=status,
    )


@router.get("/signals/atr-live/export.csv")
async def atr_live_export_csv(
    request: Request,
    tab: str = Query("live"),
    lane: str = Query(LANE_DELIVERED),
    timeframe: str = Query(""),
    governor: str = Query(""),
    alignment: str = Query(""),
    status: str = Query(""),
):
    return await _export(
        request, mechanism="chandelier", tab=tab, lane=lane, timeframe=timeframe,
        governor=governor, alignment=alignment, status=status,
    )
