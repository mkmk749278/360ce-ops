"""What population is /signals/sar actually showing, and what is it made of?

Two defects the owner caught on 2026-07-28, from one 300-row export.

**The page truncated before it filtered.** ``reduce_sar_signals`` defaulted to
``limit=300`` and cut there, so ``filter_sar_signals`` ran on the newest 300
pairs — about 4.17 hours of a ledger holding roughly 2,000. That starves the
rarest and most important population hardest: the export carried 4 ``emitted``
rows against 152 ``enqueued`` and 144 ``suppressed``, and only the delivered
ones can justify changing what users receive. "Delivered to users" meant
"delivered, within the newest 300".

**And it averaged concentration silently.** 221 of the 300 rows sat in a
``(symbol, side, setup)`` cluster with more than one stamp. SLXUSDT SHORT
MOVER_TREND_PULLBACK alone produced 10 rows in 2h10m across a 0.37% entry
spread and supplied 36% of the entire resolved population. Counted per row that
population read 32% win / −0.364R; counted per move, 55% / +0.003R. The sign of
the arm's verdict was an artifact of re-detection.

This repo already carried the rule ("disclose concentration; don't silently
average it") from the three-row BUSDT case. Ten rows is the same rule, louder.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test")

from app.routes.sar_exit import (  # noqa: E402
    SAME_MOVE_PCT,
    TABLE_ROW_CAP,
    distinct_moves,
    filter_sar_signals,
    reduce_sar_signals,
    summarize_rows,
)

CTX = "ASIA/MARKDOWN/EXPANDED/BTC_FALLING"


def _arm(suffix: str, *, ts: float, cls: str | None, symbol: str, entry: float,
         provenance: str, **kw) -> dict:
    rec = {
        "gate_name": (
            "sar_exit_shadow:base" if suffix == "@SARBASE" else "sar_exit_shadow:trail"
        ),
        "setup_class": f"MOVER_TREND_PULLBACK{suffix}",
        "symbol": symbol, "side": "SHORT", "channel": "scalp",
        "entry": entry, "stop_loss": entry * 1.03, "tp1": entry * 0.97,
        "sl_distance": entry * 0.03,
        "context_key": CTX, "suppress_timestamp": ts,
        "classification": cls, "provenance": provenance,
        "exit_model": "trailing" if suffix == "@SAREXIT" else "static",
    }
    rec.update(kw)
    return rec


def _pair(ts: float, *, symbol="SLXUSDT", entry=0.09116, provenance="suppressed",
          resolved=True, exit_mult=0.99):
    """A stamped pair in the ledger's real shape — both arms, one candidate."""
    cls = "WOULD_WIN" if resolved else None
    return [
        _arm("@SARBASE", ts=ts, cls=cls, symbol=symbol, entry=entry,
             provenance=provenance),
        _arm("@SAREXIT", ts=ts + 0.001, cls=cls, symbol=symbol, entry=entry,
             provenance=provenance,
             trail_exit_price=(entry * exit_mult) if resolved else None,
             trail_exit_reason="trail" if resolved else None,
             trail_hold_min=90.0 if resolved else None),
    ]


class TestTruncationHappensAfterFiltering:
    def test_the_reducer_no_longer_truncates_by_default(self):
        """The cap moved to the route. A reducer that truncates cannot be
        filtered correctly by anything downstream of it."""
        ledger = []
        for i in range(400):
            ledger += _pair(1000.0 + i * 60, symbol=f"S{i}USDT", entry=1.0 + i)
        assert len(reduce_sar_signals(ledger)) == 400

    def test_a_rare_provenance_survives_a_large_ledger(self):
        """The defect, stated as a contract.

        One delivered signal, stamped oldest, buried under 400 newer suppressed
        candidates. Under the old ``limit=300``-inside-the-reducer it fell off
        the end before the source filter ever ran, and the page reported zero
        delivered rows while the ledger held one.
        """
        ledger = _pair(1.0, symbol="DEXEUSDT", entry=2.95, provenance="emitted")
        for i in range(400):
            ledger += _pair(1000.0 + i * 60, symbol=f"S{i}USDT", entry=1.0 + i)

        rows = reduce_sar_signals(ledger)
        emitted = filter_sar_signals(rows, source="emitted")
        assert len(emitted) == 1, "the delivered row must not be truncated away"
        assert emitted[0]["symbol"] == "DEXEUSDT"

        # And the old ordering really would have lost it — pinned so the
        # regression is visible rather than argued about.
        assert "DEXEUSDT" not in {r["symbol"] for r in rows[:300]}

    def test_the_table_cap_is_a_render_bound_not_a_measurement_bound(self):
        """Explicit ``limit`` still works — that is what the route uses for the
        rendered table, applied to the already-filtered rows."""
        ledger = []
        for i in range(TABLE_ROW_CAP + 50):
            ledger += _pair(1000.0 + i * 60, symbol=f"S{i}USDT", entry=1.0 + i)
        assert len(reduce_sar_signals(ledger, limit=TABLE_ROW_CAP)) == TABLE_ROW_CAP
        assert len(reduce_sar_signals(ledger)) == TABLE_ROW_CAP + 50


