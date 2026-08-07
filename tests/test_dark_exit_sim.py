"""Exit methods and "max PnL before SL" on the dark feed.

Owner, 2026-08-03: *"implement same like live features in dark feed — max PnL
before hitting SL, and same exit strategies like Held to stop in dark feed
too."*

The thing these tests are really guarding is a distinction that is easy to lose
and expensive to lose: **the row's own MFE and the held arm's peak are different
measurements.** The row's walk stops at its first TP1-or-SL touch, so on a TP1
row its MFE is bounded by the TP1 distance *by construction* — it can never
answer "how much was on the table". The held arm walks the same bars with TP1
removed. Pooling them would average a truncated series with a complete one and
report the result as a fact about the market.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test")

from app.data_sources.dark_exit_sim import (  # noqa: E402
    HOLD_EXPIRED,
    HOLD_INSUFFICIENT,
    HOLD_OPEN,
    HOLD_SL,
    SKIP_NO_ARM,
    SKIP_NO_LEVEL,
    SKIP_RUNNING,
    SKIP_UNMEASURED,
    DarkRowInputs,
    build_catalog,
    compare_strategies,
    evaluate,
    get_strategy,
    hold_coverage,
    summarize,
    summarize_max_profit,
)


def _row(
    side="LONG", entry=100.0, sl=97.0, tp1=106.0, tp2=112.0, tp3=124.0,
    status="CLOSED_TP1", pnl=6.0, hold_status=HOLD_SL, hold_result=-3.0,
    hold_mfe=15.0, hold_mfe_incl=None, hold_hit_tp=2, setup="MEAN_REVERT",
    **extra,
):
    row = {
        "symbol": "AAAUSDT", "side": side, "setup_class": setup,
        "entry": entry, "stop_loss": sl, "tp1": tp1, "tp2": tp2, "tp3": tp3,
        "status": status, "pnl_pct": pnl,
        "hold_status": hold_status, "hold_result_pct": hold_result,
        "hold_mfe_pct": hold_mfe,
        "hold_mfe_incl_pct": hold_mfe if hold_mfe_incl is None else hold_mfe_incl,
        "hold_hit_tp": hold_hit_tp, "hold_mae_pre_peak_pct": 2.0,
    }
    row.update(extra)
    return row


def _inp(**kw):
    out = DarkRowInputs.from_row(_row(**kw))
    assert out is not None
    return out


# --------------------------------------------------------------------------- #
# Held to stop
# --------------------------------------------------------------------------- #


def test_held_to_stop_returns_what_the_engine_recorded_not_a_re_derivation():
    """Ops ports the math; it does not re-walk a candle or invent a fill."""
    res = evaluate(_inp(), get_strategy("hold"))
    assert res.result_pct == pytest.approx(-3.0)
    assert res.sl_frac == 1.0


def test_a_hold_arm_closed_at_the_horizon_is_scored_at_its_mark_not_at_the_stop():
    res = evaluate(
        _inp(hold_status=HOLD_EXPIRED, hold_result=1.8), get_strategy("hold")
    )
    assert res.result_pct == pytest.approx(1.8)
    assert res.sl_frac == 0.0


# --------------------------------------------------------------------------- #
# Ladders
# --------------------------------------------------------------------------- #


def test_a_tp_leg_fills_only_if_the_level_was_touched_before_the_stop():
    """`hold_hit_tp` counts levels reached strictly before the stop bar, so a leg
    that fills provably filled first."""
    tp1_full = evaluate(_inp(hold_hit_tp=1), get_strategy("tp1"))
    assert tp1_full.result_pct == pytest.approx(6.0)

    # TP1 never reached: the whole position closes at the stop.
    never = evaluate(_inp(hold_hit_tp=0), get_strategy("tp1"))
    assert never.result_pct == pytest.approx(-3.0)
    assert never.sl_frac == pytest.approx(1.0)


def test_a_half_filled_ladder_closes_the_rest_at_the_stop():
    # TP1 (+6%) reached, TP2 (+12%) not → 50% at +6, 50% at −3.
    res = evaluate(_inp(hold_hit_tp=1), get_strategy("tp1_tp2"))
    assert res.result_pct == pytest.approx(0.5 * 6.0 + 0.5 * -3.0)
    assert res.sl_frac == pytest.approx(0.5)
    assert res.filled_labels == ["TP1"]


def test_an_unstamped_level_refuses_the_row_rather_than_booking_the_stop():
    """Letting the unpriceable fraction fall through to the stop would book a
    loss the method may never have taken, and the shortfall would read as the
    method performing badly rather than as missing data."""
    res = evaluate(_inp(tp2=0.0), get_strategy("tp1_tp2"))
    assert res.result_pct is None
    assert res.skipped == SKIP_NO_LEVEL


def test_a_fixed_target_fills_off_the_peak_before_the_stop():
    hit = evaluate(_inp(hold_mfe=1.5), get_strategy("flat", 1.0))
    assert hit.result_pct == pytest.approx(1.0)

    missed = evaluate(_inp(hold_mfe=0.4, hold_hit_tp=0), get_strategy("flat", 1.0))
    assert missed.result_pct == pytest.approx(-3.0)


def test_a_short_is_priced_in_its_own_direction():
    res = evaluate(
        _inp(side="SHORT", entry=100.0, sl=103.0, tp1=94.0, tp2=88.0, hold_hit_tp=1),
        get_strategy("tp1"),
    )
    assert res.result_pct == pytest.approx(6.0)


# --------------------------------------------------------------------------- #
# Break-even
# --------------------------------------------------------------------------- #


def test_the_be_stop_scratches_instead_of_taking_the_loss():
    res = evaluate(_inp(hold_mfe=2.0, hold_hit_tp=0), get_strategy("be_tp1"), be_pct=1.0)
    assert res.result_pct == pytest.approx(0.0)
    assert res.filled_labels == ["BE"]


def test_the_be_stop_is_irrelevant_once_tp1_filled_first():
    res = evaluate(_inp(hold_mfe=9.0, hold_hit_tp=1), get_strategy("be_tp1"), be_pct=1.0)
    assert res.result_pct == pytest.approx(6.0)


def test_an_untriggered_be_falls_through_to_the_base_ladder():
    res = evaluate(_inp(hold_mfe=0.3, hold_hit_tp=0), get_strategy("be_tp1"), be_pct=1.0)
    assert res.result_pct == pytest.approx(-3.0)


# --------------------------------------------------------------------------- #
# Refusals — counted and named, never scored zero
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("status,reason", [
    (HOLD_OPEN, SKIP_RUNNING),
    (HOLD_INSUFFICIENT, SKIP_UNMEASURED),
])
def test_an_undecided_arm_is_skipped_with_its_own_reason(status, reason):
    res = evaluate(_inp(hold_status=status, hold_result=None), get_strategy("tp1"))
    assert res.result_pct is None
    assert res.skipped == reason


def test_a_row_written_before_the_arm_shipped_is_its_own_bucket():
    """A missing stamp is not a pass, and it is not the same state as an arm
    that has not moved yet — the fixes differ."""
    raw = _row()
    del raw["hold_status"]
    inp = DarkRowInputs.from_row(raw)
    assert inp is not None
    assert evaluate(inp, get_strategy("tp1")).skipped == SKIP_NO_ARM


def test_summarize_counts_the_skips_apart_from_the_scores():
    results = [
        evaluate(_inp(), get_strategy("tp1")),
        evaluate(_inp(hold_status=HOLD_OPEN, hold_result=None), get_strategy("tp1")),
    ]
    out = summarize(results, fee_pct=0.0)
    assert out["n"] == 1
    assert out["skipped"] == 1
    assert out["skips"] == {SKIP_RUNNING: 1}


# --------------------------------------------------------------------------- #
# The comparison
# --------------------------------------------------------------------------- #


def test_every_method_is_measured_on_one_shared_population():
    """A method measured on the subset it happens to be able to price would win
    on selection rather than on outcomes."""
    rows = [
        _row(),
        _row(hold_status=HOLD_OPEN, hold_result=None),   # arm undecided
        _row(pnl=None),                                  # row undecided
    ]
    out = compare_strategies(rows, target_pct=1.0, fee_pct=0.0)
    assert out["n"] == 1
    assert out["n_excluded"] == 2
    assert set(out["excluded"]) == {SKIP_RUNNING, "row_undecided"}


def test_the_fee_is_charged_to_the_baseline_as_well():
    """Charging the strategies and not the row's own exit would manufacture an
    edge out of the fee."""
    rows = [_row(pnl=6.0, hold_hit_tp=1)]
    out = compare_strategies(rows, fee_pct=0.07)
    assert out["engine"]["avg_pct"] == pytest.approx(6.0 - 0.07)
    tp1 = next(s for s in out["strategies"] if s["key"] == "tp1")
    assert tp1["stat"]["avg_pct"] == pytest.approx(6.0 - 0.07)
    # …so identical exits show no edge, which is the property that matters.
    assert tp1["edge"] == pytest.approx(0.0)


def test_gross_and_net_are_both_published():
    out = compare_strategies([_row(hold_hit_tp=1)], fee_pct=0.07)
    tp1 = next(s for s in out["strategies"] if s["key"] == "tp1")
    assert tp1["stat"]["avg_gross_pct"] == pytest.approx(6.0)
    assert tp1["stat"]["avg_pct"] == pytest.approx(5.93)


def test_the_number_of_methods_drawn_is_reported():
    """"Best of N" is not a fact about the winner until N is on screen."""
    out = compare_strategies([_row()])
    assert out["n_cells"] == len(build_catalog())
    assert out["n_cells"] >= 7


# --------------------------------------------------------------------------- #
# Max profit
# --------------------------------------------------------------------------- #


def test_max_profit_reads_the_hold_arm_not_the_rows_own_truncated_mfe():
    """The distinction the whole change exists for: a TP1 row's own MFE is
    bounded by TP1 by construction, and the arm's peak is not."""
    out = summarize_max_profit([
        _row(hold_mfe=15.0, mfe_pct=6.0),
        _row(hold_mfe=9.0, mfe_pct=6.0),
    ])
    (agg,) = out["by_setup"]
    assert agg["avg_peak_pct"] == pytest.approx(12.0)
    assert agg["best_peak_pct"] == pytest.approx(15.0)


