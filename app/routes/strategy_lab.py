"""Strategy Lab — the autonomous portfolio's measurement surface.

Renders, on real engine data from the volume:

* the current global **market-context vector** (session / phase / volatility /
  funding / rotation) the engine publishes every audit cycle,
* the **Strategy×Context edge matrix** — every strategy (live evaluators AND
  shadow-only units) measured per context, with provenance split
  (emitted / suppressed / shadow),
* per-strategy shadow-PnL rollups,
* the **suppression gate audit** (per-gate WOULD_WIN% / EV-R / KEEP-TUNE-DROP),
* the **allocator's recommendation** — what it WOULD activate right now, and why.

All reducers are pure module-top functions (unit-testable without the app).
The Wilson edge math and thresholds are ports of the engine's
``src/strategy_edge.py`` / ``src/suppression_audit.py`` constants — ops cannot
import engine code, so the numbers are mirrored and displayed in the UI footer
for honesty.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, Request

router = APIRouter()

# ── Mirrored engine constants (src/strategy_edge.py, src/suppression_audit.py) ──
EDGE_WINDOW = 50
EDGE_MIN_SAMPLES = 15
EDGE_STRONG_R = 0.25
EDGE_POSITIVE_R = 0.05
EDGE_NEGATIVE_R = -0.05
SUPPRESSION_KEEP_EV_R = 0.10
SUPPRESSION_DROP_EV_R = -0.20
SUPPRESSION_MIN_SAMPLE = 20


def wilson_lower_bound(wins: int, total: int, z: float = 1.96) -> float:
    """Port of the engine's Wilson score lower bound (confidence_calibration)."""
    if total == 0:
        return 0.0
    p_hat = wins / total
    z2 = z * z
    denominator = 1.0 + z2 / total
    centre = p_hat + z2 / (2.0 * total)
    margin = z * ((p_hat * (1.0 - p_hat) + z2 / (4.0 * total)) / total) ** 0.5
    return max(0.0, (centre - margin) / denominator)


def _edge_r(records: list[dict]) -> Optional[float]:
    """Wilson-LB expectancy in R over a cell's records (engine parity)."""
    n = len(records)
    if n < EDGE_MIN_SAMPLES:
        return None
    wins = [r for r in records if r.get("won")]
    losses = [r for r in records if not r.get("won")]
    wr = len(wins) / n
    wlb = wilson_lower_bound(len(wins), n)
    avg_win_r = sum(float(r.get("r", 0.0)) for r in wins) / len(wins) if wins else 0.0
    avg_loss_r = (
        sum(float(r.get("r", 0.0)) for r in losses) / len(losses) if losses else 0.0
    )
    return wlb * avg_win_r + (1.0 - wr) * avg_loss_r


def _verdict(edge: Optional[float]) -> str:
    if edge is None:
        return "INSUFFICIENT_DATA"
    if edge >= EDGE_STRONG_R:
        return "STRONG"
    if edge >= EDGE_POSITIVE_R:
        return "POSITIVE"
    if edge <= EDGE_NEGATIVE_R:
        return "NEGATIVE"
    return "FLAT"


def reduce_context_card(mc: Any, now_ts: Optional[float] = None) -> dict:
    """The current-context card, with staleness so a dead publisher is visible."""
    if not isinstance(mc, dict) or mc.get("error"):
        return {"error": (mc or {}).get("error", "no data") if isinstance(mc, dict) else "bad payload"}
    now = now_ts if now_ts is not None else time.time()
    generated_at = float(mc.get("generated_at") or 0.0)
    return {
        "context_key": mc.get("context_key", ""),
        "session": mc.get("mc_session", "?"),
        "is_weekend": bool(mc.get("mc_is_weekend")),
        "session_quality": mc.get("mc_session_quality"),
        "phase": mc.get("mc_phase", "?"),
        "volatility": mc.get("mc_volatility", "?"),
        "funding": mc.get("mc_funding", "?"),
        "rotation": mc.get("mc_rotation", "?"),
        "btc_led": bool(mc.get("mc_btc_led")),
        "generated_at_iso": mc.get("generated_at_iso", ""),
        "age_sec": max(0, int(now - generated_at)) if generated_at else None,
        "stale": (now - generated_at) > 900 if generated_at else True,
    }


