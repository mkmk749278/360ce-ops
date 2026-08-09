"""The SAR lane's SECOND arm: max profit before the stop, and the stop-management rules.

Two questions the SAR columns on ``/signals/sar-live`` are structurally
incapable of answering, both asked by the owner on 2026-08-09.

**"Max profit hit before hitting SL."** The page already renders ``mfe_pct`` and
it does *not* mean this. The engine's SAR loop stops advancing that figure at the
arm's own exit, so on a row that flipped out early it says how far the trade ran
before SAR closed it and is silent on everything after — a number that is always
about right and never means what a reader assumes. That is worse than a blank,
because a blank prompts a question. ``hold_mfe_pct`` is a different measurement
over a longer window (engine ``sar_live_shadow._step_hold``), and the two are
rendered in separate columns that are **never blended**.

**"What if you move SL after moving 3%."** Stop management, which no catalog in
this repo had ever priced — ``exit_sim`` and ``dark_exit_sim`` both ladder
*targets* and leave the stop where the evaluator put it.

Rules this module carries, each already in ``CLAUDE.md`` arriving from a new
direction:

* **The catalog is the engine's, not ours.** Labels, order and thresholds are
  read from the row's own ``strategies`` block and the ledger's manifest. Ops
  keeps no second copy — ``MEASUREMENT_SUFFIXES`` drifted for a week under
  exactly that shape, and the fix for a drifting mirror is not a second mirror.
  A rule ops has never heard of renders under its raw key rather than vanishing.
* **The baseline pays the same fee as the rules.** Charging a round trip to the
  methods and not to the row's own outcome manufactures an edge out of the fee.
* **Three states, never two.** A rule that never armed scored the baseline; a
  rule that armed and stopped out is a managed exit; a rule still open at the
  horizon is neither. Pooling any two misdescribes the rule.
* **Rows that predate the arm are their own bucket.** A schema-1 row carries no
  ``hold_status`` — it is owed nothing, and counting it as unresolved reports a
  fault that is not happening on a population that shrinks on its own.
* **The coverage figure is graded on a column every row has.** "Measured over N
  rows, M excluded" reads as a sampling caveat and is usually a *directional*
  one, so the excluded rows are graded on the SAR arm's own ``pnl_level_pct``,
  which every row carries whichever bucket it lands in.
"""

from __future__ import annotations

import statistics
from typing import Any, Iterable, Optional

#: Terminal states of the held arm, mirrored from the engine only as *strings to
#: match*, never as a catalog to render from. Each is its own bucket because
#: each has a different next move.
HOLD_SL = "CLOSED_SL"
HOLD_HORIZON = "HORIZON"
HOLD_INSUFFICIENT = "INSUFFICIENT"
HOLD_OPEN = "OPEN"

#: Rule outcome states, same rule.
ST_RULE_STOP = "RULE_STOP"
ST_ORIGINAL_SL = "ORIGINAL_SL"
ST_HORIZON = "HORIZON"
ST_OPEN = "OPEN"

#: Copy for each rule outcome. Looked up, never iterated — the table walks the
#: engine's payload so a state ops has not heard of still renders.
STATUS_COPY = {
    ST_RULE_STOP: "the managed stop caught it",
    ST_ORIGINAL_SL: "never armed — took the original stop",
    ST_HORIZON: "still open at the horizon, marked to the last close",
    ST_OPEN: "still walking",
}

#: Default Binance USD-M round trip: 0.02% maker in + 0.05% taker out. Same
#: default `/track-record` uses, so a reader moving between them is not silently
#: comparing a gross figure with a net one.
DEFAULT_FEE_PCT = 0.07


def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _mean(values: list[float]) -> Optional[float]:
    return statistics.mean(values) if values else None


# --------------------------------------------------------------------------- #
# Population
# --------------------------------------------------------------------------- #


def split_rows(rows: Iterable[dict]) -> dict[str, list[dict]]:
    """Partition by what the held arm can actually say about each row.

    ``pre_arm`` is the one that must not be pooled: those rows were written
    before the engine had a second arm. They are not "the arm found nothing",
    they are "nobody asked", and they age out on their own as the ring rotates.
    """
    out: dict[str, list[dict]] = {
        "resolved": [], "horizon": [], "insufficient": [], "open": [], "pre_arm": [],
    }
    for r in rows:
        status = r.get("hold_status")
        if status is None:
            out["pre_arm"].append(r)
        elif status == HOLD_SL:
            out["resolved"].append(r)
        elif status == HOLD_HORIZON:
            out["horizon"].append(r)
        elif status == HOLD_INSUFFICIENT:
            out["insufficient"].append(r)
        else:
            out["open"].append(r)
    return out


