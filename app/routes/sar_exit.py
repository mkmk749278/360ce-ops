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

from fastapi import APIRouter, Form, Query, Request
from fastapi.responses import RedirectResponse

from app import audit

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


# Status vocabulary for a SAR trade.
#
# Rewritten 2026-07-27 for the engine's **conditional-handover** redesign. The
# arm is no longer trail-only: when SAR opposes at entry the trade runs on its
# live SL/TP1 and only switches to the trail if the indicator comes onside. So
# a SAR trade CAN now end on a stop or a target, and the two new engine reasons
# (``static_sl`` / ``static_tp1``) must have statuses of their own. Without
# them ``_sar_status`` fell through to NO_DATA — the page would have reported
# "couldn't resolve it" for trades that resolved perfectly well, which is the
# panel lying about a data fault that is not happening.
SAR_RUNNING = "RUNNING"          # stamped; its 48h window has not elapsed yet
SAR_CLOSED_TRAIL = "CLOSED_TRAIL"   # the moving stop caught price
SAR_CLOSED_SL = "CLOSED_SL"      # the live stop closed it before any handover
SAR_CLOSED_TP1 = "CLOSED_TP1"    # the live target closed it before any handover
SAR_CLOSED_WINDOW = "CLOSED_WINDOW"  # 48h passed untouched; marked to the close
SAR_NO_DATA = "NO_DATA"          # window elapsed but candles couldn't resolve it

SAR_STATUSES = (
    SAR_RUNNING, SAR_CLOSED_TRAIL, SAR_CLOSED_SL, SAR_CLOSED_TP1,
    SAR_CLOSED_WINDOW, SAR_NO_DATA,
)

# Mirrors the engine's REASON_* constants (src/suppression_audit.py). Keep in
# step: a reason the engine writes and this map does not know reads as NO_DATA.
_REASON_TO_STATUS = {
    "trail": SAR_CLOSED_TRAIL,
    "static_sl": SAR_CLOSED_SL,
    "static_tp1": SAR_CLOSED_TP1,
    "window": SAR_CLOSED_WINDOW,
}

# Provenance (engine: src/suppression_audit.py). The arm stamps from both
# scanner call sites, so the ledger mixes candidates that reached subscribers
# with candidates a gate killed. Only the EMITTED half can justify changing
# what users receive — "would this exit have improved the signals we sent" is a
# different question from "…every candidate we considered", and they can have
# different answers. Records stamped before the engine recorded this read
# UNKNOWN and are excluded from both filters rather than guessed at.
# THREE states, not two (engine fix 2026-07-25). Conflating the middle one
# with EMITTED is what made this page report "Emitted to live (98)" in a window
# where 3 signals reached the feed:
#   suppressed — a scanner gate killed it
#   enqueued   — it passed every scanner gate and the queue accepted it, then
#                the ROUTER's own caps (correlation lock, cooldowns, per-channel
#                concurrency, same-direction throttle, staleness) dropped it.
#                Never delivered. This was ~97% of the old "emitted" set.
#   emitted    — the router confirmed delivery; a subscriber really saw it.
# Mirrors src/suppression_audit.py PROVENANCE_*; keep the two in step.
PROV_EMITTED = "emitted"
PROV_SUPPRESSED = "suppressed"
PROV_ENQUEUED = "enqueued"
PROV_UNKNOWN = ""


def _sar_status(rec: dict) -> str:
    cls = rec.get("classification")
    if cls is None:
        return SAR_RUNNING
    if cls == "INSUFFICIENT_DATA":
        return SAR_NO_DATA
    reason = str(rec.get("trail_exit_reason") or "")
    return _REASON_TO_STATUS.get(reason, SAR_NO_DATA)


def _sar_status_class(status: str) -> str:
    """Badge colour, mirroring the Feed's convention."""
    if status == SAR_RUNNING:
        return "st-active"
    if status in (SAR_CLOSED_TRAIL, SAR_CLOSED_TP1):
        return "st-tp"
    if status == SAR_CLOSED_SL:
        return "st-sl"
    if status == SAR_CLOSED_WINDOW:
        return "st-expired"
    return "st-closed"


# How close two entries on the same (symbol, side, setup) must be to count as
# one move rather than two independent outcomes.  **Mirrors the engine's
# ``SAR_EXIT_SHADOW_SAME_MOVE_PCT``** (0.5) — ops ports the engine's math, it
# does not invent it — and is displayed in the UI footer so the threshold is
# never a hidden constant behind a headline number.
SAME_MOVE_PCT = 0.5

