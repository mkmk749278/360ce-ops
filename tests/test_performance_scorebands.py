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


# ---------------------------------------------------------------------------
# Window filtering (2026-06-15 bug fix).  The engine writes the timestamp as
# `timestamp` / `terminal_outcome_timestamp`; the route previously filtered on
# closed_at/terminal_at/created_at (never present) so 7d/30d silently dropped
# every record while all-time worked.  These lock the real field names.
# ---------------------------------------------------------------------------
import time as _time
from app.routes.performance import _aggregate


def _rec(ts_epoch, **kw):
    base = {"symbol": "BTCUSDT", "setup_class": "SR_FLIP_RETEST",
            "status": "PROFIT_LOCKED", "pnl_pct": 0.2, "confidence": 72.0,
            "timestamp": ts_epoch}
    base.update(kw)
    return base


def test_window_counts_recent_records_via_engine_timestamp():
    now = _time.time()
    recs = [_rec(now - 3600), _rec(now - 2 * 86400)]  # 1h and 2d old
    agg7 = _aggregate(recs, 7)
    assert agg7["confidence_pnl_n"] == 2          # both inside 7d
    assert sum(b["n"] for b in agg7["by_score_band"]) == 2


def test_window_excludes_old_but_alltime_keeps_them():
    now = _time.time()
    recs = [_rec(now - 3600), _rec(now - 20 * 86400)]  # 1h and 20d old
    assert _aggregate(recs, 7)["confidence_pnl_n"] == 1     # only the 1h one
    assert _aggregate(recs, 30)["confidence_pnl_n"] == 2    # both inside 30d
    assert _aggregate(recs, None)["confidence_pnl_n"] == 2  # all-time keeps both


def test_terminal_outcome_timestamp_preferred():
    now = _time.time()
    # No `timestamp`; only the close-time field — must still be windowed in.
    rec = {"symbol": "ETHUSDT", "setup_class": "X", "status": "SL_HIT",
           "pnl_pct": -0.5, "confidence": 70.0,
           "terminal_outcome_timestamp": now - 3600}
    assert _aggregate([rec], 7)["by_setup"][0]["n"] == 1


def test_legacy_field_name_no_longer_drops_everything():
    # Records carrying ONLY the engine's real field are counted in 7d (the bug
    # was that none of closed_at/created_at existed, so 7d was always empty).
    now = _time.time()
    recs = [_rec(now - 3600) for _ in range(5)]
    assert _aggregate(recs, 7)["confidence_pnl_n"] == 5
