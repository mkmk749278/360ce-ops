"""The dark-feed page — real signals from the silenced paths, owner-only.

The rule this page exists to not break: **"the scanner was willing to send
this"** and **"a user would have seen this"** are different sentences, and
merging them is #816 — ``emitted`` stamped at the enqueue site inflated the only
population allowed to justify a live change by ~30x, and this dashboard read
"Emitted to live (98)" for a window with 3 real signals.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test")

from app.routes.dark_signals_live import (  # noqa: E402
    filter_rows,
    reduce_rows,
    reduce_state,
    summarize,
)


def _row(setup="MEAN_REVERT", gate="setup_compat:regime_STRONG_TREND",
         status="OPEN", r=None, ts=1_700_000_000.0, conf=70.0,
         insufficient_reason=None):
    row = {
        "symbol": "AAAUSDT", "side": "LONG", "setup_class": setup,
        "dark_gate": gate, "status": status, "r_multiple": r,
        "emitted_at": ts, "confidence": conf, "entry": 100.0,
        "stop_loss": 97.0, "tp1": 106.0,
    }
    if insufficient_reason is not None:
        row["insufficient_reason"] = insufficient_reason
    return row


def _payload(rows):
    return {"schema": 1, "written_at": 1_700_000_000.0, "rows": rows}


def test_rows_come_back_newest_first():
    rows = reduce_rows(_payload([_row(ts=1.0), _row(ts=9.0), _row(ts=5.0)]))
    assert [r["emitted_at"] for r in rows] == [9.0, 5.0, 1.0]


def test_a_missing_or_broken_payload_yields_no_rows_rather_than_raising():
    assert reduce_rows(None) == []
    assert reduce_rows({"rows": "nope"}) == []
    assert reduce_rows([]) == []


def test_filters_are_independent_so_a_selector_can_omit_itself():
    rows = [
        _row(setup="MEAN_REVERT", status="CLOSED_TP1"),
        _row(setup="RANGE_FADE", status="OPEN"),
    ]
    assert len(filter_rows(rows, setup="MEAN_REVERT")) == 1
    assert len(filter_rows(rows, status="OPEN")) == 1
    assert len(filter_rows(rows, setup="MEAN_REVERT", status="OPEN")) == 0


def test_an_expiry_is_not_counted_as_a_loss():
    """The setup did nothing; it did not lose. Folding the two together is the
    fabrication class this repo already paid for."""
    agg = summarize([
        _row(status="CLOSED_TP1", r=2.0),
        _row(status="CLOSED_SL", r=-1.0),
        _row(status="EXPIRED", r=0.0),
    ])["by_setup"][0]
    assert agg["expired"] == 1
    assert agg["decided"] == 2
    assert agg["win_rate"] == pytest.approx(0.5)


def test_open_rows_are_counted_but_never_scored():
    agg = summarize([_row(status="OPEN"), _row(status="CLOSED_TP1", r=2.0)])["by_setup"][0]
    assert agg["open"] == 1 and agg["resolved"] == 1
    assert agg["avg_r"] == pytest.approx(2.0), "an open row must not dilute the R"


def test_a_path_with_nothing_decided_refuses_rather_than_scoring_zero():
    agg = summarize([_row(status="OPEN")])["by_setup"][0]
    assert agg["win_rate"] is None and agg["avg_r"] is None


def test_paths_are_ranked_by_how_much_they_emitted():
    out = summarize([
        _row(setup="RANGE_FADE"), _row(setup="RANGE_FADE"), _row(setup="MEAN_REVERT"),
    ])["by_setup"]
    assert out[0]["setup_class"] == "RANGE_FADE"


# --------------------------------------------------------------------------- #
# Missing, empty and stale are three different states
# --------------------------------------------------------------------------- #


def test_a_missing_file_says_the_lane_is_off_not_that_paths_are_quiet():
    state = reduce_state({"file": "dark_signals_live_v1.json", "exists": False}, [])
    assert state["state"] == "unavailable"
    assert "DARK_EMISSION_ENABLED" in state["detail"]


def test_current_and_empty_is_a_finding_not_a_fault():
    """If the loosened gates were not what silenced these paths, that is the
    answer to the owner's question, not a broken page."""
    state = reduce_state({"exists": True, "age_sec": 30.0}, [])
    assert state["state"] == "empty"
    assert "not a fault" in state["detail"]