def test_max_profit_ignores_rows_whose_arm_has_not_decided():
    out = summarize_max_profit([
        _row(hold_status=HOLD_OPEN, hold_result=None, hold_mfe=99.0),
        _row(hold_status=HOLD_INSUFFICIENT, hold_result=None, hold_mfe=99.0),
    ])
    assert out["n"] == 0
    assert out["by_setup"] == []


def test_give_back_is_the_peak_minus_what_the_rows_own_exit_captured():
    out = summarize_max_profit([_row(hold_mfe=15.0, pnl=6.0)])
    (agg,) = out["by_setup"]
    assert agg["avg_give_back_pct"] == pytest.approx(9.0)


def test_both_readings_of_the_peak_travel_together():
    """The gap between them IS the intrabar assumption; publishing one silently
    is choosing the answer."""
    out = summarize_max_profit([_row(hold_mfe=0.0, hold_mfe_incl=12.0)])
    (agg,) = out["by_setup"]
    assert agg["avg_peak_pct"] == pytest.approx(0.0)
    assert agg["avg_peak_incl_pct"] == pytest.approx(12.0)


def test_the_drawdown_to_the_peak_is_reported_beside_it():
    """A peak with no drawdown bounds nothing — the two readings of "tighten the
    stop" have differed by more than the whole edge under discussion."""
    out = summarize_max_profit([_row(hold_mae_pre_peak_pct=4.0)])
    (agg,) = out["by_setup"]
    assert agg["avg_drawdown_pre_peak_pct"] == pytest.approx(4.0)
    assert agg["worst_drawdown_pre_peak_pct"] == pytest.approx(4.0)