# Reserved top-level key in the persisted edge store holding per-cell eviction
# counts (engine: ``strategy_edge._EVICTED_KEY``).  Cell keys are
# ``"STRATEGY|context_key"``, so a key with no ``"|"`` cannot collide with one —
# which is also exactly why this reducer's own guard used to drop it silently.
EVICTED_KEY = "__evicted__"


def _evicted_map(raw_store: Any) -> Optional[dict[str, int]]:
    """Per-cell eviction counts, or ``None`` when the engine did not report any.

    ``None`` and ``{}`` are different states and must not be pooled.  The engine
    writes this key only ``if _ev`` — so an absent key means *either* an engine
    older than 2026-08-04 *or* a store in which nothing has ever been evicted,
    and neither the reducer nor the page may guess which.  A missing stamp is
    not a pass (the ``/signals/sar-live`` ``unverified`` rule): the cell renders
    as unreported rather than as unsampled.
    """
    if not isinstance(raw_store, dict):
        return None
    raw = raw_store.get(EVICTED_KEY)
    if not isinstance(raw, dict):
        return None
    out: dict[str, int] = {}
    for key, count in raw.items():
        try:
            out[str(key)] = int(count)
        except (TypeError, ValueError):
            continue
    return out


def reduce_edge_matrix(raw_store: Any, affinity: Optional[dict] = None) -> list[dict]:
    """Cells from the raw edge-store records, engine-parity edge + verdict.

    Every cell carries its **denominator**.  ``n`` is ``len(records)`` and each
    cell is a ``deque(maxlen=50)`` engine-side, so ``n = 50`` may stand for fifty
    outcomes or five thousand — and 1,731 of 3,531 verdict-carrying cells sat at
    that cap on 2026-08-07 with nothing on screen saying so.  The engine has
    counted and persisted the evictions since 2026-08-04, under a docstring
    quoting this repo's own rule (*"when a bounded buffer feeds a statistic,
    persist the eviction count with the data and put the denominator beside the
    verdict"*).  The reducer above dropped them at ``"|" not in str(key)``: a
    field one repo writes and no repo reads, #817 with the arrow reversed.
    """
    if not isinstance(raw_store, dict) or raw_store.get("error"):
        return []
    evicted_map = _evicted_map(raw_store)
    rows: list[dict] = []
    for key, records in raw_store.items():
        if not isinstance(records, list) or "|" not in str(key):
            continue
        strategy, ctx = str(key).split("|", 1)
        records = records[-EDGE_WINDOW:]
        n = len(records)
        if n == 0:
            continue
        wins = sum(1 for r in records if r.get("won"))
        avg_r = sum(float(r.get("r", 0.0)) for r in records) / n
        avg_pnl = sum(float(r.get("pnl_pct", 0.0)) for r in records) / n
        edge = _edge_r(records)
        srcs = [str(r.get("src", "emitted")) for r in records]
        aligned = _alignment(strategy, ctx, affinity)
        evicted = None if evicted_map is None else int(evicted_map.get(str(key), 0))
        rows.append({
            "strategy": strategy,
            "context_key": ctx,
            "n": n,
            "n_emitted": srcs.count("emitted"),
            "n_suppressed": srcs.count("suppressed"),
            "n_shadow": srcs.count("shadow"),
            "win_rate": wins / n,
            "avg_r": avg_r,
            "avg_pnl_pct": avg_pnl,
            "edge_r": edge,
            "verdict": _verdict(edge),
            "aligned": aligned,
            # -- the denominator, mirroring engine ``strategy_edge.sampling()`` --
            "evicted": evicted,
            "seen": None if evicted is None else n + evicted,
            # Tri-state on purpose: True = provably a sample, False = provably
            # the whole population, None = the engine did not say.  A bool here
            # would make "not reported" read as "nothing evicted", which is the
            # flattering direction and the one a reader would not question.
            "sampled": None if evicted is None else evicted > 0,
            "at_cap": n >= EDGE_WINDOW,
        })
    rows.sort(
        key=lambda r: (r["edge_r"] is not None, r["edge_r"] or 0.0, r["n"]),
        reverse=True,
    )
    return rows