# The rendered table is capped; the *measurements* are not.  Applied in the
# route AFTER every filter, never inside the reducer — see the note on
# ``reduce_sar_signals``.
TABLE_ROW_CAP = 500


def distinct_moves(rows: list[dict]) -> int:
    """How many distinct moves these rows describe (pure).

    Overlapping entries into one move are not independent evidence — the
    concentration rule this repo already carries, and the SAR feed is the worst
    case yet seen for it. One SLXUSDT SHORT setup contributed 10 rows inside
    2h10m at a 0.37% entry spread, 36% of an entire resolved population; the
    verdict computed over the rows read 32% win / −0.364R, and over the moves
    55% / +0.003R. **The sign flipped.**

    Greedy single-pass clustering per (symbol, side, strategy), oldest first: a
    row opens a new move unless it sits within ``SAME_MOVE_PCT`` of the move
    currently open on that key. Deliberately the same rule the engine's stamp
    gate uses, so the two agree on what "one move" means.

    This is a *disclosure*, not a de-duplication: no row is dropped, and both
    counts are shown side by side. Which one an adoption decision should use is
    a judgement call, and a judgement call is not something a reducer should
    make silently.
    """
    open_move: dict[tuple[str, str, str], float] = {}
    moves = 0
    for row in sorted(rows, key=lambda r: r.get("stamped_at") or 0.0):
        key = (
            str(row.get("symbol", "")),
            str(row.get("side", "")),
            str(row.get("strategy", "")),
        )
        entry = float(row.get("entry") or 0.0)
        if entry <= 0:
            continue
        anchor = open_move.get(key)
        if anchor is not None and abs(entry - anchor) / anchor * 100.0 < SAME_MOVE_PCT:
            continue
        open_move[key] = entry
        moves += 1
    return moves


def reduce_sar_signals(records: Any, limit: int | None = None) -> list[dict]:
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

    **``limit`` defaults to None, and the route does not pass one.** It used to
    default to 300 and truncate here, which put the cut *before* every filter:
    ``filter_sar_signals`` ran on the newest 300 pairs, so "Delivered to users"
    showed the emitted rows inside a ~4-hour slice rather than all of them in
    the ledger. That starves the rarest and most important population hardest —
    the owner's export held 4 emitted rows against 152 enqueued and 144
    suppressed, and only the emitted ones can justify changing live output
    (owner-caught 2026-07-28). Truncation now happens in the route, after
    filtering, and only to the rendered table — never to anything counted.
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
                # None on a pre-fix row, or one the walker refused to replay —
                # rendered as "?" and excluded from the split, never guessed
                # into a bucket it would then skew.
                "sar_aligned": rec.get("sar_aligned_at_entry"),
                # Bars from entry to the moment the trail took control; None =
                # never handed over, which means this row IS the control arm and
                # can contribute nothing to the comparison. Explicit None test
                # everywhere: 0 is a real handover (onside at entry) and the
                # falsiest value there is.
                "handover_bars": rec.get("sar_handover_bars"),
                "r_multiple": r_mult,
                "pnl_pct": pnl_pct,
                "delta_r": delta_r,
            })
    out.sort(key=lambda r: r["stamped_at"], reverse=True)
    return out if limit is None else out[:limit]


def filter_sar_signals(
    rows: list[dict], status: str = "", strategy: str = "", source: str = "",
    alignment: str = "",
) -> list[dict]:
    """Apply the status / setup / source filters (pure).

    ``source`` empty means **all** — every stamped candidate at any stage.
    Selecting ``emitted`` narrows to the signals that were actually delivered,
    which is the only population whose answer can justify changing live
    behaviour. ``enqueued`` is the middle stage — passed the scanner, dropped by
    the router's own caps, never seen by anyone; it is deliberately NOT part of
    ``emitted`` (that conflation was a ~30x inflation of the adoption number).
    An unknown-provenance record matches no named filter: it is excluded rather
    than assumed, because guessing it into ``emitted`` would inflate exactly the
    number an adoption decision reads.
    """
    out = rows
    if status:
        out = [r for r in out if r["status"] == status]
    if strategy:
        out = [r for r in out if r["strategy"] == strategy]
    if source:
        out = [r for r in out if r["provenance"] == source]
    if alignment == "aligned":
        out = [r for r in out if r["sar_aligned"] is True]
    elif alignment == "opposed":
        out = [r for r in out if r["sar_aligned"] is False]
    elif alignment == "pending":
        # Selectable on purpose: "no verdict yet" is the majority of the ledger
        # and the owner must be able to see that population rather than infer
        # it from a count in a footnote.
        out = [r for r in out if r["sar_aligned"] is None]
    return out