def test_coverage_splits_the_three_undecided_states():
    raw = _row()
    del raw["hold_status"]
    out = hold_coverage([
        _row(),
        _row(hold_status=HOLD_OPEN),
        _row(hold_status=HOLD_INSUFFICIENT),
        raw,
    ])
    # Compared as a whole rather than key by key, so a NEW bucket cannot appear
    # and go uncounted — that is this test's point and it survives the panels
    # added 2026-08-07. Those carry structured values (`by_path`,
    # `representativeness`), so the scalar counts are what is pinned exactly.
    assert {k: v for k, v in out.items() if isinstance(v, int)} == {
        "decided": 1, SKIP_RUNNING: 1, SKIP_UNMEASURED: 1, SKIP_NO_ARM: 1,
        "total": 4,
    }


def test_the_catalog_mirrors_the_profit_tabs_keys():
    """Same words, same meaning, both pages. A reader moving between them must
    not have to check which "TP1 full" is which."""
    from app.data_sources.exit_sim import build_catalog as profit_catalog

    ours = build_catalog(1.0)
    theirs = profit_catalog(1.0)
    shared = set(theirs) & set(ours)
    assert shared == set(theirs), "a Profit-tab strategy is missing here"
    for key in shared:
        assert ours[key].label == theirs[key].label
