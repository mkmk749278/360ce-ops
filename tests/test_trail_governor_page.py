"""`/signals/trail-governor` — the page that renders a live money-path mechanism.

The failure this page must not have is a **reassuring blank**. Off, armed,
index-cold and working-on-a-quiet-book all produce an empty table, and they
have four different next moves — so most of what is asserted here is that the
states stay distinguishable, not that a number is right.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.routes import trail_governor as tgr  # noqa: E402


def _payload(**over):
    """The engine's real payload shape (``/internal/diag/trail-governor``).

    Kept beside the producing side's own contract test, which drives the real
    assembler — this one is the reader's half.
    """
    base = {
        "schema": 1,
        "enabled": True,
        "timeframe": "15m",
        "index_cold": False,
        "open_total": 3,
        "governed": 1,
        "health": {
            "cycles": 12, "governed": 1, "handovers": 1, "replaced": 4,
            "unchanged": 7, "place_failed": 0, "orphan_cancel": 0,
            "refusals": {"not_onside": 5, "ladder_touched": 2},
        },
        "rows": [{
            "signal_id": "SIG1", "symbol": "BTCUSDT", "side": "LONG",
            "mechanism": "sar", "governing": True, "entry": 100.0,
            "designed_sl": 97.0, "parked_stop": 98.25, "stop_order_id": 4242,
            "seq": 5, "last_bar_ms": 1_700_000_900_000.0, "bar_age_sec": 42.0,
            "ladder_untouched": True,
        }],
    }
    base.update(over)
    return base


@pytest.fixture
def client():
    class _API:
        """Overrides only `trail_governor`; everything else (the login
        redirect renders `/`, which calls several) falls through to the real
        client rather than being hand-written here."""

        payload: dict = _payload()

        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        async def trail_governor(self):
            return _API.payload

    _API.payload = _payload()
    # Context-manager the client so `lifespan` runs and sets
    # `app.state.templates`; the stub engine_api goes on AFTER, because
    # lifespan installs the real one.
    with TestClient(app) as c:
        prev = app.state.engine_api
        app.state.engine_api = _API(prev)
        c.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
        try:
            yield c, _API
        finally:
            app.state.engine_api = prev


def _get(client):
    c, _ = client
    r = c.get("/signals/trail-governor")
    assert r.status_code == 200, r.status_code
    return r.text


def _flat(html: str) -> str:
    """Whitespace-collapsed lowercase text, for asserting on copy.

    A sentence that wraps across two template lines is the same sentence to a
    reader; asserting on the raw source would make every reflow a test failure
    and tempt the next person to weaken the assertion instead.
    """
    import re

    return re.sub(r"\s+", " ", html).lower()


# --------------------------------------------------------------------------- #
# The five states stay distinguishable
# --------------------------------------------------------------------------- #


def test_state_machine_separates_the_four_empty_tables():
    assert tgr.lane_state(_payload()) == "governing"
    assert tgr.lane_state(_payload(governed=0, rows=[])) == "armed"
    assert tgr.lane_state(_payload(enabled=False, governed=0, rows=[])) == "off"
    assert tgr.lane_state(_payload(index_cold=True, rows=[])) == "index_cold"
    assert tgr.lane_state({"error": "boom"}) == "error"
    assert tgr.lane_state("not a dict") == "error"


def test_index_cold_never_reads_as_an_empty_book(client):
    c, api = client
    api.payload = _payload(index_cold=True, rows=[], governed=0, open_total=None)
    html = _get(client)
    assert "INDEX COLD" in html
    assert "the same as no open positions" in _flat(html)


def test_off_is_stated_as_the_default_not_as_a_fault(client):
    c, api = client
    api.payload = _payload(enabled=False, governed=0, rows=[])
    html = _get(client)
    assert "OFF" in html
    assert "is not a fault" in _flat(html)


def test_armed_but_governing_nothing_is_its_own_state(client):
    c, api = client
    api.payload = _payload(governed=0, rows=[])
    html = _get(client)
    assert "ARMED" in html
    assert "governing nothing" in _flat(html)


def test_error_is_could_not_answer_not_nothing_governed(client):
    c, api = client
    api.payload = {"error": "ConnectionError: refused"}
    html = _get(client)
    assert "UNAVAILABLE" in html
    assert "ConnectionError" in html


# --------------------------------------------------------------------------- #
# Refusals: classification is copy, not a mirror
# --------------------------------------------------------------------------- #


def test_an_unknown_refusal_renders_under_its_raw_name_rather_than_vanishing():
    """Iterating ops' own key list would be silent by construction on the next
    reason the engine adds — MEASUREMENT_SUFFIXES wearing another hat."""
    out = tgr.classify_refusals({"refusals": {"some_new_engine_reason": 3}})
    assert len(out) == 1
    assert out[0]["reason"] == "some_new_engine_reason"
    assert out[0]["cls"] == "unclassified"
    assert out[0]["count"] == 3


def test_refusals_are_classed_and_ordered_by_weight():
    out = tgr.classify_refusals(
        {"refusals": {"not_onside": 2, "stale_series": 9, "disabled": 1}}
    )
    assert [r["reason"] for r in out] == ["stale_series", "not_onside", "disabled"]
    by = {r["reason"]: r["cls"] for r in out}
    assert by["stale_series"] == "fault"
    assert by["not_onside"] == "expected"
    assert by["disabled"] == "switch"


def test_expected_refusals_are_not_classed_as_faults():
    """`not_onside` and `ladder_touched` are the mechanism working. Bucketing
    them with a fault is how a page reports a problem that is not happening."""
    for reason in ("not_onside", "ladder_touched"):
        assert tgr.REFUSAL_COPY[reason][0] == "expected"


def test_every_classified_refusal_carries_a_written_reason():
    for reason, (cls, note) in tgr.REFUSAL_COPY.items():
        assert note.strip(), f"{reason} has no explanation"
        assert cls in {"switch", "expected", "fault"}


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #


def test_pre_handover_rows_do_not_render_a_parked_stop(client):
    """Before handover the evaluator's SL is still live and nothing is parked;
    printing a level there would claim a stop that does not exist."""
    c, api = client
    row = dict(_payload()["rows"][0])
    row.update(governing=False, parked_stop=0.0, seq=0)
    api.payload = _payload(rows=[row], governed=1)
    html = _get(client)
    assert "pre-handover" in html
    assert "98.25" not in html


def test_governed_rows_lead_the_table(client):
    c, api = client
    a = dict(_payload()["rows"][0], symbol="ZZZUSDT", governing=True)
    b = dict(_payload()["rows"][0], symbol="AAAUSDT", governing=False)
    api.payload = _payload(rows=[b, a], governed=2)
    html = _get(client)
    assert html.index("ZZZUSDT") < html.index("AAAUSDT")


def test_the_page_says_the_edge_does_not_exclude_zero(client):
    """Copy is part of the measurement. This page controls a real-money
    mechanism whose measured edge over the current exit included zero; a reader
    arriving cold must not read 'GOVERNING' as 'this was proven'."""
    html = _get(client)
    assert "does not exclude zero" in _flat(html)
    assert "canary" in _flat(html)


def test_the_page_states_that_two_switches_are_required(client):
    html = _get(client)
    assert "per user" in _flat(html)
    assert "exit_mechanism" in html


def test_freshness_is_graded_on_the_engines_stamp(client):
    """#108 — a surface may not grade its own liveness on a clock it supplies.
    The page must render the engine's bar age, not compute one."""
    c, api = client
    row = dict(_payload()["rows"][0], bar_age_sec=54.348713397979736)
    api.payload = _payload(rows=[row])
    html = _get(client)
    assert "Bar age" in html
    # Rendered through the shared `secs` filter, never as a raw float repr —
    # a feed age at fifteen digits is fourteen digits of noise (2026-08-07).
    assert "54.348713397979736" not in html
    assert "54.3" in html


def test_mechanism_and_timeframe_come_from_the_payload(client):
    c, api = client
    api.payload = _payload(timeframe="5m")
    assert "5m" in _get(client)


# --------------------------------------------------------------------------- #
# Reachability — "dark work must be observable" has a last hop
# --------------------------------------------------------------------------- #


def test_the_page_is_registered_before_signal_detail():
    """`/signals/{signal_id}` matches any literal that follows it."""
    import pathlib
    import re

    src = (pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    mine = src.index("app.include_router(trail_governor.router)")
    detail = src.index("app.include_router(signal_detail.router)")
    assert mine < detail, "trail_governor must be included before signal_detail"
    assert re.search(r"trail_governor,", src), "route module not imported"


def test_the_page_actually_resolves(client):
    """The route list is not the authority — the request is."""
    _get(client)
