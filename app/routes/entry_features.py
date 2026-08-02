"""Entry features — what each path could have looked at, and whether it would help.

Owner, 2026-08-01: *"taking entry is matter, how we are taking entry based on
only EMA or what, what if we add some more data to that"*, then on the dark feed:
*"we need to concentrate on entry, on which bases entry is confirming especially
on Trend pullback EMA and mover AVWAP"*.

The engine stamps those inputs at the moment each signal is created and applies
**none** of them. This page is where they become readable.

Per path, because the paths are not blind in the same place
-----------------------------------------------------------
The first cut of the engine lane copied MVRTP's feature list onto every path,
and it was wrong for a reason this page has to keep visible: that list was chosen
for MVRTP's blindness — a three-SMA pullback trigger that never looks at volume.
The others fail elsewhere.

* **TREND_PULLBACK_EMA** applies nothing but booleans — EMA21 tagged, close back
  above both EMAs, close > prev_close, close > prev_high, RSI in 40–60 and
  rising. It records *that* each threshold was crossed and never by how much, so
  its features are the magnitudes behind its own gates.
* **MOVER_AVWAP_SCALP** already gates on volume and AVWAP slope. What it has no
  notion of is *where in the move it is*: the anchor is computed and then used
  only to produce a VWAP, so its age, the leg's size, and how many times price
  has already returned to it are unconsulted.

So each row carries a different feature set, and the selector at the top of the
page is the primary control rather than a filter.

**The registry is not mirrored here.** Which features a path declares, in what
order, and which way a rule filters all arrive in the ledger's ``spec`` block,
written by the engine that decides them. Ops kept its own copy of
``MEASUREMENT_SUFFIXES`` once and it drifted for a week; the fix for a drifting
mirror is not a second mirror. ``FALLBACK_SPEC`` below exists only so a
pre-``spec`` ledger still renders, and the page says on screen when it is used.

What this page is careful about
-------------------------------
* **Nothing here is applied.** Every "keep" column is a question, not a change.
  The engine emits the identical signal whether this lane is on or off, and tests
  on the producing side drive the real evaluators to pin that.
* **Three buckets, never two.** ``keep`` / ``drop`` / ``unknown``. A rule cannot
  be credited with an outcome it could not have seen, and folding the rows whose
  feature never computed into ``keep`` is exactly how a filter takes credit for
  rows it never filtered.
* **Never pool timeframes silently.** TPE triggers on 5m and the mover paths on
  15m. A volume ratio over 5m bars and one over 15m bars are different
  measurements, so every split reports the timeframes it covered and says so
  when there is more than one.
* **Both denominators, as everywhere else here.** R divides by the engine's
  ``sl_distance_pct_at_entry`` (#848) — the risk the trade was *sized* for, not
  the stop it exited on. But the R-scored population is not a random sample of
  the book (rows closed before that field existed carry no denominator), so the
  gross ``%`` is published beside every R rather than instead of it.
* **A rule that keeps everything has not been tested.** ``kept_fraction`` is on
  screen for every row, because a threshold nothing crosses will show a
  flattering delta forever.
* **Thin cells are named as thin, and counted.** The window that prompted this
  work had 46 MVRTP signals and 19 tested cells, one of which cleared 95% in the
  *backwards* direction against a ~62% familywise chance of a spurious hit. The
  page states n on every split **and how many cells it drew**, because "best of
  N" is not a fact about the winner until N is on screen.
"""
from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Query, Request

from app.reports import csv_response

router = APIRouter()