def test_a_stale_ledger_is_called_out_before_any_number():
    state = reduce_state({"exists": True, "age_sec": 3600.0}, [_row()])
    assert state["state"] == "stale"


def test_a_current_file_with_rows_is_live_and_says_nothing_reached_a_user():
    state = reduce_state({"exists": True, "age_sec": 30.0}, [_row()])
    assert state["state"] == "live"
    assert "order" in state["detail"]


# --------------------------------------------------------------------------- #
# The page must render, and its copy is part of the measurement
# --------------------------------------------------------------------------- #


def _client(payload=None, provenance=None, prices=None, raise_prices=False, arms=None):
    from fastapi.testclient import TestClient

    from app.main import app

    _prov = provenance if provenance is not None else {
        "file": "dark_signals_live_v1.json", "version": 1, "exists": True,
        "modified_at": "2026-07-31 07:00 UTC", "age_sec": 30.0,
    }
    _prices = {"AAAUSDT": 101.0} if prices is None else prices

    async def _fetch_all_prices():
        if raise_prices:
            raise RuntimeError("binance down")
        return _prices

    client = TestClient(app)
    client.__enter__()
    vol = app.state.data_volume
    vol.dark_signals = lambda: (payload if payload is not None else _payload([_row()]))
    vol.dark_signals_provenance = lambda: _prov
    vol.dark_sar_arms = lambda: (arms if arms is not None else {"open": [], "resolved": []})
    app.state.binance_klines.fetch_all_prices = _fetch_all_prices
    client.post("/login", data={"password": "test-token"})
    return client


def test_the_page_renders():
    client = _client()
    try:
        r = client.get("/signals/dark-live")
    finally:
        client.__exit__(None, None, None)
    assert r.status_code == 200
    assert "MEAN_REVERT" in r.text


def test_the_page_says_these_are_not_what_a_user_would_have_seen():
    """Copy is part of the measurement. A count labelled as a feed size when it
    is a pre-router population is the #816 error."""
    client = _client()
    try:
        r = client.get("/signals/dark-live")
    finally:
        client.__exit__(None, None, None)
    assert "not what a user would have seen" in r.text
    assert "no order" in r.text or "no push" in r.text


def test_a_missing_file_renders_instead_of_500():
    client = _client(payload=None, provenance={
        "file": "dark_signals_live_v1.json", "exists": False, "age_sec": None,
    })
    try:
        r = client.get("/signals/dark-live")
    finally:
        client.__exit__(None, None, None)
    assert r.status_code == 200


def test_the_csv_export_is_uncapped_and_honours_the_filter():
    client = _client(payload=_payload([_row(setup="MEAN_REVERT"), _row(setup="RANGE_FADE")]))
    try:
        r = client.get("/signals/dark-live/export.csv?setup=RANGE_FADE")
    finally:
        client.__exit__(None, None, None)
    assert r.status_code == 200
    assert "RANGE_FADE" in r.text and "MEAN_REVERT" not in r.text


# --------------------------------------------------------------------------- #
# What an open row is worth right now
#
# The owner's read of this page on the day it shipped: three open rows, entry
# prices, dashes under PnL and R. The page's whole subject was unreadable until
# a row closed. A mark answers that — but only beside a row that can say whether
# it is still being measured, which is #108 in one sentence.
# --------------------------------------------------------------------------- #


def _open_row(**kw):
    row = _row(status="OPEN")
    row.update({
        "sl_distance_pct": 3.0,
        "last_resolved_at": 1_700_000_500.0,
        "resolve_misses": 0,
        "bars_behind": 1.0,
        "stalled": False,
    })
    row.update(kw)
    return row


def test_an_open_row_carries_a_mark_and_what_it_implies():
    from app.routes.dark_signals_live import mark_live_pnl

    (row,) = mark_live_pnl([_open_row()], {"AAAUSDT": 103.0})
    assert row["current_price"] == 103.0
    assert row["unrealized_pct"] == pytest.approx(3.0)
    # Divided by the ENGINE's stamped SL distance, so a mark here is comparable
    # with a realized R there.
    assert row["unrealized_r"] == pytest.approx(1.0)
    assert row["room_to_sl_pct"] == pytest.approx((103.0 - 97.0) / 103.0 * 100.0)
    assert row["room_to_tp1_pct"] == pytest.approx((106.0 - 103.0) / 103.0 * 100.0)
    assert row["level_crossed"] == ""


