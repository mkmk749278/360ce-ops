"""The payload census panel — driven against the ENGINE's real assembler.

`/signals/ai-governor` renders what the model was shown, and every figure on it
is the engine's. Ops must reduce no ledger row: the api container has never
evaluated a candidate, so a locally-assembled version reports a healthy zero —
`INDEX COLD`, and the promotion census `{}` before it.

The contract test imports the engine's own `build_diag` rather than a fixture,
because a fixture chooses a location and then agrees with you about it. The
price-action lane card rendered `NOT REPORTED` against production with every
ops test green, and `zone_distance_atr` was uncomputable from the day it
shipped for the same reason.
"""
from __future__ import annotations

import pathlib
import sys
from typing import Any, Dict

import pytest

from app.routes import ai_governor as page


def _engine_diag() -> dict:
    """The engine's own assembler, never a shape this repo invented."""
    engine = pathlib.Path(__file__).resolve().parents[2] / "360-v2"
    if not engine.exists():
        pytest.skip("engine repo not checked out beside ops")
    sys.path.insert(0, str(engine))
    try:
        from src.execution import ai_governor as gov  # type: ignore
        return gov.build_diag()
    finally:
        sys.path.remove(str(engine))


# ---------------------------------------------------------------------------
# The cross-repo contract
# ---------------------------------------------------------------------------


def test_the_engine_actually_publishes_the_key_this_page_reads():
    """A field one repo reads and no repo writes is #817; with the arrow
    reversed it is the price-action card, and the producing side's test passes
    either way. Driven against the real `build_diag`."""
    diag = _engine_diag()
    assert "payload_census" in diag, (
        "the engine does not publish payload_census — this page would render "
        "NOT REPORTED against production with every ops test green"
    )


def test_the_census_is_top_level_on_the_diag_not_nested():
    """Asserts the PATH, including that it is not somewhere else. A fixture
    that guessed the location would agree with the guess."""
    diag = _engine_diag()
    assert isinstance(diag.get("payload_census"), dict)
    # And explicitly NOT under `health`, which is where a reader might assume
    # a census of ledger rows would live.
    assert "payload_census" not in (diag.get("health") or {})


def test_every_block_this_page_renders_is_one_the_engine_emits():
    """Each key the template reads, checked against the real payload shape.

    The engine's ledger is empty in a sandbox, so an unseeded call returns the
    two-key `measured: False` shape and the interesting half of the contract
    would SKIP — a check that only runs where it cannot fail. One real row is
    seeded through the engine's own ledger so the full shape is always
    exercised.
    """
    engine = pathlib.Path(__file__).resolve().parents[2] / "360-v2"
    if not engine.exists():
        pytest.skip("engine repo not checked out beside ops")
    sys.path.insert(0, str(engine))
    try:
        from src import ai_governor_ledger as led  # type: ignore
        from src.execution import ai_governor as gov  # type: ignore

        led.reset_ledger(led.GovernorLedger(path=""))
        led.get_ledger().add({
            "signal_id": "s1", "action": "MAINTAIN", "issued_at": 1.0,
            "usage": {"output_tokens": 40, "thinking_tokens": 1500},
            "snapshot": {
                "premise": {"thesis": "t", "conditions": [
                    {"name": "a", "at_entry": 1.0, "at_entry_reason": None,
                     "now": 2.0}]},
                "bars": {"readable": True, "n": 12},
                "macro": {"btc_opposes_now": True},
                "tp_candidates": [{"key": "tp_0"}],
                "sl_candidates": [{"key": "sl_0"}],
            },
        })
        try:
            census = gov.build_diag()["payload_census"]
            assert census.get("measured") is True
            for key in ("premise", "bars", "macro", "menu", "tokens"):
                assert key in census, (
                    f"the page renders {key} and the engine omits it"
                )
            # And the nested keys the template actually indexes.
            assert "conditions_declared" in census["premise"]
            assert "at_entry_reasons" in census["premise"]
            assert "tp_sizes" in census["menu"]
            assert "thinking_mean" in census["tokens"]
        finally:
            led.reset_ledger(led.GovernorLedger(path=""))
    finally:
        sys.path.remove(str(engine))


