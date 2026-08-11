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
        #: What the data volume's `trail_exits_v1.json` holds. The engine's own
        #: reader returns this shape for a file it has never written.
        history: object = {"error": "missing: /engine-data/trail_exits_v1.json"}

        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        async def trail_governor(self):
            return _API.payload

    _API.payload = _payload()
    _API.history = {"error": "missing: /engine-data/trail_exits_v1.json"}
    # Context-manager the client so `lifespan` runs and sets
    # `app.state.templates`; the stub engine_api goes on AFTER, because
    # lifespan installs the real one.
    with TestClient(app) as c:
        prev = app.state.engine_api
        app.state.engine_api = _API(prev)
        # The traded record is read off the DATA VOLUME, not the diag payload:
        # the payload carries only a bounded tail because it rides a snapshot
        # written every ~15s. Only the two calls that would hit disk are
        # swapped, so the seam under test is the one the page actually uses.
        vol = app.state.data_volume
        vol_hist, vol_prov = vol.trail_history, vol.trail_history_provenance
        vol.trail_history = lambda: _API.history
        vol.trail_history_provenance = lambda: {
            "file": "trail_exits_v1.json", "exists": True,
            "modified_at": "2026-08-11 06:00 UTC", "age_sec": 12.0,
        }
        c.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
        try:
            yield c, _API
        finally:
            app.state.engine_api = prev
            vol.trail_history = vol_hist
            vol.trail_history_provenance = vol_prov


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


# --------------------------------------------------------------------------- #
# A placement rejection has to say what the exchange said
#
# Live on the owner's account 2026-08-10: once the governing timeframe was
# fixed, `place_failed` climbed at 2 per sweep with `handovers` stuck at 0 —
# every stop the governor computed was being refused, and the page could only
# show the integer. -2021 (level already through the mark), -1111 (rounding)
# and a dead key are three different fixes behind one number, and the reason
# lived in a log line that needs `docker exec`.
# --------------------------------------------------------------------------- #


def _rejection_row(html: str, symbol: str) -> str:
    """The one `<tr>` of the rejections table carrying ``symbol``.

    Asserting on the row rather than the page is what keeps "this cell shows a
    dash" from being coupled to every sentence elsewhere on the page.
    """
    import re

    for row in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        if symbol in row and "seq" not in row:
            return row
    raise AssertionError(f"no rejection row for {symbol}")


def _row_containing(html: str, needle: str) -> str:
    """The one `<tr>` carrying ``needle``, wherever on the page it is.

    Asserting on a row rather than the page is what keeps a cell-level claim
    from being coupled to every sentence elsewhere — pass a value that appears
    exactly once, or this raises rather than picking one.
    """
    import re

    hits = [r for r in re.findall(r"<tr>(.*?)</tr>", html, re.S) if needle in r]
    assert len(hits) == 1, f"expected one row for {needle}, found {len(hits)}"
    return hits[0]


def _code_cell(row: str) -> str:
    """The Binance-code cell — the fifth `<td>` of a rejection row."""
    import re

    cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
    assert len(cells) >= 5, f"unexpected rejection row shape: {len(cells)} cells"
    return cells[4].strip()


def _failure(**over):
    base = {
        "ts": 1_700_000_900.0, "symbol": "BLUAIUSDT", "side": "SHORT",
        "signal_id": "SIG9", "seq": 1, "level": 0.0155252,
        "kind": "OrderRejectedByBinance", "binance_code": -2021,
        "error": ("order placement failed (phase=place): code=BINANCE_HTTP_ERROR "
                  "status=400 message=Binance returned -2021: Order would "
                  "immediately trigger."),
    }
    base.update(over)
    return base


def _with_failures(*failures, **health_over):
    health = dict(_payload()["health"])
    health.update({"place_failed": len(failures), "place_failures": list(failures)})
    health.update(health_over)
    return _payload(health=health)


def test_the_exchange_s_own_words_reach_the_page(client):
    c, api = client
    api.payload = _with_failures(_failure())
    html = _get(client)
    assert "BLUAIUSDT" in html
    assert "-2021" in html
    assert "immediately trigger" in _flat(html)