def test_a_short_marks_in_its_own_direction():
    from app.routes.dark_signals_live import mark_live_pnl

    row = _open_row(side="SHORT", entry=100.0, stop_loss=103.0, tp1=94.0)
    (row,) = mark_live_pnl([row], {"AAAUSDT": 97.0})
    assert row["unrealized_pct"] == pytest.approx(3.0)
    assert row["room_to_sl_pct"] > 0 and row["room_to_tp1_pct"] > 0


def test_a_mark_never_lands_in_a_realized_column():
    """An unrealized number in a realized column is how a still-open trade gets
    read as a finished one."""
    from app.routes.dark_signals_live import mark_live_pnl

    (row,) = mark_live_pnl([_open_row()], {"AAAUSDT": 103.0})
    assert row.get("pnl_pct") is None
    assert row.get("r_multiple") is None
    assert row["unrealized_pct"] is not None


def test_a_closed_row_is_never_marked():
    from app.routes.dark_signals_live import mark_live_pnl

    row = _row(status="CLOSED_SL", r=-1.0)
    (row,) = mark_live_pnl([row], {"AAAUSDT": 103.0})
    assert "current_price" not in row and row["r_multiple"] == -1.0


def test_a_row_without_the_engines_sl_distance_gets_a_percentage_and_no_R():
    """Refuse, don't clamp: an invented denominator makes a missing field read
    as a modest result."""
    from app.routes.dark_signals_live import mark_live_pnl

    row = _open_row()
    row.pop("sl_distance_pct")
    (row,) = mark_live_pnl([row], {"AAAUSDT": 103.0})
    assert row["unrealized_pct"] == pytest.approx(3.0)
    assert row["unrealized_r"] is None


def test_a_missing_price_blanks_the_columns_rather_than_breaking_the_row():
    from app.routes.dark_signals_live import mark_live_pnl

    (row,) = mark_live_pnl([_open_row()], {})
    assert row["current_price"] is None and row["unrealized_pct"] is None
    assert "current_price" in row, "the column means 'could not compute', not 'absent'"


def test_a_level_already_crossed_while_the_row_says_open_is_flagged():
    """Not a near miss — a contradiction. The resolver walks bars in order and
    would have closed the row, so those bars never arrived."""
    from app.routes.dark_signals_live import mark_live_pnl

    (row,) = mark_live_pnl([_open_row()], {"AAAUSDT": 95.0})   # below the 97 stop
    assert row["level_crossed"] == "SL"
    assert row["room_to_sl_pct"] < 0


# --------------------------------------------------------------------------- #
# …and whether the row beside the mark is still being measured
# --------------------------------------------------------------------------- #


def test_a_row_the_engine_advanced_recently_reads_current():
    from app.routes.dark_signals_live import mark_freshness

    now = 1_700_000_600.0
    (row,) = mark_freshness([_open_row(last_resolved_at=now - 120.0)], now=now)
    assert row["freshness"] == "current"
    assert row["is_stalled"] is False
    assert row["resolve_age_sec"] == pytest.approx(120.0)


def test_a_row_with_missed_cycles_reads_stalled_and_names_the_cause():
    from app.routes.dark_signals_live import mark_freshness

    now = 1_700_000_600.0
    row = _open_row(
        last_resolved_at=None, resolve_misses=3, resolve_miss_reason="no_candles"
    )
    (row,) = mark_freshness([row], now=now)
    assert row["freshness"] == "stalled"
    assert row["stall_reason"] == "no_candles"


def test_the_engines_own_stalled_stamp_wins_even_on_a_recent_touch():
    """The engine consumed the bar; only it knows the bar was old."""
    from app.routes.dark_signals_live import mark_freshness

    now = 1_700_000_600.0
    row = _open_row(last_resolved_at=now - 10.0, stalled=True)
    (row,) = mark_freshness([row], now=now)
    assert row["freshness"] == "stalled"


def test_a_row_without_freshness_stamps_is_unverified_not_current():
    """A missing stamp is not a pass — the rows without one are exactly the rows
    written before the check existed."""
    from app.routes.dark_signals_live import mark_freshness

    row = _row(status="OPEN")          # no engine freshness keys at all
    (row,) = mark_freshness([row], now=1_700_100_000.0)
    assert row["freshness"] == "unverified"
    assert row["is_stalled"] is False