#: Display metadata: a label, a starting threshold, and what the split is
#: actually asking. Copy only — **the split direction is not here**, because that
#: is engine math and arrives in the ledger's ``spec``.
#:
#: A feature the engine stamps but this table does not describe still renders,
#: under its raw name. Silently dropping it would make a new engine feature
#: invisible on the page that exists to read it.
FEATURE_COPY: dict[str, dict[str, Any]] = {
    # ── Core: true of every path by construction ──────────────────────────────
    "tp1_r_multiple": {
        "label": "Designed reward:risk at TP1",
        "default": 1.0,
        "asks": "Read this first. It is chosen by the evaluator, exact at stamp "
                "time, and it bounds what any entry filter can achieve — a path "
                "whose TP1 sits inside 1R needs a win rate above 50% before any "
                "of the columns below have done anything.",
    },
    "entry_ref_dist_atr": {
        "label": "Distance from the level entered against (ATR)",
        "default": 1.0,
        "asks": "'Price reclaimed the level' is true one tick past it and true "
                "again two ATR past it. Only one of those is the setup.",
    },
    "entry_bar_range_atr": {
        "label": "Trigger bar range (ATR)",
        "default": 2.0,
        "asks": "Did the entry bar behave, or was this a spike?",
    },
    "close_position_in_bar": {
        "label": "Close position in trigger bar",
        "default": 0.5,
        "asks": "TREND_PULLBACK_EMA already gates this at 0.50 and calls it "
                "body conviction. No other path tests it and nothing recorded "
                "it, so the threshold has never been checked against outcomes.",
    },
    "pullback_vol_ratio": {
        "label": "Pullback volume ratio",
        "default": 0.8,
        "asks": "Was the dip actually traded, or did participation dry up?",
    },
    "cvd_slope_aligned": {
        "label": "CVD slope, signed toward the trade",
        "default": 0.0,
        "asks": "Positive means the move was absorbed in this trade's favour. "
                "Signed by direction, because a falling CVD is bad for a long "
                "and exactly what a short wants.",
    },
    "level_dist_r": {
        "label": "Distance to opposing level (R)",
        "default": 1.0,
        "asks": "Is the target behind a level the geometry never knew about?",
    },
    "book_imbalance_aligned": {
        "label": "Book imbalance, signed toward the trade",
        "default": 0.0,
        "asks": "Was there depth behind the entry, or was it thin?",
    },
    # ── TREND_PULLBACK_EMA: the magnitudes behind its own boolean gates ───────
    "retrace_frac_of_leg": {
        "label": "Retrace of the impulse leg",
        "default": 0.6,
        "asks": "A 30% giveback is the setup; a 90% giveback is a trend that has "
                "already failed. 'Tagged EMA21 and closed back above' is true of "
                "both, and this path asks nothing else.",
    },
    "h1_trend_sep_atr": {
        "label": "1H EMA21/50 separation (ATR)",
        "default": 0.5,
        "asks": "Direction comes from EMA21 vs EMA50 with no notion of how far "
                "apart they are. A barely-crossed pair and a widely separated "
                "one are the same input to this evaluator.",
    },
    "smc_zone_dist_atr": {
        "label": "Distance to nearest fair-value gap (ATR)",
        "default": 2.0,
        "asks": "The measurement this path's SMC gate claims to make — and, "
                "measured, the gate turns out to be making it. The code tests "
                "bool(fvgs), which on paper any zone at any price satisfies; "
                "across 89 signals the nearest gap sat at a median 0.13 ATR and "
                "a maximum of 0.52, because detect_fvg only looks back ~12 bars "
                "and a gap that recent is still near price. No candidate rule "
                "on this column can discriminate; it is here as the check that "
                "would catch the gate drifting. Orderblocks are never populated "
                "engine-wide, so this reads fair-value gaps only.",
    },
    "rsi_at_entry": {
        "label": "RSI at entry",
        "default": 55.0,
        "asks": "The gate is 40–60 and rising. Where inside the band it fired "
                "has never been recorded, so the band's edges are untested.",
    },
    "prev_extreme_break_atr": {
        "label": "Break of previous bar's extreme (ATR)",
        "default": 0.2,
        "asks": "The trigger requires close > prev_high. By how much is the "
                "difference between a break and a nudge.",
    },
    "uses_1h_trend": {
        "label": "Used the 1H trend path (1) or the 5m fallback (0)",
        "default": 1.0,
        "asks": "Not a filter — a split. The 1H path and the legacy 5m-regime "
                "fallback are different strategies sharing one setup_class, and "
                "no artifact has ever distinguished them.",
    },
    # ── MOVER_AVWAP_SCALP: where in the move the entry was taken ─────────────
    "anchor_age_bars": {
        "label": "Anchor age (bars)",
        "default": 40.0,
        "asks": "How old the leg is. The anchor's whole meaning depends on it, "
                "and the evaluator uses the anchor only to compute a VWAP.",
    },
    "leg_move_pct": {
        "label": "Leg move already travelled (%)",
        "default": 12.0,
        "asks": "execution:overextended is the gate carried past on 21 of the 65 "
                "dark rows, and this is the quantity that gate is about. The "
                "path floors it and never caps it.",
    },
    "avwap_touches_in_leg": {
        "label": "Prior returns to the anchor",
        "default": 3.0,
        "asks": "The first return is the reload the strategy is named for; the "
                "fourth is a level that keeps failing to hold.",
    },
    "avwap_slope_pct": {
        "label": "AVWAP slope (%)",
        "default": 0.0,
        "asks": "Gated against a floor only, so its magnitude has never been "
                "read against outcomes.",
    },
    "vol_ratio_at_trigger": {
        "label": "Volume ratio at the trigger bar",
        "default": 1.5,
        "asks": "The exact ratio vol_ok thresholds on — so the live threshold "
                "becomes checkable rather than trusted.",
    },
    # ── Shared / other paths ────────────────────────────────────────────────
    "stack_sep_pct": {
        "label": "MA stack separation (%)",
        "default": 1.0,
        "asks": "The mover path's own trend-strength proxy, gated at a floor.",
    },
    "extension_pct": {
        "label": "Extension from the slow MA (%)",
        "default": 15.0,
        "asks": "consol_break guards against chasing an extended move; the two "
                "triggers that carry the volume do not.",
    },
    "pullback_depth_atr": {
        "label": "Pullback depth (ATR)",
        "default": 3.0,
        "asks": "A 1-ATR dip and a 3-ATR dip both tag and reclaim.",
    },
    "sigma_at_entry": {
        "label": "Z-score at entry",
        "default": 2.5,
        "asks": "The entry here IS an extension measurement, so this is the "
                "thesis rather than a hypothesis — and the 2.5σ trigger has "
                "never been read against outcomes.",
    },
    "edge_touches": {
        "label": "Range-edge touches",
        "default": 4.0,
        "asks": "A range edge is only an edge while it holds.",
    },
    "range_width_atr": {
        "label": "Range width (ATR)",
        "default": 4.0,
        "asks": "A 4-ATR range and a 12-ATR range are different trades with the "
                "same geometry.",
    },
    "funding_rate": {
        "label": "Funding rate",
        "default": 0.0005,
        "asks": "Is the side already crowded and paying for it? Deliberately "
                "unsigned — 'crowded long' is not automatically good for a short.",
    },
}

