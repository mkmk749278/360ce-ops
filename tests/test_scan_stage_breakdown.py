"""The per-stage breakdown of a slow scan cycle — three states, never two.

Built 2026-08-19, from a defect that was invisible on every surface. The
scanner has computed a per-stage timing dict for as long as it has had stages,
and it went to a **log line and nowhere else** — so when the owner's own VPS run
asked where a 156s cycle had gone, the grep returned *nothing at all* while the
deadline warnings beside it came through. The question that aims the next fix
had no answer anywhere.

The engine now keeps the breakdown captured AT the worst cycle and at the most
recent slow one, and publishes both in `loop_health.scan_cycle`. This file
asserts ops renders that honestly:

* an engine predating the stamp is **not_reported** — not "no slow cycle";
* an engine stamping it with nothing to show is **none_yet**, the HEALTHY case;
* the shares are a ratio between stages and are never presented as a partition.
"""
from __future__ import annotations

from app.data_sources.system_health import reduce_loop_health


def _payload(scan: dict) -> dict:
    return {"loop_health": {"scan_cycle": scan}}


_BASE = {"cycles": 40, "last_sec": 12.0, "worst_sec": 156.0, "over_warn": 3,
         "over_kill": 1, "warn_sec": 60.0, "kill_sec": 120.0}


def test_an_engine_without_the_stamp_reads_not_reported():
    """Absent is not empty. The distinction is the whole card.

    An engine that never stamped the breakdown and one that has had no slow
    cycle produce the same blank on screen unless something separates them —
    and they have opposite next moves (deploy, versus nothing at all).
    """
    out = reduce_loop_health(_payload(dict(_BASE)))
    assert out["stages"]["state"] == "not_reported"
    assert out["stages"]["worst"] == []


def test_the_stamp_present_and_empty_is_the_healthy_case():
    scan = dict(_BASE, worst_stages={}, last_slow_stages={})
    out = reduce_loop_health(_payload(scan))
    assert out["stages"]["state"] == "none_yet"


def test_stages_render_worst_first_with_their_share():
    scan = dict(
        _BASE,
        worst_stages={"cheap": 1.0, "smc": 60.0, "indicators": 20.0},
        last_slow_stages={"smc": 30.0},
        last_slow_sec=88.0,
    )
    out = reduce_loop_health(_payload(scan))["stages"]
    assert out["state"] == "reported"
    assert [r["stage"] for r in out["worst"]] == ["smc", "indicators", "cheap"]
    assert out["worst"][0]["sec"] == 60.0
    # 60 / 81 — a share of the STAGE TOTAL, never of the cycle's wall-time.
    assert out["worst"][0]["share"] == 74.1
    assert out["last_slow_sec"] == 88.0


def test_the_share_is_of_the_stage_total_not_of_the_cycle():
    """Symbols scan concurrently, so the stage sums exceed the cycle duration.

    Dividing by `worst_sec` would print shares under 100% that look like a
    partition of the cycle and are not one — a number that is always about
    right and never means what it says.
    """
    scan = dict(_BASE, worst_sec=100.0, worst_stages={"a": 300.0, "b": 100.0})
    rows = reduce_loop_health(_payload(scan))["stages"]["worst"]
    assert [r["share"] for r in rows] == [75.0, 25.0]
    assert sum(r["sec"] for r in rows) > scan["worst_sec"], "the premise of this test"


def test_a_non_numeric_stage_is_dropped_not_crashed_on():
    scan = dict(_BASE, worst_stages={"smc": 10.0, "junk": "n/a"})
    rows = reduce_loop_health(_payload(scan))["stages"]["worst"]
    assert [r["stage"] for r in rows] == ["smc"]


def test_stages_survive_a_json_round_trip_in_the_right_order():
    """Ops sorts rather than trusting dict order across the wire."""
    import json

    scan = dict(_BASE, worst_stages=json.loads('{"z": 1.0, "a": 99.0}'))
    rows = reduce_loop_health(_payload(scan))["stages"]["worst"]
    assert [r["stage"] for r in rows] == ["a", "z"]


# ---------------------------------------------------------------------------
# The card must be on the page. A reducer that returns the right dict into a
# template that never reads it is the "field one repo writes and no repo reads"
# seam, and it renders as a blank rather than as an error.
# ---------------------------------------------------------------------------

import os  # noqa: E402

import pytest  # noqa: E402

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")


@pytest.fixture
def client_with_stages(monkeypatch):
    """Drive the REAL route with an engine payload that carries stages.

    Patched at the engine-API seam rather than at the reducer, so the template,
    the route and the reducer are all exercised — a fixture handed straight to
    the reducer would assert my own assumption about where the block lands.
    """
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import system as system_routes

    async def _fake_probe(_request):
        return {
            "loop_health": {
                "scan_cycle": {
                    "cycles": 40, "last_sec": 12.0, "worst_sec": 156.0,
                    "over_warn": 3, "over_kill": 0,
                    "warn_sec": 60.0, "kill_sec": 120.0,
                    "worst_stages": {"smc_detect": 91.4, "indicators": 30.2},
                    "last_slow_stages": {"smc_detect": 44.0},
                    "last_slow_sec": 71.5,
                }
            }
        }

    monkeypatch.setattr(system_routes, "_loop_health_probe", _fake_probe)
    with TestClient(app) as c:
        c.post("/login", data={"password": "test-token"}, follow_redirects=False)
        yield c


def test_the_breakdown_reaches_the_page(client_with_stages):
    body = client_with_stages.get("/system/liveness").text
    assert "Where a slow cycle went" in body
    assert "smc_detect" in body, "the stage the reader came for"
    assert "indicators" in body


def test_the_page_says_the_shares_are_not_a_partition(client_with_stages):
    """Copy is part of the measurement.

    Stage sums accumulate across concurrent symbol workers and legitimately
    exceed the cycle's own wall-time. A share column with no such sentence
    reads as a partition of the cycle, which is a number that is always about
    right and never means what it says.
    """
    body = client_with_stages.get("/system/liveness").text
    assert "not a partition" in body


def test_an_engine_without_the_stamp_does_not_render_an_empty_table(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import system as system_routes

    async def _fake_probe(_request):
        return {"loop_health": {"scan_cycle": {
            "cycles": 40, "last_sec": 12.0, "worst_sec": 40.0,
            "over_warn": 0, "over_kill": 0, "warn_sec": 60.0, "kill_sec": 120.0,
        }}}

    monkeypatch.setattr(system_routes, "_loop_health_probe", _fake_probe)
    with TestClient(app) as c:
        c.post("/login", data={"password": "test-token"}, follow_redirects=False)
        body = c.get("/system/liveness").text
    assert "NOT REPORTED" in body
    assert "the split simply is not being sent" in body
