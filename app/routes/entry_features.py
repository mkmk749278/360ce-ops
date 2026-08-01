"""Entry features — what MVRTP could have looked at, and whether it would help.

Owner, 2026-08-01: *"taking entry is matter, how we are taking entry based on
only EMA or what, what if we add some more data to that"* and *"we need to know
the difference as of now vs later"*.

``_evaluate_mover_trend_pullback`` decides on price against three simple moving
averages plus one ATR. It reads the 15m volume series and hands it only to
``_mover_consol_break`` — the two triggers carrying almost all the volume,
``fast_pullback`` and ``deep_pullback``, never look at volume at all. Meanwhile
``smc_data`` arrives at that call carrying CVD, order-book depth, funding,
liquidation clusters and the level book, and the path touches none of it.

Engine ``src/entry_features.py`` now records those inputs at the moment each
MVRTP signal is created and applies **none** of them. This page is where they
become readable.

What this page is careful about
-------------------------------
* **Nothing here is applied.** Every "keep" column is a question, not a change.
  The engine emits the identical signal whether this lane is on or off, and a
  test on the producing side pins that.
* **Three buckets, never two.** ``keep`` / ``drop`` / ``unknown``. A rule cannot
  be credited with an outcome it could not have seen, and folding the rows whose
  feature never computed into ``keep`` is exactly how a filter takes credit for
  rows it never filtered.
* **Both denominators, as everywhere else here.** R divides by the engine's
  ``sl_distance_pct_at_entry`` (#848) — the risk the trade was *sized* for, not
  the stop it exited on. But the R-scored population is not a random sample of
  the book (rows closed before that field existed carry no denominator), so the
  gross ``%`` is published beside every R rather than instead of it.
* **A rule that keeps everything has not been tested.** ``kept_fraction`` is on
  screen for every row, because a threshold nothing crosses will show a
  flattering delta forever.
* **Thin cells are named as thin.** The window that prompted this work had 46
  MVRTP signals and 19 tested cells, one of which cleared 95% in the *backwards*
  direction against a ~62% familywise chance of a spurious hit. The page states
  n on every split so the same mistake cannot be made silently twice.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query, Request

from app.reports import csv_response

router = APIRouter()

#: Features the engine stamps, with the direction a candidate rule would filter
#: in and a plain-language statement of what the split is actually asking.
#: ``keep_above`` mirrors ``entry_features._KEEP_ABOVE`` on the engine side —
#: ops ports the engine's math, it does not invent it.
FEATURES: dict[str, dict[str, Any]] = {
    "pullback_vol_ratio": {
        "label": "Pullback volume ratio",
        "keep_above": True,
        "default": 0.8,
        "asks": "Was the dip actually traded, or did participation just dry up? "
                "The two primary triggers never look at volume.",
    },
    "cvd_slope": {
        "label": "CVD slope across the pullback",
        "keep_above": True,
        "default": 0.0,
        "asks": "Was the dip absorbed (positive) or sold into (negative)? "
                "Sign is the whole question.",
    },
    "pullback_depth_atr": {
        "label": "Pullback depth (ATR)",
        "keep_above": False,
        "default": 3.0,
        "asks": "A 1-ATR dip and a 3-ATR dip both 'tag SMA7 and reclaim'. "
                "Nothing downstream can currently tell them apart.",
    },
    "extension_pct": {
        "label": "Extension from SMA99 (%)",
        "keep_above": False,
        "default": 15.0,
        "asks": "consol_break guards against chasing an extended move; the two "
                "triggers that carry the volume do not.",
    },
    "level_dist_r": {
        "label": "Distance to opposing level (R)",
        "keep_above": True,
        "default": 1.0,
        "asks": "TP1 sits at exactly 1.0R by construction. Is the target behind "
                "a level the geometry never knew about?",
    },
    "book_imbalance": {
        "label": "Top-of-book imbalance",
        "keep_above": True,
        "default": 0.0,
        "asks": "Was there bid behind the reclaim, or was it thin?",
    },
    "funding_rate": {
        "label": "Funding rate",
        "keep_above": False,
        "default": 0.0005,
        "asks": "Is the side already crowded and paying for it?",
    },
}


def _f(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        out = float(value)
        return None if out != out else out
    except (TypeError, ValueError):
        return None


def join_outcomes(stamps: Any, records: Any) -> tuple[list[dict], dict]:
    """Join entry stamps to closed-signal records on ``signal_id``.

    A port of the engine's ``entry_features.join_outcomes``, kept here because
    ops renders it — same keys, same denominator, same coverage counters. If a
    number here disagrees with the engine, ops is wrong.

    Coverage is not decoration. A stamp with no record is a signal the router
    dropped or one still open; a record with no stamp predates this lane. Both
    must be visible, because a join that silently keeps only what matched
    reports a book that is not the book.
    """
    recs = records if isinstance(records, list) else []
    rows = stamps if isinstance(stamps, list) else []
    by_id = {
        str(r.get("signal_id") or ""): r for r in recs if isinstance(r, dict)
    }
    joined: list[dict] = []
    unmatched = 0
    for s in rows:
        if not isinstance(s, dict):
            continue
        rec = by_id.get(str(s.get("signal_id") or ""))
        if rec is None:
            unmatched += 1
            continue
        pnl = _f(rec.get("pnl_pct"))
        sl_pct = _f(rec.get("sl_distance_pct_at_entry"))
        row = dict(s)
        row.update({
            "pnl_pct": pnl,
            "sl_distance_pct_at_entry": sl_pct,
            # #848: divide by the risk the trade was SIZED for. The record's
            # `stop_loss` is the stop as of the exit and would score a
            # BE-shifted scratch as a full -1.00R loss.
            "r": (pnl / sl_pct) if (pnl is not None and sl_pct and sl_pct > 0) else None,
            "outcome_label": str(rec.get("outcome_label") or ""),
            "symbol": str(rec.get("symbol") or s.get("symbol") or ""),
            # The closed-signal record is authoritative: the scanner finalises
            # entry_regime in _populate_signal_context, which runs after the
            # evaluator that produced the stamp. The stamp carries the
            # evaluator's own view and is the fallback. Where the two disagree
            # the scanner reclassified between evaluation and dispatch — that
            # is information, not a conflict, so neither overwrites the other
            # in the ledger.
            "entry_regime": str(
                rec.get("entry_regime") or s.get("entry_regime") or ""
            ) or "UNPLACED",
        })
        joined.append(row)
    return joined, {
        "stamps": len(rows),
        "joined": len(joined),
        "stamped_not_closed": unmatched,
        "scored": sum(1 for r in joined if r.get("r") is not None),
    }


def _agg(rows: list[dict]) -> dict:
    rs = [v for v in (_f(r.get("r")) for r in rows) if v is not None]
    ps = [v for v in (_f(r.get("pnl_pct")) for r in rows) if v is not None]
    return {
        "n": len(rows),
        "scored": len(rs),
        "win_rate": (sum(1 for v in rs if v > 0) / len(rs)) if rs else None,
        "avg_r": (sum(rs) / len(rs)) if rs else None,
        "total_r": sum(rs) if rs else None,
        "avg_pnl_pct": (sum(ps) / len(ps)) if ps else None,
        "total_pnl_pct": sum(ps) if ps else None,
    }


def split_by_feature(joined: list[dict], feature: str, threshold: float) -> dict:
    """The book as it shipped, beside the book one candidate rule would produce.

    ``now`` is every joined row. ``keep`` is what the rule lets through, ``drop``
    what it removes, ``unknown`` what it could not judge — reported separately,
    never merged into either side.
    """
    spec = FEATURES.get(feature) or {}
    keep_above = bool(spec.get("keep_above", True))
    keep, drop, unknown = [], [], []
    for row in joined:
        val = _f(row.get(feature))
        if val is None:
            unknown.append(row)
        elif (val >= threshold) if keep_above else (val <= threshold):
            keep.append(row)
        else:
            drop.append(row)
    now_a, keep_a = _agg(joined), _agg(keep)
    delta = (
        keep_a["avg_r"] - now_a["avg_r"]
        if keep_a["avg_r"] is not None and now_a["avg_r"] is not None
        else None
    )
    delta_pnl = (
        keep_a["avg_pnl_pct"] - now_a["avg_pnl_pct"]
        if keep_a["avg_pnl_pct"] is not None and now_a["avg_pnl_pct"] is not None
        else None
    )
    return {
        "feature": feature,
        "label": spec.get("label", feature),
        "asks": spec.get("asks", ""),
        "threshold": threshold,
        "direction": "≥" if keep_above else "≤",
        "now": now_a, "keep": keep_a, "drop": _agg(drop), "unknown": _agg(unknown),
        "delta_avg_r": delta,
        "delta_avg_pnl_pct": delta_pnl,
        "kept_fraction": (keep_a["n"] / now_a["n"]) if now_a["n"] else None,
    }


def _rows_and_coverage(request: Request) -> tuple[list[dict], dict, Optional[str]]:
    dv = request.app.state.data_volume
    try:
        payload = dv.entry_features()
        perf = dv.signal_performance()
    except Exception as exc:  # noqa: BLE001
        return [], {}, f"engine data volume unavailable: {exc}"
    if not isinstance(payload, dict):
        return [], {}, None
    stamps = payload.get("rows")
    joined, coverage = join_outcomes(stamps, perf)
    coverage["written_at"] = payload.get("written_at")
    coverage["schema"] = payload.get("schema")
    return joined, coverage, None


@router.get("/signals/entry-features")
async def entry_features_page(request: Request, setup: str = Query("")):
    joined, coverage, error = _rows_and_coverage(request)
    if setup:
        joined = [r for r in joined if str(r.get("setup_class") or "") == setup]

    splits = [
        split_by_feature(joined, name, float(spec["default"]))
        for name, spec in FEATURES.items()
    ]
    # Rank by how much of the book a rule would remove, not by delta: a rule
    # that drops two rows and looks brilliant is the FAR mistake in a new suit.
    splits.sort(key=lambda s: (s["delta_avg_r"] is None, -(s["delta_avg_r"] or 0.0)))

    missing_counts: dict[str, int] = {}
    for row in joined:
        for key in row.get("missing") or []:
            missing_counts[key] = missing_counts.get(key, 0) + 1

    return request.app.state.templates.TemplateResponse(
        "entry_features.html",
        {
            "request": request,
            "error": error,
            "coverage": coverage,
            "splits": splits,
            "rows": joined[:200],
            "row_cap_bit": len(joined) > 200,
            "total_rows": len(joined),
            "missing_counts": missing_counts,
            "features": FEATURES,
            "filter_setup": setup,
        },
    )


@router.get("/signals/entry-features/export.csv")
async def entry_features_export(request: Request):
    joined, _cov, _err = _rows_and_coverage(request)
    cols = [
        "signal_id", "symbol", "side", "entry_trigger", "confidence",
        "entry_regime", "sl_dist_pct", "stack_sep_pct", "profile_would_reject",
        *FEATURES.keys(),
        "liq_clusters_n", "pnl_pct", "sl_distance_pct_at_entry", "r",
        "outcome_label", "stamped_at",
    ]
    return csv_response(
        "entry_features",
        cols,
        [[r.get(c) for c in cols] for r in joined],
    )
