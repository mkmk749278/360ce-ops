"""Reducers for the dark → live promotion lane, shared by both its surfaces.

Two pages read this: ``/control/promotions``, where the owner builds a rule,
and ``/signals/dark-live``, where he reads what the rules have done. They are
the same numbers, so they are computed once — a control panel that scored a
condition differently from the page the evidence came from would let a rule be
justified by one figure and judged by another.

Four separations this module refuses to collapse, each already paid for
elsewhere in this repo:

* **Promoted rows and dark rows are never pooled.** A promoted row went to the
  router and a dark row did not, so a pooled average describes a feed that
  never existed. Every summary here is keyed by delivery.
* **Enqueued is not delivered.** ``promoted_enqueued`` means the queue took it;
  the router's second layer then drops most of what it dequeues. The split is
  published, and the delivered count is never rounded up from the promoted one
  — the ops page once read "Emitted to live (98)" for a window with 3 real
  signals, and this is the mechanism that deliberately puts more rows into that
  queue.
* **A row written before the mechanism is `unstamped`**, its own bucket. Those
  rows are the entire evidence base for the first promotion, so they are
  counted and never folded into "not promoted" — absent is not `False`.
* **`EXPIRED` is flat, not a loss.** Wins, losses and flats are three buckets;
  ``losses = n - wins`` swept 80 flat rows into the loss column on
  `/signals/price-action` and moved a win rate by five points.

And the panel this module exists for — ``condition_evidence`` — carries the
rule that decides whether a promotion is real: **read n and the campaign count
before the average, and know how many cells were drawn.** The top row of a long
table beats a coin flip by construction, and `FAILED_AUCTION_RECLAIM` (+0.846R
on three rows, promotion requested within the day) is what that costs.
"""
from __future__ import annotations

import math
import random
from typing import Any, Iterable, Optional

#: Delivery states the engine stamps. Mirrored as *strings* rather than
#: imported, because ops cannot import the engine — but the page never
#: enumerates them: it renders whatever the rows carry and looks the label up,
#: so a state ops has not heard of shows under its raw name instead of being
#: dropped. (`LANE_REFUSAL_COPY`'s rule, one lane over.)
DELIVERY_DARK = "dark"
DELIVERY_ENQUEUED = "promoted_enqueued"
DELIVERY_DELIVERED = "promoted_delivered"
DELIVERY_DROPPED = "promoted_dropped"

#: A row from before schema 2. Not a delivery state — the absence of one.
DELIVERY_UNSTAMPED = "unstamped"

DELIVERY_COPY: dict[str, str] = {
    DELIVERY_DARK: "Diverted at the enqueue site. Reached nobody.",
    DELIVERY_ENQUEUED: (
        "A rule matched and the queue accepted it — the router has not ruled "
        "yet, so this is not a delivery."
    ),
    DELIVERY_DELIVERED: (
        "The router confirmed delivery. The only state that means a subscriber "
        "saw it."
    ),
    DELIVERY_DROPPED: (
        "Promoted and enqueued, then dropped by the router's second layer."
    ),
    DELIVERY_UNSTAMPED: (
        "Written before the promotion mechanism shipped. Owed nothing, and the "
        "evidence the first rules were built from."
    ),
}

#: Terminal statuses that carry a usable PnL. `INSUFFICIENT` is deliberately
#: absent — it is the absence of a measurement, not a zero.
_CLOSED = ("CLOSED_TP1", "CLOSED_SL", "EXPIRED")