def _alignment(strategy: str, context_key: str, affinity: Optional[dict]) -> Optional[bool]:
    """Alignment from the engine-persisted affinity map (no ops hardcoding)."""
    if not affinity:
        return None
    tags = affinity.get(strategy)
    if not isinstance(tags, dict):
        return None
    parts = (context_key or "").split("/")
    if len(parts) != 4:
        return None
    session, phase = parts[0], parts[1]
    phases = tags.get("phases") or []
    sessions = tags.get("sessions") or []
    if phases and phase not in phases:
        return False
    if sessions and session not in sessions:
        return False
    return True


# Stop-geometry A/B arm suffixes (engine: src/geometry_ab.py).  Rows named
# X@FIXED / X@ATR are counterfactual stop-geometry measurement arms, not
# strategies — they get their own A/B card and stay out of the strategy rollup.
GEOMETRY_SUFFIXES = ("@FIXED", "@ATR")

# EVERY measurement-arm suffix the engine can write into the edge matrix —
# the mirror of ``geometry_ab._VARIANT_SUFFIXES``.  This list had drifted: it
# carried only @FIXED/@ATR while the engine had been writing @TUNED, @DSV2 and
# @GOV rows for over a week, so those counterfactual arms were being counted in
# the per-strategy rollup **as if they were strategies** — double-counting the
# same candidates and moving the numbers the owner reads to make strategy
# decisions.  @SARBASE/@SAREXIT (2026-07-25) would have been two more.
#
# Keep this in sync with the engine tuple. A measurement arm is evidence about
# how to trade a strategy, never a strategy.
MEASUREMENT_SUFFIXES = (
    "@FIXED", "@ATR", "@TUNED", "@DSV2", "@GOV", "@SARBASE", "@SAREXIT",
)


def _is_geometry_variant(strategy: str) -> bool:
    return any(str(strategy or "").endswith(s) for s in GEOMETRY_SUFFIXES)


def is_measurement_variant(strategy: str) -> bool:
    """True for any counterfactual measurement row (never an activatable strategy)."""
    return any(str(strategy or "").endswith(s) for s in MEASUREMENT_SUFFIXES)


def _base_strategy(strategy: str) -> str:
    s = str(strategy or "")
    for sfx in MEASUREMENT_SUFFIXES:
        if s.endswith(sfx):
            return s[: -len(sfx)]
    return s


