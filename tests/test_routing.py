"""`/control/routing` — live → dark and dark → live on one table (2026-09-24).

Owner: *"there is no clear diversion screen, Dark to live and live to dark,
make it clear and I do it later."* What was there: the retired list rendered
read-only on the Promotions page with a link to ``/control/tunables`` — a
POST-only route, so the link 405'd — and the only way to change it was a
free-text field on a different page.

What these tests pin, and why each is shaped the way it is:

* the list edits (split a whole-path entry, never duplicate, empty is a value)
  because a wrong string written to ``retired_paths`` changes what paid
  subscribers receive;
* the read-modify-write reads the ENGINE at the moment of the write, not the
  page the operator loaded;
* the confirm sits on the direction that puts a path in front of subscribers;
* the conflict a promotion rule can create with a retirement is shown.
"""
from __future__ import annotations

import os
from tests.engine_repo import ENGINE_REPO, ABSENT as ENGINE_ABSENT

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

import pathlib  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources import routing as rt  # noqa: E402

# --------------------------------------------------------------------------- #
# The list edits
# --------------------------------------------------------------------------- #


def test_restoring_one_side_of_a_whole_path_keeps_the_other_side_retired():
    """``VSB:*`` + restore LONG must leave ``VSB:SHORT`` — one click must not
    quietly restore two things."""
    out = rt.with_restored([("VOLUME_SURGE_BREAKOUT", "*"), ("MVRTP", "SHORT")],
                           "VOLUME_SURGE_BREAKOUT", "LONG")
    assert out == [("VOLUME_SURGE_BREAKOUT", "SHORT"), ("MVRTP", "SHORT")]


def test_restoring_the_exact_pair_removes_only_it():
    out = rt.with_restored([("A", "SHORT"), ("A", "LONG"), ("B", "SHORT")], "A", "SHORT")
    assert out == [("A", "LONG"), ("B", "SHORT")]


def test_diverting_is_idempotent_and_never_duplicates():
    pairs = [("A", "*")]
    assert rt.with_diverted(pairs, "A", "SHORT") == pairs
    assert rt.with_diverted([("A", "LONG")], "a", "short") == [("A", "LONG"), ("A", "SHORT")]


def test_the_spec_is_what_the_engine_parses():
    assert rt.spec_for([("MVRTP", "SHORT"), ("VSB", "*")]) == "MVRTP:SHORT, VSB:*"
    # Empty is a real value engine-side ("retire nothing"), not a skip.
    assert rt.spec_for([]) == ""


def test_the_spec_round_trips_through_the_real_engine_parser():
    """A cross-repo string is a contract: drive the engine's own parser."""
    import sys

    engine = ENGINE_REPO
    if not (engine / "src" / "path_retirement.py").exists():
        pytest.skip(ENGINE_ABSENT)
    sys.path.insert(0, str(engine))
    try:
        from src import path_retirement  # type: ignore
    except Exception as exc:  # engine deps absent in this venv
        pytest.skip(f"engine import unavailable here: {exc}")
    pairs = [("MOVER_TREND_PULLBACK", "SHORT"), ("VOLUME_SURGE_BREAKOUT", "*"),
             ("MOVER_AVWAP_SCALP", "SHORT")]
    assert path_retirement._parse(rt.spec_for(pairs)) == pairs


def test_the_default_diff_names_both_directions():
    retirement = {
        "enabled": True,
        "retired": [{"setup_class": "MVRTP", "side": "SHORT"},
                    {"setup_class": "X", "side": "LONG"}],
        "default": [{"setup_class": "MVRTP", "side": "SHORT"},
                    {"setup_class": "MVAVW", "side": "SHORT"}],
    }
    diff = rt.default_diff(retirement)
    assert diff["to_divert"] == [("MVAVW", "SHORT")]
    assert diff["to_restore"] == [("X", "LONG")]
    assert diff["same"] is False


def test_an_unreadable_retirement_is_not_an_empty_one():
    assert rt.retirement_readable(None) is False
    assert rt.retirement_readable({"error": "boom"}) is False
    assert rt.default_diff({"error": "boom"})["readable"] is False


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #

_RETIREMENT = {
    "enabled": True,
    "retired": [{"setup_class": "MOVER_TREND_PULLBACK", "side": "SHORT"},
                {"setup_class": "VOLUME_SURGE_BREAKOUT", "side": "*"}],
    "count": 2,
    "default": [{"setup_class": "MOVER_TREND_PULLBACK", "side": "SHORT"},
                {"setup_class": "VOLUME_SURGE_BREAKOUT", "side": "*"},
                {"setup_class": "MOVER_AVWAP_SCALP", "side": "SHORT"}],
    "is_default": False,
}


def _snapshot(rules=None, master=True, lane=True, retirement=None):
    return {
        "master_enabled": master, "dark_lane_enabled": lane, "any_token": "*",
        "rules": rules or [],
        "path_retirement": _RETIREMENT if retirement is None else retirement,
    }


def _rows_by_key(rows):
    return {(r["setup_class"], r["side"]): r for r in rows}


def test_every_side_the_config_names_gets_a_row_and_its_state():
    snap = _snapshot()
    rows = _rows_by_key(rt.build_rows(snap["path_retirement"], snap, [], []))
    assert rows[("MOVER_TREND_PULLBACK", "SHORT")]["live"] == rt.DIVERTED
    assert rows[("VOLUME_SURGE_BREAKOUT", "LONG")]["live"] == rt.DIVERTED
    assert rows[("VOLUME_SURGE_BREAKOUT", "SHORT")]["live"] == rt.DIVERTED
    mvavw = rows[("MOVER_AVWAP_SCALP", "SHORT")]
    assert mvavw["live"] == rt.LIVE
    assert mvavw["in_default"] is True
    assert mvavw["can_divert"] is True and mvavw["can_restore"] is False


def test_a_listed_path_with_the_switch_off_reads_as_still_live():
    ret = dict(_RETIREMENT, enabled=False)
    snap = _snapshot(retirement=ret)
    row = _rows_by_key(rt.build_rows(ret, snap, [], []))[("MOVER_TREND_PULLBACK", "SHORT")]
    assert row["live"] == rt.DIVERTED_INERT
    assert "still reaches subscribers" in row["live_copy"]


def test_an_unreadable_list_is_unknown_and_offers_no_action():
    snap = _snapshot(retirement={"error": "firestore refused"})
    cells = [{"setup_class": "MEAN_REVERT", "side": "LONG", "n": 10}]
    row = rt.build_rows({"error": "x"}, snap, cells, [])[0]
    assert row["live"] == rt.UNKNOWN
    assert row["can_divert"] is False and row["can_restore"] is False


def test_a_promotion_rule_with_any_gate_on_a_retired_side_is_a_conflict():
    """Retirement marks its rows dark with gate `retired:P:S`; a rule whose
    gates are Any matches it and puts them back on the live feed."""
    rule = {"setup_class": "MOVER_TREND_PULLBACK", "enabled": True, "gates": ["*"],
            "direction": "short", "inert": False}
    snap = _snapshot(rules=[rule])
    row = _rows_by_key(rt.build_rows(snap["path_retirement"], snap, [], []))[
        ("MOVER_TREND_PULLBACK", "SHORT")]
    assert row["conflict"] and "undone" in row["conflict"]


def test_a_rule_naming_only_real_gates_does_not_conflict():
    rule = {"setup_class": "MOVER_TREND_PULLBACK", "enabled": True,
            "gates": ["EXECUTION:OVEREXTENDED"], "direction": "any", "inert": False}
    snap = _snapshot(rules=[rule])
    row = _rows_by_key(rt.build_rows(snap["path_retirement"], snap, [], []))[
        ("MOVER_TREND_PULLBACK", "SHORT")]
    assert row["conflict"] is None


def test_a_rule_that_cannot_run_is_not_a_conflict():
    rule = {"setup_class": "MOVER_TREND_PULLBACK", "enabled": True, "gates": ["*"],
            "direction": "any", "inert": False}
    snap = _snapshot(rules=[rule], master=False)
    row = _rows_by_key(rt.build_rows(snap["path_retirement"], snap, [], []))[
        ("MOVER_TREND_PULLBACK", "SHORT")]
    assert row["conflict"] is None
    assert row["promotion"]["state"] == "master_off"


