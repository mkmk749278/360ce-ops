"""`/path-scorecard` — and the ordering the page must never undo.

The engine decides the verdict; this page renders it. What is pinned here is
the part a rendering can still get wrong: the order of the table, the states
that must never pool, and the cross-repo key contract driven against the
engine's REAL assembler rather than a fixture this repo chose.

A fixture chooses a shape and then agrees with you about it — `zone_distance_atr`
guessed five key names that no producer in the engine carries and its two tests
passed on a shape nothing has ever produced. The price-action lane card
repeated it one level up with the shape right and the path wrong.
"""
from __future__ import annotations

import os
import pathlib
import sys

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.routes import path_scorecard as page


@pytest.fixture()
def client():
    with TestClient(app) as c:
        c.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]},
               follow_redirects=False)
        yield c


def _engine_summarise():
    """The engine's real reducer, never a shape this repo invented."""
    engine = pathlib.Path(__file__).resolve().parents[2] / "360-v2"
    if not engine.exists():
        pytest.skip("engine repo not checked out beside ops")
    sys.path.insert(0, str(engine))
    try:
        from src import path_scorecard as eng  # type: ignore

        return eng.summarise
    finally:
        sys.path.remove(str(engine))


def _rows(now):
    out = []
    # A thin cell that loses hard on every trade: its interval excludes zero.
    for sym in ("AUSDT", "BUSDT", "CUSDT"):
        out.append({"setup_class": "THIN_PATH", "direction": "LONG",
                    "symbol": sym, "pnl_pct": -3.83,
                    "terminal_outcome_timestamp": now - 3600})
    # A cell with enough evidence to be decided.
    for i in range(30):
        out.append({"setup_class": "FAT_PATH", "direction": "LONG",
                    "symbol": f"S{i % 15}USDT", "pnl_pct": -2.0,
                    "terminal_outcome_timestamp": now - 3600})
    return out


# ── The cross-repo contract ─────────────────────────────────────────────────

def test_every_key_this_page_reads_is_one_the_engine_writes():
    summarise = _engine_summarise()
    out = summarise(_rows(1_700_000_000.0), now=1_700_000_000.0)
    for key in ("cells", "cells_drawn", "cells_decidable", "window_days",
                "floor", "fee_pct", "bootstrap", "coverage",
                "retirement_candidates", "already_retired",
                "retirement_acting", "retirement_error"):
        assert key in out, f"engine no longer publishes {key!r}"
    for key in ("setup_class", "side", "n", "symbols", "net_avg_pct",
                "gross_avg_pct", "win_rate", "ci_low", "ci_high", "verdict",
                "verdict_why", "delivered_evidence_frozen"):
        assert key in out["cells"][0], f"engine no longer publishes cell.{key!r}"


def test_every_verdict_the_engine_can_emit_has_copy_on_this_page():
    """The table looks each verdict up; a verdict ops has never heard of
    renders under its raw name badged `unclassified` rather than dropped. This
    pins that the four the engine defines today are all described."""
    engine = pathlib.Path(__file__).resolve().parents[2] / "360-v2"
    if not engine.exists():
        pytest.skip("engine repo not checked out beside ops")
    sys.path.insert(0, str(engine))
    try:
        from src import path_scorecard as eng  # type: ignore

        emitted = {eng.VERDICT_EARNS, eng.VERDICT_LOSES,
                   eng.VERDICT_UNDECIDED, eng.VERDICT_INSUFFICIENT}
    finally:
        sys.path.remove(str(engine))
    assert emitted <= set(page.VERDICT_COPY)


def test_an_unknown_verdict_is_badged_not_dropped():
    rows = page.annotate([{"verdict": "SOMETHING_NEW", "n": 1}])
    assert rows[0]["unclassified"] is True
    assert len(rows) == 1, "never dropped — the next verdict must be visible"


# ── The states that must not pool ───────────────────────────────────────────

