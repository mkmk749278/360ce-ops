"""The engine's CPU against its QUOTA — the owner's first unanswered question.

2026-08-19: *"engine cpu 221% used is our vps not enough or what"*. Nothing in
this system could answer it. The raw percentage is meaningless on its own —
**221% of a 250% quota is a process at its ceiling** whose scan loop cannot meet
its healthcheck deadline however well it is written, while 221% of an uncapped
4-core box is a busy machine with a core to spare. Same number, opposite next
move, and only the quota separates them.

Three properties these tests hold:

* CPU is graded against the quota, never against the host's core count;
* a reading that could not be taken renders `unknown`, never 0% — a zero over a
  pinned engine is worse than a blank, because a blank prompts a question;
* the effective-config table is the RUNNING process's values, so a deploy that
  did not take is distinguishable from a fix that did not work.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.data_sources.system_health import reduce_host_resources  # noqa: E402


def _payload(**cpu) -> dict:
    base = {"quota_cores": 2.5, "host_cores": 4, "quota_is_host": False,
            "cores_used": 1.0, "pct_of_quota": 40.0, "window_sec": 15.0,
            "source": "cgroup_v2", "reason": ""}
    base.update(cpu)
    return {"host_resources": {
        "cpu": base,
        "memory": {"used_mb": 900.0, "limit_mb": None, "used_pct": None,
                   "source": "cgroup_v2", "reason": "no memory limit set"},
        "load": {"one": 3.0, "five": 2.0, "fifteen": 1.5, "reason": ""},
        "disk": {"path": "data", "used_pct": 30.0, "free_gb": 100.0, "total_gb": 144.0},
        "effective_config": {"cpu_budget": 2.5, "scan_executor_workers": 3},
        "sampled_at": 1.0,
    }}


def test_an_engine_without_the_block_is_not_reported_not_healthy():
    out = reduce_host_resources({})
    assert out["state"] == "not_reported"
    assert out["reported"] is False
    assert "NOT a claim" in out["note"]


def test_the_owners_actual_number_reads_at_the_ceiling():
    """221% of a 250% quota — the reading that started this."""
    out = reduce_host_resources(_payload(cores_used=2.21, quota_cores=2.5,
                                         pct_of_quota=88.4))
    # 88.4 is under the 90 bound, so this is TIGHT and not yet at the ceiling —
    # asserted deliberately, because the point of a bound is that it is one.
    assert out["state"] == "tight"

    out = reduce_host_resources(_payload(cores_used=2.4, quota_cores=2.5,
                                         pct_of_quota=96.0))
    assert out["state"] == "at_ceiling"
    assert "more CPU or less work" in out["note"], (
        "at the ceiling the next lever is not another optimisation, and the "
        "page has to say so or the reader keeps optimising"
    )


def test_the_same_core_count_reads_differently_against_a_bigger_quota():
    """The whole point: the cores are identical, the verdict is not."""
    pinned = reduce_host_resources(_payload(cores_used=2.21, quota_cores=2.5,
                                            pct_of_quota=88.4))
    roomy = reduce_host_resources(_payload(cores_used=2.21, quota_cores=8.0,
                                           pct_of_quota=27.6))
    assert pinned["state"] == "tight"
    assert roomy["state"] == "ok"


def test_an_uncapped_container_says_so_because_the_next_move_differs():
    """A quota equal to the host means nothing is capping this container.

    So a pinned engine is competing with every other container on the box,
    which is a different fix from raising a cap we chose.
    """
    out = reduce_host_resources(_payload(cores_used=3.9, quota_cores=4.0,
                                         host_cores=4, quota_is_host=True,
                                         pct_of_quota=97.5))
    assert out["state"] == "at_ceiling"
    assert "nothing is capping" in out["note"]


def test_an_unmeasurable_cpu_is_unknown_never_idle():
    out = reduce_host_resources(_payload(
        cores_used=None, pct_of_quota=None,
        reason="first sample since boot — a rate needs two readings"))
    assert out["state"] == "unknown"
    assert "first sample" in out["note"], "the engine's own reason, not ours"


def test_a_raising_sample_is_unknown_and_names_the_error():
    out = reduce_host_resources({"host_resources": {"error": "OSError: nope"}})
    assert out["state"] == "unknown"
    assert "OSError" in out["error"]
    assert out["reported"] is False, "an error is not a reading"


# ---------------------------------------------------------------------------
# It must be on the page.
# ---------------------------------------------------------------------------

@pytest.fixture
def client_with_resources(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import system as system_routes

    async def _fake(_request):
        return _payload(cores_used=2.4, quota_cores=2.5, pct_of_quota=96.0)

    monkeypatch.setattr(system_routes, "_host_resources_probe", _fake)
    with TestClient(app) as c:
        c.post("/login", data={"password": "test-token"}, follow_redirects=False)
        yield c


def test_the_card_reaches_the_page(client_with_resources):
    body = client_with_resources.get("/system").text
    assert "AT THE CEILING" in body
    assert "scan_executor_workers" in body, "the running config, on screen"


def test_the_page_says_the_reading_is_taken_in_the_engine_container(
    client_with_resources,
):
    """Ops runs on a different cgroup. A reader has to know which one this is.

    The trail-governor X-ray read INDEX COLD in production for exactly this
    confusion, and it was invisible because the number looked plausible.
    """
    body = client_with_resources.get("/system").text
    assert "inside the engine container" in body


def test_a_missing_block_does_not_render_a_zero(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.routes import system as system_routes

    async def _empty(_request):
        return {}

    monkeypatch.setattr(system_routes, "_host_resources_probe", _empty)
    with TestClient(app) as c:
        c.post("/login", data={"password": "test-token"}, follow_redirects=False)
        body = c.get("/system").text
    assert "NOT REPORTED" in body
    assert "the numbers are not being sent" in body


def test_the_page_reconciles_its_two_cpu_readings(client_with_resources):
    """Two numbers for one quantity on one page must not be left to disagree.

    Measured live 2026-08-19: the card read 1.16-1.35 cores while the container
    table beside it read 176-233% for the same engine, four samples in a row.
    Neither is wrong — `docker stats --no-stream` is one ~1s instantaneous
    sample and a scan cycle is bursty, while the card averages the cgroup
    counter over its whole window — but a reader has no way to know that, and
    "a summary that disagrees with the table under it" is the defect this repo
    has paid for under three different names.

    The memory figures come from the same cgroup and agree to a rounding
    (858.6 MB / 28.0% against 854.5MiB / 27.82%), which is what establishes
    both are reading this container and the CPU gap is method, not target.
    """
    body = client_with_resources.get("/system").text
    assert "two different" in body and "will not match" in body
    assert "--no-stream" in body, "name the instrument, not just the discrepancy"

    # The table's own header is asserted against the TEMPLATE, not the body:
    # this environment has no docker, so `containers.blind` is true and the
    # table does not render at all. Asserting it in the body would have passed
    # only where docker exists and silently covered nothing here — which is the
    # shape of test this repo keeps paying for.
    from pathlib import Path

    tpl = Path("app/templates/system_containers.html").read_text()
    assert "CPU <span" in tpl and "(instant)" in tpl, (
        "the table column has to say which measurement it is"
    )