def test_a_stamped_row_the_resolver_never_touched_is_a_fault_not_a_young_row():
    from app.routes.dark_signals_live import mark_freshness

    row = _open_row(last_resolved_at=None, emitted_at=1_700_000_000.0)
    (row,) = mark_freshness([row], now=1_700_000_000.0 + 4000.0)
    assert row["freshness"] == "stalled"


def test_a_young_unresolved_row_is_not_yet_a_fault():
    from app.routes.dark_signals_live import mark_freshness

    row = _open_row(last_resolved_at=None, emitted_at=1_700_000_000.0)
    (row,) = mark_freshness([row], now=1_700_000_060.0)
    assert row["freshness"] == "unverified"
    assert row["is_stalled"] is False


# --------------------------------------------------------------------------- #
# The open-book panel: two denominators, neither called "the" number
# --------------------------------------------------------------------------- #


def test_the_open_panel_publishes_both_populations():
    from app.routes.dark_signals_live import (
        mark_freshness, mark_live_pnl, summarize_open,
    )

    now = 1_700_000_600.0
    rows = [
        _open_row(symbol="AAAUSDT", last_resolved_at=now - 60.0),
        _open_row(symbol="BBBUSDT", last_resolved_at=None, resolve_misses=4),
        _row(status="CLOSED_TP1", r=2.0),
    ]
    mark_freshness(rows, now=now)
    mark_live_pnl(rows, {"AAAUSDT": 103.0, "BBBUSDT": 94.0})
    panel = summarize_open(rows)
    assert panel["open"] == 2 and panel["stalled"] == 1
    # Both marked; only one still being advanced. The two figures differ exactly
    # when the lane is unhealthy, which is why both are printed.
    assert panel["all_marked"]["n"] == 2
    assert panel["still_measured"]["n"] == 1
    assert panel["all_marked"]["avg_r"] == pytest.approx((1.0 + -2.0) / 2.0)
    assert panel["still_measured"]["avg_r"] == pytest.approx(1.0)
    assert panel["crossed"] == 1          # BBBUSDT is through its stop


def test_the_open_panel_counts_a_row_it_could_not_mark():
    from app.routes.dark_signals_live import mark_live_pnl, summarize_open

    rows = [_open_row()]
    mark_live_pnl(rows, {})
    panel = summarize_open(rows)
    assert panel["open"] == 1 and panel["unmarked"] == 1
    assert panel["all_marked"]["avg_pct"] is None


# --------------------------------------------------------------------------- #
# INSUFFICIENT — terminal, and deliberately unscored
# --------------------------------------------------------------------------- #


def test_an_unmeasured_row_is_kept_out_of_every_rate():
    """Past the horizon with candles that never arrived. An expiry is a walked
    window in which nothing happened; this is the absence of a measurement, and
    pooling them divides a rate by rows nobody scored."""
    agg = summarize([
        _row(status="CLOSED_TP1", r=2.0),
        _row(status="INSUFFICIENT"),
    ])["by_setup"][0]
    assert agg["insufficient"] == 1
    assert agg["resolved"] == 1 and agg["decided"] == 1
    assert agg["win_rate"] == pytest.approx(1.0)
    assert agg["avg_r"] == pytest.approx(2.0)


def test_an_unmeasured_row_is_not_counted_as_open_either():
    agg = summarize([_row(status="INSUFFICIENT")])["by_setup"][0]
    assert agg["open"] == 0 and agg["resolved"] == 0


def test_the_two_causes_of_unmeasured_are_counted_apart():
    """A series that never arrived and a series with holes are different faults
    with different fixes, so the page must never pool them into one number.

    ``partial_window`` arrived with the engine fix of 2026-08-01: a row whose
    walk did not cover its window used to be called an EXPIRED and scored 0R,
    which is a claim about bars nobody looked at. In the owner's window ROBOUSDT
    expired on 309 bars of a 362-minute window and ARBUSDT on 329 of 365.
    """
    agg = summarize([
        _row(status="INSUFFICIENT", insufficient_reason="no_walk"),
        _row(status="INSUFFICIENT", insufficient_reason="partial_window"),
        _row(status="INSUFFICIENT", insufficient_reason="partial_window"),
    ])["by_setup"][0]
    assert agg["insufficient"] == 3
    assert agg["insufficient_no_walk"] == 1
    assert agg["insufficient_partial_window"] == 2
    # Still out of every rate, whichever cause it was.
    assert agg["resolved"] == 0 and agg["decided"] == 0