def summarize_alignment(rows: list[dict]) -> dict:
    """Split resolved rows by whether SAR agreed with the signal at entry.

    Port of the engine's ``sar_exit_shadow.summarize_sar_alignment`` — ops
    mirrors engine math, it does not invent it; if these disagree, ops is wrong.
    The engine buckets **resolved** rows only, so every rate here divides by
    ``closed``, not by ``n``. They coincide today (the engine writes
    ``sar_aligned_at_entry`` in the resolve path, so a RUNNING row never carries
    one) and would silently diverge the moment that changes — which is exactly
    the change proposed for the engine, so the denominators are pinned now.

    Why the arm cannot be read as one number — restated 2026-07-27 for the
    engine's conditional-handover redesign, because the old reason stopped
    being true. Alignment at entry now decides **which leg the trade starts
    on**, so the buckets still describe different experiments:

      aligned — the trail governed from bar one; a pure read on the method.
      opposed — the trade started on its live SL/TP1 and switched only if SAR
                came onside, so its avg-R is dominated by the live geometry.
                A row that never handed over IS the control arm, bar for bar.

    What this docstring used to say — that an opposed row "exits on the first
    testable bar and records ~15 minutes of drift" — described the arm before
    the redesign and must not be repeated in the UI: copy is part of the
    measurement, and a panel that names a cause its numbers no longer show is
    wrong on screen even when every figure in it is right.

    ``distinct_exits`` counts unique (symbol, side, exit price) among the
    resolved rows. Overlapping entries into one move resolve at the *same* exit
    price and are not independent evidence: on 2026-07-27 three BUSDT rows
    (+2.23R / +2.12R / +2.72R, stamped 00:04 / 00:47 / 01:34) all exited at
    0.1959 — one rally carrying 3/8 of the agreed bucket. Disclosed rather than
    de-duplicated: collapsing them is a judgement call, counting them silently
    is not.
    """
    def _bucket(sel: list[dict]) -> dict:
        closed = [r for r in sel if r["r_multiple"] is not None]
        holds = [r["hold_min"] for r in closed if r.get("hold_min") is not None]
        wins = sum(1 for r in closed if r["r_multiple"] > 0)
        exits = {
            (r.get("symbol"), r.get("side"), r.get("exit_price")) for r in closed
        }
        return {
            "n": len(sel),
            "closed": len(closed),
            "distinct_exits": len(exits),
            "handovers": sum(1 for r in closed if r.get("handover_bars") is not None),
            "avg_r": (sum(r["r_multiple"] for r in closed) / len(closed)) if closed else None,
            "win_rate": (wins / len(closed)) if closed else None,
            "avg_hold_min": (sum(holds) / len(holds)) if holds else None,
        }

    aligned = _bucket([r for r in rows if r["sar_aligned"] is True])
    opposed = _bucket([r for r in rows if r["sar_aligned"] is False])
    # A blank verdict has two very different causes and the page must not
    # conflate them. A RUNNING trade is simply not resolved yet — the ordinary
    # state of most of the ledger, and NOT evidence of anything. A row that has
    # finished and still carries no verdict is a real exclusion: the walker
    # refused to replay it, or it predates the flag. Calling the first kind
    # "no verdict" reads as a data fault where there is none — precisely the
    # false alarm to avoid after the #800 walker bug.
    blank = [r for r in rows if r["sar_aligned"] is None]
    pending = sum(1 for r in blank if r.get("status") == SAR_RUNNING)
    known_closed = aligned["closed"] + opposed["closed"]
    return {
        "aligned": aligned,
        "opposed": opposed,
        "pending": pending,
        "unresolved": len(blank) - pending,
        "unknown": len(blank),
        "known": aligned["n"] + opposed["n"],
        "known_closed": known_closed,
        "opposed_share": (opposed["closed"] / known_closed) if known_closed else None,
        # The population the A/B actually rests on: only a row where the trail
        # took control can differ from the control arm at all.
        "handovers": aligned["handovers"] + opposed["handovers"],
        "handover_share": (
            (aligned["handovers"] + opposed["handovers"]) / known_closed
            if known_closed else None
        ),
    }