# --------------------------------------------------------------------------- #
# Max profit before the stop
# --------------------------------------------------------------------------- #


def reduce_peak(rows: Iterable[dict]) -> dict[str, Any]:
    """How far the trade ran before the ORIGINAL stop caught it.

    Scored on ``resolved`` (reached the stop — a complete walk) and reported
    separately for ``horizon`` (walked to the bound and still open, so its peak
    is a **floor** on what the trade offered, not a final answer). Blending them
    would let a growing horizon bucket move the headline without any trade
    behaving differently.

    ``mfe_incl`` is the optimistic reading — the peak counting the stop bar's own
    favourable wick, which OHLC cannot order against the stop touch. It sits
    beside the conservative figure rather than instead of it.
    """
    buckets = split_rows(rows)
    resolved, horizon = buckets["resolved"], buckets["horizon"]

    def _stat(rs: list[dict]) -> dict[str, Any]:
        peaks = [p for p in (_f(r.get("hold_mfe_pct")) for r in rs) if p is not None]
        incl = [p for p in (_f(r.get("hold_mfe_incl_pct")) for r in rs) if p is not None]
        mae = [p for p in (_f(r.get("hold_mae_pct")) for r in rs) if p is not None]
        pre = [p for p in (_f(r.get("hold_mae_pre_peak_pct")) for r in rs) if p is not None]
        return {
            "n": len(rs),
            "avg_peak": _mean(peaks),
            "median_peak": statistics.median(peaks) if peaks else None,
            "max_peak": max(peaks) if peaks else None,
            "avg_peak_incl": _mean(incl),
            "avg_mae": _mean(mae),
            "avg_mae_pre_peak": _mean(pre),
            # How often the trade was ever meaningfully green before the stop.
            # A book where most rows never clear 1% is a book whose losses are
            # not a stop-placement problem, and that is worth reading first.
            "pct_reached_1": (
                100.0 * sum(1 for p in peaks if p >= 1.0) / len(peaks) if peaks else None
            ),
            "pct_reached_3": (
                100.0 * sum(1 for p in peaks if p >= 3.0) / len(peaks) if peaks else None
            ),
            "pct_reached_5": (
                100.0 * sum(1 for p in peaks if p >= 5.0) / len(peaks) if peaks else None
            ),
        }

    ambiguous = sum(1 for r in resolved if r.get("hold_ambiguous_bar"))
    return {
        "resolved": _stat(resolved),
        "horizon": _stat(horizon),
        "n_insufficient": len(buckets["insufficient"]),
        "n_open": len(buckets["open"]),
        "n_pre_arm": len(buckets["pre_arm"]),
        # A peak printed on the stop bar itself is not counted as reached. Said
        # out loud rather than embedded, because it is a judgement.
        "n_ambiguous": ambiguous,
        # The always-present column, so the excluded rows can be graded rather
        # than merely counted (a coverage figure cannot say which way it leans).
        "coverage": _coverage(rows, buckets),
    }


def _coverage(rows: Iterable[dict], buckets: dict[str, list[dict]]) -> dict[str, Any]:
    """Grade the scored subset against everything else on a column every row has.

    "217 measured, 245 excluded" reads as a sampling caveat. On the dark feed the
    same sentence hid a 0.15pp directional gap, because the excluded rows were
    systematically different. The SAR arm's own ``pnl_level_pct`` is recorded on
    every row whichever bucket it lands in, so it is what grades the split.
    """
    scored = buckets["resolved"] + buckets["horizon"]
    rest = buckets["insufficient"] + buckets["open"] + buckets["pre_arm"]

    def _pnl(rs: list[dict]) -> Optional[float]:
        vals = [p for p in (_f(r.get("pnl_level_pct")) for r in rs) if p is not None]
        return _mean(vals)

    return {
        "n_scored": len(scored),
        "n_excluded": len(rest),
        "scored_sar_pnl": _pnl(scored),
        "excluded_sar_pnl": _pnl(rest),
    }


# --------------------------------------------------------------------------- #
# Stop-management rules
# --------------------------------------------------------------------------- #


