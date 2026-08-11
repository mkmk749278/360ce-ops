"""`/track-record` and the Lumin app must be ONE book.

Engine PR (2026-08-11) added `src/track_record.py` and `GET /api/track-record`,
which reduces the same `signal_performance.json` this page reduces so the app's
Pulse tab can render the delivered-signal record for a user who has never
traded. That makes two implementations of one number, in two repos that cannot
import each other.

This system has already paid a session for exactly that shape: on 2026-07-31 the
dark `sar_*` replay and the live SAR arm both printed "Parabolic SAR", agreed
within 0.10pp on the easy 79% of candidates, and diverged by +0.73pp on the 21%
where their definitions differed — and the agreement on the majority is what made
it invisible. `MEASUREMENT_SUFFIXES` drifted for a week the same way.

So `CONTRACT_ROWS` and `CONTRACT_EXPECTED` below are **byte-identical to 360-v2's
`tests/test_track_record.py`**, and each repo asserts its own reducers against
them. A change to either side's math fails on that side's CI, with the vector
naming what it broke.

The expected values are **derived by hand** in the comments — never recorded from
either implementation's output, which would agree with that implementation by
construction and pin nothing. Same rule `tests/test_atr_trail_contract.py` carries.

Where the two reducers legitimately differ, the difference is asserted rather
than papered over — see `TestKnownDivergences`. Ops carries panels the app has no
use for (concurrency, geometry-stamp health, per-trade sorting) and the app
carries a cumulative percentage ops does not render; those are additions, not
disagreements, and neither may move a shared number.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.routes.track_record import (
    bucket_rows,
    reduce_records,
    resolve_range,
    filter_rows,
    summarize,
)

UTC = timezone.utc


def _ts(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=UTC).timestamp()


def _rec(symbol, direction, entry, pnl, closed, **extra):
    out = {
        "symbol": symbol,
        "direction": direction,
        "setup_class": "MOVER_TREND_PULLBACK",
        "entry": entry,
        "pnl_pct": pnl,
        "terminal_outcome_timestamp": closed,
    }
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# THE SHARED CONTRACT VECTOR — keep byte-identical with 360-v2
# ---------------------------------------------------------------------------

#: Eight closed signals across four UTC days inside a seven-day window, chosen so that every rule this
#: module carries is exercised by at least one row and none of them cancel out.
CONTRACT_ROWS = [
    # --- 2026-08-04: THE BOUNDARY ROW --------------------------------------
    # The oldest day the 7-day window claims, and this row closed at 04:00 —
    # BEFORE the hour of CONTRACT_NOW. A window starting at ``now - 7 days``
    # (12:00) drops it while still labelling the bucket 2026-08-04, which is
    # precisely the fault that flipped a day's sign on the ops page: the oldest
    # bucket holds a fragment and renders identically to a complete day. The
    # row exists so that fault fails CI rather than passing unnoticed.
    _rec("LINKUSDT", "SHORT", 20.0, -2.00, _ts(2026, 8, 4, 4)),
    # --- 2026-08-08: deliberately empty ------------------------------------
    # Inside the window, nothing closed. It must be ABSENT from items, not a
    # zero point — an invented zero is indistinguishable from a real flat day.
    # --- 2026-08-09 --------------------------------------------------------
    # Two entries into one BTC move: 30000 and 30090 are 0.30% apart, inside
    # SAME_MOVE_PCT (0.5), so they are ONE move and two trades.
    _rec("BTCUSDT", "LONG", 30000.0, 2.00, _ts(2026, 8, 9, 4)),
    _rec("BTCUSDT", "LONG", 30090.0, -1.00, _ts(2026, 8, 9, 6)),
    # Same symbol, opposite side — a different (symbol, direction) key, so a
    # second move however close the price is.
    _rec("BTCUSDT", "SHORT", 30090.0, 0.50, _ts(2026, 8, 9, 8)),
    # --- 2026-08-10 --------------------------------------------------------
    # +0.05% gross is a LOSS net of a 0.07% round trip. A trade that made less
    # than its own fee did not make money.
    _rec("ETHUSDT", "SHORT", 2000.0, 0.05, _ts(2026, 8, 10, 1)),
    # No readable pnl_pct: counted in n, excluded from every money figure,
    # never scored zero.
    _rec("SOLUSDT", "LONG", 100.0, None, _ts(2026, 8, 10, 2)),
    # No close timestamp at all -> undateable, in no bucket.
    _rec("XRPUSDT", "LONG", 1.0, 1.00, None, timestamp=None, create_timestamp=None),
    # --- 2026-08-11 (== "today" for CONTRACT_NOW) --------------------------
    _rec("ADAUSDT", "LONG", 0.5, 3.00, _ts(2026, 8, 11, 2)),
]

#: The moment the contract is evaluated at. Inside 2026-08-11, so that day is
#: the partial one; a 7-day window snaps back to midnight on 2026-08-04.
#: 7 rather than 4 because ops offers PRESET windows (1d/7d/30d/90d) while
#: the engine takes an arbitrary day count — the contract has to be a
#: window both surfaces can actually be asked for.
CONTRACT_NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)

#: The window the contract is evaluated over.
CONTRACT_DAYS = 7

#: Derived by hand. Seven dateable rows, six with a readable move.
#:
#: gross moves:      -2.00, +2.00, -1.00, +0.50, +0.05, +3.00  -> sum +2.55
#: net (each -0.07): -2.07, +1.93, -1.07, +0.43, -0.02, +2.93  -> sum +2.13
#: wins on NET:      +1.93, +0.43, +2.93                        -> 3W / 3L
#: fee:              6 priced rows x 100 USDT x 0.07%           -> 0.42
#: gross_usd:        100 x 2.55 / 100                           -> +2.55
#: net_usd:          100 x 2.13 / 100                           -> +2.13
#: avg gross:        2.55 / 6                                   -> +0.425
#: avg net:          2.13 / 6                                   -> +0.355
#: moves:            BTC LONG (2 rows, 0.30% apart) = 1, BTC SHORT = 1,
#:                   ETH = 1, SOL = 1, ADA = 1, LINK = 1        -> 6
#: n counts the un-priced SOL row; n_pnl does not.
CONTRACT_EXPECTED = {
    "range_start": "2026-08-04",
    "total_records": 8,
    "undateable": 1,
    "summary": {
        "n": 7,
        "moves": 6,
        "n_pnl": 6,
        "no_pnl": 1,
        "wins": 3,
        "losses": 3,
        "win_rate": 0.5,
        "gross_usd": 2.55,
        "fee_usd": 0.42,
        "net_usd": 2.13,
        "total_pnl_pct": 2.55,
        "avg_pnl_pct": 0.425,
        "total_net_pct": 2.13,
        "avg_net_pct": 0.355,
        "best_pnl_pct": 3.00,
        "worst_pnl_pct": -2.00,
    },
    # Oldest first. 2026-08-08 closed nothing and is therefore ABSENT — an
    # empty day is not a zero-PnL day. The curve carries its level across it.
    "days": [
        # 2026-08-04: the boundary row. -2.00 gross, -2.07 net, 0W / 1L.
        {"date": "2026-08-04", "n": 1, "moves": 1, "wins": 0, "losses": 1,
         "net_usd": -2.07, "cum_net_usd": -2.07, "partial_reason": None},
        # 2026-08-09: +2.00 -1.00 +0.50 = +1.50 gross, 3 fees = 0.21,
        #             net +1.29. Wins on net: +1.93, +0.43 -> 2W / 1L.
        #             Moves: BTC LONG (one) + BTC SHORT (one) = 2.
        {"date": "2026-08-09", "n": 3, "moves": 2, "wins": 2, "losses": 1,
         "net_usd": 1.29, "cum_net_usd": -0.78, "partial_reason": None},
        # 2026-08-10: +0.05 gross on one priced row, -0.02 net; the SOL row is
        #             counted in n and in nothing else.
        {"date": "2026-08-10", "n": 2, "moves": 2, "wins": 0, "losses": 1,
         "net_usd": -0.02, "cum_net_usd": -0.80, "partial_reason": None},
        # 2026-08-11: +3.00 gross, +2.93 net, and it is TODAY -> in_progress.
        {"date": "2026-08-11", "n": 1, "moves": 1, "wins": 1, "losses": 0,
         "net_usd": 2.93, "cum_net_usd": 2.13, "partial_reason": "in_progress"},
    ],
}

AMOUNT = 100.0
FEE = 0.07


def _pipeline():
    """Drive the page's OWN reducers, in the order the route drives them.

    Deliberately not a re-implementation: `_page_context` composes
    `reduce_records` -> `resolve_range` -> `filter_rows` and the route then calls
    `summarize` / `bucket_rows`. Reproducing that composition here is what makes
    this a test of the page rather than of a helper.
    """
    all_rows = reduce_records(CONTRACT_ROWS)
    start, end, _label = resolve_range(
        f"{CONTRACT_DAYS}d", "", "", now=CONTRACT_NOW
    )
    rows = filter_rows(all_rows, start=start, end=end)
    summary = summarize(rows, amount=AMOUNT, fee_pct=FEE)
    buckets = bucket_rows(
        rows, "day", amount=AMOUNT, fee_pct=FEE,
        start=start, end=end, now=CONTRACT_NOW,
    )
    return all_rows, start, summary, buckets


class TestContractVector:
    """Ops' numbers and the engine's must be the same book."""

    def test_window_start_matches(self):
        _all, start, _s, _b = _pipeline()
        assert start.strftime("%Y-%m-%d") == CONTRACT_EXPECTED["range_start"]

    def test_ledger_counts_match(self):
        all_rows, _start, _s, _b = _pipeline()
        assert len(all_rows) == CONTRACT_EXPECTED["total_records"]
        undateable = sum(1 for r in all_rows if r.get("closed_at") is None)
        assert undateable == CONTRACT_EXPECTED["undateable"]

    def test_summary_matches_the_shared_vector(self):
        _all, _start, summary, _b = _pipeline()
        for key, want in CONTRACT_EXPECTED["summary"].items():
            assert summary[key] == pytest.approx(want, abs=1e-9), key

    def test_days_match_the_shared_vector(self):
        _all, _start, _s, buckets = _pipeline()
        # `bucket_rows` renders newest-first for the table; the engine serves
        # oldest-first for a chart. Same days, same numbers, one reversal —
        # asserted here rather than assumed, because "the app's chart is
        # backwards" is exactly the kind of defect that looks like data.
        got = list(reversed(buckets))
        assert [b["bucket"] for b in got] == [
            d["date"] for d in CONTRACT_EXPECTED["days"]
        ]
        for bucket, want in zip(got, CONTRACT_EXPECTED["days"]):
            for key, value in want.items():
                if key == "date":
                    continue
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    assert bucket[key] == pytest.approx(value, abs=1e-9), (
                        f"{want['date']}.{key}"
                    )
                else:
                    assert bucket[key] == value, f"{want['date']}.{key}"

    def test_a_day_that_closed_nothing_is_absent_not_zero(self):
        _all, _start, _s, buckets = _pipeline()
        assert "2026-08-08" not in {b["bucket"] for b in buckets}

    def test_the_boundary_row_survives_the_window(self):
        """The 04:00 row on the start day is IN.

        `resolve_range` snaps presets to midnight; un-snapped it would drop this
        row while still printing the same `range_start`, taking the book from 7
        trades to 6 and the net from +2.13 to +4.20. The row count is the
        assertion that bites — the label is identical either way.
        """
        _all, _start, summary, buckets = _pipeline()
        assert summary["n"] == 7
        assert summary["net_usd"] == pytest.approx(2.13)
        oldest = buckets[-1]
        assert oldest["bucket"] == "2026-08-04" and oldest["n"] == 1


class TestFeeAndWinRate:
    def test_win_rate_counts_on_the_net_money(self):
        """+0.05% gross against a 0.07% round trip is a loss on both surfaces."""
        rows = reduce_records([_rec("E", "LONG", 1.0, 0.05, _ts(2026, 8, 10))])
        assert summarize(rows, amount=AMOUNT, fee_pct=0.07)["wins"] == 0
        assert summarize(rows, amount=AMOUNT, fee_pct=0.0)["wins"] == 1


class TestKnownDivergences:
    """Where the two surfaces legitimately differ, say so out loud.

    A silent divergence is the defect this file exists to prevent; a *stated*
    one is a design decision. Neither of these moves a shared number.
    """

    def test_ops_carries_panels_the_app_does_not(self):
        """Concurrency and geometry-stamp health are owner diagnostics.

        They answer "can I fund this book" and "is the producer healthy", which
        are not questions a subscriber's Pulse card asks. They are additions to
        ops' summary, never changes to a shared figure.
        """
        _all, _start, summary, _b = _pipeline()
        for ops_only in ("geometry_awaiting_stamp", "geometry_no_geometry",
                         "rows_per_move"):
            assert ops_only in summary
        # ...and every shared key is still exactly the contract's.
        for key, want in CONTRACT_EXPECTED["summary"].items():
            assert summary[key] == pytest.approx(want, abs=1e-9), key

    def test_ops_buckets_are_keyed_bucket_and_the_engine_keys_date(self):
        """One field name, two spellings, deliberately not unified.

        `bucket` is ops' key across day / week / month granularities; the engine
        serves days only and calls it `date`. Renaming either would touch a
        template or a Dart model for no gain — but a reader comparing the two
        payloads should find this asserted rather than wonder.
        """
        _all, _start, _s, buckets = _pipeline()
        assert "bucket" in buckets[0] and "date" not in buckets[0]