def test_a_row_with_no_reason_stamp_is_not_silently_called_partial():
    """A missing stamp is not a pass. Rows written before the engine stamped a
    reason fall to the original cause rather than the new one."""
    agg = summarize([_row(status="INSUFFICIENT")])["by_setup"][0]
    assert agg["insufficient_no_walk"] == 1
    assert agg["insufficient_partial_window"] == 0


# --------------------------------------------------------------------------- #
# The rendered page
# --------------------------------------------------------------------------- #


def test_the_page_shows_a_mark_for_an_open_row():
    client = _client(payload=_payload([_open_row()]), prices={"AAAUSDT": 103.0})
    try:
        r = client.get("/signals/dark-live")
    finally:
        client.__exit__(None, None, None)
    assert r.status_code == 200
    assert "+3.00%" in r.text                 # unrealized move
    assert "Open right now" in r.text
    assert "marks, not results" in r.text


def test_the_page_leads_an_open_row_with_when_the_engine_last_advanced_it():
    """A surface cannot grade its own liveness on a clock it supplies (#108) —
    so the freshness the page prints is the engine's, and it is on screen."""
    client = _client(payload=_payload([_open_row(resolve_misses=3, last_resolved_at=None,
                                                 resolve_miss_reason="no_candles")]))
    try:
        r = client.get("/signals/dark-live")
    finally:
        client.__exit__(None, None, None)
    assert "stalled" in r.text and "no candles" in r.text


def test_the_page_renders_when_binance_is_unreachable():
    """A missing mark blanks a column; it never breaks the page."""
    client = _client(payload=_payload([_open_row()]), raise_prices=True)
    try:
        r = client.get("/signals/dark-live")
    finally:
        client.__exit__(None, None, None)
    assert r.status_code == 200
    assert "AAAUSDT" in r.text


def test_the_export_carries_the_mark_and_the_freshness():
    """An export is a surface and inherits the page's rules — the SAR export
    that first showed a 2h-old stop had no column that could have said so."""
    client = _client(payload=_payload([_open_row()]), prices={"AAAUSDT": 103.0})
    try:
        r = client.get("/signals/dark-live/export.csv")
    finally:
        client.__exit__(None, None, None)
    header = r.text.splitlines()[0]
    for col in ("unrealized_pct", "unrealized_r", "resolve_age_sec",
                "resolve_misses", "freshness", "level_crossed"):
        assert col in header


def test_a_row_advanced_on_an_undatable_window_is_unverified_not_current():
    """Owner-caught 2026-07-31: the engine's snapshot dropped `open_time`, so
    after a restart it could advance a row but not date the bars it read.
    `last_resolved_at` says when the RESOLVER ran — printing it as the row's
    freshness answers a question nobody asked."""
    from app.routes.dark_signals_live import mark_freshness

    now = 1_700_000_600.0
    row = _open_row(last_resolved_at=now - 60.0, stalled=None,
                    window_undated_reason="no_timestamps")
    (row,) = mark_freshness([row], now=now)
    assert row["freshness"] == "unverified"
    assert row["is_stalled"] is False
    assert row["unverified_reason"] == "no_timestamps"


def test_an_undated_row_is_kept_out_of_the_still_measured_population():
    from app.routes.dark_signals_live import (
        mark_freshness, mark_live_pnl, summarize_open,
    )

    now = 1_700_000_600.0
    rows = [_open_row(last_resolved_at=now - 60.0,
                      window_undated_reason="stamp_before_timestamps")]
    mark_freshness(rows, now=now)
    mark_live_pnl(rows, {"AAAUSDT": 103.0})
    panel = summarize_open(rows)
    assert panel["unverified"] == 1 and panel["stalled"] == 0
    assert panel["all_marked"]["n"] == 1
    assert panel["still_measured"]["n"] == 0, (
        "an undated row is being advanced, but nothing can say its bars are "
        "current — it cannot back the population that claims they are"
    )