def test_a_non_binance_rejection_shows_a_dash_rather_than_a_zero(client):
    """"The exchange refused" and "we never reached the exchange" send a reader
    to different places; a 0 there reads as a Binance code nobody can look up.

    Scoped to the rejections TABLE.  This asserted ``">-2021<" not in html``
    over the whole page, which held only while no prose on the page mentioned a
    Binance code — a page-wide substring standing in for a claim about one
    cell.  2026-08-11's copy explains the -2021 the exit leg was built to
    remove, and the assertion failed on an entirely correct page.  The claim was
    always about the cell, so it is now made there: a proxy that breaks when
    unrelated copy changes teaches the next reader to weaken it.
    """
    c, api = client
    api.payload = _with_failures(
        _failure(binance_code=None, kind="OrderPlacementKeyError",
                 error="code=KEY_BLOB_NOT_FOUND status=0 message=not connected")
    )
    html = _get(client)
    assert "KEY_BLOB_NOT_FOUND" in html
    row = _rejection_row(html, "BLUAIUSDT")
    assert "—" in row
    assert "-2021" not in row
    assert "0" not in _code_cell(row)


def test_an_engine_that_reports_no_detail_is_not_read_as_no_rejections(client):
    """A missing key means an older engine, not a clean run — the two have
    different next moves and the page must not merge them."""
    c, api = client
    health = dict(_payload()["health"])
    health["place_failed"] = 7
    health.pop("place_failures", None)
    api.payload = _payload(health=health)
    flat = _flat(_get(client))
    assert "does not report rejection detail" in flat
    assert "none recorded" not in flat


def test_no_rejections_says_the_exchange_accepted_every_stop(client):
    c, api = client
    api.payload = _with_failures(place_failed=0)
    flat = _flat(_get(client))
    assert "none recorded" in flat
    assert "accepted every stop" in flat


def test_an_empty_ring_over_a_nonzero_count_is_not_read_as_clean(client):
    """The counter and the ring are filled by the same call, so this pairing
    only happens across a deploy — and "the exchange accepted every stop" is a
    conclusion these two numbers do not support."""
    c, api = client
    api.payload = _with_failures(place_failed=6)
    flat = _flat(_get(client))
    assert "accepted every stop" not in flat
    assert "6" in flat


def test_the_ring_is_shown_as_a_sample_of_a_larger_count(client):
    """A bounded buffer feeding a display must publish the total beside it, or
    the newest few read as the whole population."""
    c, api = client
    api.payload = _with_failures(_failure(), _failure(symbol="TSTUSDT"),
                                 place_failed=412)
    flat = _flat(_get(client))
    assert "the last 2 of" in flat
    assert "412" in flat


def test_retry_deferred_is_rendered_and_explained(client):
    """It is neither a success nor a new failure; unshown, a reader cannot tell
    a throttled retry from a governor that stopped trying."""
    c, api = client
    health = dict(_payload()["health"])
    health["retry_deferred"] = 9
    api.payload = _payload(health=health)
    html = _get(client)
    assert "retry_deferred" in html
    assert ">9<" in html.replace(" ", "").replace("\n", "")


def test_no_quantity_is_classified_as_a_fault_not_an_exchange_rejection(client):
    """It is our own book saying there is nothing to protect — the exchange was
    never asked, so it must not read as a placement failure."""
    c, api = client
    health = dict(_payload()["health"])
    health["refusals"] = {"no_quantity": 2}
    api.payload = _payload(health=health)
    flat = _flat(_get(client))
    assert "no_quantity" in flat
    assert "unclassified" not in flat.split("no_quantity")[1][:400]
    assert "never asked" in flat


def test_the_two_resting_stops_are_not_described_as_close_position(client):
    """The governor's stop is reduceOnly with a size (engine #915) — Binance
    refuses a second closePosition stop in the same direction, which is what
    made every handover impossible. The copy explaining why two stops are safe
    has to describe the orders actually being placed."""
    flat = _flat(_get(client))
    assert "reduceonly" in flat
    assert "both are closeposition" not in flat


