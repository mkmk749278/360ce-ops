"""Population and aggregation contracts for the Dark Signals panel.

``test_dark_signals.py`` covers the simulation maths — resample, Wilder ATR,
SuperTrend, SAR, the trailing sim — and covers it well.  What it does not
touch is everything between the sim and the screen: ``_row``, ``_method_stat``,
``_real_stat``, ``_bake_off``, ``_grouped``, ``_paginate``.  That layer was at
zero, and it is precisely where #90/#91 lived — a summary panel measured over
267 rows sitting above a table showing 149, pooling delivered, router-dropped
and gate-killed candidates into the single number an adoption decision reads.

The rule from that incident is the one this file enforces: **a panel must be
measured on the population the page is showing**, and every count is measured
with every filter applied except its own.

Rows are built by driving the real ``_row`` over real ``DarkSignalResult`` /
``TrailResult`` objects rather than hand-written dicts.  A dict whose keys we
chose would assert our own idea of the row shape back at us — and the row
shape is exactly what the aggregators consume.
"""
from __future__ import annotations

import pytest

from app.data_sources.dark_signals import METHODS, DarkSignalResult, TrailResult
from app.routes.dark_signals import (
    _bake_off,
    _grouped,
    _method_stat,
    _paginate,
    _real_stat,
    _row,
)


def _trail(
    method: str,
    *,
    result_pct: float | None = 1.0,
    mfe_pct: float | None = 2.0,
    hold_mins: int | None = 30,
    exited: bool = True,
    reason: str = "trail",
) -> TrailResult:
    return TrailResult(
        method=method,
        exited=exited,
        result_pct=result_pct,
        gross_pct=result_pct,
        mfe_pct=mfe_pct,
        exit_price=100.0,
        hold_mins=hold_mins,
        fee_pct=0.07,
        funding_pct=0.01,
        bars=42,
        reason=reason,
    )


def _result(
    sid: str,
    *,
    active: bool = False,
    regime: str = "TREND",
    setup: str = "BREAKOUT_RETEST",
    real_pnl: float | None = 0.5,
    per_method: dict[str, TrailResult] | None = None,
) -> DarkSignalResult:
    results = per_method if per_method is not None else {
        m: _trail(m) for m in METHODS
    }
    return DarkSignalResult(
        signal_id=sid,
        symbol="BTCUSDT",
        side="LONG",
        entry=100.0,
        setup_class=setup,
        regime=regime,
        confidence=72.0,
        real_pnl_pct=real_pnl,
        real_is_active=active,
        timestamp="2026-07-28T00:00:00Z",
        minutes_ago=10,
        results=results,
        degraded=False,
        source="replay",
    )


def _rows(*results: DarkSignalResult) -> list[dict]:
    return [_row(r) for r in results]


# ---------------------------------------------------------------------------
# The #90/#91 contract: panel population == table population
# ---------------------------------------------------------------------------


def test_panel_denominator_equals_the_closed_population() -> None:
    """Every aggregate is measured over the same rows, and says how many.

    The failure this pins: a stat block whose n is drawn from a wider pool
    than the one the table beside it renders.
    """
    rows = _rows(
        _result("a"), _result("b"), _result("c"),
        _result("d", active=True), _result("e", active=True),
    )
    closed = [r for r in rows if not r["real_is_active"]]

    assert len(closed) == 3
    for block in _bake_off(closed):
        assert block["stats"]["n"] == 3
    assert _real_stat(closed)["n"] == 3
    assert sum(g["n"] for g in _grouped(closed, "regime")) == 3


def test_active_rows_never_enter_the_aggregates() -> None:
    """An unresolved signal has no realised outcome to average.

    Counting it would dilute expectancy toward zero and make a method look
    steadier than it is.
    """
    active_only = _rows(_result("x", active=True), _result("y", active=True))
    closed = [r for r in active_only if not r["real_is_active"]]

    assert closed == []
    assert _real_stat(closed)["n"] == 0
    for block in _bake_off(closed):
        assert block["stats"]["n"] == 0
        # Not 0.0 — an empty population has no average, and rendering 0.0
        # reads as "measured, and it's break-even".
        assert block["stats"]["avg"] is None
        assert block["stats"]["win_rate"] is None


