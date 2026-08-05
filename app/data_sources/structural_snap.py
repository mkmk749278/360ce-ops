"""Structural SL/TP1 snap — what level-aware geometry would have changed.

Engine ``src/structural_snap.py`` stamps, on every enqueued signal, where the
nearest swing high/low or round number sits relative to the stop and TP1 the
evaluator computed **arithmetically**.  It applies none of it by default.  This
module joins those stamps to the closed-signal record and prices the two arms.

Why the lane exists
-------------------
`MOVER_TREND_PULLBACK` is 59% of the enqueued book and its geometry is
``sl = min(ma_mid, prev_low) - atr*buf`` followed by TPs at fixed 1.0/1.6/2.5
R-multiples.  Nothing in that chain asks where price has actually traded, so
TP1 can land a hair beyond a swing high that has rejected four times.  The
repair existed in ``structural_levels.py`` from the day it was written and was
never reachable — the call sat behind a guard no caller ever satisfied.

**Two arms, never blended, and that is the whole design**
---------------------------------------------------------
The engine's TP1 snap moves **nearer only**; the SL snap moves either way
inside ±30% of the designed risk.  That asymmetry decides what the closed-signal
record can answer, and it does not answer both equally:

* **TP1 arm — fully decidable.**  ``max_favorable_excursion_pct`` says how far
  the trade ran in our favour, and all of it precedes the close.  So a nearer
  target was reached iff ``MFE >= snapped TP1 distance``, with no ordering
  ambiguity.
* **SL arm — partly decidable, and the gap is direction-biased.**  A *wider*
  stop on a trade that stopped out asks whether price would have come back, and
  the walk ended at the stop.  A *tighter* stop on a winner asks whether the
  drawdown came before or after TP1, and MFE/MAE carry no ordering between them.
  Both are refused, named separately, and counted.

  "Stopped out" is decided from **MAE against the arithmetic stop**, never from
  the record's ``hit_sl`` flag: ``trade_monitor`` moves the stop in place, so a
  trailed or BE-shifted winner carries ``hit_sl=True`` alongside a positive
  pnl.  Trusting the flag refused rows that were plainly decidable and labelled
  profitable trades as losers on screen (2026-08-05).

Dropping either silently would leave a loss-selected or win-selected sample
that looks exactly like an answer.  So the page publishes each arm with its own
decidable fraction and there is deliberately **no combined figure**; a test
asserts the key does not exist.

Two honesty notes that ride into the render
--------------------------------------------
* **MFE/MAE are tick-sampled, not intrabar.**  ``trade_monitor`` updates them on
  mark-price ticks, so a touch between ticks is not recorded.  Every "the
  snapped level was reached" verdict is therefore *conservative*: the arm can
  under-count rescues, never invent one.  That bias points against the snap,
  which is the safe direction for an adoption decision — but it is a bias and
  the page says so rather than presenting the count as exact.
* **PnL % leads and nothing here divides by a stop.**  ``signal_dispatch`` sizes
  at a fixed notional, so R equalises nothing and misranks paths (owner,
  2026-08-02).  Every figure below is a percentage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

# ── Verdicts, one per row per arm ───────────────────────────────────────────
# Never pooled: each means something different to the reader and has a
# different fix. "unchanged" is not "undecidable" is not "no data".
V_UNCHANGED = "unchanged"                 # the snap found no level; geometry identical
V_DECIDED = "decided"                     # the counterfactual is knowable
V_UNDECIDABLE_ORDER = "undecidable_ordering"     # SL: MAE vs TP1 ordering unknown
V_UNDECIDABLE_TRUNC = "undecidable_truncated"    # SL: walk ended at the stop
V_NO_OUTCOME = "no_outcome"               # never joined — not delivered, or still open
V_NO_EXCURSION = "no_excursion"           # joined, but the record carries no MFE/MAE

#: Verdicts that contribute to an arm's measured delta.  Everything else is
#: excluded and counted under its own name.
SCORED = frozenset({V_UNCHANGED, V_DECIDED})


def _f(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN check without importing math


@dataclass
class ArmRow:
    """One signal under one arm."""

    signal_id: str
    symbol: str
    setup_class: str
    direction: str
    tf: str
    verdict: str
    #: The book as it actually happened, in percent.
    actual_pnl_pct: Optional[float] = None
    #: What this arm would have produced, in percent.  None unless decided.
    arm_pnl_pct: Optional[float] = None
    #: Signed toward risk/reward: + = the level moved further from entry.
    shift_pct: Optional[float] = None
    level_source: str = ""

    @property
    def delta_pct(self) -> Optional[float]:
        if self.arm_pnl_pct is None or self.actual_pnl_pct is None:
            return None
        return self.arm_pnl_pct - self.actual_pnl_pct


@dataclass
class ArmSummary:
    """An arm's verdict over the rows it could actually judge."""

    name: str
    rows: list[ArmRow] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def scored(self) -> list[ArmRow]:
        return [r for r in self.rows if r.verdict in SCORED and r.delta_pct is not None]

    @property
    def n_total(self) -> int:
        return len(self.rows)

    @property
    def n_scored(self) -> int:
        return len(self.scored)

    @property
    def decidable_frac(self) -> Optional[float]:
        """The denominator the verdict is owed.

        A loss-selected sample is worse than no sample because it looks like an
        answer, so this sits beside every figure rather than under a tooltip.
        """
        if not self.rows:
            return None
        return self.n_scored / len(self.rows)

    @property
    def n_changed(self) -> int:
        """Rows the arm actually moved.  A rule that changes nothing has not
        been tested however flattering its delta."""
        return sum(1 for r in self.scored if r.verdict == V_DECIDED)

    @property
    def total_delta_pct(self) -> Optional[float]:
        rows = self.scored
        if not rows:
            return None
        return sum(r.delta_pct or 0.0 for r in rows)

    @property
    def avg_delta_pct(self) -> Optional[float]:
        rows = self.scored
        if not rows:
            return None
        return sum(r.delta_pct or 0.0 for r in rows) / len(rows)

    @property
    def now_avg_pnl_pct(self) -> Optional[float]:
        """The baseline, measured on **the same rows** the arm scored.

        Computed here rather than over the whole book on purpose: a summary
        taken over a different population than the table beside it is not a
        summary of anything the reader is looking at (#90).
        """
        rows = self.scored
        if not rows:
            return None
        vals = [r.actual_pnl_pct for r in rows if r.actual_pnl_pct is not None]
        return (sum(vals) / len(vals)) if vals else None

    @property
    def arm_avg_pnl_pct(self) -> Optional[float]:
        rows = self.scored
        if not rows:
            return None
        vals = [r.arm_pnl_pct for r in rows if r.arm_pnl_pct is not None]
        return (sum(vals) / len(vals)) if vals else None


