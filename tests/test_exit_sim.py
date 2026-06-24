"""Scaled-exit what-if simulator — the pure barrier logic.

Exercised without FastAPI/Binance: every number comes from the engine-recorded
fields (hit_tp, MFE/MAE, first-touch timestamps, TP-based PnL) the simulator
reads. These tests pin the honest/pessimistic rules so a refactor can't quietly
turn a stop-out into a phantom win.
"""
from __future__ import annotations

from app.data_sources.exit_sim import (
    SignalInputs,
    build_catalog,
    evaluate,
    get_strategy,
    summarize,
)


def _live(**kw):
    """A live-API-shaped signal (exact TP prices present)."""
    base = {
        "signal_id": "L1",
        "direction": "LONG",
        "entry": 100.0,
        "stop_loss": 98.0,
        "original_stop_loss": 98.0,
        "tp1": 102.0,
        "tp2": 104.0,
        "tp3": 106.0,
        "max_favorable_excursion_pct": 0.0,
        "max_adverse_excursion_pct": 0.0,
        "pnl_pct": 0.0,
        "status": "ACTIVE",
    }
    base.update(kw)
    return base


def _perf(**kw):
    """A signal_performance.json-shaped record (no TP prices; engine fields)."""
    base = {
        "signal_id": "P1",
        "direction": "LONG",
        "entry": 100.0,
        "stop_loss": 98.0,
        "hit_tp": 0,
        "hit_sl": False,
        "max_favorable_excursion_pct": 0.0,
        "max_adverse_excursion_pct": 0.0,
        "signal_quality_pnl_pct": 0.0,
        "pnl_pct": 0.0,
        "status": "INVALIDATED",
    }
    base.update(kw)
    return base


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #

def test_sl_and_tp_returns_long():
    inp = SignalInputs.from_dict(_live())
    assert abs(inp.sl_return_pct() - (-2.0)) < 1e-9        # 98 vs 100
    ret, approx = inp.tp_return_pct(1)
    assert abs(ret - 2.0) < 1e-9 and approx is False       # exact from tp1 price
    ret3, _ = inp.tp_return_pct(3)
    assert abs(ret3 - 6.0) < 1e-9


def test_sl_and_tp_returns_short():
    inp = SignalInputs.from_dict(_live(
        direction="SHORT", entry=100.0, stop_loss=102.0, original_stop_loss=102.0,
        tp1=98.0, tp2=96.0, tp3=94.0,
    ))
    assert abs(inp.sl_return_pct() - (-2.0)) < 1e-9
    ret, _ = inp.tp_return_pct(1)
    assert abs(ret - 2.0) < 1e-9


def test_missing_geometry_returns_none():
    assert SignalInputs.from_dict({"direction": "LONG", "entry": 0, "stop_loss": 0}) is None
    assert SignalInputs.from_dict({"direction": "SIDEWAYS", "entry": 1, "stop_loss": 0.5}) is None


# --------------------------------------------------------------------------- #
# TP1-full strategy
# --------------------------------------------------------------------------- #

def test_tp1_full_win_when_tp1_reached_before_sl():
    # Perf record: hit TP1, SL never touched → 100% at TP1, +2% (engine TP PnL).
    inp = SignalInputs.from_dict(_perf(
        hit_tp=1, hit_sl=False, signal_quality_pnl_pct=2.0,
        max_favorable_excursion_pct=2.3,
    ))
    res = evaluate(inp, get_strategy("tp1"))
    assert abs(res.result_pct - 2.0) < 1e-9
    assert res.filled_labels == ["TP1"]
    assert res.approx is True            # priced from engine TP-based PnL
    assert res.sl_frac == 0.0


