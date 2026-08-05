"""Structural veto — does a level ahead of the trade predict anything?

Added 2026-08-05 alongside engine ``src/structural_veto.py`` (price-action
program, Phase 4). The engine stamps, on **every** enqueued signal, how far the
nearest *opposing* level sits from entry and whether it falls between entry and
TP1. It applies none of it until a path is named in the ops allow-list.

Why this lane and not the standalone price-action signal
---------------------------------------------------------
Structurally-triggered paths are **0.62%** of the enqueued book — 15 rows in the
2026-08-05 window, **none delivered**. A standalone structural signal takes weeks
to reach a sample worth reading. The veto needs no new signal at all: it is
measurable against ~97% of the book from the day it ships, and its answer is the
precondition for the standalone lane being worth building.

What this page is careful about
-------------------------------
* **PnL % leads and nothing here divides by a stop.** ``signal_dispatch`` sizes
  at a fixed notional, so R equalises nothing (owner, 2026-08-02).
* **Three buckets, never two** — ``keep`` / ``drop`` / **``unknown``**. Folding
  rows whose feature never computed into ``keep`` is how a candidate rule takes
  credit for rows it never filtered.
* **The baseline is measured on the rows the split scored**, never over the whole
  ledger — a summary computed on a different population than the table beside it
  is not a summary of anything the reader is looking at (#90).
* **"Would have removed" colours inverted.** It is the performance of rows the
  rule would have *dropped*, so negative is the rule looking right — published
  beside the delivered book's own average, because a removal figure with no
  denominator means nothing.
* **Say how many cells were drawn.** "Best of N" is not a fact about the winner
  until N is on screen; the top row of a long table beats a coin flip by
  construction. ``FAILED_AUCTION_RECLAIM`` (+0.846R on three rows, CI
  [−1.00, +2.00], promotion requested within the day) is the standing example.
* **An empty book and clear air ahead are different findings**, never pooled. The
  engine names them ``no_levels`` and ``no_opposing``: one is a data fault, the
  other is the answer.
* **The mode is read off the rows** (``veto_mode``), never mirrored from a copy of
  the engine's flag registry.

The one rule that is enforceable
---------------------------------
``target_behind_level`` — TP1 sits beyond an opposing level. It is the only rule
here whose threshold comes from **no window**: not "closer than N ATR", but "the
target cannot be reached without breaking a level", which is arithmetic on values
the signal already carries. Every distance rule needs an N, and an N chosen from
the window it is judged on is what ``tpe_smc_zone`` was retired for the same day
it shipped. So the distance columns are **stamps whose thresholds get picked from
the distribution below**, not rules awaiting a flip.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

#: Thresholds swept for the distance splits. Deliberately a coarse, fixed ladder
#: rather than a percentile of this window: a cut chosen from the data it is
#: judged on is the thing §2 of the program warns about.
ATR_THRESHOLDS: tuple[float, ...] = (0.5, 1.0, 1.5, 2.0)


def _f(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


@dataclass
class Bucket:
    """One side of a split. Counts and money, never a verdict."""

    label: str
    n: int = 0
    wins: int = 0
    pnl_sum: float = 0.0

    def add(self, pnl: float) -> None:
        self.n += 1
        self.pnl_sum += pnl
        if pnl > 0:
            self.wins += 1

    @property
    def avg_pnl(self) -> Optional[float]:
        return (self.pnl_sum / self.n) if self.n else None

    @property
    def win_rate(self) -> Optional[float]:
        return (self.wins / self.n * 100.0) if self.n else None


@dataclass
class Split:
    """A candidate rule: what it keeps, what it drops, what it cannot judge."""

    name: str
    detail: str = ""
    keep: Bucket = field(default_factory=lambda: Bucket("keep"))
    drop: Bucket = field(default_factory=lambda: Bucket("drop"))
    #: Rows whose feature never computed. Its own bucket on every split — an
    #: inert rule reads exactly like a working one on every count except this.
    unknown: int = 0

    @property
    def n_scored(self) -> int:
        return self.keep.n + self.drop.n

    @property
    def kept_frac(self) -> Optional[float]:
        return (self.keep.n / self.n_scored) if self.n_scored else None

    @property
    def unknown_frac(self) -> Optional[float]:
        total = self.n_scored + self.unknown
        return (self.unknown / total) if total else None

    @property
    def delta(self) -> Optional[float]:
        """Kept minus the baseline **measured on the rows this split scored**."""
        if not self.n_scored or self.keep.avg_pnl is None:
            return None
        base = (self.keep.pnl_sum + self.drop.pnl_sum) / self.n_scored
        return self.keep.avg_pnl - base


@dataclass
class VetoReport:
    error: str = ""
    n_rows: int = 0
    n_joined: int = 0
    mode: str = ""
    counters: dict = field(default_factory=dict)
    retention: dict = field(default_factory=dict)
    splits: list[Split] = field(default_factory=list)
    refusal_counts: dict[str, int] = field(default_factory=dict)
    #: The delivered book's own average over the joined rows — every removal
    #: figure is published beside it, because a Δ with no denominator is not a
    #: fact about anything.
    baseline: Bucket = field(default_factory=lambda: Bucket("all joined"))
    setups: list[tuple[str, int]] = field(default_factory=list)

    @property
    def cells_drawn(self) -> int:
        """How many splits were computed. On screen, always — the top row of a
        long table beats a coin flip by construction."""
        return len(self.splits)


def _records_by_id(performance: Any) -> dict[str, dict]:
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
    return by_id


def build_report(
    ledger: Any,
    performance: Any,
    *,
    setup_class: str = "",
) -> VetoReport:
    """Join the veto ledger to the closed-signal record and price each split.

    *setup_class* filters **before** every count, so each figure describes the
    population the table beside it is showing.
    """
    report = VetoReport()
    if not isinstance(ledger, dict):
        report.error = "veto ledger unreadable"
        return report
    if ledger.get("error"):
        report.error = str(ledger["error"])
        return report
    rows = ledger.get("rows")
    if not isinstance(rows, list):
        report.error = "veto ledger carries no rows"
        return report

    report.counters = ledger.get("counters") if isinstance(ledger.get("counters"), dict) else {}
    report.retention = ledger.get("retention") if isinstance(ledger.get("retention"), dict) else {}

    # Setup census before filtering, so the selector lists every path.
    counts: dict[str, int] = {}
    for r in rows:
        counts[str(r.get("setup_class") or "?")] = counts.get(str(r.get("setup_class") or "?"), 0) + 1
    report.setups = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)

    if setup_class:
        rows = [r for r in rows if str(r.get("setup_class") or "") == setup_class]
    report.n_rows = len(rows)

    # Read off the rows the gate decided, never mirrored from a flag registry.
    modes = {str(r.get("veto_mode") or "") for r in rows if r.get("veto_mode")}
    report.mode = "enforce" if "enforce" in modes else ("measure" if modes else "")

    for r in rows:
        for reason in (r.get("refusals") or []):
            key = str(reason)
            report.refusal_counts[key] = report.refusal_counts.get(key, 0) + 1

    by_id = _records_by_id(performance)

    joined: list[tuple[dict, float]] = []
    for r in rows:
        rec = by_id.get(str(r.get("signal_id") or ""))
        if not rec:
            continue
        pnl = _f(rec.get("pnl_pct"))
        if pnl is None:
            continue
        joined.append((r, pnl))
        report.baseline.add(pnl)
    report.n_joined = len(joined)

    # ── the enforceable rule ─────────────────────────────────────────────
    s = Split(
        name="target_behind_level",
        detail=(
            "TP1 sits beyond an opposing level. The only rule here needing no "
            "threshold — it is arithmetic on values the signal already carries, "
            "not a cut chosen from this window."
        ),
    )
    for row, pnl in joined:
        v = row.get("opposing_inside_tp1")
        if v is None:
            s.unknown += 1
        elif v:
            s.drop.add(pnl)      # the veto would remove these
        else:
            s.keep.add(pnl)
    report.splits.append(s)

    # ── distance stamps, swept — thresholds NOT yet chosen ───────────────
    for thr in ATR_THRESHOLDS:
        sp = Split(
            name=f"opposing_dist_atr < {thr:g}",
            detail=(
                "A stamp, not a rule awaiting a flip: this threshold would have "
                "to come from this window, which is what tpe_smc_zone was "
                "retired for. Read n and kept-fraction before Δ."
            ),
        )
        for row, pnl in joined:
            d = _f(row.get("opposing_dist_atr"))
            if d is None:
                sp.unknown += 1
            elif d < thr:
                sp.drop.add(pnl)
            else:
                sp.keep.add(pnl)
        report.splits.append(sp)

    # ── value-area position ──────────────────────────────────────────────
    va = Split(
        name="value_area_pos != inside",
        detail=(
            "Inside value is rotational, outside is trending. A stamp — whether "
            "either is worse is what the numbers beside it are for."
        ),
    )
    for row, pnl in joined:
        pos = row.get("value_area_pos")
        if not pos:
            va.unknown += 1
        elif pos == "inside":
            va.drop.add(pnl)
        else:
            va.keep.add(pnl)
    report.splits.append(va)

    return report
