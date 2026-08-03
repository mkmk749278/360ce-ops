"""The track record must describe delivered signals honestly, or not at all.

Built 2026-07-28 for the owner's problem: paper trading is per-user and starts
empty, so a new subscriber waits a week to a month before their own book says
anything — while the engine has been recording every closed signal all along.

This page reads that record. It is **recorded, never reconstructed**: the owner
explicitly ruled out backfilling paper trades by replaying candles, so nothing
on this page may come from a replay. That is what separates it from the Profit
tab's free-run / dark-signals / exit-backtest pages, which are counterfactuals.

The rules these tests pin were all learned on other pages first:

* PnL % and money, never R (owner, 2026-08-03). R divided by a stamp 421 of 448
  rows did not carry, so it described 6% of the book while looking like the book
  — and it equalises nothing here, because the engine sizes at a fixed notional.
* The position size is an INPUT, so every dollar figure is linear in it and the
  page states the assumption rather than hiding one.
* Charge the fee and say what it is: the round trip is ~10x the window's edge.
* Refuse rather than clamp — a row with no readable move is counted and excluded,
  never scored flat; a row with no entry stamp is named in the concurrency panel
  rather than assumed either simultaneous or sequential.
* Bucket by CLOSE time, because a day's PnL is the PnL realised that day.
* Disclose concentration — the rule /signals/sar needed twice.
"""
from __future__ import annotations

import os
import re
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test")

from app.routes.track_record import (  # noqa: E402
    DEFAULT_AMOUNT_USDT,
    DEFAULT_FEE_PCT,
    PER_TRADE_LIMIT,
    SAME_MOVE_PCT,
    bucket_completeness,
    bucket_rows,
    bucket_span,
    decorate_money,
    distinct_moves,
    filter_rows,
    geometry_stamp_reason,
    local_day_boundary_note,
    money,
    net_pnl_pct,
    peak_concurrency,
    reduce_records,
    resolve_range,
    sort_rows,
    summarize,
)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


_UNSET = object()


def _rec(*, closed: datetime | None = NOW, entry=100.0, stop=99.0, pnl=2.0,
         symbol="BTCUSDT", direction="LONG", setup="SR_FLIP_RETEST",
         regime="TRENDING_UP", sl_dist_pct=_UNSET, **kw) -> dict:
    """A record in ``signal_performance.json``'s real shape (asdict(SignalRecord)).

    ``sl_dist_pct`` defaults to the entry→stop distance, which is what the engine
    stamps for a trade whose stop never moved. Pass it explicitly to model a
    moved stop (BE shift / trail), where the two diverge — that divergence is the
    whole reason the field exists. Pass ``None`` to model a pre-2026-08-01
    record, which carries no stamp at all.
    """
    rec = {
        "signal_id": kw.pop("signal_id", "sig"),
        "channel": "360_SCALP",
        "symbol": symbol,
        "direction": direction,
        "entry": entry,
        "stop_loss": stop,
        # ``pnl=None`` models a record the engine wrote with no realised move —
        # rare, and exactly the row every money figure must exclude rather than
        # score flat, so the fixture has to be able to build one.
        "hit_tp": 1 if (pnl or 0) > 0 else 0,
        "hit_sl": (pnl or 0) < 0,
        "pnl_pct": pnl,
        "confidence": 70.0,
        "setup_class": setup,
        "entry_regime": regime,
        "terminal_outcome_timestamp": closed.timestamp() if closed else None,
    }
    if sl_dist_pct is _UNSET:
        # A record with no usable stop is one the engine could not stamp either
        # — both come from a signal that never recorded its entry geometry — so
        # deriving a distance from a zero/absent stop would invent a 100% risk.
        try:
            sl_dist_pct = (
                abs(float(entry) - float(stop)) / float(entry) * 100.0
                if entry and stop
                else 0.0
            )
        except (TypeError, ValueError, ZeroDivisionError):
            sl_dist_pct = 0.0
    if sl_dist_pct is not None:
        rec["sl_distance_pct_at_entry"] = sl_dist_pct
    rec.update(kw)
    return rec