#: Used only when the ledger predates the engine shipping its own ``spec``.
#: Deliberately minimal: enough to render, not a second registry to maintain.
#: The page states when it falls back, because a silent fallback is a mirror
#: that nobody knows is a mirror.
FALLBACK_SPEC: dict[str, Any] = {
    "core": [
        "tp1_r_multiple", "entry_ref_dist_atr", "entry_bar_range_atr",
        "close_position_in_bar", "pullback_vol_ratio", "cvd_slope_aligned",
        "level_dist_r", "book_imbalance_aligned",
    ],
    "paths": {},
    "keep_above": [
        "pullback_vol_ratio", "level_dist_r", "cvd_slope_aligned",
        "book_imbalance_aligned", "tp1_r_multiple", "close_position_in_bar",
        "h1_trend_sep_atr", "prev_extreme_break_atr", "vol_ratio_at_trigger",
        "avwap_slope_pct", "stack_sep_pct", "sigma_at_entry", "range_width_atr",
    ],
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


def features_for(spec: dict, setup: str) -> list[str]:
    """Core plus this path's own, in the engine's declared order.

    With no setup selected the union is returned, ordered core-first — but the
    page then says so, because a split drawn across paths that do not share a
    trigger, a timeframe or a stop geometry is a number whose value moves with
    the setup mix rather than with the feature.
    """
    core = list(spec.get("core") or [])
    paths = spec.get("paths") or {}
    if setup:
        return core + list(paths.get(setup) or [])
    seen, out = set(core), list(core)
    for names in paths.values():
        for name in names or []:
            if name not in seen:
                seen.add(name)
                out.append(name)
    return out


def split_by_feature(
    joined: list[dict], feature: str, threshold: float, spec: dict
) -> dict:
    """The book as it shipped, beside the book one candidate rule would produce.

    ``now`` is every joined row. ``keep`` is what the rule lets through, ``drop``
    what it removes, ``unknown`` what it could not judge — reported separately,
    never merged into either side.

    The direction comes from the engine's ``spec``; ops does not decide which way
    a feature filters, because ops is not what stamps it.
    """
    copy = FEATURE_COPY.get(feature) or {}
    keep_above = feature in set(spec.get("keep_above") or ())
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
    # Which series these rows came from. One entry is a clean split; more than
    # one means the threshold spans timeframes that do not share a scale.
    timeframes = sorted({str(r.get("tf_name") or "") for r in joined} - {""})
    return {
        "feature": feature,
        "label": copy.get("label", feature),
        "asks": copy.get("asks", ""),
        "threshold": threshold,
        "direction": "≥" if keep_above else "≤",
        "now": now_a, "keep": keep_a, "drop": _agg(drop), "unknown": _agg(unknown),
        "delta_avg_r": delta,
        "delta_avg_pnl_pct": delta_pnl,
        "kept_fraction": (keep_a["n"] / now_a["n"]) if now_a["n"] else None,
        "timeframes": timeframes,
        "mixed_timeframes": len(timeframes) > 1,
    }


def entry_quality_panel(stamps: list[dict], joined: list[dict]) -> dict:
    """What the live gate did, and what the shadow rules would have done.

    Read from the **stamps**, not from the joined book, and the difference is
    the whole point: a candidate the gate suppressed never delivered, so it has
    no closed-signal record and can never appear in a join. Measuring this panel
    on ``joined`` would silently exclude exactly the population the gate acted
    on — the page would show a live gate that had done nothing.

    Three buckets per rule, and they answer different questions:

    * ``enforced`` — the gate killed it. Its outcome is **not here and cannot
      be**: it never delivered. The suppression audit forward-measures those on
      real candles, and that is the surface that says whether the rule is
      earning its place. This panel says only how many.
    * ``shadow_reject`` — the rule fired and did not act. These *did* emit, so
      their outcomes are known, and they are the only evidence a promotion may
      read.
    * ``unknown`` — the rule could not read its feature. Its own bucket, never
      folded into "passed": an enforcing rule that abstains on everything is
      inert and reads exactly like one that is working.

    Rows stamped before the gate existed carry no ``eq_*`` keys at all. They are
    counted apart as ``not_evaluated`` rather than as passes — a missing stamp
    is not a pass, and this lane's own ledger deliberately did not bump its
    schema for the verdict, so those rows are here to stay for a while.
    """
    rules: dict[str, dict] = {}
    not_evaluated = 0
    budget_suspended = 0
    outcomes_by_id = {
        str(r.get("signal_id") or ""): r for r in joined if isinstance(r, dict)
    }

    for row in stamps:
        if not isinstance(row, dict):
            continue
        per_rule = row.get("eq_rules")
        if not isinstance(per_rule, list):
            not_evaluated += 1
            continue
        if row.get("eq_budget_suspended"):
            budget_suspended += 1
        enforced_by = str(row.get("eq_enforced_by") or "")
        for outcome in per_rule:
            if not isinstance(outcome, dict):
                continue
            key = str(outcome.get("rule") or "")
            if not key:
                continue
            slot = rules.setdefault(key, {
                "key": key,
                # Which feature the rule reads, so this panel can label it from
                # the same copy the splits below use. Carried on the stamp by
                # the engine rather than derived here — ops does not decide
                # which feature a rule is about.
                "feature": str(outcome.get("feature") or ""),
                # Mode is read off the rows the gate actually decided, not
                # mirrored from a copy of the engine's registry. A rule the
                # owner flips in ops changes these stamps within one cache TTL.
                "live": False,
                "threshold": outcome.get("threshold"),
                "seen": 0, "reject": 0, "pass": 0, "unknown": 0,
                "enforced": 0, "shadow_reject": 0,
                "shadow_reject_rows": [],
                "unknown_reasons": {},
            })
            slot["seen"] += 1
            if outcome.get("live"):
                slot["live"] = True
            verdict = str(outcome.get("verdict") or "")
            if verdict in ("reject", "pass", "unknown"):
                slot[verdict] += 1
            if verdict == "unknown":
                reason = str(outcome.get("unknown_reason") or "unspecified")
                slot["unknown_reasons"][reason] = slot["unknown_reasons"].get(reason, 0) + 1
            if verdict != "reject":
                continue
            if enforced_by == key:
                slot["enforced"] += 1
                continue
            slot["shadow_reject"] += 1
            matched = outcomes_by_id.get(str(row.get("signal_id") or ""))
            if matched is not None:
                slot["shadow_reject_rows"].append(matched)

    out: list[dict] = []
    for slot in rules.values():
        seen = slot["seen"] or 0
        copy = FEATURE_COPY.get(str(slot.get("feature") or ""), {})
        # What the rule would have removed, measured only on the rows that
        # actually delivered and closed — beside the whole delivered book, so
        # the reader sees what is being compared to what.
        would_remove = _agg(slot.pop("shadow_reject_rows"))
        out.append({
            **slot,
            "label": copy.get("label", slot["key"]),
            "unknown_frac": (slot["unknown"] / seen) if seen else None,
            "reject_frac": (slot["reject"] / seen) if seen else None,
            "would_remove": would_remove,
            # A blind rule is a fault in either mode, and the first cut of this
            # panel judged only the enforcing ones. That is exactly how
            # `smc_zone_dist_atr` hid: `zone_distance_atr` read key names
            # `FVGZone` does not carry, so `tpe_smc_zone` abstained on 100% of
            # its population — 0 of 57 TPE rows — and the badge stayed off
            # because the rule was in shadow.
            #
            # Two thresholds, because the faults differ. An ENFORCING rule
            # mostly-blind is an inert gate wearing a live gate's label (0.8,
            # while it still has some sight). A SHADOW rule *totally* blind can
            # never accumulate the evidence its own promotion depends on (1.0
            # only — a shadow rule with any sight is working, and badging it
            # would make the badge stop meaning anything).
            "blind": bool(
                seen >= 20
                and (slot["unknown"] / seen) >= (0.8 if slot["live"] else 1.0)
            ),
        })
    out.sort(key=lambda r: (not r["live"], -r["enforced"], -r["shadow_reject"]))
    return {
        "rules": out,
        "evaluated": sum(1 for r in stamps if isinstance(r, dict) and isinstance(r.get("eq_rules"), list)),
        "not_evaluated": not_evaluated,
        "budget_suspended": budget_suspended,
        "any_live": any(r["live"] for r in out),
        # The delivered book, as the denominator every "would remove" is against.
        "delivered": _agg(joined),
    }


def _rows_and_coverage(
    request: Request,
) -> tuple[list[dict], dict, dict, Optional[str], list[dict]]:
    dv = request.app.state.data_volume
    try:
        payload = dv.entry_features()
        perf = dv.signal_performance()
    except Exception as exc:  # noqa: BLE001
        return [], {}, dict(FALLBACK_SPEC), f"engine data volume unavailable: {exc}", []
    if not isinstance(payload, dict):
        return [], {}, dict(FALLBACK_SPEC), None, []
    stamps = payload.get("rows")
    stamps = stamps if isinstance(stamps, list) else []
    joined, coverage = join_outcomes(stamps, perf)
    coverage["written_at"] = payload.get("written_at")
    coverage["schema"] = payload.get("schema")
    # The engine ships the registry with the data. Falling back is legitimate
    # for a pre-`spec` ledger and is stated on screen — a silent fallback is a
    # mirror nobody knows is a mirror.
    spec = payload.get("spec")
    if isinstance(spec, dict) and spec.get("core"):
        coverage["spec_source"] = "engine"
    else:
        spec = dict(FALLBACK_SPEC)
        coverage["spec_source"] = "ops_fallback"
    return joined, coverage, spec, None, stamps


def setups_present(rows: list[dict]) -> list[tuple[str, int]]:
    """``(setup_class, n)`` for every path in the joined book, most rows first.

    Driven by the data rather than a hardcoded list, so a path that starts or
    stops stamping is visible instead of silently absent.
    """
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("setup_class") or "UNKNOWN")
        counts[key] = counts.get(key, 0) + 1
    return sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))