def summarize_rows(rows: list[dict]) -> dict:
    """Headline for whatever the current filter selected.

    Averages are over **resolved** trades only — a running trade has no R, and
    counting it as 0R would drag every average toward zero and make the arm
    look flat while it is simply still measuring.
    """
    closed = [r for r in rows if r["r_multiple"] is not None]
    compared = [r for r in rows if r["delta_r"] is not None]
    wins = sum(1 for r in closed if r["r_multiple"] > 0)
    # Concentration, disclosed beside every count it could distort. A resolved
    # population of 28 rows describing 11 moves is not 28 pieces of evidence,
    # and on the owner's 2026-07-28 export the difference was the SIGN of the
    # headline: 32% win / −0.364R across the rows, 55% / +0.003R across the
    # moves. Both are shown; neither is silently chosen for the reader.
    closed_moves = distinct_moves(closed)
    return {
        "n": len(rows),
        "moves": distinct_moves(rows),
        "running": sum(1 for r in rows if r["status"] == SAR_RUNNING),
        "closed": len(closed),
        "closed_moves": closed_moves,
        # >1.0 means the average move is contributing more than one row.
        "rows_per_move": (len(closed) / closed_moves) if closed_moves else None,
        "avg_r": (sum(r["r_multiple"] for r in closed) / len(closed)) if closed else None,
        "total_r": sum(r["r_multiple"] for r in closed) if closed else None,
        "win_rate": (wins / len(closed)) if closed else None,
        "compared": len(compared),
        "avg_delta_r": (
            sum(r["delta_r"] for r in compared) / len(compared)
        ) if compared else None,
    }


def mark_running_rows(rows: list[dict], prices: dict[str, float]) -> list[dict]:
    """Mark still-running trades to the current price (pure, in place).

    Without this the tab is a dead list for 48 hours: every row reads RUNNING
    with every number blank, and there is no way to see whether a trade is
    winning or bleeding while it is open.

    Two things this deliberately does NOT do:

    * It does not write into ``r_multiple`` / ``pnl_pct`` / ``delta_r``. Those
      are **realized** results and stay blank until the trail actually exits —
      an unrealized number sitting in a realized column is how a still-open
      trade gets read as a finished one.
    * It is not the SAR arm's result. Both arms share this entry, so the
      unrealized move is identical for both and says nothing about which exit
      wins. It is the trade's current state, and the UI labels it as such.

    A missing price leaves the row untouched — the column blanks, the page
    still renders.
    """
    for row in rows:
        if row.get("status") != SAR_RUNNING:
            continue
        price = prices.get(row.get("symbol", ""))
        if not price or price <= 0:
            continue
        entry = float(row.get("entry") or 0.0)
        if entry <= 0:
            continue
        move = (price - entry) if row.get("side") == "LONG" else (entry - price)
        row["current_price"] = float(price)
        row["unrealized_pct"] = move / entry * 100.0
    return rows


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
    alignment: str = Query("", alias="alignment"),
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
    # The split panel and every dropdown count are measured on the population
    # the other filters selected — everything EXCEPT alignment itself. Applying
    # alignment here would make the panel degenerate (pick "opposed" and the
    # agreed row reads 0) and make each dropdown option describe only itself.
    #
    # Measuring the panel on the *unfiltered* ledger, which is what shipped in
    # #90, is the #88 mistake again: with Source set to "Gate-suppressed" the
    # page showed 149 rows under a split computed over all 267, silently
    # pooling delivered, router-dropped and gate-killed candidates. Only the
    # delivered population can justify changing what users receive, so the
    # panel has to move when that selector moves.
    scoped = filter_sar_signals(all_rows, status=status, strategy=strategy, source=source)
    rows = filter_sar_signals(scoped, alignment=alignment)
    # Same rule for the Source counts — every other filter applied, not its own.
    src_scope = filter_sar_signals(
        all_rows, status=status, strategy=strategy, alignment=alignment
    )
    flash = request.session.pop("_sar_flash", None)
    # One request for the whole book, TTL-cached, ban-circuit aware, and
    # {}-on-failure — so marking N open rows never costs N calls and a Binance
    # hiccup blanks a column instead of breaking the page.
    try:
        prices = await request.app.state.binance_klines.fetch_all_prices()
    except Exception:
        prices = {}
    # Everything above is measured on the FULL filtered population. Only the
    # rendered table is capped, and only here — the cap used to live inside
    # ``reduce_sar_signals`` at 300, ahead of every filter, which made
    # "Delivered to users" mean "delivered, within the newest 300 pairs"
    # (owner-caught 2026-07-28). ``shown``/``matched`` are handed to the
    # template so a capped table says so instead of looking complete.
    selected = rows
    rows = selected[:TABLE_ROW_CAP]
    mark_running_rows(rows, prices)
    return templates.TemplateResponse("sar_signals.html", {
        "request": request,
        "active": "sar_signals",
        "rows": rows,
        "total": len(all_rows),
        "matched": len(selected),
        "shown": len(rows),
        "row_cap": TABLE_ROW_CAP,
        "same_move_pct": SAME_MOVE_PCT,
        # Measured on the full selection, never on the truncated table.
        "summary": summarize_rows(selected),
        "statuses": [s for s in SAR_STATUSES if any(r["status"] == s for r in all_rows)],
        "strategies": sorted({r["strategy"] for r in all_rows}),
        "n_all": len(src_scope),
        "n_emitted": sum(1 for r in src_scope if r["provenance"] == PROV_EMITTED),
        "n_suppressed": sum(1 for r in src_scope if r["provenance"] == PROV_SUPPRESSED),
        "n_enqueued": sum(1 for r in src_scope if r["provenance"] == PROV_ENQUEUED),
        "n_unknown": sum(1 for r in src_scope if r["provenance"] == PROV_UNKNOWN),
        "filter_status": status,
        "filter_strategy": strategy,
        "filter_source": source,
        "filter_alignment": alignment,
        "alignment": summarize_alignment(scoped),
        "alignment_scoped": bool(status or strategy or source),
        "ledger_status": reduce_ledger_status(ledger, []),
        "flash": flash,
    })