def test_the_page_names_why_a_row_could_not_be_dated():
    # A live clock: the route grades freshness against real "now", so a row
    # stamped in 2023 would read stalled-by-age before the undated branch runs.
    import time as _t

    client = _client(payload=_payload([_open_row(
        emitted_at=_t.time() - 600.0, last_resolved_at=_t.time() - 60.0,
        window_undated_reason="no_timestamps")]))
    try:
        r = client.get("/signals/dark-live")
    finally:
        client.__exit__(None, None, None)
    assert "unverified" in r.text and "no_timestamps" in r.text


def test_the_page_names_the_evaluator_override_and_why_it_is_carried():
    """Copy is part of the measurement. The page's first sentence claims every
    row cleared the whole chain; the SR_FLIP long disable fires inside the
    evaluator, so the page must say the candidate is carried rather than
    published there — otherwise that claim is false for those rows."""
    client = _client(payload=_payload([
        _row(setup="SR_FLIP_RETEST", gate="evaluator:sr_flip_long_disabled"),
    ]))
    try:
        r = client.get("/signals/dark-live")
    finally:
        client.__exit__(None, None, None)
    assert "evaluator" in r.text
    assert "carried" in r.text
    assert "sr_flip_long_disabled" in r.text


def test_an_evaluator_gate_filters_like_any_other():
    rows = [
        _row(setup="SR_FLIP_RETEST", gate="evaluator:sr_flip_long_disabled"),
        _row(setup="MEAN_REVERT", gate="setup_compat:regime_STRONG_TREND"),
    ]
    assert len(filter_rows(rows, gate="evaluator:sr_flip_long_disabled")) == 1
    assert len(filter_rows(rows, setup="SR_FLIP_RETEST")) == 1

# --------------------------------------------------------------------------- #
# Regular exit vs SAR exit — two outcomes on the same entries
#
# Owner, 2026-07-31: "observe this dark feed too with SAR exit mechanism along
# with regular". The trap is the comparison population: a row that resolved
# while its arm is still running describes one mechanism and not the other.
# --------------------------------------------------------------------------- #


def _arm(signal_id="SIG-1", status="CLOSED_SAR_FLIP", r_level=0.5,
         r_confirm=0.3, bars=10, tf="5m", **kw):
    arm = {
        "arm_id": f"{signal_id}:{tf}", "signal_id": signal_id, "symbol": "AAAUSDT",
        "timeframe": tf, "status": status, "r_level": r_level,
        "r_confirm": r_confirm, "r_level_risk": r_level, "r_confirm_risk": r_confirm,
        "bars_seen": bars, "lane": "dark",
    }
    arm.update(kw)
    return arm


def test_arms_are_indexed_by_signal_and_kept_per_timeframe():
    from app.routes.dark_signals_live import reduce_arms

    idx = reduce_arms({"open": [_arm(tf="5m")], "resolved": [_arm(tf="15m")]})
    assert len(idx["SIG-1"]) == 2, "pooling timeframes makes the headline move with the mix"


def test_a_broken_arms_payload_yields_no_arms_rather_than_raising():
    from app.routes.dark_signals_live import reduce_arms

    assert reduce_arms(None) == {}
    assert reduce_arms({"open": "nope"}) == {}


def test_the_row_column_shows_the_most_advanced_arm():
    """Chosen by bars consumed, never by timeframe — otherwise the column
    silently prefers a series that stopped arriving."""
    from app.routes.dark_signals_live import attach_arms, reduce_arms

    rows = [dict(_row(), signal_id="SIG-1")]
    attach_arms(rows, reduce_arms({"open": [], "resolved": [
        _arm(tf="5m", bars=3, r_level=-1.0), _arm(tf="15m", bars=40, r_level=2.0),
    ]}))
    assert rows[0]["sar_best"]["timeframe"] == "15m"
    assert rows[0]["sar_n"] == 2


def test_a_row_with_no_arm_gets_an_empty_list_not_a_zero():
    from app.routes.dark_signals_live import attach_arms

    rows = [dict(_row(), signal_id="SIG-NONE")]
    attach_arms(rows, {})
    assert rows[0]["sar_arms"] == [] and rows[0]["sar_best"] is None