def test_direction_coverage_is_tri_state():
    base = {"setup_class": "P", "enabled": True, "gates": ["G"], "inert": False}
    snap = _snapshot(rules=[dict(base, direction="long")])
    assert rt.promotion_for_side(snap, "P", "LONG")["covers"] is True
    assert rt.promotion_for_side(snap, "P", "SHORT")["covers"] is False
    snap = _snapshot(rules=[dict(base, direction="with_trend")])
    assert rt.promotion_for_side(snap, "P", "SHORT")["covers"] is None


def test_dark_evidence_is_split_by_side():
    rows = [
        {"setup_class": "MEAN_REVERT", "side": "LONG", "status": "CLOSED_TP1", "pnl_pct": 1.0,
         "symbol": "A", "emitted_at": 1.0},
        {"setup_class": "MEAN_REVERT", "side": "SHORT", "status": "CLOSED_SL", "pnl_pct": -2.0,
         "symbol": "B", "emitted_at": 1.0},
    ]
    snap = _snapshot()
    by = _rows_by_key(rt.build_rows(snap["path_retirement"], snap, [], rows))
    assert by[("MEAN_REVERT", "LONG")]["dark"]["avg_pct"] == 1.0
    assert by[("MEAN_REVERT", "SHORT")]["dark"]["avg_pct"] == -2.0


# --------------------------------------------------------------------------- #
# Real requests
# --------------------------------------------------------------------------- #


class _Engine:
    """Stateful fake: the tunables write changes what the snapshot reports."""

    def __init__(self, retired, enabled=True):
        self.retired = list(retired)
        self.enabled = enabled
        self.writes: list[dict] = []
        self.snapshot_reads = 0

    def snapshot(self):
        self.snapshot_reads += 1
        return {
            "master_enabled": True, "dark_lane_enabled": True, "any_token": "*",
            "rules": [],
            "path_retirement": {
                "enabled": self.enabled,
                "retired": [{"setup_class": p, "side": s} for p, s in self.retired],
                "count": len(self.retired),
                "default": _RETIREMENT["default"],
                "is_default": False,
            },
        }


def _parse(spec: str):
    out = []
    for chunk in str(spec or "").split(","):
        tok = chunk.strip()
        if not tok:
            continue
        setup, _, side = tok.partition(":")
        out.append((setup.strip().upper(), (side.strip().upper() or "*")))
    return out


@pytest.fixture
def engine():
    return _Engine([("MOVER_TREND_PULLBACK", "SHORT"), ("VOLUME_SURGE_BREAKOUT", "*")])


@pytest.fixture
def client(engine):
    from app.main import app

    with TestClient(app) as c:
        c.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})

        async def _promos():
            return engine.snapshot()

        async def _set(values):
            engine.writes.append(dict(values))
            if "retired_paths" in values:
                engine.retired = _parse(values["retired_paths"])
            return {"ok": True, "initialised": True}

        async def _diag(key, args=None):
            return {"ok": True, "error": "", "result": {
                "cells": [{"setup_class": "MOVER_AVWAP_SCALP", "side": "SHORT", "n": 78,
                           "net_avg_pct": -0.468, "verdict": "INSUFFICIENT"}],
                "window_days": 30.0, "floor": {"min_trades": 25, "min_symbols": 12}}}

        app.state.engine_api.dark_promotions = _promos
        app.state.engine_api.set_tunables = _set
        app.state.engine_api.diag_run = _diag
        app.state.data_volume.dark_signals = lambda: {"schema": 2, "rows": []}
        yield c


def test_the_page_renders_every_side_and_both_directions(client):
    r = client.get("/control/routing")
    assert r.status_code == 200
    body = r.text
    assert "MOVER_AVWAP_SCALP" in body and "DIVERTED TO DARK" in body
    assert "Divert to dark" in body and "Restore to live" in body
    assert "signed-off default diverts this" in body
    assert "Adopt the signed-off default" in body


