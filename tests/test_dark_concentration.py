"""Concentration and coverage on the dark feed — the two things reading the
rendered page found that no panel on it could say (2026-08-07).

Both panels exist because a *number that was already on screen* was being read
as an answer to a question it could not answer:

* **"245 excluded"** invited the reader to treat the held arm's shortfall as a
  random sample. It is not: on the live book the priced rows averaged −0.3215%
  against −0.1706% for the retired ones, and coverage ran from 54% on
  ``MOVER_AVWAP_SCALP`` to 24% on ``FAILED_AUCTION_RECLAIM``.
* **Nothing said how few symbols the verdict rested on.** The worst eight
  campaigns — 11% of the rows — took the selection from −119.85% to positive.

The sharpest thing here is the *negative* result pinned by
``test_run_key_is_blind_to_a_spread_out_campaign``: porting
``/signals/price-action``'s run key alone would have produced a panel reading
"concentration is not a problem here" over exactly that book, because this lane
is diverted before the router and has no per-symbol cooldown to bunch its
repeats. Measuring first is the only reason we know.
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
    SKIP_RUNNING,
    SKIP_UNMEASURED,
    hold_coverage,
)
from app.routes.dark_signals_live import RUN_GAP_S, concentration  # noqa: E402

HOUR = 3600.0
T0 = 1_700_000_000.0


def _row(symbol="AAAUSDT", side="LONG", ts=T0, pnl=-1.0, setup="MEAN_REVERT",
         hold_status=HOLD_SL, **extra):
    row = {
        "symbol": symbol, "side": side, "setup_class": setup,
        "emitted_at": ts, "pnl_pct": pnl, "status": "CLOSED_SL",
        "hold_status": hold_status,
    }
    row.update(extra)
    return row


# --------------------------------------------------------------------------- #
# Concentration — two groupings, and why both
# --------------------------------------------------------------------------- #


def test_run_key_is_blind_to_a_spread_out_campaign():
    """The negative result this panel was built on.

    One symbol·side bought ten times across a day — the shape the live dark book
    actually has, because no per-symbol cooldown applies before the enqueue site.
    The run key sees ten separate one-row runs and reports no concentration; the
    campaign key sees one group carrying the whole loss.
    """
    rows = [_row(ts=T0 + i * 3 * HOUR, pnl=-2.0) for i in range(10)]
    rows += [_row(symbol=f"B{i}USDT", ts=T0 + i * HOUR, pnl=+2.0) for i in range(10)]

    out = concentration(rows, fee_pct=0.0)

    # Ten stamps three hours apart are ten runs, each one row.
    assert out["runs"]["n_groups"] == 20
    assert out["runs"]["rows_per_group"] == pytest.approx(1.0)
    assert out["runs"]["worst"]["n_rows"] == 1
    assert out["runs"]["worst"]["net_pct"] == pytest.approx(-2.0)

    # The campaign key finds the thing that actually cost the book.
    assert out["campaigns"]["n_groups"] == 11
    assert out["campaigns"]["worst"]["n_rows"] == 10
    assert out["campaigns"]["worst"]["net_pct"] == pytest.approx(-20.0)
    # …and removing it flips a −20% selection positive.
    assert out["book_net"] == pytest.approx(0.0)
    assert out["campaigns"]["worst"]["book_without"] == pytest.approx(20.0)


def test_run_key_still_groups_a_genuine_burst():
    """The run key is kept, not replaced — it answers a different question."""
    rows = [_row(ts=T0 + i * 600.0, pnl=-3.0) for i in range(4)]
    out = concentration(rows, fee_pct=0.0)
    assert out["runs"]["n_groups"] == 1
    assert out["runs"]["worst"]["n_rows"] == 4
    assert out["runs"]["worst"]["hours"] == pytest.approx(0.5)
    # Both keys agree when the repeats really are bunched.
    assert out["campaigns"]["n_groups"] == 1


def test_a_gap_at_the_threshold_splits_the_run():
    rows = [_row(ts=T0), _row(ts=T0 + RUN_GAP_S + 1)]
    assert concentration(rows, fee_pct=0.0)["runs"]["n_groups"] == 2
    rows = [_row(ts=T0), _row(ts=T0 + RUN_GAP_S - 1)]
    assert concentration(rows, fee_pct=0.0)["runs"]["n_groups"] == 1


def test_side_splits_a_symbol():
    """A long and a short on one ticker are not one directional run."""
    rows = [_row(side="LONG", ts=T0), _row(side="SHORT", ts=T0 + 60.0)]
    out = concentration(rows, fee_pct=0.0)
    assert out["campaigns"]["n_groups"] == 2
    assert out["runs"]["n_groups"] == 2


def test_fee_is_charged_per_row_to_group_and_book_alike():
    """Charging the group and not the book would manufacture concentration."""
    rows = [_row(pnl=-1.0), _row(symbol="BBBUSDT", pnl=-1.0)]
    out = concentration(rows, fee_pct=0.07)
    assert out["book_net"] == pytest.approx(-2.14)
    assert out["campaigns"]["worst"]["net_pct"] == pytest.approx(-1.07)
    assert out["campaigns"]["worst"]["book_without"] == pytest.approx(-1.07)


def test_unscored_rows_are_excluded_never_zeroed():
    """An OPEN row is not a flat one; a real EXPIRED 0.00% is."""
    rows = [_row(pnl=-4.0), _row(symbol="BBBUSDT", pnl=None, status="OPEN")]
    out = concentration(rows, fee_pct=0.0)
    assert out["book_net"] == pytest.approx(-4.0)
    # The unscored row's own group carries no verdict, so it cannot be "worst".
    assert out["campaigns"]["worst"]["label"] == "AAAUSDT LONG"


def test_share_of_book_is_none_when_the_selection_is_positive():
    """"N% of what we lost" means nothing over a book that did not lose."""
    rows = [_row(pnl=+5.0), _row(symbol="BBBUSDT", pnl=-1.0)]
    out = concentration(rows, fee_pct=0.0)
    assert out["book_net"] == pytest.approx(4.0)
    assert out["campaigns"]["worst"]["share_of_book"] is None
    assert out["campaigns"]["to_flip"] is None


def test_to_flip_reports_the_row_share_it_cost():
    """A k with no row share beside it reads as a filter rather than a measure."""
    rows = [_row(symbol="AAAUSDT", pnl=-30.0)]
    rows += [_row(symbol=f"B{i}USDT", pnl=+2.0) for i in range(10)]
    out = concentration(rows, fee_pct=0.0)
    flip = out["campaigns"]["to_flip"]
    assert flip["k"] == 1
    assert flip["n_rows"] == 1
    assert flip["row_share"] == pytest.approx(100.0 / 11)
    assert flip["book_without"] == pytest.approx(20.0)


def test_empty_selection_does_not_raise():
    out = concentration([], fee_pct=0.07)
    assert out["n_rows"] == 0 and out["campaigns"] is None and out["book_net"] is None


# --------------------------------------------------------------------------- #
# Coverage — per path, and whether the priced subset represents the book
# --------------------------------------------------------------------------- #


def test_coverage_splits_by_path():
    rows = [
        _row(setup="MVAVW", hold_status=HOLD_SL),
        _row(setup="MVAVW", hold_status=HOLD_INSUFFICIENT),
        _row(setup="FAR", hold_status=HOLD_INSUFFICIENT),
        _row(setup="FAR", hold_status=HOLD_INSUFFICIENT),
        _row(setup="FAR", hold_status=HOLD_OPEN),
    ]
    by_path = {p["setup_class"]: p for p in hold_coverage(rows)["by_path"]}
    assert by_path["MVAVW"]["priced_share"] == pytest.approx(50.0)
    assert by_path["FAR"]["priced_share"] == pytest.approx(0.0)
    assert by_path["FAR"][SKIP_UNMEASURED] == 2
    assert by_path["FAR"][SKIP_RUNNING] == 1
    # Sorted by evidence, so the path carrying the most rows leads.
    assert [p["setup_class"] for p in hold_coverage(rows)["by_path"]] == ["FAR", "MVAVW"]


def test_per_path_counts_sum_to_the_totals():
    """The two views cannot disagree about the same rows."""
    rows = [
        _row(setup="A", hold_status=HOLD_SL), _row(setup="A", hold_status=HOLD_EXPIRED),
        _row(setup="B", hold_status=HOLD_INSUFFICIENT), _row(setup="B", hold_status=HOLD_OPEN),
        _row(setup="C", hold_status=None),
    ]
    cov = hold_coverage(rows)
    for key in ("decided", SKIP_RUNNING, SKIP_UNMEASURED, SKIP_NO_ARM):
        assert sum(p[key] for p in cov["by_path"]) == cov[key]
    assert sum(p["total"] for p in cov["by_path"]) == cov["total"] == 5


def test_representativeness_reads_the_rows_own_exit_not_the_arms():
    """The point of the panel: grade the subset on the column every row has.

    The retired rows here have no held-arm result at all, and still report an
    own-exit average — which is exactly what makes the comparison possible.
    """
    rows = [_row(pnl=-3.0, hold_status=HOLD_SL, hold_result_pct=-3.0) for _ in range(2)]
    rows += [_row(pnl=+2.0, hold_status=HOLD_INSUFFICIENT) for _ in range(2)]
    rep = hold_coverage(rows)["representativeness"]

    assert rep["decided"]["avg_pct"] == pytest.approx(-3.0)
    assert rep[SKIP_UNMEASURED]["avg_pct"] == pytest.approx(+2.0)
    assert rep["all"]["avg_pct"] == pytest.approx(-0.5)
    # The lean is what the panel is for — and it points the priced way here.
    assert rep["decided"]["avg_pct"] < rep["all"]["avg_pct"]


def test_representativeness_keeps_flat_rows_out_of_the_rate_and_in_the_average():
    """"Three buckets, never two" — an EXPIRED 0.00% is not a loss."""
    rows = [
        _row(pnl=+2.0, status="CLOSED_TP1"),
        _row(pnl=-1.0, status="CLOSED_SL"),
        _row(pnl=0.0, status="EXPIRED"),
    ]
    dec = hold_coverage(rows)["representativeness"]["decided"]
    assert (dec["wins"], dec["losses"], dec["flat"]) == (1, 1, 1)
    assert dec["win_rate"] == pytest.approx(50.0)
    assert dec["avg_pct"] == pytest.approx(1.0 / 3)


def test_representativeness_counts_unscored_rows_apart():
    """A row with no readable PnL is named, never averaged in as a zero."""
    rows = [_row(pnl=-2.0), _row(pnl=None, status="OPEN")]
    allb = hold_coverage(rows)["representativeness"]["all"]
    assert allb["n"] == 2 and allb["n_scored"] == 1
    assert allb["avg_pct"] == pytest.approx(-2.0)


def test_every_coverage_bucket_has_a_representativeness_entry():
    """A bucket that can be counted and not graded is the seam this repo keeps
    paying for — derived from the counts rather than listed."""
    cov = hold_coverage([_row()])
    for key in ("decided", SKIP_RUNNING, SKIP_UNMEASURED, SKIP_NO_ARM):
        assert key in cov["representativeness"], key
    assert "all" in cov["representativeness"]


def test_coverage_on_an_empty_selection_does_not_raise():
    cov = hold_coverage([])
    assert cov["total"] == 0 and cov["by_path"] == []
    assert cov["representativeness"]["all"]["avg_pct"] is None