def test_only_rows_decided_by_BOTH_mechanisms_enter_the_comparison():
    """The whole point of the panel. Averaging over the union would compare two
    different sets of trades and call the difference a result."""
    from app.routes.dark_signals_live import attach_arms, reduce_arms, summarize_sar

    rows = [
        dict(_row(status="CLOSED_TP1", r=2.0), signal_id="BOTH"),
        dict(_row(status="CLOSED_SL", r=-1.0), signal_id="ROWONLY"),
        dict(_row(status="OPEN"), signal_id="ARMONLY"),
    ]
    attach_arms(rows, reduce_arms({"open": [_arm("ROWONLY", status="RUNNING")], "resolved": [
        _arm("BOTH", r_level=0.9), _arm("ARMONLY", r_level=1.5),
    ]}))
    panel = summarize_sar(rows)
    assert panel["n"] == 1
    assert panel["row_only"] == 1 and panel["arm_only"] == 1
    assert panel["regular"]["avg_r"] == pytest.approx(2.0)
    assert panel["sar_level"]["avg_r"] == pytest.approx(0.9)


def test_an_unmeasured_arm_is_not_scored_as_a_result():
    """INSUFFICIENT is terminal but unscored — the absence of a measurement,
    not a 0R outcome."""
    from app.routes.dark_signals_live import attach_arms, reduce_arms, summarize_sar

    rows = [dict(_row(status="CLOSED_TP1", r=2.0), signal_id="SIG-1")]
    attach_arms(rows, reduce_arms({"open": [], "resolved": [
        _arm(status="INSUFFICIENT", r_level=None),
    ]}))
    assert summarize_sar(rows)["n"] == 0


def test_there_is_no_blended_r_across_the_two_mechanisms():
    """Collapsing them before the difference is known is choosing the answer."""
    from app.routes.dark_signals_live import attach_arms, reduce_arms, summarize_sar

    rows = [dict(_row(status="CLOSED_TP1", r=2.0), signal_id="SIG-1")]
    attach_arms(rows, reduce_arms({"open": [], "resolved": [_arm()]}))
    panel = summarize_sar(rows)
    assert "avg_r" not in panel
    assert "combined" not in panel
    assert panel["sar_level"]["avg_r"] != panel["sar_confirm"]["avg_r"]


def test_the_panel_is_measured_on_the_filtered_selection():
    """A summary over the whole ledger beside a filtered table summarises
    nothing the reader is looking at (#90/#91)."""
    from app.routes.dark_signals_live import (
        attach_arms, filter_rows, reduce_arms, summarize_sar,
    )

    rows = [
        dict(_row(setup="MEAN_REVERT", status="CLOSED_TP1", r=2.0), signal_id="A"),
        dict(_row(setup="RANGE_FADE", status="CLOSED_SL", r=-1.0), signal_id="B"),
    ]
    attach_arms(rows, reduce_arms({"open": [], "resolved": [
        _arm("A", r_level=1.0), _arm("B", r_level=-0.5),
    ]}))
    panel = summarize_sar(filter_rows(rows, setup="MEAN_REVERT"))
    assert panel["n"] == 1 and panel["sar_level"]["avg_r"] == pytest.approx(1.0)


def test_the_page_renders_the_comparison_and_says_neither_is_the_result():
    client = _client(
        payload=_payload([dict(_row(status="CLOSED_TP1", r=2.0), signal_id="SIG-1")]),
        arms={"open": [], "resolved": [_arm()]},
    )
    try:
        r = client.get("/signals/dark-live")
    finally:
        client.__exit__(None, None, None)
    assert r.status_code == 200
    assert "Regular exit vs SAR exit" in r.text
    assert "Neither column" in r.text
    assert "cost of confirmation" in r.text


def test_the_page_says_the_empty_panel_is_not_a_fault():
    """A dark row resolves within six hours; its arm exits when SAR flips,
    normally later. Empty is the expected early state."""
    client = _client(payload=_payload([dict(_row(status="OPEN"), signal_id="SIG-1")]))
    try:
        r = client.get("/signals/dark-live")
    finally:
        client.__exit__(None, None, None)
    assert "No row has both verdicts yet" in r.text
    assert "not a fault" in r.text


