"""Raw Edge — how signals would perform WITHOUT pre-TP and invalidation.

The Performance tab scores the *realized* outcome — i.e. after pre-TP banking
and the invalidation kill-switch have already acted on the position. That mixes
two things the owner wants to separate: the engine's raw entry edge (does price
actually move into the profit zone after we fire?) and the exit machinery's
effect on it (pre-TP capping winners, invalidation cutting positions early).

This view answers, per setup / regime / score-band:

* **How far into the profit zone did signals move?** — max-favourable-excursion
  (MFE) distribution and the fraction that reached a real runner (MFE ≥ 1%).
* **How many hit a *proper* (full) SL** vs were closed by pre-TP or invalidation?
  — exit attribution, so a "loss" that was actually a clean structural stop is
  not conflated with a position the kill-switch cut.
* **How much edge are pre-TP + invalidation giving back?** — capture ratio
  (realized ÷ MFE) and the average give-back (MFE − realized) per cohort, plus
  the invalidation cohort's PREMATURE count and missed-R from the engine's own
  post-kill classifier.

All read-only reductions of the mounted ``signal_performance.json`` (+
``invalidation_records.json`` for the kill give-back). No engine writes; no new
hot-path cost. If MFE/MAE ever move to a pre-reduced API surface this route
should consume that instead.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any

from fastapi import APIRouter, Request

from app.reports import csv_response
from app.routes.performance import _parse_dt, _resolve_window, _score_band

router = APIRouter()

# MFE buckets (raw %): a scalp that never clears ~0.3% raw is fee-dead at 10x
# (round-trip ≈ 0.7% of margin = 0.07% price); ≥1% is a genuine runner the exit
# machinery should be letting ride rather than banking flat.
_MFE_RUNNER_THRESHOLD = 1.0

# Terminal-outcome → exit-mechanism attribution. PROFIT_LOCKED = pre-TP banked
# (+ residual to BE); INVALIDATED = invalidation kill-switch; SL_HIT = a real,
# full structural stop; EXPIRED = aged out untouched.
_PRETP_LABELS = {"PROFIT_LOCKED"}
_INVAL_LABELS = {"INVALIDATED"}
_TRUE_SL_LABELS = {"SL_HIT"}
_EXPIRY_LABELS = {"EXPIRED"}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _exit_kind(label: str) -> str:
    label = (label or "").upper()
    if label in _PRETP_LABELS:
        return "pretp"
    if label in _INVAL_LABELS:
        return "inval"
    if label in _TRUE_SL_LABELS:
        return "true_sl"
    if label in _EXPIRY_LABELS:
        return "expiry"
    return "other"


def _bucket() -> dict:
    return {
        "n": 0,
        "mfe_sum": 0.0,
        "mfe_list": [],
        "runners": 0,            # MFE ≥ runner threshold
        "realized_sum": 0.0,
        "giveback_sum": 0.0,     # positive give-back only (MFE above realized)
        "pretp": 0,
        "inval": 0,
        "true_sl": 0,
        "expiry": 0,
        "other": 0,
    }


def _add(b: dict, mfe: float, realized: float, kind: str) -> None:
    b["n"] += 1
    b["mfe_sum"] += mfe
    b["mfe_list"].append(mfe)
    if mfe >= _MFE_RUNNER_THRESHOLD:
        b["runners"] += 1
    b["realized_sum"] += realized
    giveback = mfe - realized
    if giveback > 0:
        b["giveback_sum"] += giveback
    b[kind] += 1


def _finalize(d: dict[str, dict], order: list[str] | None = None) -> list[dict]:
    out: list[dict] = []
    keys = order if order is not None else list(d.keys())
    for key in keys:
        b = d.get(key)
        if not b or b["n"] == 0:
            continue
        n = b["n"]
        out.append({
            "key": key,
            "n": n,
            "avg_mfe": b["mfe_sum"] / n,
            "median_mfe": median(b["mfe_list"]) if b["mfe_list"] else 0.0,
            "runner_rate": b["runners"] / n,
            "avg_realized": b["realized_sum"] / n,
            "giveback_avg": b["giveback_sum"] / n,
            # Book-level capture: how much of the favourable move we actually
            # kept. Low capture with healthy MFE = exit machinery is the drag,
            # not the entry. Guard a non-positive MFE sum (all flat/adverse).
            "capture": (b["realized_sum"] / b["mfe_sum"]) if b["mfe_sum"] > 0 else None,
            "pretp_rate": b["pretp"] / n,
            "inval_rate": b["inval"] / n,
            "true_sl_rate": b["true_sl"] / n,
            "expiry_rate": b["expiry"] / n,
        })
    if order is None:
        out.sort(key=lambda r: r["n"], reverse=True)
    return out


# Fixed low→high score-band order (mirrors the Performance tab).
_BAND_ORDER = ["< 65", "65–70", "70–75", "75–80", "80+"]


def _aggregate(records: Any, window_days: int | None) -> dict:
    from datetime import datetime, timezone

    by_setup: dict[str, dict] = defaultdict(_bucket)
    by_regime: dict[str, dict] = defaultdict(_bucket)
    by_band: dict[str, dict] = defaultdict(_bucket)
    overall = _bucket()
    now = datetime.now(tz=timezone.utc)

    rows = records if isinstance(records, list) else []
    for r in rows:
        if not isinstance(r, dict):
            continue
        if window_days is not None:
            ts = _parse_dt(
                r.get("terminal_outcome_timestamp")
                or r.get("timestamp")
                or r.get("create_timestamp")
            )
            if ts is None or (now - ts).total_seconds() > window_days * 86400:
                continue
        # MFE is the headline: how far price moved our way before exit. Floor at
        # 0 — a negative MFE is a tracker artefact, not a favourable move.
        mfe = max(0.0, _f(r.get("max_favorable_excursion_pct")))
        realized = _f(r.get("pnl_pct"))
        kind = _exit_kind(r.get("outcome_label") or r.get("status") or "")

        setup = r.get("setup_class") or "UNKNOWN"
        regime_raw = r.get("market_phase") or r.get("entry_regime") or r.get("regime") or "UNKNOWN"
        # market_phase is "TRENDING_DOWN | ATR%ile=10 | Vol=DISTRIBUTION" — keep
        # the leading regime token so the regime table stays low-cardinality.
        regime = str(regime_raw).split("|")[0].strip() or "UNKNOWN"

        _add(by_setup[setup], mfe, realized, kind)
        _add(by_regime[regime], mfe, realized, kind)
        _add(overall, mfe, realized, kind)
        band = _score_band(r.get("confidence"))
        if band is not None:
            _add(by_band[band], mfe, realized, kind)

    fin_overall = _finalize({"all": overall})
    return {
        "overall": fin_overall[0] if fin_overall else None,
        "by_band": _finalize(by_band, _BAND_ORDER),
        "by_setup": _finalize(by_setup),
        "by_regime": _finalize(by_regime),
    }


def _invalidation_giveback(records: Any) -> dict:
    """PREMATURE-kill give-back from the engine's own post-kill classifier.

    Mirrors ``compute_rule_ablation_metrics`` in the engine: for each PREMATURE
    record the kill missed (R_to_tp1 − R_at_kill) of favourable excursion. We
    surface the count and total missed-R per kill-family so the operator can see
    which kill-rule is cutting winners — the other half of the give-back the
    MFE-capture tables show for pre-TP."""
    by_family: dict[str, dict] = defaultdict(lambda: {"premature": 0, "protective": 0, "neutral": 0, "missed_R": 0.0})
    if not isinstance(records, list):
        return {"families": [], "premature_total": 0, "missed_R_total": 0.0}

    premature_total = 0
    missed_R_total = 0.0
    for r in records:
        if not isinstance(r, dict):
            continue
        cls = (r.get("classification") or "").upper()
        if cls not in ("PREMATURE", "PROTECTIVE", "NEUTRAL"):
            continue
        fam = r.get("kill_reason_family") or "other"
        b = by_family[fam]
        if cls == "PREMATURE":
            b["premature"] += 1
            premature_total += 1
            entry = _f(r.get("entry"))
            sl_dist = _f(r.get("sl_distance"))
            tp1 = _f(r.get("tp1"))
            kill_price = _f(r.get("kill_price"))
            direction = (r.get("direction") or "").upper()
            if sl_dist > 0 and entry > 0 and tp1 > 0 and kill_price > 0:
                if direction == "LONG":
                    r_to_tp1 = (tp1 - entry) / sl_dist
                    r_at_kill = (kill_price - entry) / sl_dist
                else:
                    r_to_tp1 = (entry - tp1) / sl_dist
                    r_at_kill = (entry - kill_price) / sl_dist
                missed = r_to_tp1 - r_at_kill
                if missed > 0:
                    b["missed_R"] += missed
                    missed_R_total += missed
        elif cls == "PROTECTIVE":
            b["protective"] += 1
        else:
            b["neutral"] += 1

    families = []
    for fam, b in by_family.items():
        total = b["premature"] + b["protective"] + b["neutral"]
        families.append({
            "key": fam,
            "premature": b["premature"],
            "protective": b["protective"],
            "neutral": b["neutral"],
            "total": total,
            "premature_rate": (b["premature"] / total) if total else 0.0,
            "missed_R": b["missed_R"],
        })
    families.sort(key=lambda f: f["missed_R"], reverse=True)
    return {
        "families": families,
        "premature_total": premature_total,
        "missed_R_total": missed_R_total,
    }


@router.get("/raw-edge")
async def raw_edge(request: Request, window: str = "all"):
    vol = request.app.state.data_volume
    window, days = _resolve_window(window)
    agg = _aggregate(vol.signal_performance(), days)
    inval = _invalidation_giveback(vol.invalidation_records())
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "raw_edge.html",
        {
            "request": request,
            "agg": agg,
            "inval": inval,
            "window": window,
            "runner_threshold": _MFE_RUNNER_THRESHOLD,
            "active": "raw_edge",
        },
    )


_EXPORT_VIEWS = {
    "setup": ("by_setup", "setup"),
    "regime": ("by_regime", "regime"),
    "score_band": ("by_band", "score_band"),
}


@router.get("/raw-edge/export.csv")
async def raw_edge_export(request: Request, window: str = "all", view: str = "setup"):
    """Download a raw-edge table as CSV. ``view`` selects the breakdown
    (setup / regime / score_band); ``window`` matches the page picker."""
    vol = request.app.state.data_volume
    _window, days = _resolve_window(window)
    agg = _aggregate(vol.signal_performance(), days)
    agg_key, label = _EXPORT_VIEWS.get(view, _EXPORT_VIEWS["setup"])
    table = agg.get(agg_key, [])

    header = [
        label, "n", "avg_mfe_pct", "median_mfe_pct", "runner_rate_pct",
        "avg_realized_pct", "giveback_avg_pct", "capture_pct",
        "pretp_rate_pct", "inval_rate_pct", "true_sl_rate_pct", "expiry_rate_pct",
    ]
    rows = [
        [
            r["key"], r["n"],
            "%.4f" % r["avg_mfe"], "%.4f" % r["median_mfe"],
            "%.1f" % (r["runner_rate"] * 100),
            "%.4f" % r["avg_realized"], "%.4f" % r["giveback_avg"],
            ("%.1f" % (r["capture"] * 100)) if r["capture"] is not None else "n/a",
            "%.1f" % (r["pretp_rate"] * 100), "%.1f" % (r["inval_rate"] * 100),
            "%.1f" % (r["true_sl_rate"] * 100), "%.1f" % (r["expiry_rate"] * 100),
        ]
        for r in table
    ]
    return csv_response(f"raw_edge_{label}_{_window}", header, rows)