def test_divert_writes_the_whole_list_and_reports_what_the_engine_says(client, engine):
    r = client.post("/control/routing/divert",
                    data={"setup_class": "MOVER_AVWAP_SCALP", "side": "SHORT"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert engine.writes == [{
        "retired_paths": "MOVER_TREND_PULLBACK:SHORT, VOLUME_SURGE_BREAKOUT:*, "
                         "MOVER_AVWAP_SCALP:SHORT"}]
    page = client.get("/control/routing").text
    assert "MOVER_AVWAP_SCALP SHORT: diverted to dark" in page


def test_the_write_is_computed_from_the_engine_at_write_time(client, engine):
    """The page may be minutes old. Something retired elsewhere in between
    must survive a divert from a stale page."""
    client.get("/control/routing")
    engine.retired.append(("MEAN_REVERT", "LONG"))  # changed after page load
    client.post("/control/routing/divert",
                data={"setup_class": "MOVER_AVWAP_SCALP", "side": "SHORT"})
    assert "MEAN_REVERT:LONG" in engine.writes[-1]["retired_paths"]


def test_restore_needs_the_confirm_and_writes_nothing_without_it(client, engine):
    r = client.post("/control/routing/restore",
                    data={"setup_class": "VOLUME_SURGE_BREAKOUT", "side": "LONG"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert engine.writes == []
    assert "needs the confirm" in client.get("/control/routing").text


def test_restore_one_side_of_a_whole_path(client, engine):
    r = client.post("/control/routing/restore",
                    data={"setup_class": "VOLUME_SURGE_BREAKOUT", "side": "LONG",
                          "confirm": "yes"})
    assert engine.writes[-1]["retired_paths"] == (
        "MOVER_TREND_PULLBACK:SHORT, VOLUME_SURGE_BREAKOUT:SHORT")
    # The followed redirect is the page the operator lands on; it carries the
    # flash (and consumes it).
    assert "VOLUME_SURGE_BREAKOUT LONG: restored" in r.text


def test_a_write_the_engine_does_not_reflect_is_not_reported_as_done(client, engine):
    """The engine is the source of truth: read back, never echo."""
    async def _set_ignored(values):
        engine.writes.append(dict(values))
        return {"ok": True}
    client.app.state.engine_api.set_tunables = _set_ignored
    r = client.post("/control/routing/divert",
                    data={"setup_class": "MOVER_AVWAP_SCALP", "side": "SHORT"})
    assert "does not report it as diverted" in r.text


def test_an_unreadable_list_is_never_overwritten(client, engine):
    async def _blind():
        return {"master_enabled": True, "rules": [],
                "path_retirement": {"error": "firestore refused"}}
    client.app.state.engine_api.dark_promotions = _blind
    client.post("/control/routing/divert",
                data={"setup_class": "MOVER_AVWAP_SCALP", "side": "SHORT"})
    assert engine.writes == []


def test_adopting_the_default_that_only_diverts_needs_no_confirm(client, engine):
    client.post("/control/routing/adopt-default", data={})
    assert rt.spec_for(engine.retired) == (
        "MOVER_TREND_PULLBACK:SHORT, VOLUME_SURGE_BREAKOUT:*, MOVER_AVWAP_SCALP:SHORT")


def test_adopting_a_default_that_restores_something_needs_the_confirm(client, engine):
    engine.retired.append(("MEAN_REVERT", "LONG"))
    client.post("/control/routing/adopt-default", data={})
    assert engine.writes == []
    client.post("/control/routing/adopt-default", data={"confirm": "yes"})
    assert ("MEAN_REVERT", "LONG") not in engine.retired


def test_the_promotions_page_no_longer_links_to_a_post_only_route():
    """`/control/tunables` is POST-only — the old link 405'd."""
    html = (pathlib.Path(__file__).resolve().parents[1]
            / "app/templates/control_promotions.html").read_text()
    assert 'href="/control/tunables"' not in html
    assert 'href="/control/routing"' in html
