"""The track record must describe delivered signals honestly, or not at all.

Built 2026-07-28 for the owner's problem: paper trading is per-user and starts
empty, so a new subscriber waits a week to a month before their own book says
anything — while the engine has been recording every closed signal all along.

This page reads that record. It is **recorded, never reconstructed**: the owner
explicitly ruled out backfilling paper trades by replaying candles, so nothing
on this page may come from a replay. That is what separates it from the Profit
tab's free-run / dark-signals / exit-backtest pages, which are counterfactuals.

The rules these tests pin were all learned on other pages first:

* R, not portfolio %, because a portfolio return needs an invented position size.
* Refuse rather than clamp — a record without a usable entry risk has no R
  and is never 0R. The denominator is the risk the trade was SIZED for, never the
  stop on the record: the engine moves that stop in place as the trade runs.
* Bucket by CLOSE time, because a day's PnL is the PnL realised that day.
* Disclose concentration — the rule /signals/sar needed twice.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test")

from app.routes.track_record import (  # noqa: E402
    SAME_MOVE_PCT,
    bucket_rows,
    distinct_moves,
    filter_rows,
    record_r,
    reduce_records,
    resolve_range,
    summarize,
    unscored_reason,
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
        "hit_tp": 1 if pnl > 0 else 0,
        "hit_sl": pnl < 0,
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


class TestR:
    def test_r_is_move_over_stop_distance(self):
        # entry 100, stop 99 → 1% risk. A +2% move is +2R.
        assert record_r(_rec(entry=100.0, stop=99.0, pnl=2.0)) == pytest.approx(2.0)

    def test_a_full_stop_out_is_minus_one_r(self):
        assert record_r(_rec(entry=100.0, stop=97.0, pnl=-3.0)) == pytest.approx(-1.0)

    def test_it_is_side_agnostic_because_pnl_pct_already_carries_the_sign(self):
        long_r = record_r(_rec(entry=100.0, stop=99.0, pnl=2.0, direction="LONG"))
        short_r = record_r(_rec(entry=100.0, stop=101.0, pnl=2.0, direction="SHORT"))
        assert long_r == pytest.approx(short_r)

    def test_a_missing_stop_refuses_rather_than_scoring_zero(self):
        """The whole point. 0.0 would drag every average toward zero and make a
        data gap read as mediocre performance."""
        assert record_r(_rec(stop=0.0)) is None
        assert record_r(_rec(stop=None)) is None

    def test_a_zero_width_stop_refuses(self):
        assert record_r(_rec(entry=100.0, stop=100.0)) is None

    def test_a_junk_record_refuses_rather_than_raising(self):
        assert record_r({"entry": "abc", "stop_loss": 1, "pnl_pct": 1}) is None
        assert record_r({}) is None

    def test_a_break_even_stop_out_is_not_a_full_loss(self):
        """The nine rows that changed this denominator (2026-08-01).

        The engine moves a signal's stop in place, so a trade BE-shifted and then
        stopped out for −0.1% arrives here with ``stop_loss == entry``. Dividing
        by *that* made it −1.00R — identical to a trade that gave back its whole
        designed 3% risk. It is a −0.03R scratch.
        """
        rec = _rec(entry=100.0, stop=100.0, pnl=-0.1, sl_dist_pct=3.0)
        assert record_r(rec) == pytest.approx(-0.1 / 3.0)

    def test_a_trailed_winner_divides_by_the_risk_taken_not_the_trailed_stop(self):
        """Same defect, other sign. A stop trailed into profit shrinks or inverts
        the old denominator and silently rescales the win."""
        rec = _rec(entry=100.0, stop=101.5, pnl=4.0, sl_dist_pct=2.0)
        assert record_r(rec) == pytest.approx(2.0)

    def test_a_pre_stamp_record_refuses_rather_than_falling_back_to_the_stop(self):
        """Records closed before the engine carried the field have no honest
        denominator — their stop has already been moved. Falling back to
        ``stop_loss`` here would silently reinstate the bug for exactly the rows
        that have it."""
        rec = _rec(entry=100.0, stop=97.0, pnl=-3.0, sl_dist_pct=None)
        assert "sl_distance_pct_at_entry" not in rec
        assert record_r(rec) is None


class TestUnscoredReasons:
    """Not-yet-stamped and could-not-be-stamped are different states with
    different fixes, so the page never pools them into one sentence."""

    def test_a_scored_record_has_no_reason(self):
        assert unscored_reason(_rec(entry=100.0, stop=97.0)) is None

    def test_a_pre_stamp_record_is_awaiting_not_a_fault(self):
        rec = _rec(sl_dist_pct=None)
        assert unscored_reason(rec) == "awaiting_engine_stamp"

    def test_a_current_engine_record_with_no_geometry_is_a_fault(self):
        """Stamped by today's engine and still zero — that is upstream failing to
        record an entry→SL distance, and it does not age out."""
        rec = _rec(sl_dist_pct=0.0)
        assert unscored_reason(rec) == "no_geometry"

    def test_the_two_are_counted_apart_in_the_summary(self):
        rows = reduce_records([
            _rec(signal_id="a", entry=100.0, stop=97.0, pnl=-3.0),
            _rec(signal_id="b", sl_dist_pct=None),
            _rec(signal_id="c", sl_dist_pct=0.0),
        ])
        s = summarize(rows)
        assert s["scored"] == 1
        assert s["unscored"] == 2
        assert s["unscored_awaiting_stamp"] == 1
        assert s["unscored_no_geometry"] == 1


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
    def test_r_figures_divide_by_scored_not_by_n(self):
        """Mirror the engine's denominators, not just its numerators — a rate
        over 'all rows' where the real one is over 'scored rows' agrees only
        until the two populations differ."""
        rows = reduce_records([
            _rec(entry=100.0, stop=99.0, pnl=2.0),
            _rec(stop=0.0, pnl=2.0),   # no usable stop → no R
        ])
        s = summarize(rows)
        assert s["n"] == 2
        assert s["scored"] == 1
        assert s["unscored"] == 1
        assert s["avg_r"] == pytest.approx(2.0), "the unscored row must not dilute"

    def test_win_rate_is_over_scored_rows(self):
        rows = reduce_records([
            _rec(pnl=2.0), _rec(pnl=-1.0, stop=99.0), _rec(stop=0.0, pnl=5.0),
        ])
        s = summarize(rows)
        assert s["wins"] == 1 and s["losses"] == 1
        assert s["win_rate"] == pytest.approx(0.5)

    def test_nothing_scored_yields_none_not_zero(self):
        """None renders as an em-dash; 0.0 would render as a flat result."""
        s = summarize(reduce_records([_rec(stop=0.0)]))
        assert s["avg_r"] is None and s["total_r"] is None
        assert s["win_rate"] is None
        assert s["n"] == 1

    def test_empty_selection_is_all_none(self):
        s = summarize([])
        assert s["n"] == 0 and s["avg_r"] is None and s["rows_per_move"] is None

    def test_best_and_worst_come_from_scored_rows(self):
        rows = reduce_records([
            _rec(entry=100.0, stop=99.0, pnl=3.0),
            _rec(entry=100.0, stop=99.0, pnl=-1.0),
        ])
        s = summarize(rows)
        assert s["best_r"] == pytest.approx(3.0)
        assert s["worst_r"] == pytest.approx(-1.0)


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
        # Identical geometry on both rows so the assertion isolates "was any row
        # dropped" — differing entries would also change each row's stop distance
        # and therefore its R, making a failure ambiguous between the two causes.
        rows = reduce_records([
            _rec(entry=100.0, stop=99.0, pnl=2.0, closed=NOW - timedelta(minutes=5)),
            _rec(entry=100.0, stop=99.0, pnl=2.0, closed=NOW),
        ])
        s = summarize(rows)
        assert s["moves"] == 1 and s["scored"] == 2
        assert s["total_r"] == pytest.approx(4.0), "both rows must still be averaged"


class TestFiltersAndRange:
    def test_window_presets_resolve_to_a_start(self):
        start, end, label = resolve_range("7d", "", "", now=NOW)
        assert start == NOW - timedelta(days=7) and end is None
        assert "7d" in label

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

    def test_an_unscored_population_says_so_rather_than_showing_zero(self, monkeypatch):
        self._stub(monkeypatch, [_rec(stop=0.0)])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record?window=all")
            assert r.status_code == 200
            assert "no R to quote" in r.text
            assert "0.000R" not in r.text

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
            assert "bucket" in body
            assert "2026-07-26" in body and "2026-07-28" not in body