class TestMoney:
    """The unit the owner banks: a recorded move, times a size he typed, minus
    the cost of taking it."""

    def test_net_is_gross_minus_the_round_trip(self):
        row = {"pnl_pct": 2.0}
        assert net_pnl_pct(row, 0.07) == pytest.approx(1.93)

    def test_a_zero_fee_leaves_the_gross_book_untouched(self):
        assert net_pnl_pct({"pnl_pct": -1.25}, 0.0) == pytest.approx(-1.25)

    def test_dollars_are_linear_in_the_amount(self):
        """The engine sizes at a fixed notional, which is what makes this a
        multiplication rather than a model. 5x the size, 5x the money, and the
        percentages do not move."""
        assert money(1.93, 100.0) == pytest.approx(1.93)
        assert money(1.93, 500.0) == pytest.approx(9.65)

    def test_a_row_with_no_readable_move_refuses_rather_than_scoring_flat(self):
        assert net_pnl_pct({"pnl_pct": None}, 0.07) is None
        assert net_pnl_pct({"pnl_pct": "abc"}, 0.07) is None
        assert money(None, 100.0) is None

    def test_the_owners_window_reconciles(self):
        """The arithmetic behind the change, pinned so a refactor cannot drift it.

        Three trades averaging the owner's 30d book: gross is nearly flat and the
        fee is what makes it a loss. That relationship — cost ~10x edge — is the
        finding, so it is asserted rather than described.
        """
        rows = reduce_records([
            _rec(signal_id="a", pnl=1.0), _rec(signal_id="b", pnl=-1.0),
            _rec(signal_id="c", pnl=0.02),
        ])
        s = summarize(rows, amount=100.0, fee_pct=0.07)
        assert s["gross_usd"] == pytest.approx(0.02)
        assert s["fee_usd"] == pytest.approx(0.21)     # 3 trades x 0.07% of $100
        assert s["net_usd"] == pytest.approx(-0.19)
        assert s["net_usd"] < s["gross_usd"], "the fee must never flatter the book"

    def test_the_win_rate_counts_on_the_net_money(self):
        """A trade that made less than its own fee did not make money, and the
        page says its win rate is on the net."""
        rows = reduce_records([_rec(pnl=0.05), _rec(signal_id="b", pnl=0.5)])
        s = summarize(rows, amount=100.0, fee_pct=0.07)
        assert s["wins"] == 1 and s["losses"] == 1

    def test_pnl_covers_rows_that_R_would_have_dropped(self):
        """The regression this whole change exists for.

        Four delivered trades, one of which carries the engine's entry-risk
        stamp. R described that single row and called it the book; PnL needs no
        denominator, so it keeps all four.
        """
        rows = reduce_records([
            _rec(signal_id="a", pnl=2.0, entry=100.0, stop=99.0),
            _rec(signal_id="b", pnl=-3.0, sl_dist_pct=None),
            _rec(signal_id="c", pnl=-3.0, sl_dist_pct=None),
            _rec(signal_id="d", pnl=-3.0, sl_dist_pct=None),
        ])
        stamped = [r for r in rows if r["geometry_stamp_reason"] is None]
        assert len(stamped) == 1, "the fixture must model the 421-of-448 shape"
        s = summarize(rows, amount=100.0, fee_pct=0.0)
        assert s["n"] == 4 and s["n_pnl"] == 4
        assert s["avg_pnl_pct"] == pytest.approx(-1.75)
        assert s["win_rate"] == pytest.approx(0.25)

    def test_each_row_carries_its_own_money_so_the_table_and_headline_agree(self):
        rows = decorate_money(
            reduce_records([_rec(pnl=2.0)]), amount=250.0, fee_pct=0.07
        )
        assert rows[0]["net_pct"] == pytest.approx(1.93)
        assert rows[0]["gross_usd"] == pytest.approx(5.0)
        assert rows[0]["fee_usd"] == pytest.approx(0.175)
        assert rows[0]["net_usd"] == pytest.approx(4.825)

    def test_the_defaults_are_the_owners(self):
        assert DEFAULT_AMOUNT_USDT == 100.0
        assert DEFAULT_FEE_PCT == 0.07


class TestGeometryStampHealth:
    """Nothing on this page divides by the entry risk any more, but the stamp is
    still reported because ``no_geometry`` is a live producer-side fault and this
    is the only surface it shows on. Not-yet-stamped and could-not-be-stamped
    have different fixes, so they are never pooled into one sentence."""

    def test_a_stamped_record_has_no_reason(self):
        assert geometry_stamp_reason(_rec(entry=100.0, stop=97.0)) is None

    def test_a_pre_stamp_record_is_awaiting_not_a_fault(self):
        assert geometry_stamp_reason(_rec(sl_dist_pct=None)) == "awaiting_engine_stamp"

    def test_a_current_engine_record_with_no_geometry_is_a_fault(self):
        """Stamped by today's engine and still zero — that is upstream failing to
        record an entry→SL distance, and it does not age out."""
        assert geometry_stamp_reason(_rec(sl_dist_pct=0.0)) == "no_geometry"

    def test_the_two_are_counted_apart_in_the_summary(self):
        rows = reduce_records([
            _rec(signal_id="a", entry=100.0, stop=97.0, pnl=-3.0),
            _rec(signal_id="b", sl_dist_pct=None),
            _rec(signal_id="c", sl_dist_pct=0.0),
        ])
        s = summarize(rows)
        assert s["geometry_awaiting_stamp"] == 1
        assert s["geometry_no_geometry"] == 1

    def test_a_missing_stamp_shrinks_no_population_on_this_page(self):
        """The point of removing R: two of these three rows have no entry risk,
        and all three are still measured."""
        rows = reduce_records([
            _rec(signal_id="a", entry=100.0, stop=97.0, pnl=-3.0),
            _rec(signal_id="b", sl_dist_pct=None, pnl=1.0),
            _rec(signal_id="c", sl_dist_pct=0.0, pnl=1.0),
        ])
        s = summarize(rows)
        assert s["n"] == 3 and s["n_pnl"] == 3 and s["no_pnl"] == 0


