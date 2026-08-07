"""Exit-method what-ifs on the dark feed — the Profit tab's question, one lane over.

Owner, 2026-08-03, against `/signals/dark-live`: *"implement same like live
features in dark feed — max PnL before hitting SL, and same exit strategies like
Held to stop in dark feed too."*

Why this could not simply reuse `exit_sim`
------------------------------------------
`app/data_sources/exit_sim.py` prices a **delivered** signal from the engine's
closed-signal record: `hit_tp`, the first-touch timestamps, MFE/MAE, the
TP-based PnL. A dark row has none of those — it never delivered, so
`trade_monitor` never tracked it and `signal_performance.json` never saw it. Its
only record is the dark ledger, and until engine #869 that ledger's walk
**stopped at the first TP1-or-SL touch**.

That truncation is the whole problem, and it is not a missing column:

* `mfe_pct` on a row that closed at TP1 is bounded by TP1 **by construction**.
  It answers "how far did it run before its own exit", never "how far was it
  going to run" — and reading the first as the second is a claim about bars
  nobody walked.
* Every strategy in the Profit tab's catalog needs to know what happened
  *after* TP1 was touched. None of it survives an early break.

So the engine grew a second arm (`dark_emission._walk_hold`): the same bars with
TP1 removed, exiting only at the original stop or at the six-hour horizon,
recording the peak before the stop and the highest ladder level reached before
it. This module prices strategies off those stamps. **It ports; it does not
re-derive.** Nothing here re-walks a candle or invents a fill.

Rules this module holds to
--------------------------
* **Refuse, don't clamp, and name the reason.** A row whose held arm is still
  running, retired unmeasured, or written before the arm existed is *excluded
  and counted*, never scored 0. A ladder leg whose level the engine never
  stamped makes the whole row unpriceable under that strategy — it does not
  quietly fall through to the stop, which would book a loss the strategy never
  took.
* **PnL % leads and there is no R here.** `signal_dispatch` sizes at a fixed
  notional, so R equalises nothing and misranks paths (owner, 2026-08-02). Every
  figure below is a percentage; the dark page keeps its own muted R column for
  the bridge to the Strategy Lab and nothing on this panel divides by a stop.
* **The strategy catalog mirrors the Profit tab's keys and labels**, so the same
  words mean the same thing on both pages. `hold` is the extra one, because on
  the Profit tab held-to-stop is the base replay rather than a selectable
  strategy.
* **Both readings of the peak travel together.** `hold_mfe_pct` excludes the
  stop bar's favourable wick and `hold_mfe_incl_pct` includes it; the gap is the
  intrabar assumption, and a strategy is priced off the pessimistic one.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# Mirrors of the engine's `dark_emission` constants. Kept as literals rather
# than imported because ops cannot import engine code — and named here so a
# drift is one grep rather than a silent mis-bucketing.
HOLD_OPEN = "HOLD_OPEN"
HOLD_SL = "HOLD_SL"
HOLD_EXPIRED = "HOLD_EXPIRED"
HOLD_INSUFFICIENT = "HOLD_INSUFFICIENT"

#: The arm has an outcome that can be priced. Everything else is excluded and
#: counted under its own reason.
HOLD_DECIDED = frozenset({HOLD_SL, HOLD_EXPIRED})

# Why a row could not be scored. Never pooled — each one has a different fix and
# a different meaning for the reader ("wait", "widen", "the lane is broken").
SKIP_NO_ARM = "no_hold_arm"
SKIP_RUNNING = "hold_running"
SKIP_UNMEASURED = "hold_unmeasured"
SKIP_NO_LEVEL = "level_not_stamped"
SKIP_NO_GEOMETRY = "no_geometry"

_TP_LEVEL = "tp"
_FIXED = "fixed"


@dataclass(frozen=True)
class Target:
    kind: str      # "tp" | "fixed"
    value: float   # TP level (1/2/3) or the fixed pct

    @property
    def label(self) -> str:
        if self.kind == _TP_LEVEL:
            return f"TP{int(self.value)}"
        return f"+{self.value:g}%"


@dataclass(frozen=True)
class Leg:
    fraction: float
    target: Target


@dataclass(frozen=True)
class Strategy:
    key: str
    label: str
    legs: tuple[Leg, ...]
    #: The held-to-stop arm itself rather than a laddered exit. It has no legs:
    #: its result is what the engine recorded, not something priced here.
    is_hold: bool = False


@dataclass
class ExitResult:
    """One dark row replayed under one strategy."""

    result_pct: Optional[float] = None
    filled_labels: list[str] = field(default_factory=list)
    sl_frac: float = 0.0
    #: Why this row was not scored. Empty when it was.
    skipped: str = ""


def _f(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _pos(value: Any) -> Optional[float]:
    """A strictly positive float, or None. An unstamped level is not a zero one."""
    out = _f(value)
    return out if out is not None and out > 0 else None


@dataclass
class DarkRowInputs:
    """The engine-stamped held-arm fields, and nothing derived from anything else."""

    is_long: bool
    entry: float
    stop: float
    tps: dict[int, Optional[float]]
    hold_status: str
    hold_result_pct: Optional[float]
    hold_mfe_pct: Optional[float]
    hold_hit_tp: int

    @classmethod
    def from_row(cls, row: dict) -> Optional["DarkRowInputs"]:
        entry = _pos(row.get("entry"))
        stop = _pos(row.get("stop_loss"))
        side = str(row.get("side") or "").upper()
        if entry is None or stop is None or side not in ("LONG", "SHORT"):
            return None
        # Absent, not empty: a row written before the arm shipped carries no
        # `hold_status` at all, and that is a different state from an arm that
        # has not moved yet. The caller renders them apart.
        raw_status = row.get("hold_status")
        return cls(
            is_long=side == "LONG",
            entry=entry,
            stop=stop,
            tps={1: _pos(row.get("tp1")), 2: _pos(row.get("tp2")), 3: _pos(row.get("tp3"))},
            hold_status="" if raw_status is None else str(raw_status),
            hold_result_pct=_f(row.get("hold_result_pct")),
            hold_mfe_pct=_f(row.get("hold_mfe_pct")),
            hold_hit_tp=int(_f(row.get("hold_hit_tp")) or 0),
        )

    def stop_return_pct(self) -> float:
        """Signed return if closed at the original stop — negative for a real loss.

        The dark ledger's `stop_loss` is stamped at emission and never moved: no
        break-even shift, no trail. So this is the stop the row was actually
        measured against, which is exactly what #848 found the delivered book's
        record was *not*.
        """
        if self.is_long:
            return (self.stop - self.entry) / self.entry * 100.0
        return (self.entry - self.stop) / self.entry * 100.0

    def tp_return_pct(self, level: int) -> Optional[float]:
        px = self.tps.get(level)
        if px is None:
            return None
        if self.is_long:
            return (px - self.entry) / self.entry * 100.0
        return (self.entry - px) / self.entry * 100.0

    def skip_reason(self) -> str:
        """Why this row cannot be scored at all, or "" if it can."""
        if not self.hold_status:
            return SKIP_NO_ARM
        if self.hold_status == HOLD_INSUFFICIENT:
            return SKIP_UNMEASURED
        if self.hold_status not in HOLD_DECIDED:
            return SKIP_RUNNING
        if self.hold_result_pct is None:
            # Terminal but unpriced — treat as unmeasured rather than as a zero.
            return SKIP_UNMEASURED
        return ""


def evaluate(inp: DarkRowInputs, strategy: Strategy, be_pct: float = 1.0) -> ExitResult:
    """Price one dark row under ``strategy``. Gross of fees — the caller charges those.

    Fill order is settled by the engine's stamps rather than inferred here:
    ``hold_hit_tp`` counts only levels touched **strictly before** the stop bar,
    and ``hold_mfe_pct`` is the peak over those same bars. So a leg that fills
    provably filled first, and a same-bar tie was already resolved against the
    trade upstream.
    """
    skip = inp.skip_reason()
    if skip:
        return ExitResult(skipped=skip)

    if strategy.is_hold:
        # The arm itself. Nothing to price: this is what the engine recorded.
        return ExitResult(
            result_pct=inp.hold_result_pct,
            filled_labels=["stop"] if inp.hold_status == HOLD_SL else ["horizon"],
            sl_frac=1.0 if inp.hold_status == HOLD_SL else 0.0,
        )

    if strategy.key == "be_tp1":
        be = _evaluate_be(inp, be_pct)
        if be is not None:
            return be

    total = 0.0
    filled = 0.0
    labels: list[str] = []
    for leg in strategy.legs:
        if leg.target.kind == _FIXED:
            ret: Optional[float] = leg.target.value
            hit = (
                inp.hold_mfe_pct is not None
                and inp.hold_mfe_pct + 1e-9 >= leg.target.value
            )
        else:
            level = int(leg.target.value)
            ret = inp.tp_return_pct(level)
            if ret is None:
                # The engine never stamped this level, so the leg cannot be
                # priced. Refuse the whole row rather than let its fraction fall
                # through to the stop — that would book a loss under a strategy
                # that may well have taken profit, and the shortfall would read
                # as the strategy performing badly rather than as missing data.
                return ExitResult(skipped=SKIP_NO_LEVEL)
            hit = inp.hold_hit_tp >= level
        if hit:
            total += leg.fraction * ret
            filled += leg.fraction
            labels.append(leg.target.label)

    remaining = max(0.0, 1.0 - filled)
    sl_frac = 0.0
    if remaining > 1e-9:
        if inp.hold_status == HOLD_SL:
            total += remaining * inp.stop_return_pct()
            sl_frac = remaining
        else:
            # Held to the horizon without the stop being touched: the untaken
            # fraction closes where the arm did, at the last bar's close.
            total += remaining * (inp.hold_result_pct or 0.0)
    return ExitResult(result_pct=total, filled_labels=labels, sl_frac=sl_frac)


def _evaluate_be(inp: DarkRowInputs, be_pct: float) -> Optional[ExitResult]:
    """The break-even arm: stop moves to entry once the trade is ``be_pct`` up.

    Ported from `exit_sim.evaluate_be` deliberately, including its
    approximation, so the same words mean the same thing on both pages. It fires
    when the trade went ``be_pct`` in our favour and then reached the original
    stop without TP1 having filled first — a scratch at 0% instead of the loss.

    **The approximation, stated because it has a direction.** A BE stop parked
    at entry exits at the *first* retrace through entry after the trigger, which
    can precede a TP1 that this model still books. So the BE row is optimistic
    where a trade went up, back through entry, and only then on to TP1 — and it
    is exact everywhere else. Returns None when BE does not fire, so the caller
    falls through to the base ladder unchanged.
    """
    if inp.hold_mfe_pct is None or inp.hold_mfe_pct + 1e-9 < be_pct:
        return None
    if inp.hold_status != HOLD_SL or inp.hold_hit_tp >= 1:
        return None
    return ExitResult(result_pct=0.0, filled_labels=["BE"], sl_frac=0.0)


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #

def build_catalog(target_pct: float = 1.0) -> dict[str, Strategy]:
    """The selectable exit strategies, keyed as the Profit tab keys them.

    ``hold`` leads because it is the arm every other row is measured against and
    the one the owner named. The rest mirror `exit_sim.build_catalog` exactly —
    same keys, same labels, same order — so a number here and a number there are
    comparable without anyone having to check which "TP1 full" is which.
    """
    g = target_pct
    fixed = Target(_FIXED, g)
    tp1, tp2, tp3 = Target(_TP_LEVEL, 1), Target(_TP_LEVEL, 2), Target(_TP_LEVEL, 3)
    strategies = [
        Strategy("hold", "Held to stop (no TP)", (), is_hold=True),
        Strategy("tp1", "TP1 full (100% @ TP1)", (Leg(1.0, tp1),)),
        Strategy("be_tp1", f"BE stop at +{g:g}% → hold to TP1", (Leg(1.0, tp1),)),
        Strategy("flat", f"Pre-TP only +{g:g}% · no invalidation", (Leg(1.0, fixed),)),
        Strategy("tp1_tp2", "50% TP1 · 50% TP2", (Leg(0.5, tp1), Leg(0.5, tp2))),
        Strategy("flat_tp1", f"Pre-TP +{g:g}% · TP1 · no invalidation",
                 (Leg(0.5, fixed), Leg(0.5, tp1))),
        Strategy("tp1_tp2_tp3", "TP1/TP2/TP3 thirds",
                 (Leg(1 / 3, tp1), Leg(1 / 3, tp2), Leg(1 / 3, tp3))),
    ]
    return {s.key: s for s in strategies}


def get_strategy(key: str, target_pct: float = 1.0) -> Strategy:
    catalog = build_catalog(target_pct)
    return catalog.get(key) or catalog["hold"]


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #

def summarize(results: list[ExitResult], fee_pct: float = 0.0) -> dict:
    """Net read-out over the rows a strategy could actually price.

    **Gross and net both, and the fee named** — the cost of trading has been
    ~10× the edge on this book (`/track-record`, 2026-08-03), so a gross-only
    figure answers the wrong question and a net-only one hides which half moved.

    The skip counts ride along, split by cause. A strategy that scored 12 rows
    of 60 is not a strategy that performed on 60, and the difference between
    "the arm is still running" (wait) and "the level was never stamped" (a
    producer fault) is the reader's next move.
    """
    scored = [r.result_pct for r in results if r.result_pct is not None]
    skips: dict[str, int] = {}
    for r in results:
        if r.skipped:
            skips[r.skipped] = skips.get(r.skipped, 0) + 1
    if not scored:
        return {
            "n": 0, "avg_pct": None, "total_pct": None, "avg_gross_pct": None,
            "total_gross_pct": None, "win_rate": None, "wins": 0, "losses": 0,
            "skipped": sum(skips.values()), "skips": skips, "fee_pct": fee_pct,
        }
    net = [v - fee_pct for v in scored]
    wins = sum(1 for v in net if v > 1e-9)
    losses = sum(1 for v in net if v < -1e-9)
    return {
        "n": len(net),
        "avg_pct": sum(net) / len(net),
        "total_pct": sum(net),
        "avg_gross_pct": sum(scored) / len(scored),
        "total_gross_pct": sum(scored),
        "win_rate": wins / len(net) * 100.0,
        "wins": wins,
        "losses": losses,
        "skipped": sum(skips.values()),
        "skips": skips,
        "fee_pct": fee_pct,
    }


def compare_strategies(
    rows: list[dict], *, target_pct: float = 1.0, fee_pct: float = 0.0
) -> dict:
    """Every strategy against the row's own dark exit, on ONE shared population.

    The population is the rows where **both** verdicts exist: the row's own
    TP1-or-SL outcome and a decided held arm. That is the same rule the SAR
    panel on this page already carries — a row that resolved while its arm is
    still running describes one mechanism and not the other, and averaging over
    the union compares two different sets of trades and calls the difference a
    result.

    It matters more here than there, because a strategy that can price fewer
    rows would otherwise be measured on an easier subset and win on selection.
    So the shared population is fixed first, and a strategy that cannot price a
    row inside it is reported as covering fewer rows rather than being handed a
    different denominator.
    """
    catalog = build_catalog(target_pct)
    paired: list[tuple[dict, DarkRowInputs]] = []
    excluded: dict[str, int] = {}
    for row in rows:
        inp = DarkRowInputs.from_row(row)
        if inp is None:
            excluded[SKIP_NO_GEOMETRY] = excluded.get(SKIP_NO_GEOMETRY, 0) + 1
            continue
        skip = inp.skip_reason()
        if skip:
            excluded[skip] = excluded.get(skip, 0) + 1
            continue
        if _f(row.get("pnl_pct")) is None:
            # The row's own exit has no result, so there is nothing to compare a
            # strategy against on this row. Counted apart from a held-arm skip.
            excluded["row_undecided"] = excluded.get("row_undecided", 0) + 1
            continue
        paired.append((row, inp))

    engine = [(_f(r.get("pnl_pct")) or 0.0) for r, _ in paired]
    engine_results = [ExitResult(result_pct=v) for v in engine]
    out = []
    for key, strat in catalog.items():
        results = [evaluate(inp, strat, be_pct=target_pct) for _, inp in paired]
        stat = summarize(results, fee_pct)
        base = summarize(engine_results, fee_pct)
        edge = (
            stat["total_pct"] - base["total_pct"]
            if stat["total_pct"] is not None and base["total_pct"] is not None
            else None
        )
        out.append({"key": key, "label": strat.label, "stat": stat, "edge": edge})
    return {
        "n": len(paired),
        "excluded": excluded,
        "n_excluded": sum(excluded.values()),
        "engine": summarize(engine_results, fee_pct),
        "strategies": out,
        # "Best of N" is not a fact about the winner until N is on screen: the
        # top row of a table beats a coin flip by construction.
        "n_cells": len(out),
        "fee_pct": fee_pct,
        "target_pct": target_pct,
    }


def summarize_max_profit(rows: list[dict]) -> dict:
    """"Max PnL before hitting SL", per path — the owner's first ask.

    Measured on the held arm, so it is **not** bounded by TP1 the way the row's
    own ``mfe_pct`` is. That bound is the entire reason this number could not be
    read off the page before: a row that closed at TP1 had, by construction, an
    MFE of about the TP1 distance, whatever the trade went on to do.

    ``give_back_pct`` is the gap between that peak and what the row's own exit
    actually captured — how much was on the table. Only rows with both a decided
    arm and a decided row contribute to it, for the reason above.

    Every figure carries both readings of the peak. ``avg_peak_pct`` excludes the
    stop bar's favourable wick; ``avg_peak_incl_pct`` includes it. They agree
    except on rows whose peak and stop landed in one bar, which is exactly where
    a single figure would be a choice rather than a measurement.
    """
    by_setup: dict[str, dict] = {}
    for row in rows:
        status = str(row.get("hold_status") or "")
        if status not in HOLD_DECIDED:
            continue
        setup = str(row.get("setup_class") or "UNKNOWN")
        agg = by_setup.setdefault(setup, {
            "setup_class": setup, "n": 0, "peaks": [], "peaks_incl": [],
            "draw": [], "give": [], "stopped": 0, "horizon": 0, "ambiguous": 0,
        })
        agg["n"] += 1
        if status == HOLD_SL:
            agg["stopped"] += 1
        else:
            agg["horizon"] += 1
        if row.get("hold_ambiguous_bar"):
            agg["ambiguous"] += 1
        peak = _f(row.get("hold_mfe_pct"))
        if peak is not None:
            agg["peaks"].append(peak)
            captured = _f(row.get("pnl_pct"))
            if captured is not None:
                agg["give"].append(peak - captured)
        incl = _f(row.get("hold_mfe_incl_pct"))
        if incl is not None:
            agg["peaks_incl"].append(incl)
        draw = _f(row.get("hold_mae_pre_peak_pct"))
        if draw is not None:
            agg["draw"].append(draw)

    def _mean(vals: list[float]) -> Optional[float]:
        return (sum(vals) / len(vals)) if vals else None

    def _median(vals: list[float]) -> Optional[float]:
        if not vals:
            return None
        s = sorted(vals)
        mid = len(s) // 2
        return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2.0

    out = []
    for agg in by_setup.values():
        peaks = agg["peaks"]
        out.append({
            "setup_class": agg["setup_class"],
            "n": agg["n"],
            "stopped": agg["stopped"],
            "horizon": agg["horizon"],
            "ambiguous": agg["ambiguous"],
            "n_peak": len(peaks),
            "avg_peak_pct": _mean(peaks),
            "median_peak_pct": _median(peaks),
            "best_peak_pct": max(peaks) if peaks else None,
            "avg_peak_incl_pct": _mean(agg["peaks_incl"]),
            # How far it went against us on the way to that peak — without it
            # the peak bounds nothing, because "would a tighter stop have kept
            # this" is exactly "did it survive its own drawdown first".
            "avg_drawdown_pre_peak_pct": _mean(agg["draw"]),
            "worst_drawdown_pre_peak_pct": max(agg["draw"]) if agg["draw"] else None,
            "n_give": len(agg["give"]),
            "avg_give_back_pct": _mean(agg["give"]),
        })
    out.sort(key=lambda a: -a["n"])
    total_peaks = [p for a in by_setup.values() for p in a["peaks"]]
    return {
        "by_setup": out,
        "n": sum(a["n"] for a in by_setup.values()),
        "avg_peak_pct": _mean(total_peaks),
        "best_peak_pct": max(total_peaks) if total_peaks else None,
    }


def _bucket(row: dict) -> str:
    """Which coverage bucket a row falls in. One classifier, three readers."""
    status = row.get("hold_status")
    if status is None:
        return SKIP_NO_ARM
    if str(status) in HOLD_DECIDED:
        return "decided"
    if str(status) == HOLD_INSUFFICIENT:
        return SKIP_UNMEASURED
    return SKIP_RUNNING


def _own_exit(rows: list[dict]) -> dict:
    """The rows' OWN SL/TP1 outcome — the column that exists on every row.

    Deliberately not the held arm's: this is the yardstick the arm's subset gets
    measured against, so it has to be readable on rows the arm never scored.
    """
    vals = [v for v in (_f(r.get("pnl_pct")) for r in rows) if v is not None]
    wins = sum(1 for v in vals if v > 0)
    losses = sum(1 for v in vals if v < 0)
    decided = wins + losses
    return {
        "n": len(rows),
        "n_scored": len(vals),
        "avg_pct": (sum(vals) / len(vals)) if vals else None,
        # Flat rows (`EXPIRED` at exactly 0.00%) are excluded from the RATE and
        # kept in the average — a walked window in which nothing happened is not
        # a loss, and it still paid its round trip. "Three buckets, never two."
        "win_rate": (wins / decided * 100.0) if decided else None,
        "wins": wins,
        "losses": losses,
        "flat": len(vals) - decided,
    }


def hold_coverage(rows: list[dict]) -> dict:
    """How much of this selection the held arm can speak for, and why not the rest.

    Rendered whether or not anything is missing. A check that appears only when
    it trips teaches the reader that its absence means "fine", when it equally
    means the check stopped running — the rule `/signals/sar-live` paid for.

    Two things beyond the counts, both added 2026-08-07 after reading the
    rendered page rather than the code.

    **Coverage is not uniform across paths**, and the exit-method table above is
    the whole page's headline. On the 2026-08-07 book the arm had decided 54% of
    `MOVER_AVWAP_SCALP` and **24%** of `FAILED_AUCTION_RECLAIM` — so "measured
    over 217 rows" silently meant a book weighted quite differently from the one
    the per-path table beside it shows. A reader cannot discount a path's
    exit-method number without knowing that path's coverage, and it was nowhere.

    **And the shortfall is directional, not merely a sample.** Every row carries
    its own SL/TP1 `pnl_pct` whichever bucket it lands in, so that column can
    grade whether the arm's subset represents the book — which is the one
    question a coverage count cannot answer. It did not: the decided rows
    averaged **−0.3215%** against **−0.1706%** for the retired ones, and all five
    still-running rows were winners. The priced half is the *worse* half, so
    every exit method's absolute number is pessimistic and its *edge over the
    baseline* — measured on the same rows — is the figure that survives.

    Saying "245 excluded" invites the reader to treat the residue as random.
    This says which way it leans. It is the `/signals/sar-live` rule about
    loss-selected samples, arriving at a population defined by a second arm's
    reach rather than by a resolver's budget.
    """
    counts = {"decided": 0, SKIP_RUNNING: 0, SKIP_UNMEASURED: 0, SKIP_NO_ARM: 0}
    buckets: dict[str, list[dict]] = {
        "decided": [], SKIP_RUNNING: [], SKIP_UNMEASURED: [], SKIP_NO_ARM: [],
    }
    by_path: dict[str, dict] = {}
    for row in rows:
        bucket = _bucket(row)
        counts[bucket] += 1
        buckets[bucket].append(row)
        path = str(row.get("setup_class") or "?")
        agg = by_path.setdefault(path, {
            "setup_class": path, "total": 0,
            "decided": 0, SKIP_RUNNING: 0, SKIP_UNMEASURED: 0, SKIP_NO_ARM: 0,
        })
        agg["total"] += 1
        agg[bucket] += 1

    for agg in by_path.values():
        agg["priced_share"] = agg["decided"] / agg["total"] * 100.0 if agg["total"] else 0.0

    counts["total"] = len(rows)
    counts["by_path"] = sorted(by_path.values(), key=lambda a: -a["total"])
    # Keyed by the same bucket names, so a new bucket cannot appear in the
    # counts and be silently absent here.
    counts["representativeness"] = {k: _own_exit(v) for k, v in buckets.items()}
    counts["representativeness"]["all"] = _own_exit(rows)
    return counts