@dataclass
class SnapReport:
    tp1: ArmSummary
    sl: ArmSummary
    #: Rows in the ledger, before any join.
    n_rows: int = 0
    #: Rows carrying a measurement (``refused`` empty).
    n_measured: int = 0
    #: Why the rest carry none, by named cause.
    refusals: dict[str, int] = field(default_factory=dict)
    #: Level provenance across measured rows — swing vs the round-number grid.
    sl_sources: dict[str, int] = field(default_factory=dict)
    tp1_sources: dict[str, int] = field(default_factory=dict)
    #: The ring is capped, so every rate here is a sample. Shipped by the
    #: engine with the data because a reader in another process cannot see the
    #: cap, and a verdict without its denominator reads as covering everything.
    evicted: int = 0
    max_rows: Optional[int] = None
    spec: dict[str, Any] = field(default_factory=dict)
    #: True when the engine's own apply flag was on for at least one row. The
    #: mode is read off the rows the lane decided, never mirrored from a copy
    #: of the engine's flag registry.
    any_applied: bool = False
    #: Per-setup scoring-timeframe census, ONE row per signal.
    #:
    #: Deliberately NOT read from the engine's `tf_census` counters: six
    #: consumers call the resolver per candidate, so that denominator is ~6x
    #: the signal count and a book fraction taken from it would be inflated
    #: sixfold while looking entirely plausible. These come off `score_tf_*` on
    #: the rows, which are stamped once each.
    tf_rows: int = 0
    tf_mismatched: int = 0
    tf_unmapped: int = 0
    tf_correction_live: bool = False
    tf_by_setup: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str = ""

    @property
    def tf_by_setup_rows(self) -> list[tuple[str, dict[str, Any]]]:
        """``(setup, slot)`` commonest first, sorted HERE rather than in Jinja.

        The template tried ``dictsort(by='value', attribute='n')``. Jinja's
        ``dictsort`` takes no ``attribute`` argument, so that raised
        ``TypeError`` and 500'd the page — and it did so only once real rows
        existed, because the block is behind ``{% if tf_by_setup %}`` and the
        test fixture omitted the ``score_tf_*`` keys the engine actually
        stamps. Sorting a dict-of-dicts is Python's job; a template that has to
        reach into values to order them has outgrown the filter.
        """
        return sorted(
            self.tf_by_setup.items(),
            key=lambda kv: (-int(kv[1].get("n") or 0), kv[0]),
        )


def _percent_from(entry: float, level: float) -> Optional[float]:
    if entry <= 0:
        return None
    return abs(level - entry) / entry * 100.0