class TestCloseTimeBucketing:
    def test_a_trade_is_bucketed_by_when_it_closed_not_when_it_opened(self):
        """Bucketing by entry would credit Monday with a trade that closed
        Thursday, which is not what a daily PnL means."""
        opened = datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc)
        closed = datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)
        rows = reduce_records([
            _rec(closed=closed, create_timestamp=opened.timestamp())
        ])
        buckets = bucket_rows(rows, "day")
        assert [b["bucket"] for b in buckets] == ["2026-07-23"]

    def test_it_falls_back_to_the_record_timestamp(self):
        """Older rows may lack a terminal timestamp but always carry one."""
        rec = _rec(closed=None)
        rec["timestamp"] = NOW.timestamp()
        rows = reduce_records([rec])
        assert rows[0]["closed_at"] is not None

    def test_an_undateable_record_is_kept_but_never_bucketed(self):
        """Dropping it silently would shrink the ledger count with no cause
        given; bucketing it under a guessed date would be worse."""
        rec = _rec(closed=None)
        rec.pop("terminal_outcome_timestamp")
        rows = reduce_records([rec])
        assert len(rows) == 1 and rows[0]["closed_at"] is None
        assert bucket_rows(rows, "day") == []

    def test_week_and_month_granularity(self):
        rows = reduce_records([
            _rec(closed=datetime(2026, 7, 1, tzinfo=timezone.utc)),
            _rec(closed=datetime(2026, 7, 28, tzinfo=timezone.utc)),
        ])
        assert len(bucket_rows(rows, "month")) == 1
        assert len(bucket_rows(rows, "week")) == 2

    def test_buckets_are_newest_first(self):
        rows = reduce_records([
            _rec(closed=datetime(2026, 7, 1, tzinfo=timezone.utc)),
            _rec(closed=datetime(2026, 7, 5, tzinfo=timezone.utc)),
        ])
        assert [b["bucket"] for b in bucket_rows(rows, "day")] == [
            "2026-07-05", "2026-07-01",
        ]

    def test_an_unknown_granularity_falls_back_rather_than_raising(self):
        rows = reduce_records([_rec()])
        assert bucket_rows(rows, "fortnight")[0]["bucket"] == "2026-07-28"


class TestSummaryDenominators:
    def test_money_figures_divide_by_n_pnl_not_by_n(self):
        """Mirror the engine's denominators, not just its numerators — a rate
        over 'all rows' where the real one is over 'readable rows' agrees only
        until the two populations differ."""
        rows = reduce_records([
            _rec(pnl=2.0),
            _rec(signal_id="b", pnl=None),   # no readable move
        ])
        s = summarize(rows, fee_pct=0.0)
        assert s["n"] == 2
        assert s["n_pnl"] == 1
        assert s["no_pnl"] == 1
        assert s["avg_pnl_pct"] == pytest.approx(2.0), "the blank row must not dilute"

    def test_nothing_readable_yields_none_not_zero(self):
        """None renders as an em-dash; 0.0 would render as a flat result."""
        s = summarize(reduce_records([_rec(pnl=None)]))
        assert s["avg_pnl_pct"] is None and s["total_pnl_pct"] is None
        assert s["net_usd"] is None and s["fee_usd"] is None
        assert s["win_rate"] is None
        assert s["n"] == 1

    def test_empty_selection_is_all_none(self):
        s = summarize([])
        assert s["n"] == 0 and s["avg_pnl_pct"] is None and s["rows_per_move"] is None
        assert s["net_usd"] is None

    def test_best_and_worst_are_the_gross_moves(self):
        """Reported gross so the fee is not subtracted twice in the reader's head
        — the page says which columns are net."""
        rows = reduce_records([
            _rec(pnl=3.0), _rec(signal_id="b", pnl=-1.0),
        ])
        s = summarize(rows, fee_pct=0.07)
        assert s["best_pnl_pct"] == pytest.approx(3.0)
        assert s["worst_pnl_pct"] == pytest.approx(-1.0)

    def test_the_cumulative_column_runs_oldest_first(self):
        """The table renders newest-first, so the top row must carry the whole
        selection rather than the oldest day's."""
        rows = reduce_records([
            _rec(closed=datetime(2026, 7, 26, tzinfo=timezone.utc), pnl=1.0),
            _rec(signal_id="b", closed=datetime(2026, 7, 27, tzinfo=timezone.utc), pnl=2.0),
        ])
        buckets = bucket_rows(rows, "day", amount=100.0, fee_pct=0.0)
        assert [b["bucket"] for b in buckets] == ["2026-07-27", "2026-07-26"]
        assert buckets[0]["cum_net_usd"] == pytest.approx(3.0)
        assert buckets[1]["cum_net_usd"] == pytest.approx(1.0)


