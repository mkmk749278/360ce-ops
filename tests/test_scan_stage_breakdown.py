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


# ---------------------------------------------------------------------------
# The hang the completed-cycle counters cannot see (2026-08-19).
# ---------------------------------------------------------------------------

def test_a_hanging_cycle_outranks_every_completed_cycle_counter():
    """The live defect, reproduced.

    On 2026-08-19 this card read "0 past the deadline, last cycle 20.76s" while
    autoheal was restarting the engine on a failing streak of 3. Every counter
    on it records a cycle at COMPLETION; `healthcheck.py` kills on heartbeat
    age, and on THIS engine that file is touched once per completed cycle. A
    cycle hung past the deadline is therefore invisible to all of them — the
    page read healthy precisely while the container was being killed.

    The payload carries no `progress_heartbeat_enabled`, which is what makes
    that last sentence true: on an engine without the progress beat, cycle
    wall-time and heartbeat age ARE one number, so a long cycle is a restart and
    the verdict stays pooled. The wording changed when the beat shipped (a
    beating engine is told "slow, not wedged"); the reader must still be told
    the container is going down here, so this asserts the CLAIM rather than the
    old sentence.
    """
    scan = dict(_BASE, over_warn=0, over_kill=0, last_sec=20.76, worst_sec=113.47,
                in_flight_sec=204.3, heartbeat_age_sec=210.9)
    out = reduce_loop_health(_payload(scan))
    assert out["state"] == "hanging", "a hang outranks a clean completed book"
    assert out["progress_heartbeat"] is None, "the pooled verdict is the point here"
    assert "being killed either way" in out["note"], "the reader must be told it is going down"
    assert "only at the END of a cycle" in out["note"], "and why the two are one number"
    assert out["in_flight_sec"] == 204.3


def test_a_healthy_in_flight_cycle_does_not_trip_it():
    """A cycle mid-run is the normal state; only past the deadline is a fault."""
    scan = dict(_BASE, over_warn=0, over_kill=0, in_flight_sec=12.0,
                heartbeat_age_sec=18.0)
    assert reduce_loop_health(_payload(scan))["state"] == "ok"


def test_an_engine_without_the_stamp_reads_not_reported_not_healthy():
    """Absent is not zero, and 0s would read as 'a cycle just completed'."""
    out = reduce_loop_health(_payload(dict(_BASE, over_warn=0, over_kill=0)))
    assert out["hang_reported"] is False
    assert out["in_flight_sec"] is None
    assert out["state"] != "hanging", "we cannot claim a hang we cannot see"


def test_the_hang_is_graded_on_the_engines_numbers_not_ops_clock():
    """Both are ages of an in-progress state, so only the engine can take them.

    Ops computing `now - last_cycle_at` would grade the engine on the
    dashboard's clock — the rule /signals/sar-live paid for twice.
    """
    import inspect

    from app.data_sources import system_health as sh

    src = inspect.getsource(sh.reduce_loop_health)
    assert "in_flight_sec" in src
    for forbidden in ("time.time()", "datetime.now", "utcnow"):
        assert forbidden not in src, (
            f"{forbidden} in the reducer means ops is grading freshness on its "
            "own clock"
        )


def test_the_page_leads_with_the_hang_and_says_why(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import system as system_routes

    async def _fake(_request):
        return {"loop_health": {"scan_cycle": dict(
            _BASE, over_warn=0, over_kill=0, last_sec=20.76,
            in_flight_sec=204.3, heartbeat_age_sec=210.9,
            max_concurrent_scans=20, executor_workers=8)}}

    monkeypatch.setattr(system_routes, "_loop_health_probe", _fake)
    with TestClient(app) as c:
        c.post("/login", data={"password": "test-token"}, follow_redirects=False)
        body = c.get("/system/liveness").text
    assert "LOOP WEDGED NOW" in body, (
        "renamed from CYCLE HANGING NOW when `slow` arrived beside it — "
        "'hanging' was the word that conflated a stopped loop with a slow one"
    )
    assert "CYCLE SLOW, LOOP ALIVE" not in body, "this engine cannot be graded slow"
    assert "cannot see the work" in body, "the reader must be told why"
    assert "204.3" in body and "Concurrent scans" in body


def test_a_hang_reports_the_stage_it_is_stuck_in():
    """The only breakdown a hung cycle can produce.

    `worst_stages` is captured at completion, so the hung cycle — the one that
    matters — contributes nothing to it.
    """
    scan = dict(_BASE, over_warn=0, over_kill=0, in_flight_sec=186.05,
                heartbeat_age_sec=190.04, worst_stages={},
                in_flight_stages={"smc": 3.1, "indicators": 181.4})
    out = reduce_loop_health(_payload(scan))
    assert out["state"] == "hanging"
    assert out["in_flight_stages"][0]["stage"] == "indicators"
    assert out["in_flight_stages"][0]["sec"] == 181.4


def test_the_stage_table_renders_on_a_cycle_that_is_still_running(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import system as system_routes

    async def _hang(_request):
        return {"loop_health": {"scan_cycle": dict(
            _BASE, over_warn=0, over_kill=0, in_flight_sec=186.05,
            heartbeat_age_sec=190.04, in_flight_stages={"indicators": 181.4})}}

    monkeypatch.setattr(system_routes, "_loop_health_probe", _hang)
    with TestClient(app) as c:
        c.post("/login", data={"password": "test-token"}, follow_redirects=False)
        body = c.get("/system/liveness").text
    assert "Where the cycle still running has spent its time" in body, (
        "the table is keyed on `hanging` OR `slow` now: a slow cycle asks the "
        "same question a wedged one does, and it is the population the answer "
        "is most useful for. The heading no longer presumes the answer."
    )
    assert "awaiting something that has not returned" in body