# --------------------------------------------------------------------------- #
# 2026-08-11 — the page must state what it is about to be read as
# --------------------------------------------------------------------------- #


def test_armed_names_the_timeframe_and_the_open_book(client):
    """GUARD (fails pre-fix).

    ``timeframe`` and ``open_total`` were rendered ONLY inside the `governing`
    branch, so in the state the owner was actually in the page showed neither —
    and the two states it has to separate there are "nobody opted in" and "the
    engine has lost a position it was governing". The timeframe is the setting
    that has already been wrong once (`5` where the store is keyed `5m`).
    """
    c, api = client
    api.payload = _payload(governed=0, rows=[], open_total=3, timeframe="5m")
    flat = _flat(_get(client))
    assert "armed" in flat
    assert "5m" in flat
    assert "3" in flat
    assert "exit_mechanism" in flat


def test_armed_with_open_positions_points_at_the_per_user_setting(client):
    """Open positions and none governed is the state worth acting on: the
    mechanism is per user, so that is where a reader has to look next."""
    c, api = client
    api.payload = _payload(governed=0, rows=[], open_total=2)
    assert "/control/users" in _get(client)


def test_armed_with_an_empty_book_does_not_raise_the_alarm(client):
    """...and with nothing open it must NOT, or the page cries wolf on the
    ordinary quiet case."""
    c, api = client
    api.payload = _payload(governed=0, rows=[], open_total=0)
    assert "/control/users" not in _get(client)


def test_the_refusal_section_renders_when_the_mix_is_empty(client):
    """GUARD. The state copy says "read the refusal mix below"; the section was
    gated on the mix being non-empty, so it pointed at evidence that was not on
    the page in exactly the state where its absence is the finding."""
    c, api = client
    health = dict(_payload()["health"])
    health["refusals"] = {}
    api.payload = _payload(governed=0, rows=[], health=health)
    flat = _flat(_get(client))
    assert "why the rest are not governed" in flat
    assert "no refusal recorded" in flat


def test_the_two_governed_counts_are_never_silently_merged(client):
    """GUARD. `payload["governed"]` counts every open position with a
    mechanism; `health["governed"]` also requires `protection_mode=managed`.
    Both rendered under the word "governed" — one in the badge, one in the
    counters table — so a legitimate difference read as a contradiction."""
    c, api = client
    health = dict(_payload()["health"])
    health["governed"] = 0
    api.payload = _payload(governed=1, health=health)
    flat = _flat(_get(client))
    assert "different populations" in flat
    assert "protection_mode" in flat


def test_matching_counts_do_not_print_the_note(client):
    """It is a reconciliation note, not decoration — it must appear only when
    the two actually differ."""
    c, api = client
    api.payload = _payload()
    assert "different populations" not in _flat(_get(client))


def _history(*rows, evicted=0):
    return {"schema": 1, "written_at": 1_700_000_500.0, "evicted": evicted,
            "max_rows": 5000, "rows": list(rows)}


def _exit(**over):
    base = {
        "ts": 1_700_000_100.0, "signal_id": "S1", "uid": "U1",
        "symbol": "BTCUSDT", "side": "LONG", "mechanism": "sar",
        "exit_kind": "trail_stop", "entry": 100.0, "exit": 98.0,
        "pnl_pct": -2.0, "designed_sl": 97.0, "parked_stop": 98.0, "seq": 4,
    }
    base.update(over)
    return base


def test_traded_history_renders_and_keeps_the_two_fills_apart(client):
    """GUARD (fails pre-fix — there was no such panel).

    `handovers`/`replaced` say the machine is turning and cannot say what it
    earned, so a governed exit's realized result was on no surface in either
    repo. The two fills are never pooled, exactly as on `/signals/sar-live`.
    """
    c, api = client
    api.history = _history(
        _exit(signal_id="S1", symbol="BTCUSDT", exit_kind="trail_stop",
              pnl_pct=-2.0),
        _exit(signal_id="S2", symbol="ETHUSDT", side="SHORT",
              exit_kind="flip_close", entry=50.0, exit=49.0, pnl_pct=2.0),
    )
    html = _get(client)
    flat = _flat(html)
    assert "traded history" in flat
    assert "BTCUSDT" in html and "ETHUSDT" in html
    assert "@level" in html and "@confirm" in html
    assert "no blended average" in flat