class TestAdverseExcursionReachesTheSurface:
    """MAE is only useful if it is readable — a field the engine writes and no
    page renders is the #817 shape, and this one exists specifically to settle
    an argument the owner is having right now.

    Without it, "would a tighter stop have helped" can only be bounded: on the
    2026-08-01 window the optimistic answer (+0.203R) and the pessimistic one
    differed by more than the whole edge under discussion, because the gap is
    exactly "did the winners survive the tighter stop" and nothing counted it.
    """

    def test_the_export_carries_both_halves_of_the_excursion(self):
        from app.routes.dark_signals_live import _DARK_COLS

        assert "mfe_pct" in _DARK_COLS
        assert "mae_pct" in _DARK_COLS, (
            "MFE without MAE cannot answer any stop-distance question"
        )

    def test_the_export_carries_the_walk_coverage_and_its_cause(self):
        """An expiry is only as good as the walk behind it, and INSUFFICIENT has
        two causes that must never be pooled."""
        from app.routes.dark_signals_live import _DARK_COLS

        assert "window_coverage" in _DARK_COLS
        assert "insufficient_reason" in _DARK_COLS

    def test_the_page_renders_mae_beside_mfe(self):
        row = _row(status="CLOSED_SL", r=-1.0)
        row["mfe_pct"] = 0.42
        row["mae_pct"] = 3.10

        client = _client(payload=_payload([row]))
        try:
            page = client.get("/signals/dark-live").text
        finally:
            client.__exit__(None, None, None)

        assert "MAE" in page, "the column must be labelled, not just exported"
        assert "3.10" in page, "and the value must actually render"


class TestTheMoneyLeadsNotR:
    """Owner, 2026-08-02: *"that R is purely confusing — 3% SL still only 1R"*.

    `signal_dispatch` sizes every position at a fixed notional
    (`raw_qty = notional / entry_price`), so the stop distance appears nowhere in
    the sizing. R divides each outcome by its own stop, which equalises trades
    only when size scales inversely to that stop — and it does not. Two trades
    can share a −1.00R while costing wildly different money.

    This is not presentation. On the 2026-08-02 window MEAN_REVERT reads
    worst-in-book by R (−0.481R) while losing $2.02 per trade, and
    MA_CROSS_TREND_SHIFT reads mid-table (−0.293R) while losing $11.05 — R ranks
    the bigger money-loser as the healthier path, because its stops are ~5x wider.
    """

    @staticmethod
    def _row_with(setup, pnl, sl_pct):
        row = _row(setup=setup, status="CLOSED_SL", r=-1.0)
        row["pnl_pct"] = pnl
        row["sl_distance_pct"] = sl_pct
        return row

    def test_the_rollup_reports_the_money(self):
        agg = summarize([
            self._row_with("MEAN_REVERT", -1.0, 1.0),
            self._row_with("MEAN_REVERT", -1.0, 1.0),
        ])["by_setup"][0]

        assert agg["avg_pnl_pct"] == pytest.approx(-1.0)
        assert agg["total_pnl_pct"] == pytest.approx(-2.0)
        # R is still there as the bridge to the Strategy Lab, just not the lead.
        assert agg["avg_r"] == pytest.approx(-1.0)

    def test_R_and_money_can_rank_two_paths_in_opposite_orders(self):
        """The exact distortion the owner named, reproduced as a unit test.

        Both paths take a full stop-out, so both read −1.00R. One risks 1% and
        the other 6%: identical in R, six times apart in money.
        """
        out = summarize([
            self._row_with("TIGHT_STOP_PATH", -1.0, 1.0),
            self._row_with("WIDE_STOP_PATH", -6.0, 6.0),
        ])["by_setup"]
        by_name = {a["setup_class"]: a for a in out}

        tight, wide = by_name["TIGHT_STOP_PATH"], by_name["WIDE_STOP_PATH"]
        # R cannot tell them apart at all...
        assert tight["avg_r"] == wide["avg_r"] == pytest.approx(-1.0)
        # ...while the money says one is six times worse.
        assert wide["avg_pnl_pct"] == pytest.approx(6.0 * tight["avg_pnl_pct"])

    def test_a_row_without_an_entry_risk_stamp_still_reports_its_money(self):
        """R silently shrinks its own population; a percentage needs no
        denominator, so the money column keeps the row."""
        row = self._row_with("MEAN_REVERT", -2.5, 2.5)
        row["r_multiple"] = None
        agg = summarize([row])["by_setup"][0]

        assert agg["avg_r"] is None
        assert agg["avg_pnl_pct"] == pytest.approx(-2.5)