def reduce_strategies(
    rows: Iterable[dict],
    *,
    fee_pct: float = DEFAULT_FEE_PCT,
    catalog: Optional[list] = None,
) -> dict[str, Any]:
    """Price every rule the engine stamped, against the row's own SAR outcome.

    Walks **the engine's keys**, in the order the engine wrote them, and takes
    each rule's label from the ledger's own ``strategy_catalog`` manifest. Ops
    keeps no copy of the rules: a rule the manifest does not describe renders
    under its raw key and is badged, rather than vanishing or being given a name
    ops invented. The baseline is charged the same round trip as every rule.

    A rule is only counted on a row where it reached a terminal state; a rule
    still walking has no outcome and is counted apart rather than scored as a
    zero, which would drag every average toward flat as the open set grows.
    """
    rows = [r for r in rows if isinstance(r, dict)]
    order: list[str] = []
    seen: set[str] = set()
    for r in rows:
        for key in (r.get("strategies") or {}):
            if key not in seen:
                seen.add(key)
                order.append(key)

    labels: dict[str, str] = {}
    triggers: dict[str, Any] = {}
    for entry in (catalog or []):
        if isinstance(entry, dict) and entry.get("key"):
            labels[str(entry["key"])] = str(entry.get("label") or entry["key"])
            triggers[str(entry["key"])] = entry.get("trigger_pct")

    fee = float(fee_pct or 0.0)
    out: list[dict[str, Any]] = []
    # The baseline: the row's own SAR exit, charged the same fee. Published as a
    # row of the same table so the comparison is visible rather than implied.
    base_rows = [r for r in rows if _f(r.get("pnl_level_pct")) is not None]
    base_vals = [_f(r.get("pnl_level_pct")) - fee for r in base_rows]  # type: ignore[operator]
    baseline = {
        "key": "__baseline__",
        "label": "The SAR arm's own exit (baseline)",
        "n": len(base_vals),
        "avg_pnl": _mean(base_vals),
        "total_pnl": sum(base_vals) if base_vals else None,
        "win_rate": (
            100.0 * sum(1 for v in base_vals if v > 0) / len(base_vals) if base_vals else None
        ),
        "armed": None,
        "edge": 0.0,
        "is_baseline": True,
    }

    for key in order:
        vals: list[float] = []
        paired: list[tuple[float, float]] = []
        armed = 0
        states: dict[str, int] = {}
        label = labels.get(key, key)
        trigger = triggers.get(key)
        described = key in labels
        for r in rows:
            st = (r.get("strategies") or {}).get(key)
            if not isinstance(st, dict):
                continue
            # A per-row label wins if the engine ever writes one; the manifest
            # is the normal source. Neither is an ops-side list.
            label = st.get("label") or label
            if st.get("trigger_pct") is not None:
                trigger = st.get("trigger_pct")
            status = str(st.get("status") or "")
            states[status] = states.get(status, 0) + 1
            if status == ST_OPEN:
                continue
            pnl = _f(st.get("pnl_pct"))
            if pnl is None:
                continue
            net = pnl - fee
            vals.append(net)
            if st.get("armed"):
                armed += 1
            base = _f(r.get("pnl_level_pct"))
            if base is not None:
                paired.append((net, base - fee))
        # The edge is measured ONLY on rows where both the rule and the baseline
        # priced — same rows, same fee. An edge taken from two different
        # populations moves with the difference between them.
        edge = _mean([a - b for a, b in paired]) if paired else None
        out.append({
            "key": key,
            "label": label,
            "trigger_pct": trigger,
            "n": len(vals),
            "n_paired": len(paired),
            "armed": armed,
            "armed_pct": (100.0 * armed / len(vals)) if vals else None,
            "avg_pnl": _mean(vals),
            "total_pnl": sum(vals) if vals else None,
            "win_rate": (
                100.0 * sum(1 for v in vals if v > 0) / len(vals) if vals else None
            ),
            "edge": edge,
            "states": states,
            "still_open": states.get(ST_OPEN, 0),
            "is_baseline": False,
            # False means the ledger's manifest did not describe this rule —
            # rendered under its raw key and badged, never silently dropped and
            # never handed a name ops made up.
            "described": described,
        })

    return {
        "fee_pct": fee,
        "baseline": baseline,
        "rules": out,
        "n_rows": len(rows),
        # How many rules were drawn. "Best of N" is not a fact about the winner
        # until N is on screen — the top row of a long table beats a coin flip
        # by construction.
        "n_cells": len(out),
    }