def test_the_history_survives_a_restart_and_says_so(client):
    """The point of the ledger. The first cut of this panel read an in-memory
    ring of 40 that every deploy destroyed — on a mechanism that re-deploys
    several times a session, that is not history."""
    c, api = client
    api.history = _history(_exit())
    flat = _flat(_get(client))
    assert "persisted across deploys" in flat
    assert "not a measurement" in flat


def test_an_unclassified_fill_renders_under_its_raw_name(client):
    """Iterating ops' own key list would be silent by construction on the next
    fill the engine adds — MEASUREMENT_SUFFIXES wearing another hat."""
    c, api = client
    api.history = _history(_exit(exit_kind="some_new_engine_fill"))
    html = _get(client)
    assert "some_new_engine_fill" in html
    assert "unclassified" in _flat(html)


def test_an_unreadable_ledger_is_not_read_as_an_empty_book(client):
    """"Could not answer" and "nothing has closed" send a reader to completely
    different places."""
    c, api = client
    api.history = {"error": "JSONDecodeError: line 1"}
    flat = _flat(_get(client))
    assert "unreadable" in flat
    assert "no governed exit recorded yet" not in flat


def test_a_file_the_engine_never_wrote_is_not_a_fault(client):
    """The engine's reader says "missing: <path>" for a file it has never
    written — an engine that predates the ledger, or a governor that has never
    closed a position. Not a fault on our side."""
    c, api = client
    api.history = {"error": "missing: /engine-data/trail_exits_v1.json"}
    flat = _flat(_get(client))
    assert "no governed exit recorded yet" in flat
    assert "unreadable" not in flat


def test_an_evicted_ring_says_this_is_not_the_whole_book(client):
    """A verdict on a capped buffer is a verdict on a sample, and a reader
    cannot see the cap."""
    c, api = client
    api.payload = _payload(history_stats={
        "rows": 5000, "evicted": 12, "max_rows": 5000, "complete": False,
    })
    api.history = _history(_exit(), evicted=12)
    flat = _flat(_get(client))
    assert "rotated" in flat
    assert "12" in flat


def test_the_export_is_reachable_and_uncapped(client):
    """A truncated export is a trade record with a hole in it, and these rows
    cannot be re-derived."""
    c, api = client
    api.history = _history(*[_exit(signal_id=f"S{i}") for i in range(3)])
    assert "/signals/trail-governor/history.csv" in _get(client)
    r = c.get("/signals/trail-governor/history.csv")
    assert r.status_code == 200
    body = r.text
    assert "exit_kind" in body
    assert body.count("BTCUSDT") == 3


def test_the_export_honours_the_filter(client):
    c, api = client
    api.history = _history(
        _exit(signal_id="S1", symbol="BTCUSDT"),
        _exit(signal_id="S2", symbol="ETHUSDT"),
    )
    r = c.get("/signals/trail-governor/history.csv?symbol=ETHUSDT")
    assert "ETHUSDT" in r.text
    assert "BTCUSDT" not in r.text


def test_each_selector_counts_with_every_filter_except_its_own(client):
    """#90/#91 — a selector applied to its own counts makes every option read
    "n = whatever I picked"."""
    rows = [
        _exit(signal_id="S1", symbol="BTCUSDT", exit_kind="trail_stop"),
        _exit(signal_id="S2", symbol="BTCUSDT", exit_kind="flip_close"),
        _exit(signal_id="S3", symbol="ETHUSDT", exit_kind="trail_stop"),
    ]
    from app.routes import trail_governor as t

    parsed, err = t.reduce_history(_history(*rows))
    assert err is None
    # The FILL selector's counts are computed with the symbol filter applied
    # and its own not — so with symbol=BTCUSDT both fills still show.
    scoped = t.filter_history(parsed, symbol="BTCUSDT")
    assert {r["exit_kind"] for r in scoped} == {"trail_stop", "flip_close"}
    # ...and the SYMBOL selector's with the fill filter applied.
    scoped2 = t.filter_history(parsed, fill="trail_stop")
    assert {r["symbol"] for r in scoped2} == {"BTCUSDT", "ETHUSDT"}