def test_pagination_counts_agree_with_the_aggregate_population() -> None:
    """``n_closed`` on the pager and the panel denominator are one number."""
    rows = _rows(
        *[_result(f"c{i}") for i in range(7)],
        *[_result(f"a{i}", active=True) for i in range(3)],
    )
    closed = [r for r in rows if not r["real_is_active"]]
    pag = _paginate(rows, page=1)

    assert pag["n_closed"] == len(closed) == 7
    assert pag["n_active"] == 3
    assert _real_stat(closed)["n"] == pag["n_closed"]


def test_grouped_partitions_the_population_exactly_once() -> None:
    """Group counts must sum to the whole — no row dropped, none double-counted.

    A row landing in two buckets is how a concentration problem hides.
    """
    rows = _rows(
        _result("a", regime="TREND"),
        _result("b", regime="TREND"),
        _result("c", regime="RANGE"),
        _result("d", regime="VOLATILE"),
    )
    groups = _grouped(rows, "regime")

    assert sum(g["n"] for g in groups) == len(rows)
    assert len({g["key"] for g in groups}) == len(groups)


def test_missing_group_key_buckets_as_unknown_not_dropped() -> None:
    """A null regime must not silently shrink the population."""
    rows = _rows(_result("a", regime=""), _result("b", regime="TREND"))
    groups = _grouped(rows, "regime")

    assert sum(g["n"] for g in groups) == 2
    assert "UNKNOWN" in {g["key"] for g in groups}


# ---------------------------------------------------------------------------
# Per-method denominators — the subtle one
# ---------------------------------------------------------------------------


def test_method_n_counts_only_rows_that_method_resolved() -> None:
    """Each method has its own denominator, and it can be smaller.

    A signal whose candles were missing resolves under some methods and not
    others.  Dividing every method by the row count would understate the
    ones that resolved fewer — the "mirror the engine's denominators too"
    rule from the ops brief.
    """
    both = _result("a")
    only_atr = _result("b", per_method={
        "atr": _trail("atr", result_pct=2.0),
        "supertrend": _trail("supertrend", result_pct=None, reason="no-data"),
        "sar": _trail("sar", result_pct=None, reason="no-data"),
    })
    rows = _rows(both, only_atr)

    assert _method_stat(rows, "atr")["n"] == 2
    assert _method_stat(rows, "supertrend")["n"] == 1
    assert _method_stat(rows, "sar")["n"] == 1


def test_absent_method_entry_is_skipped_not_counted_as_zero() -> None:
    """A method with no TrailResult at all contributes nothing, not a 0.0."""
    rows = _rows(_result("a", per_method={"atr": _trail("atr", result_pct=1.0)}))

    assert _method_stat(rows, "atr")["n"] == 1
    assert _method_stat(rows, "supertrend")["n"] == 0
    assert _method_stat(rows, "supertrend")["avg"] is None


# ---------------------------------------------------------------------------
# The statistics themselves
# ---------------------------------------------------------------------------


def test_expectancy_and_total_are_consistent() -> None:
    rows = _rows(
        _result("a", per_method={"atr": _trail("atr", result_pct=3.0)}),
        _result("b", per_method={"atr": _trail("atr", result_pct=-1.0)}),
        _result("c", per_method={"atr": _trail("atr", result_pct=1.0)}),
    )
    st = _method_stat(rows, "atr")

    assert st["n"] == 3
    assert st["total"] == pytest.approx(3.0)
    assert st["avg"] == pytest.approx(1.0)
    assert st["win_rate"] == pytest.approx(2 / 3 * 100.0)


def test_profit_factor_is_gross_win_over_gross_loss() -> None:
    rows = _rows(
        _result("a", per_method={"atr": _trail("atr", result_pct=4.0)}),
        _result("b", per_method={"atr": _trail("atr", result_pct=-2.0)}),
    )
    assert _method_stat(rows, "atr")["profit_factor"] == pytest.approx(2.0)


def test_profit_factor_is_none_with_no_losses_not_infinity() -> None:
    """An all-winners window has an undefined PF; ``inf`` renders as a lie."""
    rows = _rows(_result("a", per_method={"atr": _trail("atr", result_pct=4.0)}))
    assert _method_stat(rows, "atr")["profit_factor"] is None


def test_a_flat_zero_result_is_neither_win_nor_loss() -> None:
    """Exactly-zero sits in the epsilon band and must not inflate win-rate."""
    rows = _rows(
        _result("a", per_method={"atr": _trail("atr", result_pct=0.0)}),
        _result("b", per_method={"atr": _trail("atr", result_pct=2.0)}),
    )
    st = _method_stat(rows, "atr")
    assert st["n"] == 2
    assert st["win_rate"] == pytest.approx(50.0)
    assert st["profit_factor"] is None  # no loss recorded