def reduce_geometry_ab(rows: list[dict], min_sample: int = EDGE_MIN_SAMPLES) -> list[dict]:
    """Per-strategy fixed-vs-ATR stop rollup (port of engine summarize_geometry_ab).

    Pools each arm's matrix cells across contexts (sample-weighted); a leader
    is named only when BOTH arms clear ``min_sample`` — one thin arm means the
    A/B is still MEASURING.  Sorted by |ΔR| so the biggest effects lead.
    """
    pooled: dict[str, dict[str, dict]] = {}
    for row in rows:
        strategy = str(row.get("strategy", ""))
        if not _is_geometry_variant(strategy):
            continue
        arm = "atr" if strategy.endswith("@ATR") else "fixed"
        n = int(row.get("n", 0) or 0)
        if n <= 0:
            continue
        agg = pooled.setdefault(_base_strategy(strategy), {}).setdefault(
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
        fixed = _arm("fixed")
        atr = _arm("atr")
        measured = fixed["n"] >= min_sample and atr["n"] >= min_sample
        delta = atr["avg_r"] - fixed["avg_r"]
        out.append({
            "strategy": base,
            "fixed": fixed,
            "atr": atr,
            "delta_r": delta if measured else None,
            "leader": (
                ("ATR" if delta > 0 else "FIXED" if delta < 0 else "TIE")
                if measured
                else "MEASURING"
            ),
        })
    out.sort(
        key=lambda r: abs(r["delta_r"]) if r["delta_r"] is not None else -1.0,
        reverse=True,
    )
    return out


def reduce_per_strategy(rows: list[dict]) -> list[dict]:
    """Aggregate matrix cells per strategy (sample-weighted).

    **Every** measurement arm is excluded — geometry (@FIXED/@ATR), tuned
    recipes (@TUNED), dispatch-rescue arms (@DSV2/@GOV) and the exit A/B
    (@SARBASE/@SAREXIT). They are counterfactuals stamped from the same
    candidates as the real rows, so counting them here double-counts the
    candidate and shifts the per-strategy numbers. Each rolls up in its own
    card instead.
    """
    agg: dict[str, dict] = {}
    for row in rows:
        if is_measurement_variant(row.get("strategy", "")):
            continue
        a = agg.setdefault(row["strategy"], {
            "strategy": row["strategy"],
            "n": 0, "n_emitted": 0, "n_suppressed": 0, "n_shadow": 0,
            "cells": 0, "_win": 0.0, "_r": 0.0, "best_cell": None,
        })
        n = row["n"]
        a["n"] += n
        a["n_emitted"] += row["n_emitted"]
        a["n_suppressed"] += row["n_suppressed"]
        a["n_shadow"] += row["n_shadow"]
        a["cells"] += 1
        a["_win"] += row["win_rate"] * n
        a["_r"] += row["avg_r"] * n
        if row["edge_r"] is not None and (
            a["best_cell"] is None or row["edge_r"] > a["best_cell"]["edge_r"]
        ):
            a["best_cell"] = {"context_key": row["context_key"], "edge_r": row["edge_r"]}
    out = []
    for a in agg.values():
        n = a["n"]
        a["win_rate"] = (a.pop("_win") / n) if n else 0.0
        a["avg_r"] = (a.pop("_r") / n) if n else 0.0
        out.append(a)
    out.sort(key=lambda a: a["n"], reverse=True)
    return out


def _r_to_tp1(record: dict) -> float:
    entry = float(record.get("entry") or 0.0)
    tp1 = float(record.get("tp1") or 0.0)
    sl_distance = float(record.get("sl_distance") or 0.0)
    if sl_distance <= 0:
        return 0.0
    return abs(tp1 - entry) / sl_distance


def _suppression_delta_r(record: dict) -> Optional[float]:
    cls = record.get("classification")
    if cls == "WOULD_LOSE":
        return 1.0
    if cls == "WOULD_WIN":
        return -_r_to_tp1(record)
    if cls == "WOULD_EXPIRE":
        return 0.0
    return None


def reduce_gate_metrics(records: Any) -> dict:
    """Per-gate suppression audit — port of compute_gate_suppression_metrics."""
    if not isinstance(records, list):
        return {"by_gate": {}, "pending": 0, "classified": 0}
    by_gate: dict[str, dict] = {}
    pending = 0
    classified = 0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        cls = rec.get("classification")
        if cls in (None, "INSUFFICIENT_DATA"):
            pending += 1
            continue
        classified += 1
        gate = str(rec.get("gate_name") or "unknown")
        g = by_gate.setdefault(gate, {
            "WOULD_WIN": 0, "WOULD_LOSE": 0, "WOULD_EXPIRE": 0,
            "saved_r": 0.0, "missed_r": 0.0,
            # A pre-scoring gate (setup_compat / execution) fires BEFORE the
            # scoring engine, so its rows carry the evaluator's confidence and
            # never a scored one, and the engine keeps them out of the edge
            # matrix. Ranking them in one list beside post-scoring gates without
            # saying so would invite a comparison between two differently
            # selected populations — the rows are right, the reading would not
            # be. Flagged per gate rather than inferred from the name, so a
            # renamed gate cannot silently lose its badge.
            "pre_scoring": False,
        })
        if rec.get("pre_scoring"):
            g["pre_scoring"] = True
        g[cls] = g.get(cls, 0) + 1
        ev = _suppression_delta_r(rec)
        if ev is None:
            continue
        if ev > 0:
            g["saved_r"] += ev
        elif ev < 0:
            g["missed_r"] += -ev
    for g in by_gate.values():
        n = g["WOULD_WIN"] + g["WOULD_LOSE"] + g["WOULD_EXPIRE"]
        g["n"] = n
        g["would_win_pct"] = (g["WOULD_WIN"] / n * 100.0) if n else 0.0
        ev_per = ((g["saved_r"] - g["missed_r"]) / n) if n else 0.0
        g["ev_per_suppression_r"] = ev_per
        if n < SUPPRESSION_MIN_SAMPLE:
            g["verdict"] = "INSUFFICIENT_SAMPLE"
        elif ev_per >= SUPPRESSION_KEEP_EV_R:
            g["verdict"] = "KEEP"
        elif ev_per <= SUPPRESSION_DROP_EV_R:
            g["verdict"] = "DROP"
        else:
            g["verdict"] = "TUNE"
    return {"by_gate": by_gate, "pending": pending, "classified": classified}


def reduce_path_silence(records: Any) -> list[dict]:
    """Per setup: which gate is stopping this path, and is that gate right?

    The per-gate table pools every setup into one row, so it cannot answer the
    question the delivered book actually poses. On 2026-07-31 the last 100
    delivered signals were **76% MOVER_\*** — ``SR_FLIP_RETEST`` shipped one
    signal in nine days, ``RANGE_FADE`` and ``MEAN_REVERT`` zero — and the
    reason is not one global gate. It is per path, and it splits three ways:
    the detector never fires, the regime setup-compat gate confines it, or its
    counterfactuals measure negative and the gate is doing its job.

    ``MOVER_TREND_PULLBACK`` takes **zero** ``setup_compat:regime_*`` rejects
    because a mover is defined by its own move rather than by the market's
    state — it is the one path legal in every regime, which is why it is the
    one path with volume. This table is where that asymmetry becomes visible.

    Sorted by suppressions descending: the paths losing the most candidates
    first, since those are where a gate change moves the feed.
    """
    if not isinstance(records, list):
        return []
    by_setup: dict[str, dict] = {}
    for rec in records:
        if not isinstance(rec, dict):
            continue
        cls = rec.get("classification")
        setup = str(rec.get("setup_class") or "UNKNOWN")
        gate = str(rec.get("gate_name") or "unknown")
        row = by_setup.setdefault(setup, {
            "setup_class": setup, "n": 0, "classified": 0, "would_win": 0,
            "gates": {}, "pre_scoring_only": True,
        })
        row["n"] += 1
        if not rec.get("pre_scoring"):
            row["pre_scoring_only"] = False
        row["gates"][gate] = row["gates"].get(gate, 0) + 1
        if cls in (None, "INSUFFICIENT_DATA"):
            continue
        row["classified"] += 1
        if cls == "WOULD_WIN":
            row["would_win"] += 1
    out = []
    for row in by_setup.values():
        top = sorted(row["gates"].items(), key=lambda kv: -kv[1])
        row["top_gate"], row["top_gate_n"] = top[0] if top else ("—", 0)
        row["top_gate_share"] = (row["top_gate_n"] / row["n"] * 100.0) if row["n"] else 0.0
        row["gate_count"] = len(row["gates"])
        # Refuse rather than clamp: a path with nothing classified has no
        # would-win rate, and printing 0.0% would read as "none of these would
        # have won" when it means "we do not know yet".
        row["would_win_pct"] = (
            (row["would_win"] / row["classified"] * 100.0) if row["classified"] else None
        )
        out.append(row)
    out.sort(key=lambda r: -r["n"])
    return out


def reduce_allocations(alloc: Any, now_ts: Optional[float] = None) -> dict:
    """The allocator's would-do panel, with staleness."""
    if not isinstance(alloc, dict) or alloc.get("error"):
        return {"error": (alloc or {}).get("error", "no data") if isinstance(alloc, dict) else "bad payload"}
    now = now_ts if now_ts is not None else time.time()
    generated_at = float(alloc.get("generated_at") or 0.0)
    return {
        "mode": alloc.get("mode", "?"),
        "context_key": alloc.get("context_key", ""),
        "activate": alloc.get("activate") or [],
        "demote": alloc.get("demote") or [],
        "unallocated_weight": alloc.get("unallocated_weight"),
        "limits": alloc.get("limits") or {},
        "cells_in_context": alloc.get("cells_in_context", 0),
        "generated_at_iso": alloc.get("generated_at_iso", ""),
        "age_sec": max(0, int(now - generated_at)) if generated_at else None,
        "stale": (now - generated_at) > 900 if generated_at else True,
    }


#: Render bound for the edge matrix, applied after the verdict split and after
#: sorting — never inside a reducer (#97). The export stays uncapped, because a
#: truncated download is the same defect wearing a button.
MATRIX_ROW_CAP = 400


def matrix_sampling(rows: list[dict]) -> dict:
    """How much of the matrix is a rolling sample rather than a population.

    Measured over the WHOLE matrix, never the capped render — a sampling summary
    computed on "the 400 cells with the most evidence" would describe exactly the
    cells most likely to be at the cap and would read far worse than the truth.

    ``reported`` is the honest gate on every figure here: when the engine wrote
    no eviction map, this panel says so and states nothing else.  Reporting
    "0 evicted" from an absent key is how a missing measurement comes to read as
    a clean result.
    """
    reported = any(r.get("evicted") is not None for r in rows)
    decidable = [r for r in rows if r.get("verdict") != "INSUFFICIENT_DATA"]
    at_cap = [r for r in decidable if r.get("at_cap")]
    sampled = [r for r in decidable if r.get("sampled")]
    evicted_total = sum(int(r.get("evicted") or 0) for r in rows)
    held_total = sum(int(r.get("n") or 0) for r in rows)
    return {
        "reported": reported,
        "window": EDGE_WINDOW,
        "decidable": len(decidable),
        "at_cap": len(at_cap),
        "sampled": len(sampled) if reported else None,
        "evicted_total": evicted_total if reported else None,
        "held_total": held_total,
        # The share of observed outcomes the matrix can still see. A cell's
        # verdict is computed on `held`; everything in `evicted` shaped the
        # strategy's history and is unreachable to every reader of this page.
        "visible_frac": (
            held_total / (held_total + evicted_total)
            if reported and (held_total + evicted_total) else None
        ),
    }


def split_matrix(rows: list[dict], *, show_all: bool = False) -> dict:
    """Decision-grade cells first, and say what was withheld.

    The matrix had **9,261 cells on 2026-08-06 and 5,766 of them —
    62% — read INSUFFICIENT_DATA**: a cell below ``EDGE_MIN_SAMPLES`` carries no
    verdict by construction, so those rows are the page telling the reader
    nothing, 5,766 times, in 3.9 MB of HTML. Nothing was wrong with any number;
    the page was simply unreadable, which for a surface whose whole job is to be
    read is the same as being broken.

    Two rules this follows rather than invents:

    * **The cap is a render bound.** It applies here, after the split and the
      sort, and the page says when it bit — never inside a reducer, which is how
      `/signals/sar` came to filter the newest 300 rows of a 2,000-row ledger.
    * **The sort must not manufacture a leaderboard.** Sorting by edge puts the
      best-looking cell of thousands on the top line, and "best of N" is not a
      fact about the winner until N is on screen. So the default order is **most
      evidence first** (``n`` descending); the cells that a decision could rest
      on come first for the reason that they are the cells a decision could rest
      on, not because they flatter.
    """
    decidable = [r for r in rows if r.get("verdict") != "INSUFFICIENT_DATA"]
    thin = len(rows) - len(decidable)
    shown = rows if show_all else decidable
    shown = sorted(shown, key=lambda r: (-(r.get("n") or 0), str(r.get("strategy") or "")))
    capped = shown[:MATRIX_ROW_CAP]
    return {
        "rows": capped,
        "total": len(rows),
        "decidable": len(decidable),
        "thin": thin,
        "shown": len(capped),
        "cap_bit": len(shown) > len(capped),
        "cap": MATRIX_ROW_CAP,
        "show_all": show_all,
    }


def _build_view(vol, *, show_all: bool = False) -> dict:
    mc_raw = vol.market_context()
    affinity = mc_raw.get("strategy_affinity") if isinstance(mc_raw, dict) else None
    matrix_rows = reduce_edge_matrix(vol.strategy_edge(), affinity)
    return {
        "context": reduce_context_card(mc_raw),
        # Every panel below still reduces over the WHOLE matrix — the split is a
        # render bound on one table, not a filter on the page's arithmetic. A
        # rollup measured on the capped rows would silently become a rollup of
        # "the 400 cells with the most evidence".
        "matrix": split_matrix(matrix_rows, show_all=show_all),
        "matrix_sampling": matrix_sampling(matrix_rows),
        "matrix_rows": matrix_rows,
        "per_strategy": reduce_per_strategy(matrix_rows),
        "geometry": reduce_geometry_ab(matrix_rows),
        "gates": reduce_gate_metrics(vol.suppressed_candidates()),
        "path_silence": reduce_path_silence(vol.suppressed_candidates()),
        "allocations": reduce_allocations(vol.strategy_allocations()),
        "thresholds": {
            "edge_min_samples": EDGE_MIN_SAMPLES,
            "edge_strong_r": EDGE_STRONG_R,
            "edge_positive_r": EDGE_POSITIVE_R,
            "edge_negative_r": EDGE_NEGATIVE_R,
            "keep_ev_r": SUPPRESSION_KEEP_EV_R,
            "drop_ev_r": SUPPRESSION_DROP_EV_R,
            "suppression_min_sample": SUPPRESSION_MIN_SAMPLE,
        },
    }


@router.get("/strategy-lab")
async def strategy_lab(request: Request, show: str = ""):
    templates = request.app.state.templates
    ctx = _build_view(request.app.state.data_volume, show_all=show == "all")
    ctx.update({"request": request, "active": "strategy_lab"})
    return templates.TemplateResponse("strategy_lab.html", ctx)


@router.get("/_partial/strategy_lab")
async def strategy_lab_partial(request: Request, show: str = ""):
    templates = request.app.state.templates
    ctx = _build_view(request.app.state.data_volume, show_all=show == "all")
    ctx.update({"request": request})
    return templates.TemplateResponse("_strategy_lab_tables.html", ctx)


# ``evicted``/``seen``/``sampled`` ride into the CSV for the same reason the
# structural lanes' stamps do: a spreadsheet is exactly where a rolling 50-row
# window gets averaged against an all-time one without anybody noticing.
_MATRIX_COLS = [
    "strategy", "context_key", "n", "n_emitted", "n_suppressed", "n_shadow",
    "win_rate", "avg_r", "avg_pnl_pct", "edge_r", "verdict", "aligned",
    "evicted", "seen", "sampled",
]


@router.get("/strategy-lab/export.csv")
async def strategy_lab_export_csv(request: Request):
    """The Strategy×Context edge matrix as a flat CSV — one row per cell."""
    from app.reports import csv_response

    rows = _build_view(request.app.state.data_volume).get("matrix_rows", [])
    data = [[r.get(c) for c in _MATRIX_COLS] for r in rows]
    return csv_response("strategy_lab_matrix", _MATRIX_COLS, data)


@router.get("/strategy-lab/export.json")
async def strategy_lab_export_json(request: Request):
    """The full Strategy Lab view (matrix + per-strategy + gates + allocator) as JSON."""
    from app.reports import json_response

    return json_response("strategy_lab", _build_view(request.app.state.data_volume))