def _f(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        out = float(value)
        return None if math.isnan(out) else out
    except (TypeError, ValueError):
        return None


def delivery_of(row: dict) -> str:
    """This row's delivery state, with absent read as `unstamped`.

    ``.get`` returning ``None`` is the whole point: a row written before the
    mechanism has no block at all, and calling that "not promoted" would fold
    the population that justified every rule into the population the rules
    declined to act on.
    """
    raw = row.get("delivery")
    if raw is None:
        return DELIVERY_UNSTAMPED
    return str(raw)


#: Prefix the engine gives every promoted state. Matched as a prefix rather
#: than against the three names above, and the difference is not cosmetic:
#: rendering the panel with a fourth state in the ledger showed it silently
#: missing from the promoted count — ops iterating its own list of known states,
#: which is the defect this repo has now paid for under four names.
#:
#: A prefix is still an assumption, but it fails in the safe direction: an
#: unrecognised ``promoted_*`` is counted as promoted and named as unclassified,
#: rather than disappearing from the one figure that says how many rows a rule
#: put on the queue. A state that does not carry the prefix cannot be silently
#: pooled either — it surfaces under `unclassified` in the split.
PROMOTED_PREFIX = "promoted_"


def is_promoted(row: dict) -> bool:
    return delivery_of(row).startswith(PROMOTED_PREFIX)


def session_of(row: dict) -> str:
    """The session component of the engine's context key.

    ``session/phase/volatility/rotation``. Parsed rather than stored twice —
    `market_context` owns the key's shape and a second copy here is one more
    mirror to drift.
    """
    ctx = str(row.get("context_key") or "")
    return ctx.split("/")[0].strip() if ctx else ""


def _pnl_rows(rows: Iterable[dict]) -> list[float]:
    out = []
    for row in rows:
        if str(row.get("status") or "") not in _CLOSED:
            continue
        val = _f(row.get("pnl_pct"))
        if val is not None:
            out.append(val)
    return out


def _campaigns(rows: Iterable[dict]) -> int:
    """Distinct symbol·side over the whole window.

    The concentration key that works on this lane. A dark candidate is diverted
    before the router, so no per-symbol cooldown applies to it and its repeats
    are spread across hours rather than bunched — a time-clustered "run" key
    reads 1.10 rows/group here and calls concentration a non-problem over a book
    whose sign is ten campaigns.
    """
    return len({
        (str(r.get("symbol") or "?"), str(r.get("side") or "?")) for r in rows
    })


def bootstrap_ci(
    values: list[float], *, iterations: int = 4000, seed: int = 11
) -> tuple[Optional[float], Optional[float]]:
    """95% CI of the mean. Published instead of the mean alone, always.

    "Two winners are not a promotion" is the rule this implements: a cell's
    mean says nothing until its interval is beside it, and the interval is what
    made `FAILED_AUCTION_RECLAIM`'s +0.846R legible as [−1.00, +2.00].

    Seeded, so the same rows produce the same interval on every reload — a
    figure that moves when the owner refreshes the page is one he stops
    trusting, and the noise would be entirely ours.
    """
    if len(values) < 2:
        return (None, None)
    rng = random.Random(seed)
    n = len(values)
    means = sorted(
        sum(rng.choices(values, k=n)) / n for _ in range(iterations)
    )
    return (means[int(0.025 * iterations)], means[int(0.975 * iterations)])


def summarize(rows: list[dict], *, fee_pct: float = 0.0) -> dict:
    """PnL-led stats over one population. Three outcome buckets, never two."""
    closed = [r for r in rows if str(r.get("status") or "") in _CLOSED]
    pnl = _pnl_rows(closed)
    wins = [p for p in pnl if p > 0]
    losses = [p for p in pnl if p < 0]
    flats = [p for p in pnl if p == 0]
    total = sum(pnl) if pnl else None
    avg = (total / len(pnl)) if pnl else None
    lo, hi = bootstrap_ci(pnl)
    # A flat row still pays its round trip, so it stays in the money figures.
    net_avg = None if avg is None else avg - fee_pct
    return {
        "n_rows": len(rows),
        "n_closed": len(closed),
        "n_scored": len(pnl),
        "wins": len(wins),
        "losses": len(losses),
        "flats": len(flats),
        # Both denominators, neither called "the" win rate: a flat row resolved
        # and is not a loss, but it also never reached a level.
        "win_rate_closed": (100.0 * len(wins) / len(pnl)) if pnl else None,
        "win_rate_levelled": (
            100.0 * len(wins) / (len(wins) + len(losses))
            if (wins or losses) else None
        ),
        "total_pct": total,
        "avg_pct": avg,
        "net_avg_pct": net_avg,
        "ci_low": lo,
        "ci_high": hi,
        "campaigns": _campaigns(closed),
        "rows_per_campaign": (
            len(closed) / _campaigns(closed) if closed and _campaigns(closed) else None
        ),
        "fee_pct": fee_pct,
    }


def delivery_split(rows: list[dict]) -> dict:
    """How the promoted rows actually landed — and how many never were.

    Iterates the rows' own states rather than a list kept here, so a state this
    build has never heard of is counted under its raw name instead of silently
    vanishing into a bucket somebody chose in advance.
    """
    counts: dict[str, int] = {}
    for row in rows:
        state = delivery_of(row)
        counts[state] = counts.get(state, 0) + 1
    promoted = [r for r in rows if is_promoted(r)]
    delivered = [r for r in promoted if delivery_of(r) == DELIVERY_DELIVERED]
    dropped = [r for r in promoted if delivery_of(r) == DELIVERY_DROPPED]
    drop_reasons: dict[str, int] = {}
    for row in dropped:
        reason = str(row.get("router_drop_reason") or "unnamed")
        drop_reasons[reason] = drop_reasons.get(reason, 0) + 1
    unclassified = {k: v for k, v in counts.items() if k not in DELIVERY_COPY}
    # Promoted rows in a state this build has no name for. Counted in
    # `n_promoted` (they were put on the queue) but never inside `delivered` or
    # `dropped`, so an unknown state cannot quietly improve or worsen the
    # delivery rate — it shows up as rows the page cannot place.
    n_unclassified_promoted = sum(
        v for k, v in unclassified.items() if k.startswith(PROMOTED_PREFIX)
    )
    return {
        "counts": counts,
        "known": {k: v for k, v in counts.items() if k in DELIVERY_COPY},
        "unclassified": unclassified,
        "n_unclassified_promoted": n_unclassified_promoted,
        "n_promoted": len(promoted),
        "n_delivered": len(delivered),
        "n_dropped": len(dropped),
        "n_awaiting_router": (
            len(promoted) - len(delivered) - len(dropped) - n_unclassified_promoted
        ),
        # The share a promotion decision actually buys. Never inferred from
        # `n_promoted`: the correlation lock alone takes ~89% of what the
        # router dequeues, so "promoted 20" and "20 subscribers saw it" can
        # differ by an order of magnitude.
        "delivery_rate": (
            100.0 * len(delivered) / len(promoted) if promoted else None
        ),
        "drop_reasons": dict(
            sorted(drop_reasons.items(), key=lambda kv: -kv[1])
        ),
    }


def promoted_vs_dark(rows: list[dict], *, fee_pct: float = 0.0) -> dict:
    """The two populations side by side, and never blended.

    The question a live rule has to keep answering: *are the rows it promotes
    still behaving like the rows that justified it?* Both halves are measured
    on the same column (``pnl_pct``, present on every closed row whichever
    bucket it lands in) so the comparison is not itself an artefact of what each
    side happens to stamp.

    ``unstamped`` is reported apart from both. It is neither — it predates the
    mechanism — and folding it into the dark side would slowly bury a live
    rule's own evidence under a year of history that could never have been
    promoted.
    """
    promoted = [r for r in rows if is_promoted(r)]
    dark = [r for r in rows if delivery_of(r) == DELIVERY_DARK]
    unstamped = [r for r in rows if delivery_of(r) == DELIVERY_UNSTAMPED]
    delivered = [r for r in rows if delivery_of(r) == DELIVERY_DELIVERED]
    return {
        "promoted": summarize(promoted, fee_pct=fee_pct),
        # The subset that actually reached a subscriber. The only population
        # allowed to justify changing what subscribers receive, and it is a
        # subset of `promoted` rather than a second reading of it.
        "delivered": summarize(delivered, fee_pct=fee_pct),
        "dark": summarize(dark, fee_pct=fee_pct),
        "unstamped": summarize(unstamped, fee_pct=fee_pct),
        "split": delivery_split(rows),
    }


def condition_evidence(
    rows: list[dict],
    *,
    dimension: str,
    fee_pct: float = 0.0,
    min_rows: int = 1,
) -> dict:
    """What one condition dimension is worth, cell by cell.

    The panel the owner builds a rule from: for each value of *dimension*
    (``gate`` / ``regime`` / ``session`` / ``side``), the rows carrying it and
    what they did. Deliberately reports, in this order, **n, campaigns, then
    the average** — a cell is not evidence until its row count and its
    concentration have been read, and a table sorted by edge puts the
    best-looking cell of many on the top line by construction.

    So the sort is by **evidence**, not by edge, and ``cells_drawn`` rides in
    the payload: "best of N" is not a fact about the winner until N is on
    screen.

    Measured over closed rows only — an open row has no outcome, and counting
    it in the denominator would make a busy day look like a bad one.
    """
    key = {
        "gate": lambda r: str(r.get("dark_gate") or ""),
        "regime": lambda r: str(r.get("regime") or ""),
        "session": session_of,
        "side": lambda r: str(r.get("side") or ""),
    }.get(dimension)
    if key is None:
        raise ValueError(f"unknown dimension {dimension!r}")

    closed = [r for r in rows if str(r.get("status") or "") in _CLOSED]
    buckets: dict[str, list[dict]] = {}
    for row in closed:
        token = key(row)
        if not token:
            continue
        buckets.setdefault(token, []).append(row)

    cells = []
    for token, group in buckets.items():
        stats = summarize(group, fee_pct=fee_pct)
        stats["value"] = token
        # Both delivery populations inside the cell, because a cell already
        # partly promoted is not fresh evidence for promoting it — its recent
        # rows are the rule's own output.
        stats["n_promoted"] = sum(1 for r in group if is_promoted(r))
        cells.append(stats)

    kept = [c for c in cells if c["n_scored"] >= min_rows]
    kept.sort(key=lambda c: (-c["n_scored"], c["value"]))
    return {
        "dimension": dimension,
        "cells": kept,
        # How many cells this table drew from, before any floor. Printed, so a
        # reader can price the winner against the number of draws.
        "cells_drawn": len(cells),
        "cells_shown": len(kept),
        "min_rows": min_rows,
        "baseline": summarize(closed, fee_pct=fee_pct),
    }


def rule_for(snapshot: Any, setup_class: str) -> Optional[dict]:
    """This path's stored rule, or ``None``.

    Reads the engine's snapshot; ops keeps no copy of the registry. The rule's
    ``inert`` flag is the engine's own verdict, not one recomputed here — a
    second implementation of "does this rule match anything" would be a mirror,
    and it would disagree on exactly the edge cases that matter.
    """
    if not isinstance(snapshot, dict):
        return None
    for rule in snapshot.get("rules") or []:
        if str(rule.get("setup_class") or "").upper() == str(setup_class or "").upper():
            return rule
    return None


def rule_state(snapshot: Any, rule: Optional[dict]) -> tuple[str, str]:
    """One of five states, and the sentence that goes with it.

    Five rather than two, because "this rule is doing nothing" has four
    different causes with four different next moves, and a single OFF badge
    over all of them sends the reader to the wrong one. This is the
    exit-mechanism control's *three states, never two* rule with two more
    states, because there are two more switches in the chain.
    """
    if rule is None:
        return ("none", "No rule — every dark row for this path is diverted.")
    if not rule.get("enabled"):
        return (
            "off",
            "Rule saved and switched off. Its conditions are kept, so arming "
            "it is one switch.",
        )
    if rule.get("inert"):
        return (
            "inert",
            "Switched ON but no condition can match — an allow-list is empty, "
            "so this rule promotes nothing. It is not off; it is unfinished.",
        )
    if isinstance(snapshot, dict) and not snapshot.get("master_enabled"):
        return (
            "master_off",
            "Armed, but the engine-wide master switch is OFF — nothing is "
            "promoted whatever this rule says.",
        )
    if isinstance(snapshot, dict) and not snapshot.get("dark_lane_enabled"):
        return (
            "lane_off",
            "Armed, but the dark lane is OFF — the candidates this rule would "
            "promote are killed by the gate upstream and never reach it.",
        )
    return ("live", "LIVE — matching candidates are enqueued for real.")


# --------------------------------------------------------------------------- #
# Why a rule promoted nothing
#
# Added 2026-08-17, for the owner's *"still no LSR or any dark feed signal that
# we enable is actually delivered to users"*. Two walls stood between an armed
# rule and a subscriber and the page showed neither:
#
#   1. `dark_promotion.decide` refused every candidate, and the engine's only
#      counter was one integer per path. Engine-side fix: a per-dimension
#      refusal census, published in the snapshot's `runtime` block.
#   2. The rows that DID promote were taken by the router's second layer —
#      already measured (`promoted_dropped` + reason), already on this page.
#
# What follows renders the first, plus the number the page never had: what the
# whole conjunction selects. Every dimension's evidence is offered MARGINALLY,
# one table each, while the rule built from those tables is an INTERSECTION —
# so the best-looking cell of each can combine into a rule matching nothing,
# and until now the page could not say so.
# --------------------------------------------------------------------------- #

ANY_TOKEN = "*"

#: What each refusal dimension means, and what the owner's next move is. COPY,
#: not a mirror of the engine's list: the panel iterates the ENGINE's payload
#: and looks each dimension up here, so a dimension ops has never heard of
#: renders under its raw name badged `unclassified` rather than being dropped.
#: Iterating these keys instead would be silent by construction on the next
#: dimension the engine adds — `MEASUREMENT_SUFFIXES` wearing a seventh hat.
DIMENSION_REFUSAL_COPY: dict[str, str] = {
    "gate": (
        "The candidate was diverted at a gate this rule does not list. A path "
        "is usually diverted at several different gates — tick the ones you "
        "mean."
    ),
    "regime": "The regime stamped at entry is not in the rule's allow-list.",
    "session": (
        "The market-context session is not in the rule's allow-list. A blank "
        "session matches nothing but the wildcard."
    ),
    "direction": (
        "The side failed the direction condition. `with_trend` / "
        "`counter_trend` ABSTAIN when the regime names no trend — a RANGING "
        "or VOLATILE label is neither, so both refuse it."
    ),
    "confidence": "Scored confidence is below the rule's floor.",
    "daily_cap": (
        "The rule MATCHED and had already spent its blast-radius cap for the "
        "UTC day. This is the rule working and throttled, not misconfigured."
    ),
}


def _trend_of(regime: Any) -> Optional[str]:
    """Port of the engine's `dark_promotion.trend_of`.

    Substring-based for the same reason it is there: the regime vocabulary
    belongs to the engine's detector and a label list kept in ops would be
    silent on the next one. `None` for a range — and `None` is not "no", it is
    the abstain that makes both trend conditions refuse.
    """
    token = str(regime or "").strip().upper()
    if not token:
        return None
    up = "UP" in token or "BULL" in token or "MARKUP" in token
    down = "DOWN" in token or "BEAR" in token or "MARKDOWN" in token
    if up and not down:
        return "UP"
    if down and not up:
        return "DOWN"
    return None


def _matches(allowed: Any, value: Any) -> bool:
    tokens = [str(t or "").strip() for t in (allowed or []) if str(t or "").strip()]
    if not tokens:
        return False
    if ANY_TOKEN in tokens:
        return True
    return str(value or "").strip().upper() in [
        t if t == ANY_TOKEN else t.upper() for t in tokens
    ]


def rule_unmet(row: dict, rule: Optional[dict]) -> Optional[list[str]]:
    """Which of the rule's conditions this row fails. ``None`` when no rule.

    **This is ops reconstructing the engine's predicate, and the page says so.**
    The engine's own census (snapshot `runtime.refusals`) is the authority for
    what actually happened; this exists because that census only covers
    candidates seen since the engine last restarted, while the question the
    owner is asking — *what would this rule have selected* — is answerable over
    the whole ledger and only here.

    `tests/test_dark_promotion_refusals.py` drives the real
    `dark_promotion.decide` when the engine repo is present and asserts the two
    agree row for row, so this cannot drift silently. The daily cap is
    deliberately NOT reproduced: it is a fact about a running process on a
    given day, not about a row.
    """
    if not isinstance(rule, dict):
        return None
    unmet: list[str] = []
    if not _matches(rule.get("gates"), row.get("dark_gate")):
        unmet.append("gate")
    if not _matches(rule.get("regimes"), row.get("regime")):
        unmet.append("regime")
    if not _matches(rule.get("sessions"), session_of(row)):
        unmet.append("session")

    side = str(row.get("side") or "").strip().upper()
    direction = str(rule.get("direction") or "any").strip().lower()
    if direction == "long" and side != "LONG":
        unmet.append("direction")
    elif direction == "short" and side != "SHORT":
        unmet.append("direction")
    elif direction in ("with_trend", "counter_trend"):
        trend = _trend_of(row.get("regime")) or _trend_of(row.get("regime_15m"))
        if trend is None:
            unmet.append("direction")
        else:
            aligned = (trend == "UP" and side == "LONG") or (
                trend == "DOWN" and side == "SHORT"
            )
            if aligned is not (direction == "with_trend"):
                unmet.append("direction")

    floor = rule.get("min_confidence")
    if floor is not None:
        try:
            if float(row.get("confidence") or 0.0) < float(floor):
                unmet.append("confidence")
        except (TypeError, ValueError):
            unmet.append("confidence")
    return unmet


def rule_selection(
    rows: list[dict], rule: Optional[dict], *, fee_pct: float = 0.0
) -> dict:
    """What the whole conjunction selects, and which condition costs the rest.

    The number this page never had. Each dimension's evidence table is
    marginal; the rule is an intersection. On the owner's book the two can
    differ by everything — a gate cell reading n=97 and a regime cell reading
    n=120 can intersect at zero, and every table above would still look
    well-evidenced.

    So: `n_selected` with its own summary (measured on the rows the rule keeps,
    never on the whole ledger), the marginal refusal count per dimension, and
    `sole_blocker` — the rows that failed on exactly one condition, i.e. what
    relaxing that one condition would add. Sorted by evidence, never by edge.
    """
    closed = [r for r in rows if str(r.get("status") or "") in _CLOSED]
    if not isinstance(rule, dict):
        return {
            "has_rule": False,
            "n_rows": len(rows),
            "n_closed": len(closed),
            "selected": summarize([], fee_pct=fee_pct),
            "n_selected": 0,
            "by_dimension": {},
            "sole_blocker": {},
            "baseline": summarize(closed, fee_pct=fee_pct),
        }

    selected: list[dict] = []
    by_dimension: dict[str, int] = {}
    sole_blocker: dict[str, int] = {}
    for row in closed:
        unmet = rule_unmet(row, rule) or []
        if not unmet:
            selected.append(row)
            continue
        for dim in unmet:
            by_dimension[dim] = by_dimension.get(dim, 0) + 1
        if len(unmet) == 1:
            sole_blocker[unmet[0]] = sole_blocker.get(unmet[0], 0) + 1

    return {
        "has_rule": True,
        "n_rows": len(rows),
        "n_closed": len(closed),
        "selected": summarize(selected, fee_pct=fee_pct),
        "n_selected": len(selected),
        "n_refused": len(closed) - len(selected),
        # Marginal: a row failing three conditions is counted in all three, so
        # these sum past `n_refused` by design. Stated on the panel.
        "by_dimension": dict(
            sorted(by_dimension.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        # The actionable half: relax this one condition and exactly this many
        # closed rows join the selection.
        "sole_blocker": dict(
            sorted(sole_blocker.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
        "baseline": summarize(closed, fee_pct=fee_pct),
    }


def engine_refusals(snapshot: Any, setup_class: str) -> dict:
    """The engine's own refusal census for this path, in three states.

    `not_reported` — an engine that predates the census, or (in isolated mode)
    an API container that never received the engine's published block. NOT the
    same as zero: a rule refusing everything and a rule nobody has evaluated
    read identically otherwise, which is the whole reason this shipped.

    `idle` — the engine is reporting and has refused nothing for this path. The
    benign case, and it means something specific: no candidate has reached the
    decision, so the gate upstream or the dark lane is where to look next.

    `refusing` — with the per-dimension counts.
    """
    runtime = {}
    if isinstance(snapshot, dict):
        runtime = snapshot.get("runtime") or {}
    if not isinstance(runtime, dict) or not runtime.get("source"):
        # The engine names the cause when it has one — in isolated mode the API
        # container replaces its own zeros with an explicit `unavailable`
        # rather than serving them under `source: "engine"`. Carry that
        # sentence through: "not reported" and "not reported *because the
        # engine is down*" send the reader to different places.
        return {
            "state": "not_reported",
            "cell": {},
            "near_misses": [],
            "unavailable": (
                (runtime or {}).get("unavailable")
                if isinstance(runtime, dict) else None
            ),
        }

    cell = (runtime.get("refusals") or {}).get(str(setup_class or "").upper()) or {}
    near = [
        m for m in (runtime.get("near_misses") or [])
        if str(m.get("setup_class") or "").upper() == str(setup_class or "").upper()
    ]
    return {
        "state": "refusing" if cell.get("total") else "idle",
        "cell": {
            "total": int(cell.get("total") or 0),
            "by_dimension": dict(cell.get("by_dimension") or {}),
            "sole_blocker": dict(cell.get("sole_blocker") or {}),
        },
        # Newest first — a sample is read from the top.
        "near_misses": list(reversed(near)),
        "source": runtime.get("source"),
        "generated_at": runtime.get("generated_at"),
        # The ring's bound against the unbounded count, so the newest few
        # cannot read as the whole population.
        "ring": runtime.get("near_miss_ring"),
        "seen": runtime.get("near_miss_seen"),
    }