# ---------------------------------------------------------------------------
# The classifier is its OWN, and keys on its own shape
# ---------------------------------------------------------------------------


def test_the_census_has_its_own_classifier():
    """Running one payload through another's classifier grades a healthy lane
    as an engine predating the page. Three classifiers, three shape keys."""
    healthy = {"ok": True, "error": "", "result": {
        "payload_census": {"rows": 3, "measured": True}}}
    assert page.classify_census(healthy) == "ok"
    # The LANE classifier keys on measure_enabled, which this payload lacks —
    # so it must not be what grades the census.
    assert page.classify(healthy) != "ok"


def test_an_engine_without_the_block_reads_not_reported_not_empty():
    payload = {"ok": True, "error": "", "result": {"measure_enabled": True}}
    assert page.classify_census(payload) == "not_reported"


def test_an_unmeasured_lane_is_quiet_not_broken():
    """A panel rendering 0% here would report a healthy lane on an empty one."""
    payload = {"ok": True, "error": "", "result": {
        "payload_census": {"rows": 0, "measured": False}}}
    assert page.classify_census(payload) == "unmeasured"


def test_a_blank_transport_error_is_still_an_error():
    """`str(httpx.ReadTimeout())` is "", so a timed-out call fails an
    `if payload.get("error")` exactly as a healthy one does. Tell producers
    apart by a key only one of them has."""
    assert page.classify_census({"endpoint": "/x", "error": ""}) == "unreachable"
    assert page.classify_census({"endpoint": "/x", "error": "boom"}) == "unreachable"


# ---------------------------------------------------------------------------
# The distinctions the panel must not collapse
# ---------------------------------------------------------------------------


def test_a_missing_block_is_counted_apart_from_an_unreadable_one():
    census = {
        "premise": {"rows_with_block": 3, "rows_without_block": 7},
        "bars": {"rows_with_block": 10, "rows_without_block": 0},
        "macro": {"rows_with_block": 10, "rows_without_block": 0},
    }
    rows = {r["key"]: r for r in page.census_blocks(census)}
    assert rows["premise"]["without_block"] == 7
    assert rows["premise"]["share"] == pytest.approx(30.0)
    assert rows["bars"]["share"] == pytest.approx(100.0)


def test_the_reason_tables_iterate_the_engines_payload():
    """A reason ops has never heard of renders under its raw name badged,
    never dropped — iterating this page's own keys would be silent by
    construction on the next reason the engine adds."""
    rows = page.annotate({"no_entry_stamp": 4, "brand_new_reason": 1},
                         page.PREMISE_ENTRY_COPY)
    by_name = {r["name"]: r for r in rows}
    assert by_name["no_entry_stamp"]["meaning"]
    assert by_name["brand_new_reason"]["unclassified"] is True
    assert by_name["brand_new_reason"]["count"] == 1


def test_no_annotated_row_key_collides_with_a_dict_method():
    """Jinja resolves an attribute before an item, so a payload key named
    `copy` renders `<built-in method copy…>` at the reader. `/system/redis`
    paid for this on `keys` and the throttle table paid for it on `copy`."""
    for row in page.annotate({"x": 1}, {}):
        assert not (set(row) & set(dir({}))), row


# ---------------------------------------------------------------------------
# Menu sizes — the number that makes an all-zero column readable
# ---------------------------------------------------------------------------


def test_a_menu_of_one_is_badged_as_offering_no_alternative():
    rows = page.menu_size_rows({"menu": {"tp_sizes": {"1": 7, "2": 3},
                                         "sl_sizes": {"3": 10}}})
    tp = [r for r in rows if r["side"] == "TP"]
    inert = [r for r in tp if r["inert"]]
    assert len(inert) == 1 and inert[0]["size"] == 1 and inert[0]["count"] == 7
    assert inert[0]["pct"] == pytest.approx(70.0)
    assert all(not r["inert"] for r in rows if r["side"] == "SL")


def test_menu_rows_are_empty_rather_than_invented_when_absent():
    assert page.menu_size_rows({}) == []
    assert page.menu_size_rows(None) == []