class TestConcurrency:
    """What the account had to carry: the most positions open at one time."""

    def _row(self, opened: datetime, closed: datetime, **kw) -> dict:
        return reduce_records([
            _rec(closed=closed, dispatch_timestamp=opened.timestamp(), **kw)
        ])[0]

    def test_overlapping_trades_need_funding_together(self):
        rows = [
            self._row(NOW, NOW + timedelta(hours=2), signal_id="a"),
            self._row(NOW + timedelta(hours=1), NOW + timedelta(hours=3), signal_id="b"),
        ]
        c = peak_concurrency(rows)
        assert c["peak"] == 2
        assert c["peak_at"] == NOW + timedelta(hours=1)

    def test_sequential_trades_reuse_the_same_capital(self):
        rows = [
            self._row(NOW, NOW + timedelta(hours=1), signal_id="a"),
            self._row(NOW + timedelta(hours=2), NOW + timedelta(hours=3), signal_id="b"),
        ]
        assert peak_concurrency(rows)["peak"] == 1

    def test_a_close_at_the_same_instant_as_an_open_is_not_double_funded(self):
        rows = [
            self._row(NOW, NOW + timedelta(hours=1), signal_id="a"),
            self._row(NOW + timedelta(hours=1), NOW + timedelta(hours=2), signal_id="b"),
        ]
        assert peak_concurrency(rows)["peak"] == 1

    def test_a_row_with_no_entry_stamp_is_named_never_assumed(self):
        """Assuming it overlapped would overstate the balance the book needed;
        assuming it did not would understate it. Neither is knowable, so it is
        counted and excluded."""
        rows = [
            self._row(NOW, NOW + timedelta(hours=2), signal_id="a"),
            reduce_records([_rec(signal_id="b")])[0],  # no dispatch/create stamp
        ]
        c = peak_concurrency(rows)
        assert c["peak"] == 1
        assert c["undated"] == 1 and c["bad_window"] == 0
        assert c["measured"] == 1

    def test_a_close_before_its_open_is_counted_apart_from_a_missing_stamp(self):
        """Present-and-wrong is a producer fault; absent is an old record. Pooling
        them would report missing data where the data is there."""
        rows = [self._row(NOW, NOW - timedelta(hours=1), signal_id="a")]
        c = peak_concurrency(rows)
        assert c["peak"] == 0
        assert c["bad_window"] == 1 and c["undated"] == 0
        assert c["measured"] == 0

    def test_an_empty_book_needs_nothing(self):
        c = peak_concurrency([])
        assert c["peak"] == 0 and c["peak_at"] is None


class TestSorting:
    def test_an_unknown_sort_key_falls_back_to_newest_closed_first(self):
        rows = reduce_records([
            _rec(signal_id="old", closed=NOW - timedelta(days=1)),
            _rec(signal_id="new", closed=NOW),
        ])
        assert [r["signal_id"] for r in sort_rows(rows, "'; DROP", "desc")] == [
            "new", "old",
        ]

    def test_it_sorts_on_the_money(self):
        rows = decorate_money(reduce_records([
            _rec(signal_id="a", pnl=1.0), _rec(signal_id="b", pnl=-2.0),
            _rec(signal_id="c", pnl=5.0),
        ]), amount=100.0, fee_pct=0.0)
        assert [r["signal_id"] for r in sort_rows(rows, "net_usd", "desc")] == [
            "c", "a", "b",
        ]
        assert [r["signal_id"] for r in sort_rows(rows, "net_usd", "asc")] == [
            "b", "a", "c",
        ]

    def test_an_unreadable_value_sorts_last_in_either_direction(self):
        """A blank is not a low number — floating it to the top of an ascending
        sort would make "worst trades" mean "trades we could not read"."""
        rows = decorate_money(reduce_records([
            _rec(signal_id="a", pnl=1.0), _rec(signal_id="blank", pnl=None),
            _rec(signal_id="b", pnl=-2.0),
        ]), amount=100.0, fee_pct=0.0)
        assert sort_rows(rows, "net_usd", "asc")[-1]["signal_id"] == "blank"
        assert sort_rows(rows, "net_usd", "desc")[-1]["signal_id"] == "blank"


class TestConcentration:
    def test_repeated_entries_into_one_move_count_once(self):
        rows = reduce_records([
            _rec(entry=100.0, closed=NOW - timedelta(minutes=30)),
            _rec(entry=100.2, closed=NOW - timedelta(minutes=20)),
            _rec(entry=100.3, closed=NOW - timedelta(minutes=10)),
        ])
        assert summarize(rows)["n"] == 3
        assert summarize(rows)["moves"] == 1
        assert summarize(rows)["rows_per_move"] == pytest.approx(3.0)

    def test_a_move_outside_the_band_counts_separately(self):
        rows = reduce_records([
            _rec(entry=100.0), _rec(entry=100.0 + SAME_MOVE_PCT + 0.01),
        ])
        assert distinct_moves(rows) == 2

    def test_opposite_sides_on_one_symbol_are_different_moves(self):
        rows = reduce_records([
            _rec(entry=100.0, direction="LONG"),
            _rec(entry=100.0, direction="SHORT"),
        ])
        assert distinct_moves(rows) == 2

    def test_drift_anchors_on_the_open_move_not_the_previous_row(self):
        """Comparing each row only to its predecessor would let one 'move' span
        arbitrarily far while every step stayed inside the band."""
        rows = reduce_records([
            _rec(entry=100.0 + i * 0.4, closed=NOW + timedelta(minutes=i))
            for i in range(6)
        ])
        assert distinct_moves(rows) > 1

    def test_concentration_never_drops_a_row_from_the_averages(self):
        """Disclosure, not de-duplication — which count to act on is a
        judgement call a reducer does not get to make silently."""
        rows = reduce_records([
            _rec(entry=100.0, pnl=2.0, closed=NOW - timedelta(minutes=5)),
            _rec(entry=100.0, pnl=2.0, closed=NOW),
        ])
        s = summarize(rows, amount=100.0, fee_pct=0.0)
        assert s["moves"] == 1 and s["n_pnl"] == 2
        assert s["total_pnl_pct"] == pytest.approx(4.0), "both rows must still be averaged"
        assert s["net_usd"] == pytest.approx(4.0), "and both must still cost money"