def test_a_timed_out_call_is_UNREACHABLE_not_empty():
    """`str(ReadTimeout())` is the empty string and the engine's own envelope
    carries `error` on SUCCESS, so `ok` is the only thing that can tell the two
    producers apart. Graded on truthiness, an outage renders as the benign
    'running, nothing recorded' caption."""
    assert page.classify({"endpoint": "x", "error": ""}) == page.STATE_UNREACHABLE


def test_an_engine_that_answered_and_refused_is_NOT_unreachable():
    assert page.classify({"ok": False, "error": "unknown key"}) == page.STATE_NOT_REPORTED


def test_reporting_with_nothing_in_the_window_is_EMPTY_not_a_fault():
    assert page.classify({"ok": True, "result": {"cells": []}}) == page.STATE_EMPTY


def test_an_engine_predating_the_entry_is_NOT_REPORTED():
    assert page.classify({"ok": True, "result": {}}) == page.STATE_NOT_REPORTED


# ── The page ────────────────────────────────────────────────────────────────

def _render(client, monkeypatch, payload):
    async def _run(key, args=None):
        assert key == "read.path_scorecard"
        return {"ok": True, "result": payload}

    monkeypatch.setattr(app.state.engine_api, "diag_run", _run)
    return client.get("/path-scorecard")


def test_the_thin_cell_is_not_a_candidate_and_the_page_says_so(client, monkeypatch):
    """The whole point. Three trades with an interval excluding zero must not
    reach the candidate table — and the page must say that zero candidates is
    a finding rather than a blank."""
    summarise = _engine_summarise()
    now = 1_700_000_000.0
    payload = summarise([r for r in _rows(now) if r["setup_class"] == "THIN_PATH"],
                        now=now)
    resp = _render(client, monkeypatch, payload)
    assert resp.status_code == 200
    body = resp.text
    assert "INSUFFICIENT" in body
    assert "No cell has both cleared the sample floor" in body


def test_the_cells_render_in_the_ENGINE_S_order(client, monkeypatch):
    """Sorting by the interval puts the thinnest and most extreme cell on the
    top line, which is the one arrangement this page exists to avoid. The
    engine orders by mean, worst first, and ops must not re-sort."""
    summarise = _engine_summarise()
    now = 1_700_000_000.0
    payload = summarise(_rows(now), now=now)
    engine_order = [c["setup_class"] for c in payload["cells"]]
    resp = _render(client, monkeypatch, payload)
    # Scoped to the full table, not the whole page: the candidates panel above
    # it deliberately lists a subset first, and searching the body as a whole
    # would compare two different tables' orders and call the difference a
    # re-sort.
    table = resp.text.split("Every cell, worst mean first", 1)[1]
    positions = [table.index(name) for name in engine_order]
    assert positions == sorted(positions), (
        f"page re-ordered the cells; engine order was {engine_order}"
    )


def test_the_number_of_cells_drawn_is_on_screen(client, monkeypatch):
    """"Best of N" is not a fact about the winner until N is on screen."""
    summarise = _engine_summarise()
    now = 1_700_000_000.0
    payload = summarise(_rows(now), now=now)
    body = _render(client, monkeypatch, payload).text
    assert "Cells drawn" in body
    assert str(payload["cells_drawn"]) in body


def test_an_unreadable_retirement_list_is_stated_on_the_page(client, monkeypatch):
    """An empty list renders every frozen cell as a live one — a stale verdict
    shown as a current reading about a path somebody may act on."""
    summarise = _engine_summarise()
    now = 1_700_000_000.0
    payload = summarise(_rows(now), now=now)
    payload["retirement_error"] = "RuntimeError: no tunables"
    body = _render(client, monkeypatch, payload).text
    assert "retirement list could not be read" in body


def test_a_frozen_cell_is_badged(client, monkeypatch):
    summarise = _engine_summarise()
    now = 1_700_000_000.0
    payload = summarise(_rows(now), now=now)
    payload["cells"][0]["delivered_evidence_frozen"] = True
    body = _render(client, monkeypatch, payload).text
    assert "frozen" in body
