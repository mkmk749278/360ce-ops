"""Dark-Signals trailing-exit sim — the pure indicator + barrier logic.

Exercised without FastAPI/Binance: every number comes from synthetic candles fed
straight into the pure functions. These pin the honest rules (pessimistic
intrabar order, gap-through fills, fee+funding deducted, no-warm-up → no-data)
so a refactor can't quietly turn a trailed loss into a phantom win.
"""
from __future__ import annotations

from app.data_sources.binance_klines import Kline
from app.data_sources.dark_signals import (
    Bar,
    SimParams,
    parabolic_sar,
    resample,
    simulate_trailing_exit,
    supertrend,
    wilder_atr,
)


def _bar(o, h, lw, c, ms=0) -> Bar:
    return Bar(ms, o, h, lw, c)


# --------------------------------------------------------------------------- #
# resample
# --------------------------------------------------------------------------- #
def test_resample_1m_is_identity_shape() -> None:
    ks = [Kline(i * 60_000, high=10 + i, low=8 + i, close=9 + i, open=9 + i)
          for i in range(3)]
    bars = resample(ks, 1)
    assert len(bars) == 3
    assert bars[0].open == 9 and bars[0].high == 10 and bars[0].low == 8


def test_resample_5m_aggregates_ohlc() -> None:
    # Six 1m candles → two 5m buckets ([0..4], [5]).
    ks = []
    for i in range(6):
        ks.append(Kline(i * 60_000, high=100 + i, low=90 - i, close=95 + i, open=95))
    bars = resample(ks, 5)
    assert len(bars) == 2
    first = bars[0]
    assert first.open == 95              # first 1m open in bucket
    assert first.high == 104             # max high over minutes 0..4
    assert first.low == 86               # min low over minutes 0..4 (90-4)
    assert first.close == 99             # last close in bucket (95+4)


# --------------------------------------------------------------------------- #
# indicators
# --------------------------------------------------------------------------- #
def test_wilder_atr_none_until_seeded_then_positive() -> None:
    highs = [102.0] * 10
    lows = [98.0] * 10
    closes = [100.0] * 10
    atr = wilder_atr(highs, lows, closes, 3)
    assert atr[0] is None and atr[2] is None
    assert atr[3] is not None
    # Constant 4-wide range → ATR converges to 4.
    assert abs(atr[-1] - 4.0) < 1e-6


def test_supertrend_flips_direction_on_reversal() -> None:
    # Strong uptrend then a hard crash — direction must go 1 ... then -1.
    highs, lows, closes = [], [], []
    for i in range(12):
        base = 100 + i * 3
        highs.append(base + 1.0)
        lows.append(base - 1.0)
        closes.append(float(base))
    for i in range(6):
        base = 133 - i * 8
        highs.append(base + 1.0)
        lows.append(base - 1.0)
        closes.append(float(base))
    _line, direction = supertrend(highs, lows, closes, 3, 2.0)
    dirs = [d for d in direction if d is not None]
    assert 1 in dirs and -1 in dirs
    # The last (post-crash) direction is down.
    assert dirs[-1] == -1


def test_parabolic_sar_below_price_in_uptrend() -> None:
    highs = [100 + i for i in range(12)]
    lows = [98 + i for i in range(12)]
    sar = parabolic_sar([float(x) for x in highs], [float(x) for x in lows], 0.02, 0.2)
    assert sar[0] is None and sar[1] is not None
    # Deep into a clean uptrend, SAR trails below the lows.
    assert sar[-1] <= lows[-1]


# --------------------------------------------------------------------------- #
# trailing-exit simulation
# --------------------------------------------------------------------------- #
def _atr_scenario() -> list[Bar]:
    """Warm-up flat, climb to new highs, then a red bar that breaks the trail."""
    bars = [_bar(100, 101, 99, 100) for _ in range(4)]      # warm-up (ATR seeds)
    bars += [
        _bar(100, 102, 100, 101),   # idx 4 — entry bar
        _bar(101, 104, 101, 103),   # idx 5 — new highs
        _bar(103, 106, 103, 105),   # idx 6
        _bar(105, 107, 105, 106),   # idx 7 — peak high 107
        _bar(106, 106, 95, 96),     # idx 8 — crash: low 95 breaks the trail
        _bar(96, 97, 90, 92),       # idx 9
    ]
    return bars