class TestPartialBuckets:
    """A bucket that measured less than the period its label claims must say so.

    Paid for on 2026-08-03: `window=1d` rendered `2026-08-02` as +12.29% over 3
    trades, 3W/0L. The real day was −0.40% over 11, 4W/7L — the rolling window
    had cut 8 trades, 7 of them losers, and the sign flipped. The row looked
    exactly like a complete day.
    """

    # 12:00 UTC — deliberately mid-day, so an unsnapped window would cut.
    MID = datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc)

    def _day(self, key: str, **kw) -> dict:
        return bucket_completeness(key, "day", now=self.MID, **kw)

    def test_a_snapped_preset_cannot_cut_a_day(self):
        for window in ("1d", "7d", "30d", "90d"):
            start, end, _ = resolve_range(window, "", "", now=self.MID)
            got = self._day("2026-08-02", start=start, end=end)
            assert got["partial_reason"] is None, f"{window} cut a whole past day"
            assert got["span_fraction"] == pytest.approx(1.0)

    def test_the_current_day_is_in_progress_and_a_past_day_is_not(self):
        """Asserted together: a change that flags everything, or nothing, fails."""
        start, _, _ = resolve_range("7d", "", "", now=self.MID)
        assert self._day("2026-08-03", start=start)["partial_reason"] == "in_progress"
        assert self._day("2026-08-02", start=start)["partial_reason"] is None

    def test_an_unsnapped_start_is_still_caught(self):
        """The guard survives the snap. If `resolve_range` ever regresses, or a
        caller passes its own start, the badge is still there."""
        cut = datetime(2026, 8, 2, 6, 52, 42, tzinfo=timezone.utc)
        got = self._day("2026-08-02", start=cut)
        assert got["partial_reason"] == "window_cut"
        assert got["covers_from"] == cut
        assert got["span_fraction"] == pytest.approx(1 - (6 * 60 + 52.7) / 1440, abs=1e-3)

    def test_snapping_days_is_not_snapping_weeks_or_months(self):
        """A 7d window does not begin on a Monday, nor a 30d one on the 1st."""
        wed = datetime(2026, 8, 5, 6, 52, tzinfo=timezone.utc)
        s7, _, _ = resolve_range("7d", "", "", now=wed)
        s30, _, _ = resolve_range("30d", "", "", now=wed)
        week = bucket_completeness("2026-W31", "week", start=s7, now=wed)
        month = bucket_completeness("2026-07", "month", start=s30, now=wed)
        assert week["partial_reason"] == "window_cut"
        assert month["partial_reason"] == "window_cut"
        # ...and the current week/month are in progress, not cut.
        assert bucket_completeness(
            "2026-W32", "week", start=s7, now=wed
        )["partial_reason"] == "in_progress"

    def test_the_two_reasons_are_never_both_set(self):
        """window_cut wins — it is the one the reader can act on."""
        cut = datetime(2026, 8, 3, 6, 0, tzinfo=timezone.utc)   # inside today
        got = self._day("2026-08-03", start=cut)
        assert got["partial_reason"] == "window_cut"

    def test_an_end_before_the_bucket_ends_is_also_a_cut(self):
        _, end, _ = resolve_range("custom", "2026-08-01", "2026-08-02", now=self.MID)
        # end is exclusive-midnight of the 3rd, so the 2nd is whole...
        assert self._day("2026-08-02", end=end)["partial_reason"] is None
        # ...but an end inside the day is not.
        mid = datetime(2026, 8, 2, 10, 0, tzinfo=timezone.utc)
        assert self._day("2026-08-02", end=mid)["partial_reason"] == "window_cut"

    def test_all_time_flags_only_the_current_period(self):
        start, end, _ = resolve_range("all", "", "", now=self.MID)
        assert start is None and end is None
        assert self._day("2026-07-01", start=start, end=end)["partial_reason"] is None
        assert self._day("2026-08-03", start=start, end=end)["partial_reason"] == "in_progress"

    def test_an_unparseable_key_says_cannot_say_rather_than_complete(self):
        got = bucket_completeness("not-a-date", "day", now=self.MID)
        assert got["partial_reason"] is None
        assert got["covers_from"] is None and got["span_fraction"] is None

    def test_the_owners_day_is_whole_again(self):
        """The reported bug, as data.

        11 trades on 2026-08-02: 8 closing before 06:52 UTC (1W/7L, −12.68%
        total) and 3 after (3W/0L, +12.29%). Under the old rolling start the
        bucket showed only the second group and read +12.29%; snapped, it must
        report the whole day and come out negative.
        """
        early = [_rec(signal_id=f"e{i}", pnl=p,
                      closed=datetime(2026, 8, 2, 3, i, tzinfo=timezone.utc))
                 for i, p in enumerate([1.4, -1.9, -1.9, -1.8, -1.9, -1.8, -1.9, -2.85])]
        late = [_rec(signal_id=f"l{i}", pnl=p,
                     closed=datetime(2026, 8, 2, h, 0, tzinfo=timezone.utc))
                for i, (h, p) in enumerate([(9, 3.88), (12, 4.54), (19, 3.86)])]
        rows = reduce_records(early + late)

        start, end, _ = resolve_range("1d", "", "", now=datetime(
            2026, 8, 3, 6, 52, 42, tzinfo=timezone.utc))
        kept = filter_rows(rows, start=start, end=end)
        buckets = bucket_rows(kept, "day", amount=100.0, fee_pct=0.0,
                              start=start, end=end,
                              now=datetime(2026, 8, 3, 6, 52, 42, tzinfo=timezone.utc))
        day = next(b for b in buckets if b["bucket"] == "2026-08-02")

        assert day["n"] == 11, "the window must no longer cut the day"
        assert (day["wins"], day["losses"]) == (4, 7)
        assert day["total_pnl_pct"] < 0, "the day was a loss; +12.29% was the tail"
        assert day["partial_reason"] is None, "and it is a whole day, so no badge"


