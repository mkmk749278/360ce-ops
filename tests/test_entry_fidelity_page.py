"""The recorded book beside the same exits priced from the tape.

``entry`` on a closed-signal record is the close of the candle the evaluator
triggered on, and every figure on ``/track-record`` divides by it. The order
goes out seconds later. Measured against Binance's own USD-M 1m tape
(2026-09-07, 605 of the 652 rows closed in the 30 days to 09-06): mean drift
**+0.226%**, already moved with the trade on **67%** of rows, and the page's
**+0.342%/trade** is **+0.118%** when the same exits are priced from the tape.

These pin the rules the panel carries, each already in this repo arriving at a
new surface:

* **Beside, never instead** (owner). Nothing recorded is recomputed, and there
  is deliberately no blended third number — one figure over both books would
  move with the coverage rate rather than with the market.
* **Coverage leads**, and an unstamped row is a NAMED refusal rather than a
  zero folded into the average. The observation is knowable exactly once, so
  there is no backfill and a low count is the honest state, not a fault.
* **The panel filters with the table** (#90/#91).
* **The refusal table iterates the DATA**, so a reason ops has never heard of
  renders badged rather than vanishing — ``MEASUREMENT_SUFFIXES`` wearing
  another hat.
* **The port is pinned against the engine**, driven rather than fixtured: a
  fixture chooses a shape and then agrees with you about it.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test")

from app.data_sources import entry_fidelity as ef  # noqa: E402
from app.routes.track_record import reduce_records  # noqa: E402

NOW = datetime(2026, 9, 7, 12, 0, tzinfo=timezone.utc)


def _rec(**kw) -> dict:
    rec = {
        "signal_id": kw.pop("signal_id", "sig"),
        "symbol": "BTCUSDT",
        "direction": kw.pop("direction", "LONG"),
        "entry": kw.pop("entry", 100.0),
        "stop_loss": 97.0,
        "pnl_pct": kw.pop("pnl", -3.0),
        "setup_class": "MOVER_TREND_PULLBACK",
        "outcome_label": "SL_HIT",
        "hit_sl": True,
        "hit_tp": 0,
        "terminal_outcome_timestamp": NOW.timestamp(),
        "dispatch_timestamp": NOW.timestamp() - 600,
    }
    rec.update(kw)
    return rec


# ---------------------------------------------------------------------------
# The port, driven against the engine's own module
# ---------------------------------------------------------------------------


def _engine_module():
    """Import the ENGINE's entry_fidelity, not a shape this repo invented."""
    engine = Path(__file__).resolve().parents[2] / "360-v2"
    if not engine.exists():
        pytest.skip("engine repo not checked out beside ops")
    sys.path.insert(0, str(engine))
    try:
        from src import entry_fidelity as engine_ef  # type: ignore

        return engine_ef
    finally:
        sys.path.remove(str(engine))


@pytest.mark.parametrize(
    "entry,pnl,direction,observed",
    [
        (100.0, -3.0, "LONG", 99.0),
        (100.0, -3.0, "LONG", 101.5),
        (100.0, 1.0, "LONG", 100.5),
        (100.0, -3.0, "SHORT", 101.0),
        (0.02087, -3.0, "LONG", 0.02024),
        (64328.8, 2.5, "SHORT", 64100.0),
    ],
)
def test_the_port_agrees_with_the_engine_on_a_shared_vector(
    entry, pnl, direction, observed
):
    """Ops ports the engine's math and does not invent it.

    A second implementation of a cost model is a mirror that drifts silently,
    and it drifts in the flattering direction — so the mirror is driven against
    the original rather than against a fixture of what it might return.
    """
    engine_ef = _engine_module()
    theirs = engine_ef.rebase(
        entry=entry, pnl_pct=pnl, direction=direction, observed_entry=observed
    )
    ours = ef.rebase_record(
        {"entry": entry, "pnl_pct": pnl, "direction": direction,
         "first_observed_price": observed}
    )
    assert ours["refusal"] is None and theirs.ok
    assert ours["drift_pct"] == pytest.approx(theirs.drift_pct, abs=1e-9)
    assert ours["rebased_pnl_pct"] == pytest.approx(theirs.rebased_pnl_pct, abs=1e-9)


def test_the_refusal_NAMES_match_the_engines():
    engine_ef = _engine_module()
    assert ef.REFUSAL_NO_ENTRY == engine_ef.REFUSAL_NO_ENTRY
    assert ef.REFUSAL_NO_OBSERVATION == engine_ef.REFUSAL_NO_OBSERVATION
    assert ef.REFUSAL_STALE_OBSERVATION == engine_ef.REFUSAL_STALE_OBSERVATION