def test_capture_is_expectancy_over_average_mfe() -> None:
    rows = _rows(
        _result("a", per_method={"atr": _trail("atr", result_pct=1.0, mfe_pct=4.0)}),
    )
    assert _method_stat(rows, "atr")["capture"] == pytest.approx(25.0)


def test_zero_mfe_does_not_divide_by_zero() -> None:
    rows = _rows(
        _result("a", per_method={"atr": _trail("atr", result_pct=0.0, mfe_pct=0.0)}),
    )
    assert _method_stat(rows, "atr")["capture"] is None


def test_exited_rate_separates_trailed_from_still_open() -> None:
    """A method that rarely fires is not the same as one that fires and loses."""
    rows = _rows(
        _result("a", per_method={"atr": _trail("atr", result_pct=1.0, exited=True)}),
        _result("b", per_method={
            "atr": _trail("atr", result_pct=0.5, exited=False, reason="open")}),
    )
    assert _method_stat(rows, "atr")["exited_rate"] == pytest.approx(50.0)


def test_real_baseline_ignores_rows_with_no_engine_pnl() -> None:
    """The baseline's denominator is its own — rows without a real exit
    cannot be averaged into it."""
    rows = _rows(_result("a", real_pnl=2.0), _result("b", real_pnl=None))
    st = _real_stat(rows)
    assert st["n"] == 1
    assert st["avg"] == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Ordering + pagination
# ---------------------------------------------------------------------------


def test_bake_off_ranks_by_total_best_first() -> None:
    rows = _rows(_result("a", per_method={
        "atr": _trail("atr", result_pct=1.0),
        "supertrend": _trail("supertrend", result_pct=5.0),
        "sar": _trail("sar", result_pct=-3.0),
    }))
    order = [b["method"] for b in _bake_off(rows)]
    assert order == ["supertrend", "atr", "sar"]


def test_bake_off_sorts_unmeasured_methods_last() -> None:
    """A method with no data must not outrank a measured loser.

    ``None`` sorting as a large value would put an empty column at the top
    of a table the owner reads as a ranking.
    """
    rows = _rows(_result("a", per_method={
        "atr": _trail("atr", result_pct=-2.0),
        "supertrend": _trail("supertrend", result_pct=None, reason="no-data"),
        "sar": _trail("sar", result_pct=None, reason="no-data"),
    }))
    assert _bake_off(rows)[0]["method"] == "atr"


def test_page_one_shows_active_rows_alongside_the_first_closed_page() -> None:
    rows = _rows(
        *[_result(f"c{i}") for i in range(60)],
        _result("live", active=True),
    )
    pag = _paginate(rows, page=1)

    assert pag["total_pages"] == 2
    assert pag["rows"][0]["id"] == "live"
    assert len(pag["rows"]) == 51  # 1 active + 50 closed


def test_later_pages_do_not_repeat_the_active_rows() -> None:
    """Repeating them would double-count on screen across pages."""
    rows = _rows(
        *[_result(f"c{i}") for i in range(60)],
        _result("live", active=True),
    )
    page2 = _paginate(rows, page=2)

    assert all(not r["real_is_active"] for r in page2["rows"])
    assert len(page2["rows"]) == 10


def test_out_of_range_page_clamps_into_the_population() -> None:
    rows = _rows(*[_result(f"c{i}") for i in range(5)])
    assert _paginate(rows, page=99)["page"] == 1
    assert _paginate(rows, page=0)["page"] == 1


def test_empty_population_still_reports_one_page() -> None:
    pag = _paginate([], page=1)
    assert pag["total_pages"] == 1
    assert pag["rows"] == []
    assert pag["n_closed"] == 0


# ---------------------------------------------------------------------------
# Row shaping
# ---------------------------------------------------------------------------


def test_row_carries_every_method_key_even_when_unresolved() -> None:
    """The template indexes methods positionally; a missing key is a 500."""
    row = _row(_result("a", per_method={"atr": _trail("atr")}))
    assert set(row["methods"].keys()) == set(METHODS)
    assert row["methods"]["supertrend"] is None


def test_row_capture_is_none_when_mfe_is_zero() -> None:
    row = _row(_result("a", per_method={
        "atr": _trail("atr", result_pct=1.0, mfe_pct=0.0)}))
    assert row["methods"]["atr"]["capture"] is None