def test_an_unpriced_fill_renders_a_dash_not_a_zero(client):
    """An accepted MARKET order reports no average until it fills. A zero
    averages in as a flat trade, which is a claim; None says it does not know."""
    c, api = client
    api.history = _history(_exit(symbol="UNPRICEDUSDT", exit=0.0, pnl_pct=None))
    html = _get(client)
    row = _row_containing(html, "UNPRICEDUSDT")
    assert "—" in row
    assert "0.00" not in row
    assert "carry no fill price" in _flat(html)


def test_the_summary_never_blends_the_two_fills():
    """There is deliberately no pooled average over both fills — their
    difference IS the cost of confirmation."""
    from app.routes import trail_governor as t

    rows, _ = t.reduce_history(_history(
        _exit(signal_id="A", exit_kind="trail_stop", pnl_pct=-2.0),
        _exit(signal_id="B", exit_kind="flip_close", pnl_pct=4.0),
    ))
    out = t.summarize_history(rows)
    assert set(out["by_fill"]) == {"trail_stop", "flip_close"}
    assert "avg_pnl_pct" not in out
    assert "total_pnl_pct" not in out


def test_no_recorded_exit_is_not_read_as_an_inert_mechanism(client):
    """An empty ring is expected while every governed trade is still open —
    "blank needs a cause before it gets a caption", on the money leg."""
    c, api = client
    assert "no governed exit recorded yet" in _flat(_get(client))


def test_the_page_no_longer_claims_binance_retires_the_orphan(client):
    """GUARD. The page said Binance cancels the superseded stop once the
    position closes. It does not for a conditional stop — PROMUSDT's outlived
    its position by 28 minutes — and that sentence told the owner an order
    needing manual cleanup would clean itself up."""
    flat = _flat(_get(client))
    assert "the exchange cancels the other" not in flat
    assert "does not clear itself" in flat


def test_the_counter_and_the_record_are_reconciled_on_screen(client):
    """GUARD, from what the deployed page showed on 2026-08-11.

    Minutes after the record shipped, the counters read `stops_filled: 2` over
    a one-row history. Both were right — the engine's ledger had deduplicated
    one exit seen by two observers — and nothing on screen said which was the
    book. Two numbers for one thing, which is the defect this session has now
    fixed three times.
    """
    c, api = client
    health = dict(_payload()["health"])
    health.update(exits=0, stops_filled=2, duplicate_fills=1)
    api.payload = _payload(health=health)
    api.history = _history(_exit())
    flat = _flat(_get(client))
    assert "reconcile" in flat
    assert "maintenance heartbeat" in flat
    assert "deduplicated" in flat


def test_no_reconcile_note_when_they_agree(client):
    """It is a reconciliation note, not decoration."""
    c, api = client
    health = dict(_payload()["health"])
    health.update(exits=0, stops_filled=1, duplicate_fills=0)
    api.payload = _payload(health=health)
    api.history = _history(_exit())
    assert "reconcile" not in _flat(_get(client))


def test_a_record_larger_than_the_counters_reads_as_a_restart(client):
    """The counters reset on restart and the ledger survives it — which is the
    whole reason the ledger exists, so it must not read as a fault."""
    c, api = client
    health = dict(_payload()["health"])
    health.update(exits=0, stops_filled=0, duplicate_fills=0)
    api.payload = _payload(health=health)
    api.history = _history(_exit(signal_id="A"), _exit(signal_id="B"))
    flat = _flat(_get(client))
    assert "reset by a restart" in flat


def test_duplicate_fills_is_rendered_in_the_counters(client):
    c, api = client
    health = dict(_payload()["health"])
    health["duplicate_fills"] = 3
    api.payload = _payload(health=health)
    assert "duplicate_fills" in _get(client)