def _tp1_arm(row: dict, rec: Optional[dict]) -> ArmRow:
    """TP1 moves nearer only, so MFE settles it outright."""
    base = ArmRow(
        signal_id=str(row.get("signal_id") or ""),
        symbol=str(row.get("symbol") or ""),
        setup_class=str(row.get("setup_class") or ""),
        direction=str(row.get("direction") or ""),
        tf=str(row.get("tf") or ""),
        verdict=V_NO_OUTCOME,
        shift_pct=_f(row.get("tp1_shift_pct")),
        level_source=str(row.get("tp1_source") or ""),
    )
    if rec is None:
        return base

    actual = _f(rec.get("pnl_pct"))
    mfe = _f(rec.get("max_favorable_excursion_pct"))
    base.actual_pnl_pct = actual
    if actual is None:
        return base
    if mfe is None:
        base.verdict = V_NO_EXCURSION
        return base

    entry = _f(row.get("entry")) or 0.0
    arith = _f(row.get("tp1_arith"))
    snapped = _f(row.get("tp1_snapped"))
    if arith is None or snapped is None or entry <= 0:
        base.verdict = V_NO_EXCURSION
        return base

    if snapped == arith:
        base.verdict = V_UNCHANGED
        base.arm_pnl_pct = actual
        return base

    snap_pct = _percent_from(entry, snapped)
    if snap_pct is None:
        base.verdict = V_NO_EXCURSION
        return base

    base.verdict = V_DECIDED
    if mfe >= snap_pct:
        # The nearer target was reached, and every recorded excursion precedes
        # the close, so it was reached before whatever ended the trade.
        base.arm_pnl_pct = snap_pct
    else:
        # Never reached — the trade ends exactly as it did.
        base.arm_pnl_pct = actual
    return base


def _sl_arm(row: dict, rec: Optional[dict]) -> ArmRow:
    """The SL arm, with its two refusals kept apart.

    ``undecidable_truncated`` (a wider stop on a trade whose drawdown reached
    its designed stop) and
    ``undecidable_ordering`` (a tighter stop on a winner) remove opposite ends
    of the distribution.  Pooling them into one "unknown" count would hide that
    the residue is biased, which is the only thing a reader needs to know
    before trusting the delta.
    """
    base = ArmRow(
        signal_id=str(row.get("signal_id") or ""),
        symbol=str(row.get("symbol") or ""),
        setup_class=str(row.get("setup_class") or ""),
        direction=str(row.get("direction") or ""),
        tf=str(row.get("tf") or ""),
        verdict=V_NO_OUTCOME,
        shift_pct=_f(row.get("sl_shift_pct")),
        level_source=str(row.get("sl_source") or ""),
    )
    if rec is None:
        return base

    actual = _f(rec.get("pnl_pct"))
    mae = _f(rec.get("max_adverse_excursion_pct"))
    base.actual_pnl_pct = actual
    if actual is None:
        return base
    if mae is None:
        base.verdict = V_NO_EXCURSION
        return base

    entry = _f(row.get("entry")) or 0.0
    arith = _f(row.get("sl_arith"))
    snapped = _f(row.get("sl_snapped"))
    if arith is None or snapped is None or entry <= 0:
        base.verdict = V_NO_EXCURSION
        return base

    if snapped == arith:
        base.verdict = V_UNCHANGED
        base.arm_pnl_pct = actual
        return base

    arith_pct = _percent_from(entry, arith) or 0.0
    snap_pct = _percent_from(entry, snapped) or 0.0
    drawdown = abs(mae)

    # "The walk ended at the stop" is a question about the DESIGNED stop, and
    # ``hit_sl`` cannot answer it.  ``trade_monitor`` moves ``sig.stop_loss``
    # in place — BE shift, TP1 park, trail — so a trade that runs and then
    # exits on the *moved* stop is recorded ``hit_sl=True`` with a POSITIVE
    # pnl.  On the 2026-08-05 export three of the five rows in the truncated
    # bucket were exactly that (+6.230%, 0.000%, 0.000%), sitting under copy
    # calling them losers — and each was decidable, because a drawdown that
    # never reached the arithmetic stop cannot have reached a WIDER one.
    # #848's mechanism arriving one surface later.
    #
    # MAE answers it directly and comes off the same stream: the monitor
    # detects the stop hit on the mark-price ticks that write
    # ``max_adverse_excursion_pct = min(..., pnl_pct)``, so a close at the
    # designed stop necessarily left an excursion at least that deep.  The
    # tighter branch below already asked its question this way; only the wider
    # branch trusted the flag, and the asymmetry was the tell.
    reached_designed_stop = drawdown >= arith_pct
    # Whether the trade ENDED down — not how it got there.  Same reason:
    # ``hit_sl`` on a trailed winner would book ``-snap_pct``, fabricating a
    # loss on a profitable trade rather than refusing.
    lost = actual < 0

    if snap_pct < arith_pct:
        # Tighter stop.
        if drawdown < snap_pct:
            # Never touched — the trade is unchanged either way.
            base.verdict = V_DECIDED
            base.arm_pnl_pct = actual
        elif lost:
            # It was going to lose anyway; the tighter stop just loses less.
            base.verdict = V_DECIDED
            base.arm_pnl_pct = -snap_pct
        else:
            # A winner that drew down past the tighter stop. Whether the
            # drawdown came before or after the target is not in the record.
            base.verdict = V_UNDECIDABLE_ORDER
    else:
        # Wider stop.
        if not reached_designed_stop:
            # The nearer stop was never reached, so the further one cannot be.
            base.verdict = V_DECIDED
            base.arm_pnl_pct = actual
        else:
            # Would price have come back? The walk ended at the stop.
            base.verdict = V_UNDECIDABLE_TRUNC
    return base