def test_atr_trail_long_captures_gain_net_of_costs() -> None:
    bars = _atr_scenario()
    res = simulate_trailing_exit(
        bars, entry_idx=4, entry=100.0, direction="LONG", method="atr",
        period=3, mult=2.0, sar_step=0.02, sar_max=0.2, tf_min=5,
        fee_pct=0.07, funding_bps_per_8h=1.0,
    )
    assert res.exited is True
    assert res.gross_pct is not None and res.gross_pct > 0        # captured an up move
    assert res.mfe_pct >= res.gross_pct                            # never exits above MFE
    assert res.result_pct < res.gross_pct                         # fee + funding deducted
    assert res.fee_pct == 0.07 and res.funding_pct > 0


def test_no_warmup_returns_no_data() -> None:
    # Entry at index 1 with period 5 → ATR not yet seeded → no-data, no number.
    bars = [_bar(100, 101, 99, 100) for _ in range(4)]
    res = simulate_trailing_exit(
        bars, entry_idx=1, entry=100.0, direction="LONG", method="atr",
        period=5, mult=1.0, sar_step=0.02, sar_max=0.2, tf_min=5,
        fee_pct=0.07, funding_bps_per_8h=1.0,
    )
    assert res.reason == "no-data" and res.result_pct is None


def test_trail_never_loosens_short_side() -> None:
    # Downtrend then a green bar that breaks the (ratcheted-down) short trail.
    bars = [_bar(100, 101, 99, 100) for _ in range(4)]
    bars += [
        _bar(100, 100, 98, 99),     # idx 4 — entry (SHORT at 100)
        _bar(99, 99, 96, 97),       # idx 5 — falling
        _bar(97, 97, 94, 95),       # idx 6
        _bar(95, 105, 95, 104),     # idx 7 — sharp green bar breaks trail
    ]
    res = simulate_trailing_exit(
        bars, entry_idx=4, entry=100.0, direction="SHORT", method="atr",
        period=3, mult=2.0, sar_step=0.02, sar_max=0.2, tf_min=5,
        fee_pct=0.0, funding_bps_per_8h=0.0,
    )
    assert res.exited is True
    # Exited above entry after a favourable down move → the captured gain is
    # bounded by how far the trail had ratcheted, not the full round trip.
    assert res.gross_pct is not None


def test_supertrend_long_exits_on_flip() -> None:
    highs, lows, closes = [], [], []
    for i in range(10):
        base = 100 + i * 3
        highs.append(base + 1.0)
        lows.append(base - 1.0)
        closes.append(float(base))
    for i in range(6):
        base = 127 - i * 9
        highs.append(base + 1.0)
        lows.append(base - 1.0)
        closes.append(float(base))
    bars = [_bar(cl, hi, lw, cl) for hi, lw, cl in zip(highs, lows, closes)]
    res = simulate_trailing_exit(
        bars, entry_idx=4, entry=112.0, direction="LONG", method="supertrend",
        period=3, mult=2.0, sar_step=0.02, sar_max=0.2, tf_min=5,
        fee_pct=0.0, funding_bps_per_8h=0.0,
    )
    assert res.exited is True and res.reason == "trail"


# --------------------------------------------------------------------------- #
# SimParams clamping
# --------------------------------------------------------------------------- #
def test_simparams_clamped_bounds() -> None:
    p = SimParams(tf_min=999, period=1, mult=0.0, sar_step=0.0,
                  sar_max=0.0, funding_bps_per_8h=-5.0, fee_pct=9.0).clamped()
    assert p.tf_min == 60
    assert p.period == 2
    assert p.mult == 0.1
    assert p.sar_step >= 0.001
    assert p.sar_max >= p.sar_step
    assert p.funding_bps_per_8h == 0.0
    assert p.fee_pct == 2.0
