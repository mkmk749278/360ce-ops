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
         status="OPEN", r=None, ts=1_700_000_000.0, conf=70.0):
    return {
        "symbol": "AAAUSDT", "side": "LONG", "setup_class": setup,
        "dark_gate": gate, "status": status, "r_multiple": r,
        "emitted_at": ts, "confidence": conf, "entry": 100.0,
        "stop_loss": 97.0, "tp1": 106.0,
    }


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


def _client(payload=None, provenance=None):
    from fastapi.testclient import TestClient

    from app.main import app

    _prov = provenance if provenance is not None else {
        "file": "dark_signals_live_v1.json", "version": 1, "exists": True,
        "modified_at": "2026-07-31 07:00 UTC", "age_sec": 30.0,
    }
    client = TestClient(app)
    client.__enter__()
    vol = app.state.data_volume
    vol.dark_signals = lambda: (payload if payload is not None else _payload([_row()]))
    vol.dark_signals_provenance = lambda: _prov
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