class TestDistinctMoves:
    def test_the_real_slx_cluster_counts_as_one_move(self):
        """Replayed from the owner's export: ten stamps, 0.37% entry spread."""
        entries = [0.09118, 0.09088, 0.09093, 0.09116, 0.09101,
                   0.09116, 0.09117, 0.09115, 0.09116, 0.09084]
        ledger = []
        for i, entry in enumerate(entries):
            ledger += _pair(1000.0 + i * 780, entry=entry)
        rows = reduce_sar_signals(ledger)
        assert len(rows) == 10
        assert distinct_moves(rows) == 1

    def test_a_move_outside_the_band_is_counted_separately(self):
        ledger = _pair(1000.0, entry=100.0) + _pair(2000.0, entry=100.0 + SAME_MOVE_PCT)
        assert distinct_moves(reduce_sar_signals(ledger)) == 2

    def test_different_symbols_are_never_one_move(self):
        ledger = _pair(1000.0, symbol="AAAUSDT", entry=100.0)
        ledger += _pair(1001.0, symbol="BBBUSDT", entry=100.0)
        assert distinct_moves(reduce_sar_signals(ledger)) == 2

    def test_drift_accumulates_from_the_open_move_not_from_the_previous_row(self):
        """A slow walk must eventually open a new move.

        Comparing each row only against its immediate predecessor would let a
        setup drift arbitrarily far while every step stayed inside the band —
        one 'move' spanning 5%, which is not one move.
        """
        ledger = []
        for i in range(6):
            ledger += _pair(1000.0 + i * 600, entry=100.0 + i * 0.4)
        assert distinct_moves(reduce_sar_signals(ledger)) > 1

    def test_rows_without_a_usable_entry_are_skipped_not_counted(self):
        ledger = _pair(1000.0, entry=100.0)
        ledger += _pair(2000.0, entry=0.0)
        assert distinct_moves(reduce_sar_signals(ledger)) == 1

    def test_empty_is_zero(self):
        assert distinct_moves([]) == 0


class TestSummaryDisclosesConcentration:
    def test_it_reports_moves_beside_trades(self):
        entries = [0.09118, 0.09088, 0.09093, 0.09116, 0.09101]
        ledger = []
        for i, entry in enumerate(entries):
            ledger += _pair(1000.0 + i * 780, entry=entry)
        summary = summarize_rows(reduce_sar_signals(ledger))
        assert summary["n"] == 5
        assert summary["moves"] == 1
        assert summary["closed"] == 5
        assert summary["closed_moves"] == 1
        assert summary["rows_per_move"] == pytest.approx(5.0)

    def test_an_unconcentrated_population_reads_one_row_per_move(self):
        ledger = []
        for i in range(5):
            ledger += _pair(1000.0 + i * 600, symbol=f"S{i}USDT", entry=1.0 + i)
        summary = summarize_rows(reduce_sar_signals(ledger))
        assert summary["rows_per_move"] == pytest.approx(1.0)

    def test_running_rows_have_no_resolved_moves(self):
        """``closed_moves`` must follow the same resolved-only denominator the
        R averages use — mirroring the engine, denominators included."""
        ledger = _pair(1000.0, resolved=False)
        summary = summarize_rows(reduce_sar_signals(ledger))
        assert summary["n"] == 1 and summary["moves"] == 1
        assert summary["closed"] == 0 and summary["closed_moves"] == 0
        assert summary["rows_per_move"] is None

    def test_the_headline_still_averages_every_row(self):
        """Disclosure, not de-duplication. Dropping rows is a judgement call and
        the reducer does not get to make it silently — both counts are shown and
        the averages remain over all rows."""
        ledger = _pair(1000.0, entry=100.0, exit_mult=0.94)     # a winner (SHORT)
        ledger += _pair(2000.0, entry=100.05, exit_mult=0.94)   # same move again
        summary = summarize_rows(reduce_sar_signals(ledger))
        assert summary["closed"] == 2
        assert summary["closed_moves"] == 1
        assert summary["win_rate"] == pytest.approx(1.0)
