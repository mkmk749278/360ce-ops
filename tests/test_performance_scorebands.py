"""Tests for the score-band + confidence/PnL correlation view (session 19).

These exercise the pure reducer functions in ``app.routes.performance`` so
they collect even when the app-level smoke tests can't (the reducers have no
FastAPI app-state dependency).
"""
from __future__ import annotations

from app.routes.performance import _aggregate, _classify_outcome, _pearson, _score_band


def test_score_band_edges():
    assert _score_band(64.9) == "< 65"
    assert _score_band(65.0) == "65–70"
    assert _score_band(69.99) == "65–70"
    assert _score_band(70.0) == "70–75"
    assert _score_band(80.0) == "80+"
    assert _score_band(99.9) == "80+"
    assert _score_band(None) is None
    assert _score_band("not-a-number") is None


def test_profit_locked_counts_as_win():
    assert _classify_outcome("PROFIT_LOCKED") == "win"
    assert _classify_outcome("TP1_HIT") == "win"
    assert _classify_outcome("SL_HIT") == "loss"
    assert _classify_outcome("INVALIDATED") == "neutral"
    assert _classify_outcome("EXPIRED") == "neutral"


def test_pearson_needs_data_and_variance():
    assert _pearson([(1.0, 2.0)]) is None  # too few
    # zero variance in x → undefined
    assert _pearson([(70.0, 1.0), (70.0, 2.0), (70.0, 3.0), (70.0, 4.0), (70.0, 5.0)]) is None
    # perfect positive
    r = _pearson([(1.0, 1.0), (2.0, 2.0), (3.0, 3.0), (4.0, 4.0), (5.0, 5.0)])
    assert r is not None and abs(r - 1.0) < 1e-9


def test_bands_ordered_and_none_confidence_excluded():
    recs = [
        {"confidence": 87.5, "setup_class": "SR_FLIP_RETEST", "status": "PROFIT_LOCKED", "pnl_pct": 0.27},
        {"confidence": 80.5, "setup_class": "SR_FLIP_RETEST", "status": "SL_HIT", "pnl_pct": -0.92},
        {"confidence": 68.0, "setup_class": "FAILED_AUCTION_RECLAIM", "status": "PROFIT_LOCKED", "pnl_pct": 2.75},
        {"confidence": 73.3, "setup_class": "DIVERGENCE_CONTINUATION", "status": "INVALIDATED", "pnl_pct": -0.20},
        # no confidence → must be excluded from bands + correlation
        {"confidence": None, "setup_class": "SR_FLIP_RETEST", "status": "PROFIT_LOCKED", "pnl_pct": 0.10},
    ]
    agg = _aggregate(recs, None)
    keys = [b["key"] for b in agg["by_score_band"]]
    assert keys == ["65–70", "70–75", "80+"]  # fixed low→high order, empty bands dropped
    assert agg["confidence_pnl_n"] == 4  # None-confidence record excluded

    band_80 = next(b for b in agg["by_score_band"] if b["key"] == "80+")
    assert band_80["n"] == 2 and band_80["wins"] == 1 and band_80["losses"] == 1


def test_win_rate_reflects_profit_locked():
    recs = [
        {"confidence": 68.0, "setup_class": "FAILED_AUCTION_RECLAIM", "status": "PROFIT_LOCKED", "pnl_pct": 2.75},
        {"confidence": 67.0, "setup_class": "FAILED_AUCTION_RECLAIM", "status": "SL_HIT", "pnl_pct": -0.70},
    ]
    agg = _aggregate(recs, None)
    far = next(s for s in agg["by_setup"] if s["key"] == "FAILED_AUCTION_RECLAIM")
    assert far["wins"] == 1 and far["losses"] == 1
    assert abs(far["win_rate"] - 0.5) < 1e-9