@router.get("/signals/entry-features")
async def entry_features_page(request: Request, setup: str = Query("")):
    joined, coverage, spec, error, stamps = _rows_and_coverage(request)

    # Counted with every filter applied except its own, so the selector
    # describes the whole book rather than only the option already chosen.
    setup_options = setups_present(joined)

    if setup:
        joined = [r for r in joined if str(r.get("setup_class") or "") == setup]
        # Filtered on the same axis as the table beside it — a gate panel
        # measured over the whole ledger above a table showing one path is not
        # a summary of anything the reader is looking at (#90).
        stamps = [
            r for r in stamps
            if isinstance(r, dict) and str(r.get("setup_class") or "") == setup
        ]

    quality = entry_quality_panel(stamps, joined)

    splits = [
        split_by_feature(
            joined, name, float((FEATURE_COPY.get(name) or {}).get("default", 0.0)), spec
        )
        for name in features_for(spec, setup)
    ]
    # Ranked on the MONEY, not on R (owner, 2026-08-02). Sizing is a fixed
    # notional, so the stop distance is absent from it and R equalises trades
    # that risk very different amounts — on the same window it ranks
    # MEAN_REVERT below MA_CROSS_TREND_SHIFT while MEAN_REVERT loses a fifth as
    # much money per trade. Sorting by R would order this table by the wrong
    # quantity.
    #
    # The page still leads with n and kept-fraction whatever the order: a rule
    # that drops two rows and looks brilliant is the FAILED_AUCTION_RECLAIM
    # mistake in a new suit.
    splits.sort(
        key=lambda s: (
            s["delta_avg_pnl_pct"] is None,
            -(s["delta_avg_pnl_pct"] or 0.0),
        )
    )

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
            "quality": quality,
            "splits": splits,
            "rows": joined[:200],
            "row_cap_bit": len(joined) > 200,
            "total_rows": len(joined),
            "missing_counts": missing_counts,
            "feature_copy": FEATURE_COPY,
            "columns": features_for(spec, setup),
            "filter_setup": setup,
            "setup_options": setup_options,
            # "Best of N" is not a fact about the winner until N is on screen.
            "cells_drawn": len(splits),
        },
    )


@router.get("/signals/entry-features/export.csv")
async def entry_features_export(request: Request, setup: str = Query("")):
    joined, _cov, spec, _err, _stamps = _rows_and_coverage(request)
    if setup:
        joined = [r for r in joined if str(r.get("setup_class") or "") == setup]
    cols = [
        "signal_id", "symbol", "side", "setup_class", "entry_trigger",
        "tf_name", "entry_ref_name", "confidence", "entry_regime",
        "sl_dist_pct", "profile_would_reject",
        # What the live gate did to this row. Exported because the promotion
        # question — would this rule have helped — is answered by joining
        # `eq_would_reject_by` to the outcome columns below, and that is an
        # analysis nobody should have to re-derive from the page.
        "eq_would_reject_by", "eq_enforced_by", "eq_budget_suspended",
        # Every feature any path declares, so one export covers the whole book
        # and a per-path column does not vanish because of the current filter.
        *features_for(spec, ""),
        "pnl_pct", "sl_distance_pct_at_entry", "r", "outcome_label", "stamped_at",
    ]
    return csv_response(
        "entry_features",
        cols,
        [[r.get(c) for c in cols] for r in joined],
    )