def test_every_field_this_page_reads_is_one_the_ENGINE_actually_writes():
    """#817 with the arrow reversed: a field ops reads and no repo writes is a
    full-looking table describing nothing. Checked against the real record."""
    engine = Path(__file__).resolve().parents[2] / "360-v2"
    if not engine.exists():
        pytest.skip("engine repo not checked out beside ops")
    sys.path.insert(0, str(engine))
    try:
        from src.performance_tracker import SignalRecord  # type: ignore

        written = set(SignalRecord.__dataclass_fields__)
    except ImportError as exc:  # engine third-party deps absent in this env
        # Named rather than swallowed: a skip for a missing dependency is an
        # environment fact, and it must not read as a passing contract check.
        pytest.skip(f"engine module not importable here: {exc}")
    finally:
        sys.path.remove(str(engine))
    for field in (
        "first_observed_price", "first_observed_source", "first_observed_stale",
        "peak_pnl_pct", "trough_pnl_pct", "max_favorable_excursion_pct",
    ):
        assert field in written, f"ops reads {field}, the engine does not write it"


# ---------------------------------------------------------------------------
# The reducer
# ---------------------------------------------------------------------------


class TestReducer:
    def test_a_stamped_row_carries_both_books(self):
        row = reduce_records([_rec(first_observed_price=99.0)])[0]
        assert row["entry_drift_pct"] == pytest.approx(-1.0)
        assert row["rebased_pnl_pct"] == pytest.approx(-2.0202, abs=1e-4)
        assert row["rebase_refusal"] is None
        # ...and the recorded number is untouched beside it.
        assert row["pnl_pct"] == pytest.approx(-3.0)

    def test_a_row_from_before_the_stamp_is_REFUSED_by_name(self):
        row = reduce_records([_rec()])[0]
        assert row["rebase_refusal"] == ef.REFUSAL_NO_OBSERVATION
        assert row["entry_drift_pct"] is None
        assert row["rebased_pnl_pct"] is None

    def test_a_frozen_mover_close_is_refused_rather_than_used(self):
        row = reduce_records(
            [_rec(first_observed_price=99.0, first_observed_stale=True)]
        )[0]
        assert row["rebase_refusal"] == ef.REFUSAL_STALE_OBSERVATION

    def test_the_unclamped_peak_travels_beside_the_clamped_mfe(self):
        row = reduce_records(
            [_rec(max_favorable_excursion_pct=0.0, peak_pnl_pct=-0.4)]
        )[0]
        assert row["mfe_pct"] == 0.0
        assert row["peak_pnl_pct"] == pytest.approx(-0.4)

    def test_a_row_predating_the_peak_stamp_reads_None_not_zero(self):
        row = reduce_records([_rec(max_favorable_excursion_pct=0.0)])[0]
        assert row["mfe_pct"] == 0.0
        assert row["peak_pnl_pct"] is None


# ---------------------------------------------------------------------------
# The census
# ---------------------------------------------------------------------------


class TestSummarise:
    def _rows(self, *recs):
        return reduce_records(list(recs))

    def test_unstamped_rows_are_counted_apart_never_averaged_in(self):
        out = ef.summarise(
            self._rows(_rec(first_observed_price=99.0), _rec(signal_id="b"))
        )
        assert out["rows"] == 2 and out["priced"] == 1
        assert out["coverage_pct"] == 50.0
        assert out["drift_mean_pct"] == pytest.approx(-1.0)
        assert [r["reason"] for r in out["refusals"]] == [ef.REFUSAL_NO_OBSERVATION]

    def test_an_all_unstamped_selection_publishes_no_numbers_at_all(self):
        out = ef.summarise(self._rows(_rec()))
        assert out["priced"] == 0
        for key in ("drift_mean_pct", "book_avg_pct", "rebased_avg_pct"):
            assert key not in out

    def test_there_is_no_blended_third_number(self):
        """One figure over both books would move with the coverage rate rather
        than with the market — the both-fills rule, at a third surface."""
        out = ef.summarise(self._rows(_rec(first_observed_price=99.0)))
        assert "book_avg_pct" in out and "rebased_avg_pct" in out
        for forbidden in ("avg_pct", "combined_avg_pct", "blended_avg_pct"):
            assert forbidden not in out

    def test_both_books_are_measured_on_the_SAME_priced_rows(self):
        out = ef.summarise(
            self._rows(_rec(first_observed_price=99.0), _rec(signal_id="b", pnl=5.0))
        )
        # The unstamped +5.0% row must not inflate the recorded average here,
        # or the two rows of the table would describe different populations.
        assert out["book_avg_pct"] == pytest.approx(-3.0)

    def test_no_refusal_row_uses_a_key_that_shadows_a_dict_method(self):
        """``row.copy`` resolves to ``dict.copy`` in Jinja and renders the
        builtin at the reader. This repo has paid for that on ``keys`` and on
        ``copy``; it happened again here and was caught by rendering the page,
        not by a test, because dict access in Python works perfectly.

        Derived over the whole vocabulary rather than one sample row, so the
        next refusal reason is covered without anybody remembering.
        """
        rows = ef.summarise(
            [{"rebase_refusal": reason} for reason in ef.REFUSAL_COPY]
        )["refusals"]
        assert rows, "no refusal rows produced to check"
        for row in rows:
            clash = set(row) & set(dir({}))
            assert not clash, f"row key(s) {clash} shadow a dict method in Jinja"

    def test_an_unknown_refusal_renders_under_its_raw_name_badged(self):
        rows = [{"rebase_refusal": "some_future_reason"}]
        out = ef.summarise(rows)
        entry = out["refusals"][0]
        assert entry["reason"] == "some_future_reason"
        assert entry["known"] is False