def build_report(
    ledger: Any,
    performance: Any,
    *,
    setup_class: str = "",
) -> SnapReport:
    """Join the snap ledger to the closed-signal record and price both arms.

    *setup_class* filters **before** every count, so each figure describes the
    population the table beside it is showing.
    """
    empty = SnapReport(tp1=ArmSummary("tp1"), sl=ArmSummary("sl"))
    if not isinstance(ledger, dict):
        empty.error = "snap ledger unreadable"
        return empty
    if ledger.get("error"):
        empty.error = str(ledger["error"])
        return empty

    rows = ledger.get("rows")
    if not isinstance(rows, list):
        empty.error = "snap ledger carries no rows"
        return empty

    if setup_class:
        rows = [r for r in rows if str(r.get("setup_class") or "") == setup_class]

    by_id: dict[str, dict] = {}
    records: Iterable[Any] = ()
    if isinstance(performance, dict):
        maybe = performance.get("records") or performance.get("signals") or []
        if isinstance(maybe, list):
            records = maybe
    elif isinstance(performance, list):
        records = performance
    for rec in records:
        if isinstance(rec, dict):
            sid = str(rec.get("signal_id") or "")
            if sid:
                by_id[sid] = rec

    report = SnapReport(
        tp1=ArmSummary("tp1"),
        sl=ArmSummary("sl"),
        n_rows=len(rows),
        evicted=int(ledger.get("evicted") or 0),
        max_rows=ledger.get("max_rows"),
        spec=ledger.get("spec") if isinstance(ledger.get("spec"), dict) else {},
    )

    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("apply_mode"):
            report.any_applied = True
        refused = str(row.get("refused") or "")
        if refused:
            report.refusals[refused] = report.refusals.get(refused, 0) + 1
            continue
        report.n_measured += 1

        # Scoring-timeframe census, per signal.
        mismatch = row.get("score_tf_mismatch")
        if "score_tf_used" in row:
            report.tf_rows += 1
            if row.get("score_tf_correction_live"):
                report.tf_correction_live = True
            sc = str(row.get("setup_class") or "UNKNOWN")
            slot = report.tf_by_setup.setdefault(
                sc,
                {"n": 0, "mismatched": 0, "unmapped": 0,
                 "declared": row.get("score_tf_declared"),
                 "used": row.get("score_tf_used")},
            )
            slot["n"] += 1
            # None is "cannot be checked" (no declared timeframe), never
            # folded into False, which is "checked, agrees" — an unmapped
            # evaluator would otherwise read as a healthy 5m path forever.
            if mismatch is None:
                report.tf_unmapped += 1
                slot["unmapped"] += 1
            elif mismatch:
                report.tf_mismatched += 1
                slot["mismatched"] += 1

        for key, bucket in (("sl_source", report.sl_sources),
                            ("tp1_source", report.tp1_sources)):
            src = str(row.get(key) or "none")
            bucket[src] = bucket.get(src, 0) + 1

        rec = by_id.get(str(row.get("signal_id") or ""))
        report.tp1.rows.append(_tp1_arm(row, rec))
        report.sl.rows.append(_sl_arm(row, rec))

    for arm in (report.tp1, report.sl):
        for r in arm.rows:
            arm.counts[r.verdict] = arm.counts.get(r.verdict, 0) + 1

    return report


def setups_present(ledger: Any) -> list[tuple[str, int]]:
    """Setup classes in the ledger with their row counts, commonest first."""
    if not isinstance(ledger, dict):
        return []
    rows = ledger.get("rows")
    if not isinstance(rows, list):
        return []
    counts: dict[str, int] = {}
    for row in rows:
        if isinstance(row, dict):
            sc = str(row.get("setup_class") or "")
            if sc:
                counts[sc] = counts.get(sc, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
