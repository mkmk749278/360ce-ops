"""Ops' ATR trail must be the engine's ATR trail — the level, not just the name.

`/exit-backtest` and `/dark-signals` have printed the words **"ATR-trail
(Chandelier)"** since long before the engine had a live chandelier arm, and
`/signals/atr-live` now prints them too. Two surfaces under one label computing
two different levels is a defect this system has already paid a session for: on
2026-07-31 the dark `sar_*` replay and the live SAR arm agreed on the easy 79%
of candidates and diverged by **+0.73pp** on the 21% where their definitions
differed, and nobody noticed because the agreement on the majority looked like
validation.

So the vector below is **byte-identical to 360-v2's
`tests/test_trail_mechanisms.py`**, generated from the engine's function, and
this file asserts ops against it — never the reverse. *Ops ports the engine's
math, it does not invent it.*

The expected levels are derived by hand rather than recorded from either
implementation: every bar of this series prints `high - prev_close == 1.6`,
which is its widest true range, so ATR is a flat **1.6** from the seed onward
and the chandelier level is simply `running_high - 2 x 1.6`. A vector copied out
of one implementation's output would agree with that implementation by
construction and pin nothing.
"""
from __future__ import annotations

import pytest

from app.data_sources.dark_signals import Bar, simulate_trailing_exit, wilder_atr

PERIOD, MULT = 5, 2.0

HIGHS = [
    100.6, 101.6, 102.6, 103.6, 104.6, 105.6, 106.6, 107.6, 108.6, 109.6,
    108.6, 107.6, 106.6, 105.6, 104.6, 105.6, 106.6, 107.6, 108.6, 109.6,
]
LOWS = [
    99.4, 100.4, 101.4, 102.4, 103.4, 104.4, 105.4, 106.4, 107.4, 108.4,
    107.4, 106.4, 105.4, 104.4, 103.4, 104.4, 105.4, 106.4, 107.4, 108.4,
]
CLOSES = [
    100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0,
    108.0, 107.0, 106.0, 105.0, 104.0, 105.0, 106.0, 107.0, 108.0, 109.0,
]

#: The level the engine's ``trail_mechanisms`` parks at each bar, for a LONG.
ENGINE_LEVELS = [
    None, None, None, None, None,
    102.4, 103.4, 104.4, 105.4, 106.4,
    106.4, 106.4, 106.4, 106.4, 106.4,
    106.4, 106.4, 106.4, 106.4, 106.4,
]


def _bars(highs=None, lows=None, closes=None):
    highs = HIGHS if highs is None else highs
    lows = LOWS if lows is None else lows
    closes = CLOSES if closes is None else closes
    return [
        Bar(open_time_ms=i * 60_000, open=c, high=h, low=lo, close=c)
        for i, (h, lo, c) in enumerate(zip(highs, lows, closes))
    ]


def test_ops_atr_is_the_engines_atr():
    """Wilder smoothing, seeded on the mean of the first ``period`` true ranges.

    Three implementations exist — ``indicators.atr`` (numpy, engine scoring),
    ``trail_mechanisms.wilder_atr`` (lists, engine arms) and this one. That is
    three implementations of **one definition**, and the only thing that keeps
    it one definition is an assertion rather than a comment.
    """
    got = wilder_atr(HIGHS, LOWS, CLOSES, PERIOD)
    assert got[:PERIOD] == [None] * PERIOD
    for i in range(PERIOD, len(HIGHS)):
        assert got[i] == pytest.approx(1.6, rel=1e-9), f"bar {i}"


def test_the_bake_offs_chandelier_stops_where_the_live_arm_would_have():
    """Drive the REAL simulator, not a re-implementation of its ratchet.

    A test that rebuilt the trail here would assert this file's arithmetic
    against itself one repo short of the thing that ships — which is exactly how
    ``zone_distance_atr`` passed for its whole life on a shape nothing produced.
    """
    # A 21st bar that trades straight through the level the engine says is
    # parked at bar 19. If ops' trail sat anywhere else, this fill would differ.
    level = ENGINE_LEVELS[-1]
    bars = _bars(
        HIGHS + [level + 0.4],
        LOWS + [level - 3.0],
        CLOSES + [level - 2.5],
    )
    got = simulate_trailing_exit(
        bars,
        entry_idx=PERIOD,
        entry=CLOSES[PERIOD],
        direction="LONG",
        method="atr",
        period=PERIOD,
        mult=MULT,
        sar_step=0.02,
        sar_max=0.2,
        tf_min=1,
        fee_pct=0.0,
        funding_bps_per_8h=0.0,
    )
    assert got.exited is True
    assert got.exit_price == pytest.approx(level, rel=1e-9)
    assert got.reason == "trail"


def test_the_trail_never_loosens_in_either_implementation():
    """The ratchet is the property that makes this a *stop*.

    The engine vector holds 106.4 for ten bars while the highs fall to 104.6 —
    un-ratcheted that would be 101.4, so a fill at 101.4 here would mean one of
    the two implementations lets the stop widen. A trailing stop that can move
    away from price hands the trade more risk than it was sized for.
    """
    assert min(HIGHS[10:15]) == 104.6
    unratcheted = 104.6 - MULT * 1.6
    assert unratcheted < ENGINE_LEVELS[-1]
    bars = _bars(
        HIGHS + [102.0],
        LOWS + [unratcheted + 0.1],   # dips below 106.4, above the loose level
        CLOSES + [102.5],
    )
    got = simulate_trailing_exit(
        bars, entry_idx=PERIOD, entry=CLOSES[PERIOD], direction="LONG",
        method="atr", period=PERIOD, mult=MULT, sar_step=0.02, sar_max=0.2,
        tf_min=1, fee_pct=0.0, funding_bps_per_8h=0.0,
    )
    assert got.exited is True, "a loosened trail would have survived this bar"
    assert got.exit_price == pytest.approx(ENGINE_LEVELS[-1], rel=1e-9)


def test_the_label_the_two_surfaces_share_is_the_same_string():
    """If these ever diverge, one page's column and another page's heading stop
    describing the same mechanism while both keep rendering."""
    from app.data_sources.dark_signals import METHOD_LABELS
    from app.routes.sar_live import MECHANISM_FALLBACK

    assert METHOD_LABELS["atr"] == MECHANISM_FALLBACK["chandelier"]["label"]