class TestBucketSpan:
    """The inverse of `_bucket_key` — what a bucket label CLAIMS to cover."""

    def test_a_day_claims_a_day(self):
        assert bucket_span("2026-08-02", "day") == (
            datetime(2026, 8, 2, tzinfo=timezone.utc),
            datetime(2026, 8, 3, tzinfo=timezone.utc),
        )

    def test_an_iso_week_claims_monday_to_monday(self):
        start, end = bucket_span("2026-W31", "week")
        assert start.weekday() == 0 and (end - start) == timedelta(days=7)

    def test_a_month_claims_its_own_length_not_thirty_days(self):
        """February and July are different lengths; a fixed +30d would misreport
        the fraction measured on every month."""
        assert bucket_span("2026-02", "month") == (
            datetime(2026, 2, 1, tzinfo=timezone.utc),
            datetime(2026, 3, 1, tzinfo=timezone.utc),
        )
        assert bucket_span("2026-12", "month")[1] == datetime(
            2027, 1, 1, tzinfo=timezone.utc
        )

    def test_an_unparseable_key_refuses(self):
        assert bucket_span("fortnight", "day") is None
        assert bucket_span("", "week") is None


class TestDayBoundaryNote:
    """Every figure is UTC. This only says where a UTC day ends locally."""

    def test_it_names_the_local_rollover(self):
        assert local_day_boundary_note("Asia/Kolkata", now=NOW) == "05:30 IST"

    def test_utc_reports_itself(self):
        assert local_day_boundary_note("UTC", now=NOW) == "00:00 UTC"

    def test_an_unknown_or_empty_zone_says_nothing_rather_than_guessing(self):
        """A wrong offset is worse than no offset — the template then just says
        UTC, which is true."""
        assert local_day_boundary_note("Not/AZone", now=NOW) == ""
        assert local_day_boundary_note("", now=NOW) == ""


class TestFiltersAndRange:
    def test_window_presets_snap_to_midnight(self):
        """NOW is 12:00, so an unsnapped start would land mid-day and cut that
        day's bucket in half — the 2026-08-03 fault."""
        start, end, label = resolve_range("7d", "", "", now=NOW)
        assert start == datetime(2026, 7, 21, tzinfo=timezone.utc)
        assert start.hour == 0 and start.minute == 0 and start.second == 0
        assert end is None
        assert "7" in label and "2026-07-21" in label

    def test_all_is_open_ended(self):
        assert resolve_range("all", "", "", now=NOW) == (None, None, "all time")

    def test_a_custom_end_date_is_inclusive(self):
        """A user picking 2026-07-28 means all of that day, not up to midnight."""
        _, end, _ = resolve_range("custom", "2026-07-01", "2026-07-28", now=NOW)
        assert end == datetime(2026, 7, 29, tzinfo=timezone.utc)
        rows = reduce_records([_rec(closed=datetime(2026, 7, 28, 23, 0, tzinfo=timezone.utc))])
        assert len(filter_rows(rows, end=end)) == 1

    def test_an_unparseable_custom_date_goes_open_ended(self):
        """Never 500, and never silently return zero rows — that would read as
        'no trades' when it means 'bad input'."""
        start, end, _ = resolve_range("custom", "not-a-date", "", now=NOW)
        assert start is None and end is None

    def test_filters_compose(self):
        rows = reduce_records([
            _rec(regime="TRENDING_UP", setup="A", symbol="BTCUSDT"),
            _rec(regime="RANGING", setup="A", symbol="BTCUSDT"),
            _rec(regime="TRENDING_UP", setup="B", symbol="ETHUSDT"),
        ])
        assert len(filter_rows(rows, regime="TRENDING_UP")) == 2
        assert len(filter_rows(rows, regime="TRENDING_UP", setup="A")) == 1
        assert len(filter_rows(rows, symbol="ETHUSDT")) == 1

    def test_undateable_rows_drop_out_of_any_dated_range(self):
        rec = _rec(closed=None)
        rec.pop("terminal_outcome_timestamp")
        rows = reduce_records([rec, _rec()])
        assert len(filter_rows(rows, start=NOW - timedelta(days=1))) == 1


