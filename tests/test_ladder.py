"""The price ladder that replaced four price columns on /positions and the
raw JSON on the signal dossier (outside audit, 2026-11-25)."""
from __future__ import annotations

from app.ladder import ladder


def test_long_places_levels_on_one_axis():
    lad = ladder("LONG", 90, 100, 105, 110, 120)
    assert lad["refused"] is None
    pos = {m["label"]: m["pos"] for m in lad["marks"]}
    assert pos == {"SL": 0.0, "Entry": 33.33, "TP1": 66.67, "TP2": 100.0}
    assert lad["current"]["pos"] == 50.0 and lad["current"]["favourable"]


def test_short_uses_the_same_axis_reversed():
    lad = ladder("SHORT", 110, 100, 95, 90, 80)
    assert lad["refused"] is None
    assert lad["current"]["favourable"] is True
    assert [m["pos"] for m in lad["marks"]] == [0.0, 33.33, 66.67, 100.0]


def test_levels_out_of_order_are_refused_not_clamped():
    # A LONG with its stop above entry has no honest axis.
    assert ladder("LONG", 105, 100, 101, 110)["refused"]
    assert ladder("SHORT", 90, 100, 99, 110)["refused"]


def test_missing_levels_are_refused():
    assert ladder("LONG", None, 100, 101, 110)["refused"]
    assert ladder("LONG", 90, 100, 101, None)["refused"]
    assert ladder("", 90, 100, 101, 110)["refused"]


def test_a_price_past_the_stop_is_shown_at_the_edge_and_named():
    cur = ladder("LONG", 90, 100, 85, 110)["current"]
    assert cur["pos"] == 0.0 and cur["beyond"] == "past stop"
    assert cur["favourable"] is False


def test_a_moved_stop_is_its_own_mark_and_does_not_move_the_axis():
    lad = ladder("LONG", 90, 100, 105, 110, stop_now=100)
    marks = {m["label"]: m for m in lad["marks"]}
    assert marks["SL"]["price"] == 90 and marks["SL"]["pos"] == 0.0
    assert marks["Stop now"]["price"] == 100


def test_no_current_price_still_draws_the_levels():
    lad = ladder("LONG", 90, 100, None, 110)
    assert lad["refused"] is None and lad["current"] is None
