"""Tests for scripts/analyze_regime_pnl.py.

Covers: CSV column normalisation, timestamp parsing, signal matching,
alignment classification, stats aggregation, and the end-to-end report
render.  All assertions use synthetic data so no engine data volume is
required.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone, timedelta
from io import StringIO
from pathlib import Path

import pytest

# The script lives outside the package — add its directory to path.
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from analyze_regime_pnl import (  # noqa: E402
    _normalise_headers,
    _parse_dt,
    _parse_float,
    _parse_duration_raw,
    load_binance_csv,
    load_signals,
    build_signal_index,
    match_position_to_signal,
    classify_alignment,
    aggregate,
    render_report,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=timezone.utc)


def _make_position(
    symbol: str = "BTCUSDT",
    direction: str = "LONG",
    pnl: float = 0.5,
    commission: float = 0.05,
    open_time: datetime | None = None,
    close_time: datetime | None = None,
    duration_sec: int | None = None,
) -> dict:
    open_time = open_time or _utc(2026, 6, 1, 10, 0)
    close_time = close_time or open_time + timedelta(minutes=5)
    return {
        "symbol": symbol,
        "direction": direction,
        "pnl_usdt": pnl,
        "commission_usdt": commission,
        "net_pnl_usdt": pnl - abs(commission),
        "open_time": open_time,
        "close_time": close_time,
        "duration_sec": duration_sec or int((close_time - open_time).total_seconds()),
        "entry_price": 0.0,
        "exit_price": 0.0,
    }


def _make_signal(
    symbol: str = "BTCUSDT",
    direction: str = "LONG",
    regime: str = "TRENDING_UP",
    setup: str = "SR_FLIP_RETEST",
    ts: datetime | None = None,
) -> dict:
    ts = ts or _utc(2026, 6, 1, 9, 55)
    return {
        "symbol": symbol,
        "direction": direction,
        "entry_regime": regime,
        "setup_class": setup,
        "timestamp": ts.isoformat(),
        "detected_at": ts.isoformat(),
        "_ts": ts,
    }


def _write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    headers = list(rows[0].keys())
    with path.open("w", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Column normalisation
# ---------------------------------------------------------------------------

class TestNormaliseHeaders:
    def test_standard_binance_columns(self):
        raw = ["Symbol", "Side", "Entry Price", "Close Price", "Realized PNL", "Commission",
               "Open Time", "Close Time"]
        mapping = _normalise_headers(raw)
        assert mapping["Symbol"] == "symbol"
        assert mapping["Side"] == "direction"
        assert mapping["Entry Price"] == "entry_price"
        assert mapping["Close Price"] == "exit_price"
        assert mapping["Realized PNL"] == "pnl_usdt"
        assert mapping["Commission"] == "commission_usdt"
        assert mapping["Open Time"] == "open_time"
        assert mapping["Close Time"] == "close_time"

    def test_alternate_column_names(self):
        raw = ["Type", "Avg. Entry Price", "Avg. Close Price", "Closed PNL",
               "Trading Fee", "Opened Time", "Closed Time"]
        mapping = _normalise_headers(raw)
        assert mapping["Type"] == "direction"
        assert mapping["Avg. Entry Price"] == "entry_price"
        assert mapping["Avg. Close Price"] == "exit_price"
        assert mapping["Closed PNL"] == "pnl_usdt"
        assert mapping["Trading Fee"] == "commission_usdt"
        assert mapping["Opened Time"] == "open_time"
        assert mapping["Closed Time"] == "close_time"

    def test_unknown_columns_not_mapped(self):
        mapping = _normalise_headers(["SomeUnknownColumn", "AnotherCol"])
        assert mapping == {}

    def test_case_insensitive(self):
        mapping = _normalise_headers(["SYMBOL", "SIDE"])
        assert mapping["SYMBOL"] == "symbol"
        assert mapping["SIDE"] == "direction"


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

class TestParseDt:
    def test_iso_format(self):
        dt = _parse_dt("2026-06-01T10:30:00")
        assert dt is not None
        assert dt.year == 2026
        assert dt.hour == 10

    def test_space_separated(self):
        dt = _parse_dt("2026-06-01 10:30:00")
        assert dt is not None
        assert dt.minute == 30

    def test_with_z_suffix(self):
        dt = _parse_dt("2026-06-01T10:30:00Z")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_none_returns_none(self):
        assert _parse_dt(None) is None

    def test_empty_returns_none(self):
        assert _parse_dt("") is None

    def test_garbage_returns_none(self):
        assert _parse_dt("not-a-date") is None

    def test_naive_made_utc(self):
        dt = _parse_dt("2026-06-01 10:00:00")
        assert dt.tzinfo == timezone.utc


# ---------------------------------------------------------------------------
# Float parsing
# ---------------------------------------------------------------------------

class TestParseFloat:
    def test_simple(self):
        assert _parse_float("1.23") == pytest.approx(1.23)

    def test_negative(self):
        assert _parse_float("-0.45") == pytest.approx(-0.45)

    def test_comma_separated(self):
        assert _parse_float("1,234.56") == pytest.approx(1234.56)

    def test_empty_string(self):
        assert _parse_float("") == 0.0

    def test_dash_placeholder(self):
        assert _parse_float("-") == 0.0

    def test_none(self):
        assert _parse_float(None) == 0.0


# ---------------------------------------------------------------------------
# Duration parsing
# ---------------------------------------------------------------------------

class TestParseDurationRaw:
    def test_computed_from_open_close(self):
        open_t = _utc(2026, 6, 1, 10, 0)
        close_t = _utc(2026, 6, 1, 10, 5)
        assert _parse_duration_raw(None, open_t, close_t) == 300

    def test_hhmmss_string(self):
        assert _parse_duration_raw("01:30:00", None, None) == 5400

    def test_hm_string(self):
        assert _parse_duration_raw("2h30m", None, None) == 9000

    def test_prefer_computed_over_raw(self):
        open_t = _utc(2026, 6, 1, 10, 0)
        close_t = _utc(2026, 6, 1, 10, 1)
        # Should use computed (60s) not raw string
        assert _parse_duration_raw("99h", open_t, close_t) == 60

    def test_none_with_no_times(self):
        result = _parse_duration_raw(None, None, None)
        assert result is None


# ---------------------------------------------------------------------------
# CSV loading
# ---------------------------------------------------------------------------

class TestLoadBinanceCsv:
    def test_standard_format_parsed(self, tmp_path):
        p = tmp_path / "pos.csv"
        _write_csv(p, [
            {
                "Symbol": "BTCUSDT", "Side": "Long",
                "Entry Price": "43000.00", "Close Price": "43500.00",
                "Realized PNL": "0.500", "Commission": "0.050",
                "Open Time": "2026-06-01 10:00:00",
                "Close Time": "2026-06-01 10:05:00",
            }
        ])
        rows, warnings = load_binance_csv(p)
        assert len(rows) == 1
        row = rows[0]
        assert row["symbol"] == "BTCUSDT"
        assert row["direction"] == "LONG"
        assert row["pnl_usdt"] == pytest.approx(0.5)
        assert row["commission_usdt"] == pytest.approx(0.05)
        assert row["net_pnl_usdt"] == pytest.approx(0.45)
        assert row["open_time"].year == 2026

    def test_sell_direction_normalised_to_short(self, tmp_path):
        p = tmp_path / "pos.csv"
        _write_csv(p, [{
            "Symbol": "ETHUSDT", "Side": "Sell",
            "Realized PNL": "-0.1", "Commission": "0.01",
            "Open Time": "2026-06-01 10:00:00",
        }])
        rows, _ = load_binance_csv(p)
        assert rows[0]["direction"] == "SHORT"

    def test_alternate_column_names(self, tmp_path):
        p = tmp_path / "pos.csv"
        _write_csv(p, [{
            "Symbol": "SOLUSDT", "Type": "SHORT",
            "Avg. Entry Price": "100.0", "Avg. Close Price": "99.0",
            "Closed PNL": "1.00", "Trading Fee": "0.07",
            "Opened Time": "2026-06-01 08:00:00",
            "Closed Time": "2026-06-01 08:10:00",
        }])
        rows, _ = load_binance_csv(p)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "SOLUSDT"
        assert rows[0]["direction"] == "SHORT"

    def test_invalid_direction_skipped(self, tmp_path):
        p = tmp_path / "pos.csv"
        _write_csv(p, [
            {"Symbol": "XUSDT", "Side": "NONE", "Realized PNL": "0.1",
             "Commission": "0.01", "Open Time": "2026-06-01 10:00:00"},
        ])
        rows, warnings = load_binance_csv(p)
        assert len(rows) == 0
        assert any("unrecognised direction" in w for w in warnings)

    def test_missing_open_time_skipped(self, tmp_path):
        p = tmp_path / "pos.csv"
        _write_csv(p, [
            {"Symbol": "XUSDT", "Side": "Long", "Realized PNL": "0.1",
             "Commission": "0.01", "Open Time": ""},
        ])
        rows, _ = load_binance_csv(p)
        assert len(rows) == 0

    def test_duration_computed_from_open_close(self, tmp_path):
        p = tmp_path / "pos.csv"
        _write_csv(p, [{
            "Symbol": "BTCUSDT", "Side": "Long",
            "Realized PNL": "0.5", "Commission": "0.05",
            "Open Time": "2026-06-01 10:00:00",
            "Close Time": "2026-06-01 10:05:00",
        }])
        rows, _ = load_binance_csv(p)
        assert rows[0]["duration_sec"] == 300


# ---------------------------------------------------------------------------
# Signal index + matching
# ---------------------------------------------------------------------------

class TestSignalMatching:
    def _make_index(self, signals: list[dict]) -> dict:
        return build_signal_index(signals)

    def test_exact_match(self):
        sig_ts = _utc(2026, 6, 1, 9, 55)
        sig = _make_signal(ts=sig_ts)
        index = self._make_index([sig])
        pos = _make_position(open_time=_utc(2026, 6, 1, 9, 56))
        result = match_position_to_signal(pos, index, window_sec=900)
        assert result is not None
        assert result["symbol"] == "BTCUSDT"

    def test_position_before_signal_within_skew_accepted(self):
        sig_ts = _utc(2026, 6, 1, 10, 0)
        sig = _make_signal(ts=sig_ts)
        index = self._make_index([sig])
        # Position 30s before signal (within 60s clock-skew tolerance)
        pos = _make_position(open_time=_utc(2026, 6, 1, 9, 59, 30))
        result = match_position_to_signal(pos, index, window_sec=900)
        assert result is not None

    def test_position_too_early_rejected(self):
        sig_ts = _utc(2026, 6, 1, 10, 0)
        sig = _make_signal(ts=sig_ts)
        index = self._make_index([sig])
        # Position 5 minutes BEFORE signal (outside -60s skew window)
        pos = _make_position(open_time=_utc(2026, 6, 1, 9, 54))
        result = match_position_to_signal(pos, index, window_sec=900)
        assert result is None

    def test_position_after_window_rejected(self):
        sig_ts = _utc(2026, 6, 1, 10, 0)
        sig = _make_signal(ts=sig_ts)
        index = self._make_index([sig])
        # Position 20 minutes after signal (outside 900s / 15min window)
        pos = _make_position(open_time=_utc(2026, 6, 1, 10, 20))
        result = match_position_to_signal(pos, index, window_sec=900)
        assert result is None

    def test_wrong_direction_not_matched(self):
        sig = _make_signal(direction="LONG")
        index = self._make_index([sig])
        pos = _make_position(direction="SHORT")
        result = match_position_to_signal(pos, index, window_sec=900)
        assert result is None

    def test_closest_signal_selected_when_multiple(self):
        sig_ts1 = _utc(2026, 6, 1, 9, 50)  # 10 min before position
        sig_ts2 = _utc(2026, 6, 1, 9, 57)  # 3 min before position — closer
        sig1 = _make_signal(setup="SR_FLIP_RETEST", ts=sig_ts1)
        sig2 = _make_signal(setup="DIVERGENCE_CONTINUATION", ts=sig_ts2)
        index = self._make_index([sig1, sig2])
        pos = _make_position(open_time=_utc(2026, 6, 1, 10, 0))
        result = match_position_to_signal(pos, index, window_sec=900)
        assert result is not None
        assert result["setup_class"] == "DIVERGENCE_CONTINUATION"

    def test_no_signals_returns_none(self):
        index = build_signal_index([])
        pos = _make_position()
        assert match_position_to_signal(pos, index) is None


# ---------------------------------------------------------------------------
# Alignment classification
# ---------------------------------------------------------------------------

class TestClassifyAlignment:
    def test_trending_up_long_is_trend_aligned(self):
        assert classify_alignment("TRENDING_UP", "LONG") == "trend-aligned"

    def test_trending_up_short_is_counter_trend(self):
        assert classify_alignment("TRENDING_UP", "SHORT") == "counter-trend"

    def test_trending_down_short_is_trend_aligned(self):
        assert classify_alignment("TRENDING_DOWN", "SHORT") == "trend-aligned"

    def test_trending_down_long_is_counter_trend(self):
        assert classify_alignment("TRENDING_DOWN", "LONG") == "counter-trend"

    def test_ranging_is_ranging(self):
        assert classify_alignment("RANGING", "LONG") == "ranging"
        assert classify_alignment("RANGING", "SHORT") == "ranging"

    def test_volatile_is_volatile(self):
        assert classify_alignment("VOLATILE", "LONG") == "volatile"

    def test_quiet_is_quiet(self):
        assert classify_alignment("QUIET", "SHORT") == "quiet"

    def test_empty_is_untagged(self):
        assert classify_alignment("", "LONG") == "untagged"

    def test_case_insensitive(self):
        assert classify_alignment("trending_up", "long") == "trend-aligned"


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

class TestAggregate:
    def test_matched_position_tagged_with_regime(self):
        sig_ts = _utc(2026, 6, 1, 9, 55)
        sig = _make_signal(regime="TRENDING_UP", ts=sig_ts)
        index = build_signal_index([sig])
        pos = _make_position(pnl=0.5, open_time=_utc(2026, 6, 1, 9, 56))
        result = aggregate([pos], index, window_sec=900)
        assert result["matched_count"] == 1
        assert result["unmatched_count"] == 0
        assert "TRENDING_UP" in result["by_regime"]

    def test_unmatched_counted_separately(self):
        index = build_signal_index([])  # no signals
        pos = _make_position()
        result = aggregate([pos], index, window_sec=900)
        assert result["unmatched_count"] == 1
        assert result["matched_count"] == 0

    def test_win_loss_counts(self):
        sig_ts = _utc(2026, 6, 1, 9, 55)
        sigs = [
            _make_signal(regime="TRENDING_UP", ts=sig_ts),
            _make_signal(regime="TRENDING_UP", symbol="ETHUSDT", ts=sig_ts),
        ]
        index = build_signal_index(sigs)
        positions = [
            _make_position(symbol="BTCUSDT", pnl=0.5, open_time=_utc(2026, 6, 1, 9, 56)),
            _make_position(symbol="ETHUSDT", pnl=-0.3, open_time=_utc(2026, 6, 1, 9, 56)),
        ]
        result = aggregate(positions, index, window_sec=900)
        reg = result["by_regime"]["TRENDING_UP"]
        assert reg["wins"] == 1
        assert reg["losses"] == 1
        assert reg["win_rate"] == pytest.approx(0.5)

    def test_alignment_counter_trend_tracked(self):
        sig_ts = _utc(2026, 6, 1, 9, 55)
        sig = _make_signal(regime="TRENDING_DOWN", direction="SHORT", ts=sig_ts)
        index = build_signal_index([sig])
        # Counter-trend LONG in TRENDING_DOWN regime
        pos = _make_position(direction="LONG", pnl=-0.2, open_time=_utc(2026, 6, 1, 9, 56))
        # Won't match because direction mismatch
        result = aggregate([pos], index, window_sec=900)
        # The LONG won't find the SHORT signal → unmatched
        assert result["unmatched_count"] == 1

    def test_trend_aligned_vs_counter_trend_same_regime(self):
        ts = _utc(2026, 6, 1, 9, 55)
        sig_long = _make_signal(symbol="BTCUSDT", direction="LONG", regime="TRENDING_UP", ts=ts)
        sig_short = _make_signal(symbol="ETHUSDT", direction="SHORT", regime="TRENDING_UP", ts=ts)
        index = build_signal_index([sig_long, sig_short])
        pos_long = _make_position(symbol="BTCUSDT", direction="LONG", pnl=0.4,
                                   open_time=_utc(2026, 6, 1, 9, 56))
        pos_short = _make_position(symbol="ETHUSDT", direction="SHORT", pnl=-0.2,
                                    open_time=_utc(2026, 6, 1, 9, 56))
        result = aggregate([pos_long, pos_short], index, window_sec=900)
        assert "trend-aligned" in result["by_alignment"]
        assert "counter-trend" in result["by_alignment"]
        aln = result["by_alignment"]
        assert aln["trend-aligned"]["count"] == 1
        assert aln["counter-trend"]["count"] == 1


# ---------------------------------------------------------------------------
# Report rendering (smoke — just checks it doesn't crash and has key labels)
# ---------------------------------------------------------------------------

class TestRenderReport:
    def _minimal_result(self) -> dict:
        ts = _utc(2026, 6, 1, 9, 55)
        sig = _make_signal(regime="TRENDING_UP", ts=ts)
        index = build_signal_index([sig])
        pos = _make_position(pnl=0.5, open_time=_utc(2026, 6, 1, 9, 56))
        return aggregate([pos], index, window_sec=900)

    def test_report_renders_without_error(self):
        result = self._minimal_result()
        report = render_report(result)
        assert isinstance(report, str)
        assert len(report) > 100

    def test_report_contains_alignment_section(self):
        result = self._minimal_result()
        report = render_report(result)
        assert "BY ALIGNMENT" in report

    def test_report_contains_regime_section(self):
        result = self._minimal_result()
        report = render_report(result)
        assert "BY ENTRY REGIME" in report

    def test_report_contains_setup_section(self):
        result = self._minimal_result()
        report = render_report(result)
        assert "BY SETUP CLASS" in report

    def test_report_shows_trend_aligned(self):
        result = self._minimal_result()
        report = render_report(result)
        assert "trend-aligned" in report

    def test_report_shows_key_findings(self):
        result = self._minimal_result()
        report = render_report(result)
        assert "KEY FINDINGS" in report

    def test_unmatched_section_shown_when_requested(self):
        index = build_signal_index([])
        pos = _make_position()
        result = aggregate([pos], index, window_sec=900)
        report = render_report(result, show_unmatched=True)
        assert "UNMATCHED POSITIONS" in report

    def test_unmatched_section_hidden_by_default(self):
        index = build_signal_index([])
        pos = _make_position()
        result = aggregate([pos], index, window_sec=900)
        report = render_report(result, show_unmatched=False)
        assert "UNMATCHED POSITIONS" not in report


# ---------------------------------------------------------------------------
# End-to-end: CSV → signals → report (file-based)
# ---------------------------------------------------------------------------

class TestEndToEnd:
    def test_full_pipeline(self, tmp_path):
        csv_path = tmp_path / "positions.csv"
        _write_csv(csv_path, [
            {
                "Symbol": "BTCUSDT", "Side": "Long",
                "Entry Price": "43000", "Close Price": "43500",
                "Realized PNL": "0.50", "Commission": "0.05",
                "Open Time": "2026-06-01 10:01:00",
                "Close Time": "2026-06-01 10:06:00",
            },
            {
                "Symbol": "ETHUSDT", "Side": "Short",
                "Entry Price": "3200", "Close Price": "3150",
                "Realized PNL": "0.30", "Commission": "0.03",
                "Open Time": "2026-06-01 10:02:00",
                "Close Time": "2026-06-01 10:12:00",
            },
            {
                "Symbol": "SOLUSDT", "Side": "Long",
                "Entry Price": "150", "Close Price": "148",
                "Realized PNL": "-0.20", "Commission": "0.02",
                "Open Time": "2026-06-01 10:03:00",
                "Close Time": "2026-06-01 10:08:00",
            },
        ])

        signals_path = tmp_path / "signal_history.json"
        signals_path.write_text(json.dumps([
            {
                "symbol": "BTCUSDT", "direction": "LONG",
                "entry_regime": "TRENDING_UP", "setup_class": "SR_FLIP_RETEST",
                "timestamp": "2026-06-01T09:58:00+00:00",
                "detected_at": "2026-06-01T09:58:00+00:00",
            },
            {
                "symbol": "ETHUSDT", "direction": "SHORT",
                "entry_regime": "TRENDING_DOWN", "setup_class": "DIVERGENCE_CONTINUATION",
                "timestamp": "2026-06-01T09:59:00+00:00",
                "detected_at": "2026-06-01T09:59:00+00:00",
            },
            {
                "symbol": "SOLUSDT", "direction": "LONG",
                "entry_regime": "QUIET", "setup_class": "FAR_SIDE_RECLAIM",
                "timestamp": "2026-06-01T10:00:00+00:00",
                "detected_at": "2026-06-01T10:00:00+00:00",
            },
        ]))

        positions, warnings = load_binance_csv(csv_path)
        assert len(positions) == 3
        assert not any("ERROR" in w for w in warnings)

        signals = load_signals(signals_path)
        index = build_signal_index(signals)
        result = aggregate(positions, index, window_sec=900)

        assert result["matched_count"] == 3
        assert result["unmatched_count"] == 0

        # BTCUSDT LONG in TRENDING_UP → trend-aligned
        # ETHUSDT SHORT in TRENDING_DOWN → trend-aligned
        # SOLUSDT LONG in QUIET → quiet
        assert result["by_alignment"]["trend-aligned"]["count"] == 2
        assert result["by_alignment"]["quiet"]["count"] == 1

        # QUIET has negative PNL
        q = result["by_alignment"]["quiet"]
        assert q["total_pnl"] == pytest.approx(-0.20)

        # Trend-aligned has positive PNL
        ta = result["by_alignment"]["trend-aligned"]
        assert ta["total_pnl"] == pytest.approx(0.80)

        report = render_report(result)
        assert "trend-aligned" in report
        assert "quiet" in report
        assert "QUIET" in report