class TestRegimeProvenance:
    def test_a_pre_change_record_reads_unplaced_not_a_real_regime(self):
        """``entry_regime`` shipped 2026-07-28 (engine #817). Records closed
        before it carry nothing, and the regime at entry cannot be recovered —
        so they get their own visible bucket rather than being folded into a
        real regime, which would corrupt exactly the comparison the filter
        exists to make."""
        rec = _rec()
        rec.pop("entry_regime")
        assert reduce_records([rec])[0]["regime"] == "UNPLACED"

    def test_an_empty_regime_string_is_also_unplaced(self):
        assert reduce_records([_rec(regime="")])[0]["regime"] == "UNPLACED"

    def test_a_real_regime_is_preserved_verbatim(self):
        assert reduce_records([_rec(regime="VOLATILE")])[0]["regime"] == "VOLATILE"


class TestDegradation:
    def test_a_non_list_ledger_yields_no_rows_rather_than_raising(self):
        assert reduce_records({"error": "engine-data not mounted"}) == []
        assert reduce_records(None) == []

    def test_junk_entries_are_skipped_individually(self):
        assert len(reduce_records([_rec(), "nonsense", None, 42])) == 1


# ---------------------------------------------------------------------------
# Route level — the page must render, and its copy must match its numbers
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.data_volume import DataVolumeReader  # noqa: E402
from app.main import app  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


