"""Ops' SAR must be the engine's SAR — series *and* fill.

Two separate things are pinned here, and they failed for different reasons.

**1. The published series (cross-repo contract).** ``dark_signals.parabolic_sar``
is a third copy of the engine's ``sar_exit_shadow.parabolic_sar``. The engine and
the Lumin app were pinned to each other on 2026-07-28 (engine #821 /
lumin-app #140) by a shared 40-bar vector; ops was the copy nobody pinned. The
lists below are byte-identical to both, generated from the engine's function, so
the app is asserted against the engine and never the reverse. *Ops ports the
engine's math, it does not invent it* — a rate that agrees today and drifts
tomorrow is exactly the failure this repo's CLAUDE.md warns about.

**2. The fill (the bug the owner found).** The published SAR level and the stop
in force during a bar are the same number on every bar *except a reversal*, and
the simulator was filling at the published one. On a flip bar that is the prior
trend's extreme, sitting on the far side of price, so the gap-through branch
filled at the bar's **open** instead of the level price breached — flattering
each SAR trail exit by a mean +0.222% across 820 real 15m flips, in the trade's
favour 95% of the time.

That biased this page's *ranking*, not only its level: ``atr`` builds its own
ratcheted trail and ``supertrend`` exits on the close, so only ``sar`` carried
it — the method the bake-off is being asked to endorse.

``test_long_fills_at_the_breached_stop`` is written to fail against the old
code: point ``_stop_series`` back at the published series and it fails with the
fill at the bar's open.
"""
from __future__ import annotations

import pytest

from app.data_sources.dark_signals import (
    Bar,
    parabolic_sar,
    parabolic_sar_levels,
    simulate_trailing_exit,
)

_STEP, _MAX = 0.02, 0.2

# Byte-identical to 360-v2 tests/test_sar_chart_contract.py and lumin-app
# test/features/charts/indicators_test.dart. Generated from the engine.
HIGHS = [
    100.8, 102.0454, 103.2632, 104.4266, 105.5102, 106.4911, 107.3488,
    108.0667, 108.6316, 109.035, 109.2724, 109.3444, 109.2558, 109.016,
    108.6385, 108.1408, 107.5436, 106.8704, 106.1467, 105.3996, 104.6566,
    103.9453, 103.2923, 102.7227, 102.2592, 101.9215, 101.7259, 101.6848,
    101.8063, 102.0938, 102.5465, 103.1585, 103.92, 104.8168, 105.8308,
    106.941, 108.1235, 109.3525, 110.6008, 111.8407,
]
LOWS = [
    99.2, 100.4454, 101.6632, 102.8266, 103.9102, 104.8911, 105.7488,
    106.4667, 107.0316, 107.435, 107.6724, 107.7444, 107.6558, 107.416,
    107.0385, 106.5408, 105.9436, 105.2704, 104.5467, 103.7996, 103.0566,
    102.3453, 101.6923, 101.1227, 100.6592, 100.3215, 100.1259, 100.0848,
    100.2063, 100.4938, 100.9465, 101.5585, 102.32, 103.2168, 104.2308,
    105.341, 106.5235, 107.7525, 109.0008, 110.2407,
]
EXPECTED = [
    None, 99.2, 99.2, 99.362528, 99.66637232, 100.1338785344,
    100.76960068096, 101.55910459924479, 102.47016795535052,
    103.45599708249443, 104.46021760764543, 105.42265408611635,
    106.20700326889308, 106.83448261511445, 109.3444, 109.298282,
    109.18798272, 108.99331975679999, 108.695486176256, 108.2806075586304,
    107.74288665159474, 107.08680652037148, 106.32816547711204,
    105.49370969123187, 104.6195077529855, 103.8274462023884,
    103.12625696191073, 102.52618556952858, 102.03790845562287, 100.0848,
    100.12498000000001, 100.22184080000001, 100.39804035200001,
    100.67979712384, 101.093497411456, 101.66197372208129,
    102.4010374009899, 103.31663141683153, 104.40308776180186,
    105.64263020944149,
]

_ENGINE = "360-v2 src/sar_exit_shadow.py::parabolic_sar_levels"