_SIGNAL_COLS = [
    "stamped_iso", "symbol", "side", "strategy", "provenance", "entry",
    "current_price", "unrealized_pct", "exit_price", "status", "exit_reason",
    "hold_min", "r_multiple", "pnl_pct", "delta_r", "mfe_pct", "sar_aligned",
    "context_key",
]


@router.get("/signals/sar/export.csv")
async def sar_signals_export_csv(
    request: Request,
    status: str = Query("", alias="status"),
    strategy: str = Query("", alias="strategy"),
    source: str = Query("", alias="source"),
    alignment: str = Query("", alias="alignment"),
):
    """The SAR signal feed as CSV, honouring the current filter."""
    from app.reports import csv_response

    ledger = request.app.state.data_volume.sar_exit_candidates()
    rows = filter_sar_signals(
        reduce_sar_signals(ledger), status=status, strategy=strategy, source=source,
        alignment=alignment,
    )
    try:
        prices = await request.app.state.binance_klines.fetch_all_prices()
    except Exception:
        prices = {}
    mark_running_rows(rows, prices)
    data = [[r.get(c) for c in _SIGNAL_COLS] for r in rows]
    return csv_response("sar_signals", _SIGNAL_COLS, data)


@router.post("/signals/sar/clear")
async def sar_signals_clear(request: Request, confirm: str = Form("")):
    """Purge the SAR shadow ledger (owner-gated on the engine, audited).

    A measurement window is only worth reading if it is honest, so the owner
    needs a way to throw one away the moment it isn't — without waiting on an
    engine deploy. That is not hypothetical: on 2026-07-26 every resolved row
    in this ledger had been replayed against the wrong candle, and 172 of them
    were sitting here reading -4.4R while describing nothing at all.

    Destructive and irreversible, so it follows the control doctrine: explicit
    confirm, POST-redirect-GET so a refresh can't re-fire it, audited, and the
    engine (which owns the buffer and the file) does the actual clearing.
    """
    settings = request.app.state.settings
    if confirm != "CLEAR":
        request.session["_sar_flash"] = {
            "ok": False,
            "text": "Not cleared — confirmation checkbox was not ticked.",
        }
        return RedirectResponse("/signals/sar", status_code=303)

    result = await request.app.state.engine_api.clear_sar_ledger()
    ok = not (isinstance(result, dict) and result.get("error"))
    audit.record(
        settings.audit_log_path,
        action="sar_ledger_clear",
        params={},
        result=result if isinstance(result, dict) else {},
        ok=ok,
    )
    if ok and isinstance(result, dict) and result.get("queued"):
        text = "Clear queued — the engine container applies it within ~15s."
    elif ok:
        n = (result or {}).get("cleared_records", 0)
        text = f"SAR ledger cleared ({n} records)."
    else:
        text = f"Clear failed — {(result or {}).get('error', 'engine unreachable')}."
    request.session["_sar_flash"] = {"ok": ok, "text": text}
    return RedirectResponse("/signals/sar", status_code=303)


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
