"""`/system/firestore` — the read allowance, and the bill at 1,000 members.

The page exists because a per-user Firestore read is invisible at one user and
linear in subscribers: `worker_manager`'s roster scan cost 1,440 reads a day on
this account and 1.44 MILLION at the owner's 1,000-member target, and nothing
in the GCP console, the bill or the engine's own census said so.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import firestore_cost  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


PROJECTION = {
    "process_role": "engine",
    "uptime_sec": 7200,
    "uptime_is_short": False,
    "measured_members": 1,
    "target_members": 1000,
    "measured_per_day": 3000,
    "projected_per_day": 1_500_000,
    "free_tier_reads_per_day": 50000,
    "projected_over_free_tier": 1_450_000,
    "projected_usd_per_month_multi_region": 26.1,
    "projected_usd_per_month_regional": 13.05,
    "note": "Projection, not measurement.",
    "sites": [
        {"site": "keystore.list_active_uids", "docs": 120, "calls": 120,
         "docs_per_call": 1.0, "per_day": 1440, "last_ts": 0.0,
         "scales_with_members": True, "projected_per_day": 1_440_000},
        {"site": "kill_switch.global_doc", "docs": 20, "calls": 20,
         "docs_per_call": 1.0, "per_day": 288, "last_ts": 0.0,
         "scales_with_members": False, "projected_per_day": 288},
    ],
}

GENERATION = {
    "redis_configured": True,
    "documents": ["kill_switch_global", "runtime_tunables"],
    "seen": {},
    "bumps": 3, "bump_failures": 0,
    "polls": 900, "poll_failures": 0, "invalidations": 3,
}


def _patch(monkeypatch, *, projection=PROJECTION, generation=GENERATION,
           census=None):
    async def fake_run(self, key, args=None):
        if key == "read.firestore_projection":
            return projection
        if key == "read.control_generation":
            return generation
        return census if census is not None else {"sites": []}

    monkeypatch.setattr(EngineApiClient, "diag_run", fake_run)


def _get(monkeypatch, **kw) -> str:
    _patch(monkeypatch, **kw)
    with TestClient(app) as client:
        _login(client)
        return client.get("/system/firestore").text


def test_the_page_renders_the_projection_and_the_cost(monkeypatch):
    body = _get(monkeypatch)
    assert "1,500,000" in body
    assert "1,450,000 reads/day over the free allowance" in body
    assert "$13.05" in body and "$26.1" in body


def test_both_location_tiers_are_published(monkeypatch):
    """The project's Firestore location is a console fact, not a code fact —
    quoting one price silently would be choosing the half of a number the
    reader cannot check."""
    body = _get(monkeypatch)
    assert "regional" in body and "multi-region" in body


def test_the_per_user_sites_are_marked_apart_from_the_flat_ones(monkeypatch):
    """Which sites scale is the whole content of the projection: it is the
    difference between a cost model and a multiplication."""
    body = _get(monkeypatch)
    assert "per-user" in body and "flat" in body
    assert "keystore.list_active_uids" in body


def test_a_short_uptime_is_badged_rather_than_quoted_confidently(monkeypatch):
    body = _get(monkeypatch, projection={**PROJECTION, "uptime_is_short": True})
    assert "UPTIME UNDER 15 MINUTES" in body


def test_inside_the_allowance_reads_as_healthy_not_as_a_bill(monkeypatch):
    body = _get(monkeypatch, projection={
        **PROJECTION, "projected_per_day": 12000,
        "projected_over_free_tier": 0,
        "projected_usd_per_month_regional": 0.0,
        "projected_usd_per_month_multi_region": 0.0})
    assert "Inside the free allowance" in body
    assert "over the free allowance" not in body


def test_three_states_for_the_projection_never_pooled(monkeypatch):
    """`unreachable`, `not_reported` and `empty` have three different next
    moves — a network check, a deploy, and a shrug. Pooling them into "no data"
    is how a page reports a fault that is not happening."""
    assert firestore_cost.classify({"error": "boom"}) == "unreachable"
    assert firestore_cost.classify("nonsense") == "unreachable"
    assert firestore_cost.classify(
        {"ok": False, "error": "unknown key"}) == "not_reported"
    assert firestore_cost.classify({"sites": []}) == "empty"
    assert firestore_cost.classify({"sites": [{"site": "x"}]}) == "ok"


def test_an_unreachable_engine_says_so_rather_than_showing_zero(monkeypatch):
    body = _get(monkeypatch, projection={"error": "ConnectError"})
    assert "UNREACHABLE" in body
    assert "not a cost one" in body


def test_a_dead_invalidation_channel_is_named(monkeypatch):
    """A dead channel is invisible in the ordinary case — the 300s defensive
    floor still converges, just minutes late. These counters are the only thing
    that can say a kill-switch flip is taking the slow path."""
    body = _get(monkeypatch, generation={
        **GENERATION, "polls": 0, "bumps": 5})
    assert "BUMPS WITH NO POLLS" in body

    body = _get(monkeypatch, generation={
        **GENERATION, "redis_configured": False})
    assert "REDIS NOT CONFIGURED" in body

    body = _get(monkeypatch, generation={**GENERATION, "poll_failures": 12})
    assert "POLL FAILURES" in body


def test_a_healthy_channel_says_healthy(monkeypatch):
    body = _get(monkeypatch)
    assert "Channel healthy." in body


def test_an_engine_without_the_generation_entry_reads_as_not_reported(monkeypatch):
    body = _get(monkeypatch, generation={"error": "unknown key"})
    assert "NOT REPORTED" in body


def test_the_page_requires_auth():
    with TestClient(app) as client:
        r = client.get("/system/firestore", follow_redirects=False)
        assert r.status_code == 302


def test_an_ops_side_timeout_is_unreachable_not_an_empty_census():
    """`str(httpx.ReadTimeout())` is the empty string, so ops' transport
    envelope is `{"error": "", "endpoint": …}` — falsy, and the old truthiness
    check let it through to be graded on SHAPE. Here that lands on EMPTY,
    *"running, nothing recorded"*: the benign caption for a call that never
    came back, on the page the owner reads during a quota outage.

    The two producers are told apart by `ok`, never by `error`, because the
    engine's own envelope carries `error` on success too — empty.
    """
    from app.routes import firestore_cost as page

    assert page.classify({"endpoint": "/internal/diag/catalog/run", "error": ""}) == page.STATE_UNREACHABLE
    assert page.classify({"error": "connect timeout"}) == page.STATE_UNREACHABLE
    assert page.classify({"ok": False, "error": "unknown catalog entry"}) == page.STATE_NOT_REPORTED
    assert page.classify({"ok": True, "error": "", "result": {"sites": [{"n": 1}]}}) == page.STATE_OK