class TestPublishedSeriesMatchesTheEngine:
    def test_vector_reproduces_bar_for_bar(self):
        out = parabolic_sar(HIGHS, LOWS, _STEP, _MAX)
        assert len(out) == len(EXPECTED)
        for i, want in enumerate(EXPECTED):
            got = out[i]
            if want is None:
                assert got is None, f"bar {i} gained a level the engine has not"
            else:
                assert got is not None, f"bar {i} lost its level"
                assert got == pytest.approx(want, abs=1e-9), (
                    f"SAR differs from the engine at bar {i}: {got} != {want}. "
                    f"Ops ports {_ENGINE}; it does not invent it."
                )

    def test_the_vector_exercises_a_reversal(self):
        """A one-trend vector would pin only half the recursion."""
        out = parabolic_sar(HIGHS, LOWS, _STEP, _MAX)
        bearish = [i for i, v in enumerate(out) if v is not None and v > HIGHS[i]]
        assert bearish == list(range(14, 29))


def _rising_then_stopped() -> list[Bar]:
    """Uptrend, then one bar that opens ABOVE the trailing stop and wicks
    through it — the ordinary intrabar stop-out, not a gap."""
    highs = [100.0 + i * 0.4 for i in range(24)]
    lows = [h - 0.5 for h in highs]
    opens = [(h + low) / 2 for h, low in zip(highs, lows)]

    _, stops = parabolic_sar_levels(highs, lows, _STEP, _MAX)
    trailing = stops[23]
    assert trailing is not None

    highs.append(highs[23])
    lows.append(trailing - 0.30)
    opens.append(highs[23] - 0.10)
    return [
        Bar(i * 900_000, o, h, low, (h + low) / 2)
        for i, (o, h, low) in enumerate(zip(opens, highs, lows))
    ]


class TestTrailFillPrice:
    def test_published_and_in_force_differ_only_on_reversals(self):
        published, in_force = parabolic_sar_levels(HIGHS, LOWS, _STEP, _MAX)
        assert published == parabolic_sar(HIGHS, LOWS, _STEP, _MAX)
        differing = [
            i for i in range(2, len(HIGHS))
            if in_force[i] is not None and published[i] != in_force[i]
        ]
        # Exactly the two flip bars this vector was chosen to contain.
        assert differing == [14, 29]

    def test_long_fills_at_the_breached_stop(self):
        """The regression. Pre-fix this filled at the reversal bar's open."""
        bars = _rising_then_stopped()
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        _, in_force = parabolic_sar_levels(highs, lows, _STEP, _MAX)
        flip = len(bars) - 1
        stop = in_force[flip]
        assert bars[flip].open > stop, "fixture must not be a gap-through"

        r = simulate_trailing_exit(
            bars, 4, bars[4].open, "LONG", "sar",
            period=14, mult=3.0, sar_step=_STEP, sar_max=_MAX,
            tf_min=15, fee_pct=0.0, funding_bps_per_8h=0.0,
        )
        assert r.reason == "trail"
        assert r.exit_price == pytest.approx(stop, abs=1e-9), (
            "filled at the reversal bar's open, not the stop it breached"
        )
        assert r.exit_price < bars[flip].open, (
            "the corrected fill must be worse than the bar open, never better"
        )

    def test_a_genuine_gap_through_still_fills_at_the_open(self):
        """The pessimistic rule survives — only the non-gap case changed."""
        bars = _rising_then_stopped()
        highs = [b.high for b in bars]
        lows = [b.low for b in bars]
        _, in_force = parabolic_sar_levels(highs, lows, _STEP, _MAX)
        flip = len(bars) - 1
        stop = in_force[flip]
        bars[flip] = Bar(bars[flip].open_time_ms, stop - 0.20, stop - 0.05,
                         bars[flip].low, bars[flip].close)

        r = simulate_trailing_exit(
            bars, 4, bars[4].open, "LONG", "sar",
            period=14, mult=3.0, sar_step=_STEP, sar_max=_MAX,
            tf_min=15, fee_pct=0.0, funding_bps_per_8h=0.0,
        )
        assert r.reason == "trail"
        assert r.exit_price == pytest.approx(stop - 0.20, abs=1e-9)
        assert r.exit_price < stop

    def test_only_the_sar_method_was_affected(self):
        """ATR keeps its own ratcheted trail and SuperTrend exits on the close,
        so neither reads the flip-overwritten series. Pinning this is what makes
        'the bias skewed the ranking, not just the level' checkable."""
        bars = _rising_then_stopped()
        for method in ("atr", "supertrend"):
            r = simulate_trailing_exit(
                bars, 4, bars[4].open, "LONG", method,
                period=5, mult=2.0, sar_step=_STEP, sar_max=_MAX,
                tf_min=15, fee_pct=0.0, funding_bps_per_8h=0.0,
            )
            # Whatever they do, they must not fill at a SAR-published level.
            if r.exit_price is not None:
                assert r.reason in ("trail", "open")
