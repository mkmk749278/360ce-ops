"""Tests for the Raw Edge reducers (no pre-TP / no invalidation view).

Exercise the pure reducers in ``app.routes.raw_edge`` directly — no FastAPI
app-state dependency, so they collect even when app smoke tests cannot."""
from __future__ import annotations

from app.routes.raw_edge import (
    _aggregate,
    _exit_kind,
    _invalidation_giveback,
)


def test_exit_kind_attribution():
    assert _exit_kind("PROFIT_LOCKED") == "pretp"
    assert _exit_kind("INVALIDATED") == "inval"
    assert _exit_kind("SL_HIT") == "true_sl"
    assert _exit_kind("EXPIRED") == "expiry"
    assert _exit_kind("something_else") == "other"
    assert _exit_kind("") == "other"


def test_capture_and_giveback_isolate_exit_machinery():
    # A pre-TP signal: reached 1.0% MFE but banked only 0.3% (residual to BE).
    # Capture = 0.3/1.0 = 30%; give-back = 0.7%.
    recs = [
        {"setup_class": "SR_FLIP_RETEST", "market_phase": "RANGING | ATR%ile=10",
         "confidence": 82.0, "outcome_label": "PROFIT_LOCKED",
         "max_favorable_excursion_pct": 1.0, "pnl_pct": 0.3},
    ]
    agg = _aggregate(recs, None)
    o = agg["overall"]
    assert o["n"] == 1
    assert abs(o["avg_mfe"] - 1.0) < 1e-9
    assert abs(o["avg_realized"] - 0.3) < 1e-9
    assert abs(o["capture"] - 0.3) < 1e-9
    assert abs(o["giveback_avg"] - 0.7) < 1e-9
    assert o["pretp_rate"] == 1.0


def test_true_sl_vs_inval_not_conflated():
    recs = [
        {"setup_class": "X", "market_phase": "RANGING", "confidence": 70.0,
         "outcome_label": "SL_HIT", "max_favorable_excursion_pct": 0.0, "pnl_pct": -0.9},
        {"setup_class": "X", "market_phase": "RANGING", "confidence": 70.0,
         "outcome_label": "INVALIDATED", "max_favorable_excursion_pct": 0.0, "pnl_pct": -0.2},
    ]
    agg = _aggregate(recs, None)
    o = agg["overall"]
    assert o["true_sl_rate"] == 0.5
    assert o["inval_rate"] == 0.5


def test_runner_threshold_counts_real_movers():
    recs = [
        {"setup_class": "X", "market_phase": "TRENDING_UP", "confidence": 90.0,
         "outcome_label": "PROFIT_LOCKED", "max_favorable_excursion_pct": 2.1, "pnl_pct": 1.0},
        {"setup_class": "X", "market_phase": "TRENDING_UP", "confidence": 90.0,
         "outcome_label": "PROFIT_LOCKED", "max_favorable_excursion_pct": 0.4, "pnl_pct": 0.3},
    ]
    agg = _aggregate(recs, None)
    assert agg["overall"]["runner_rate"] == 0.5


def test_negative_mfe_floored_to_zero():
    recs = [
        {"setup_class": "X", "market_phase": "VOLATILE", "confidence": 70.0,
         "outcome_label": "SL_HIT", "max_favorable_excursion_pct": -0.5, "pnl_pct": -0.9},
    ]
    agg = _aggregate(recs, None)
    assert agg["overall"]["avg_mfe"] == 0.0
    # MFE sum is 0 → capture undefined rather than a divide-by-zero.
    assert agg["overall"]["capture"] is None


def test_score_band_table_ordered_low_to_high():
    recs = [
        {"setup_class": "X", "market_phase": "RANGING", "confidence": 82.0,
         "outcome_label": "PROFIT_LOCKED", "max_favorable_excursion_pct": 1.0, "pnl_pct": 0.3},
        {"setup_class": "X", "market_phase": "RANGING", "confidence": 67.0,
         "outcome_label": "SL_HIT", "max_favorable_excursion_pct": 0.0, "pnl_pct": -0.9},
    ]
    agg = _aggregate(recs, None)
    keys = [b["key"] for b in agg["by_band"]]
    assert keys == ["65–70", "80+"]


def test_invalidation_giveback_missed_R_long_and_short():
    recs = [
        # LONG: entry 100, SL 99 (dist 1), TP1 102, killed at 100.5 → at-kill 0.5R,
        # to-TP1 2R → missed 1.5R.
        {"classification": "PREMATURE", "kill_reason_family": "momentum_loss",
         "direction": "LONG", "entry": 100.0, "stop_loss": 99.0, "sl_distance": 1.0,
         "tp1": 102.0, "kill_price": 100.5},
        # PROTECTIVE record shouldn't add missed-R.
        {"classification": "PROTECTIVE", "kill_reason_family": "momentum_loss",
         "direction": "LONG", "entry": 100.0, "stop_loss": 99.0, "sl_distance": 1.0,
         "tp1": 102.0, "kill_price": 99.5},
    ]
    out = _invalidation_giveback(recs)
    assert out["premature_total"] == 1
    assert abs(out["missed_R_total"] - 1.5) < 1e-9
    fam = out["families"][0]
    assert fam["key"] == "momentum_loss"
    assert fam["premature"] == 1 and fam["protective"] == 1


def test_invalidation_giveback_handles_error_payload():
    assert _invalidation_giveback({"error": "missing"})["premature_total"] == 0
    assert _invalidation_giveback([])["families"] == []