def test_tp1_full_loss_when_tp1_never_reached_and_stop_hit():
    inp = SignalInputs.from_dict(_perf(
        hit_tp=0, hit_sl=True, max_adverse_excursion_pct=2.5, pnl_pct=-2.0,
    ))
    res = evaluate(inp, get_strategy("tp1"))
    assert abs(res.result_pct - (-2.0)) < 1e-9   # full size out at SL (-2%)
    assert res.filled_labels == []
    assert abs(res.sl_frac - 1.0) < 1e-9


def test_tp1_both_touched_uses_first_touch_order():
    # TP1 and SL both touched; engine says TP1 touched first → win.
    win = SignalInputs.from_dict(_perf(
        hit_tp=1, hit_sl=True, signal_quality_pnl_pct=2.0,
        max_adverse_excursion_pct=2.5,
        first_tp_touch_timestamp=1000.0, first_sl_touch_timestamp=1500.0,
    ))
    res = evaluate(win, get_strategy("tp1"))
    assert abs(res.result_pct - 2.0) < 1e-9
    # Same, but SL touched first → the stop wins the fill.
    loss = SignalInputs.from_dict(_perf(
        hit_tp=1, hit_sl=True, signal_quality_pnl_pct=2.0,
        max_adverse_excursion_pct=2.5,
        first_tp_touch_timestamp=1500.0, first_sl_touch_timestamp=1000.0,
    ))
    res2 = evaluate(loss, get_strategy("tp1"))
    assert abs(res2.result_pct - (-2.0)) < 1e-9
    assert abs(res2.sl_frac - 1.0) < 1e-9


# --------------------------------------------------------------------------- #
# Flat +1% strategy
# --------------------------------------------------------------------------- #

def test_flat_target_win_when_mfe_reaches_and_no_stop():
    inp = SignalInputs.from_dict(_perf(
        hit_tp=0, hit_sl=False, max_favorable_excursion_pct=1.4, pnl_pct=0.3,
        status="INVALIDATED",
    ))
    res = evaluate(inp, get_strategy("flat", 1.0))
    assert abs(res.result_pct - 1.0) < 1e-9    # +1% banked
    assert res.filled_labels == ["+1%"]
    assert res.approx is False                 # fixed target needs no TP price


def test_flat_target_not_reached_and_stopped_is_sl_loss():
    inp = SignalInputs.from_dict(_perf(
        hit_tp=0, hit_sl=True, max_favorable_excursion_pct=0.3,
        max_adverse_excursion_pct=2.1, pnl_pct=-2.0,
    ))
    res = evaluate(inp, get_strategy("flat", 1.0))
    assert abs(res.result_pct - (-2.0)) < 1e-9
    assert abs(res.sl_frac - 1.0) < 1e-9


def test_flat_target_pessimistic_when_order_unknown():
    # Both +1% and SL reached, no timestamps, TP1 not touched → can't prove
    # the target filled first → stop-first (pessimistic), not a phantom win.
    inp = SignalInputs.from_dict(_perf(
        hit_tp=0, hit_sl=True, max_favorable_excursion_pct=1.5,
        max_adverse_excursion_pct=2.1, pnl_pct=-2.0,
    ))
    res = evaluate(inp, get_strategy("flat", 1.0))
    assert abs(res.result_pct - (-2.0)) < 1e-9
    assert res.filled_labels == []


def test_flat_target_configurable():
    inp = SignalInputs.from_dict(_perf(
        hit_tp=0, hit_sl=False, max_favorable_excursion_pct=2.5, pnl_pct=0.0,
    ))
    res = evaluate(inp, get_strategy("flat", 2.0))
    assert abs(res.result_pct - 2.0) < 1e-9
    assert res.filled_labels == ["+2%"]


# --------------------------------------------------------------------------- #
# Partial / scaled strategies
# --------------------------------------------------------------------------- #