class TestMfeFloor:
    def test_it_separates_the_floor_from_a_reading(self):
        out = ef.mfe_floor(
            reduce_records([
                _rec(signal_id="a", max_favorable_excursion_pct=0.0, peak_pnl_pct=-0.4),
                _rec(signal_id="b", max_favorable_excursion_pct=0.0, peak_pnl_pct=0.0),
                _rec(signal_id="c", max_favorable_excursion_pct=1.2, peak_pnl_pct=1.2),
            ])
        )
        assert out["zero_mfe"] == 2
        assert out["with_peak"] == 3
        assert out["floor_rows"] == 1

    def test_rows_without_the_peak_cannot_say_and_are_not_counted_as_clean(self):
        out = ef.mfe_floor(
            reduce_records([_rec(max_favorable_excursion_pct=0.0)])
        )
        assert out["zero_mfe"] == 1
        assert out["with_peak"] == 0
        assert out["floor_pct"] is None


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------

from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.data_volume import DataVolumeReader  # noqa: E402
from app.main import app  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


class TestPage:
    def _stub(self, monkeypatch, records):
        monkeypatch.setattr(
            DataVolumeReader, "signal_performance", lambda self: records
        )

    def test_it_renders_both_books_beside_each_other(self, monkeypatch):
        self._stub(monkeypatch, [
            _rec(signal_id="a", first_observed_price=99.0),
            _rec(signal_id="b", first_observed_price=101.0, pnl=1.0),
        ])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record?window=all")
            assert r.status_code == 200
            assert "Entry fidelity" in r.text
            assert "Rebased on the observed price" in r.text
            assert "Recorded" in r.text

    def test_an_unstamped_book_says_so_rather_than_printing_zeros(self, monkeypatch):
        self._stub(monkeypatch, [_rec()])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record?window=all")
            assert r.status_code == 200
            assert "No row in this selection carries a first-observation stamp" in r.text
            assert "Not a fault" in r.text

    def test_the_panel_filters_WITH_the_table(self, monkeypatch):
        """A summary over the whole ledger above a filtered table is not a
        summary of anything the reader is looking at (#90)."""
        self._stub(monkeypatch, [
            _rec(signal_id="a", symbol="BTCUSDT", first_observed_price=99.0),
            _rec(signal_id="b", symbol="ETHUSDT", first_observed_price=99.0),
        ])
        with TestClient(app) as client:
            _login(client)
            both = client.get("/track-record?window=all").text
            one = client.get("/track-record?window=all&symbol=BTCUSDT").text
        assert "2/2</span>\n      priced" in both or "2/2" in both
        assert "1/1" in one

    def test_the_export_carries_the_rebased_columns_and_the_refusal(
        self, monkeypatch
    ):
        """A spreadsheet is exactly where two populations get averaged into one,
        so the refusal travels with the numbers."""
        self._stub(monkeypatch, [_rec(first_observed_price=99.0)])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record/trades.csv?window=all")
            assert r.status_code == 200
            header = r.text.splitlines()[0]
            for col in (
                "first_observed_price", "entry_drift_pct", "rebased_pnl_pct",
                "rebase_refusal", "mfe_pct", "peak_pnl_pct",
            ):
                assert col in header

    def test_the_recorded_figures_above_are_untouched(self, monkeypatch):
        """Beside, never instead: adding the panel must not move the number the
        page has always led with."""
        self._stub(monkeypatch, [_rec(first_observed_price=99.0, pnl=2.0)])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/track-record?window=all&amount=100&fee_pct=0")
            assert "+2.00" in r.text  # the RECORDED move, not the rebased one