class TestRoute:
    def _stub(self, monkeypatch, records):
        monkeypatch.setattr(
            DataVolumeReader, "signal_performance", lambda self: records
        )

    def test_requires_auth(self):
        with TestClient(app) as client:
            r = client.get("/track-record", follow_redirects=False)
            assert r.status_code in (302, 303, 307, 401)

    def test_it_renders_buckets(self, monkeypatch):
        self._stub(monkeypatch, [
            _rec(entry=100.0, stop=99.0, pnl=2.0, closed=NOW),
            _rec(entry=100.0, stop=99.0, pnl=-1.0, closed=NOW - timedelta(days=1)),
        ])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record?window=all")
            assert r.status_code == 200
            assert "Track record" in r.text
            assert "2026-07-28" in r.text

    def test_it_says_recorded_not_reconstructed(self, monkeypatch):
        """Copy is part of the measurement. This page must never be mistaken
        for the Profit tab's replays — the owner ruled out backfill explicitly."""
        self._stub(monkeypatch, [_rec()])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record?window=all")
            assert "recorded" in r.text.lower()
            assert "reconstructed" in r.text.lower()

    def test_the_page_quotes_no_R(self, monkeypatch):
        """Owner, 2026-08-03: "show only in PnL percentage".

        The page still *explains* why R is gone — that paragraph is the record of
        the decision — but no figure on it carries an R unit.
        """
        self._stub(monkeypatch, [
            _rec(entry=100.0, stop=99.0, pnl=2.0),
            _rec(signal_id="b", entry=100.0, stop=97.0, pnl=-3.0),
        ])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record?window=all")
            assert r.status_code == 200
            assert not re.search(r"[-+]?\d+\.\d+R\b", r.text), "an R figure is rendered"
            assert "Why there is no R here" in r.text

    def test_a_row_with_no_entry_risk_is_still_measured(self, monkeypatch):
        """The regression the change exists for: R dropped these rows silently,
        and 421 of 448 in the owner's window were this row."""
        self._stub(monkeypatch, [_rec(sl_dist_pct=None, pnl=-2.0)])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record?window=all&fee_pct=0")
            assert r.status_code == 200
            assert "1 trades" in r.text or "1 trade" in r.text
            assert "-2.00%" in r.text

    def test_the_amount_and_fee_round_trip_into_the_page(self, monkeypatch):
        self._stub(monkeypatch, [_rec(pnl=2.0)])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record?window=all&amount=500&fee_pct=0")
            assert r.status_code == 200
            # +2% of 500 USDT, no fee.
            assert "+10.00" in r.text
            assert 'value="500"' in r.text

    def test_the_fee_is_charged_and_named(self, monkeypatch):
        self._stub(monkeypatch, [_rec(pnl=2.0)])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record?window=all&amount=100&fee_pct=0.07")
            assert r.status_code == 200
            assert "+1.93" in r.text, "net must be gross minus the round trip"
            assert "0.07" in r.text, "the rate must be on screen, not implied"

    def test_a_bad_amount_or_fee_does_not_500(self, monkeypatch):
        """A typo must fall back to the default, never crash and never silently
        become zero — a zero notional would render a book that earned nothing.

        The record carries a real entry stamp so the balance panel has a non-zero
        requirement to warn against; without one the underfunded assertion below
        would pass whatever the parsing did.
        """
        self._stub(monkeypatch, [
            _rec(pnl=2.0, dispatch_timestamp=(NOW - timedelta(hours=1)).timestamp()),
        ])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record?window=all&amount=abc&fee_pct=&balance=xyz")
            assert r.status_code == 200
            assert 'value="100"' in r.text
            assert "Underfunded" not in r.text, (
                "an unreadable balance is 'not asked', not 'you have zero' — "
                "warning against a balance the owner never claimed is a fault "
                "report about the reader"
            )

    def test_the_balance_panel_flags_an_underfunded_book(self, monkeypatch):
        self._stub(monkeypatch, [
            _rec(signal_id="a", pnl=1.0, closed=NOW + timedelta(hours=2),
                 dispatch_timestamp=NOW.timestamp()),
            _rec(signal_id="b", pnl=1.0, closed=NOW + timedelta(hours=3),
                 dispatch_timestamp=(NOW + timedelta(hours=1)).timestamp()),
        ])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record?window=all&amount=100&balance=150")
            assert r.status_code == 200
            assert "Underfunded" in r.text, "200 needed against 150 entered"

    def test_the_page_states_its_day_boundary(self, monkeypatch):
        """A date with no zone is the same omission as a percentage with no
        denominator — it is what let a 01:18-IST trade read as "yesterday"."""
        self._stub(monkeypatch, [_rec()])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record?window=all")
            assert r.status_code == 200
            assert "UTC" in r.text
            assert "05:30 IST" in r.text

    def test_the_partial_bucket_note_renders_even_when_nothing_is_flagged(
        self, monkeypatch
    ):
        """A check that appears only when it fires teaches the reader that its
        absence means "fine" when it equally means the check stopped running."""
        self._stub(monkeypatch, [_rec(closed=NOW - timedelta(days=3))])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record?window=all")
            assert "widening the window brings them back" in r.text

    def test_todays_bucket_is_badged_in_progress(self, monkeypatch):
        """Asserted on "% elapsed", which only the row badge emits. The footer
        paragraph also contains the words "in progress", so asserting those would
        pass even with every badge removed."""
        self._stub(monkeypatch, [
            _rec(closed=datetime.now(tz=timezone.utc) - timedelta(minutes=5)),
        ])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record?window=1d")
            assert r.status_code == 200
            assert "% elapsed" in r.text

    def test_the_bucket_csv_carries_the_caveat(self, monkeypatch):
        """An export that drops the caveat is the same fault one file over — a
        spreadsheet is exactly where a part-period gets averaged in.

        Asserted on a VALUE in a data row, not on the header: `_EXPORT_COLS` is
        static, so a header check passes even when nothing populates the column.
        """
        self._stub(monkeypatch, [
            _rec(closed=datetime.now(tz=timezone.utc) - timedelta(minutes=5)),
        ])
        with TestClient(app) as client:
            _login(client)
            body = client.get("/track-record/export.csv?window=all").text
            header, *data = [ln for ln in body.splitlines() if ln.strip()]
            assert "partial_reason" in header and "covers_from_iso" in header
            assert any("in_progress" in row for row in data)

    def test_a_bad_granularity_does_not_500(self, monkeypatch):
        self._stub(monkeypatch, [_rec()])
        with TestClient(app) as client:
            _login(client)
            assert client.get("/track-record?granularity=fortnight").status_code == 200

    def test_an_unavailable_ledger_renders_the_error(self, monkeypatch):
        self._stub(monkeypatch, {"error": "engine-data not mounted"})
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record")
            assert r.status_code == 200
            assert "engine-data not mounted" in r.text

    def test_csv_export_honours_the_filter(self, monkeypatch):
        self._stub(monkeypatch, [
            _rec(regime="TRENDING_UP", closed=NOW),
            _rec(regime="RANGING", closed=NOW - timedelta(days=2)),
        ])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record/export.csv?window=all&regime=RANGING")
            assert r.status_code == 200
            body = r.text
            assert "bucket" in body and "net_usd" in body
            assert "total_r" not in body, "the R columns are gone with the page's R"
            assert "2026-07-26" in body and "2026-07-28" not in body

    def test_the_per_trade_csv_is_uncapped_and_priced(self, monkeypatch):
        """The row cap is a render bound. A truncated export would be #97
        wearing a download button."""
        self._stub(monkeypatch, [
            _rec(signal_id=f"s{i}", pnl=2.0, closed=NOW - timedelta(minutes=i))
            for i in range(PER_TRADE_LIMIT + 10)
        ])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record/trades.csv?window=all&amount=100&fee_pct=0.07")
            assert r.status_code == 200
            lines = [ln for ln in r.text.splitlines() if ln.strip()]
            assert len(lines) == PER_TRADE_LIMIT + 11, "header + every row"
            assert "net_usd" in lines[0]

    def test_the_per_trade_table_caps_and_says_so(self, monkeypatch):
        self._stub(monkeypatch, [
            _rec(signal_id=f"s{i}", pnl=2.0, closed=NOW - timedelta(minutes=i))
            for i in range(PER_TRADE_LIMIT + 10)
        ])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record?window=all")
            assert r.status_code == 200
            assert f"newest {PER_TRADE_LIMIT}" in r.text