def test_partial_tp1_tp2_both_filled_live_prices():
    # 50% @ TP1 (+2%), 50% @ TP2 (+4%); both reached, no stop → blended +3%.
    inp = SignalInputs.from_dict(_live(
        status="PROFIT_LOCKED", max_favorable_excursion_pct=4.5,
        max_adverse_excursion_pct=0.0,
    ))
    # hit_tp not in live payload; reached is inferred from MFE vs TP price.
    inp.hit_tp = 2  # engine-confirmed both levels touched
    res = evaluate(inp, get_strategy("tp1_tp2"))
    assert abs(res.result_pct - 3.0) < 1e-9     # 0.5*2 + 0.5*4
    assert set(res.filled_labels) == {"TP1", "TP2"}
    assert res.approx is False                  # exact TP prices


def test_partial_tp1_filled_tp2_missed_then_stop():
    # 50% off at TP1 (+2%); TP2 never reached; remaining 50% stops out (-2%).
    inp = SignalInputs.from_dict(_live(
        status="SL_HIT", hit_tp=1,
        max_favorable_excursion_pct=2.1, max_adverse_excursion_pct=2.2,
        first_tp_touch_timestamp=1000.0, first_sl_touch_timestamp=2000.0,
    ))
    inp.hit_tp = 1
    res = evaluate(inp, get_strategy("tp1_tp2"))
    # 0.5 * (+2) + 0.5 * (-2) = 0
    assert abs(res.result_pct - 0.0) < 1e-9
    assert res.filled_labels == ["TP1"]
    assert abs(res.sl_frac - 0.5) < 1e-9


def test_partial_flat_then_tp1():
    # 50% @ +1%, 50% @ TP1 (+2%); both reached, no stop → +1.5% blended.
    inp = SignalInputs.from_dict(_live(
        status="PROFIT_LOCKED", hit_tp=1,
        max_favorable_excursion_pct=2.2, max_adverse_excursion_pct=0.0,
        first_tp_touch_timestamp=1000.0,
    ))
    inp.hit_tp = 1
    res = evaluate(inp, get_strategy("flat_tp1", 1.0))
    assert abs(res.result_pct - 1.5) < 1e-9
    assert set(res.filled_labels) == {"+1%", "TP1"}


# --------------------------------------------------------------------------- #
# Open (still-running) tail
# --------------------------------------------------------------------------- #

def test_active_tail_marks_to_live_pnl():
    # Active signal, TP1 filled half, rest still running marked to live pnl.
    inp = SignalInputs.from_dict(_live(
        status="ACTIVE", hit_tp=1, pnl_pct=1.2,
        max_favorable_excursion_pct=2.1, max_adverse_excursion_pct=0.0,
    ))
    inp.hit_tp = 1
    res = evaluate(inp, get_strategy("tp1_tp2"))
    # 0.5 * (+2 at TP1) + 0.5 * (live +1.2)
    assert abs(res.result_pct - (1.0 + 0.6)) < 1e-9
    assert res.is_active is True
    assert abs(res.open_frac - 0.5) < 1e-9


# --------------------------------------------------------------------------- #
# Catalog + summary
# --------------------------------------------------------------------------- #

def test_catalog_and_unknown_key_falls_back():
    cat = build_catalog(1.0)
    assert set(cat) >= {"tp1", "flat", "tp1_tp2", "flat_tp1", "tp1_tp2_tp3"}
    assert get_strategy("nonsense").key == "tp1"


def test_summary_avg_winrate_total():
    rows = [
        evaluate(SignalInputs.from_dict(_perf(hit_tp=1, signal_quality_pnl_pct=2.0)), get_strategy("tp1")),
        evaluate(SignalInputs.from_dict(_perf(hit_tp=0, hit_sl=True, max_adverse_excursion_pct=2.5, pnl_pct=-2.0)), get_strategy("tp1")),
    ]
    s = summarize(rows)
    assert s["n"] == 2
    assert s["wins"] == 1 and s["losses"] == 1
    assert abs(s["total_pct"] - 0.0) < 1e-9
    assert abs(s["win_rate"] - 50.0) < 1e-9
