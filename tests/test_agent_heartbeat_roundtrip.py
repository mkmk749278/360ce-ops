"""The agent's heartbeat has to survive the trip, not just be written.

`sinks` shipped on 2026-08-19 so `/alerts` could stop asserting a delivery path
it could not observe. It was **dropped in transit for hours** and nobody could
tell: `heartbeat.publish` wrote it, `AgentAlertsReader.heartbeat` built a fixed
dict of known keys that did not name it, and the page rendered
`DELIVERY NOT REPORTED` — which is a legitimate state, so it looked correct.

Both sides had tests. The writer's asserted its own payload. The reader's
monkeypatched `heartbeat()` to return a dict the *author* wrote, so it asserted
an assumption back at itself and could never see the key being discarded. The
seam between them had nothing.

CLAUDE.md names this class twice already — a field one writer populates and a
serializer drops, invisible at both ends. So this file tests the **round trip**:
publish through the real function into a fake Redis, read back through the real
reader, and assert what survives.
"""
from __future__ import annotations

import json

import pytest

from app.agent import heartbeat
from app.data_sources.agent_alerts import AgentAlertsReader


class _FakeRedis:
    """Stores what publish() actually serialises — bytes in, bytes out."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def set(self, key, value, ex=None):
        assert isinstance(value, str), "publish must serialise before storing"
        self.store[key] = value

    async def get(self, key):
        return self.store.get(key)


def _reader_over(client: _FakeRedis) -> AgentAlertsReader:
    reader = AgentAlertsReader.__new__(AgentAlertsReader)
    reader._get_client = lambda: client  # type: ignore[method-assign]
    return reader


async def _publish(client, **kw):
    base = dict(
        cycle_ok=True, alerts_firing=0, detector_count=9,
        redis_probe_summary="ok: '27'", redis_probe_ok=True,
        poll_interval_s=60,
    )
    base.update(kw)
    await heartbeat.publish(client, **base)


@pytest.mark.asyncio
async def test_every_published_field_survives_the_round_trip():
    """Derived, not listed.

    Asserting a hand-written list of expected keys is the same mistake one
    layer up: it would pass over the next field somebody adds and drops. This
    reads what publish() actually wrote and requires the reader to carry all of
    it, so a new field is covered without anyone updating this test.
    """
    client = _FakeRedis()
    await _publish(client, sinks={"telegram": True, "push_service_account": False,
                                  "healthchecks": True})

    written = json.loads(client.store[heartbeat.KEY])
    got = await _reader_over(client).heartbeat()

    dropped = sorted(k for k in written if k not in got and k != "at")
    assert not dropped, (
        f"the reader silently discards {dropped} — a field the agent publishes "
        f"and this reader does not name is invisible at both ends"
    )


@pytest.mark.asyncio
async def test_armed_sinks_reach_the_reader():
    """The specific field, and the specific values /alerts branches on."""
    client = _FakeRedis()
    await _publish(client, sinks={"telegram": True, "push_service_account": False,
                                  "healthchecks": True})
    got = await _reader_over(client).heartbeat()
    assert got["sinks"] == {"telegram": True, "push_service_account": False,
                            "healthchecks": True}


@pytest.mark.asyncio
async def test_an_agent_with_nothing_armed_is_not_an_agent_that_said_nothing():
    """The distinction "Nothing pages you" is allowed to rest on.

    All-false is a REPORT that no sink is armed. A missing key is the absence
    of a report. Only the first may produce that sentence.
    """
    client = _FakeRedis()
    await _publish(client, sinks={"telegram": False, "push_service_account": False,
                                  "healthchecks": False})
    got = await _reader_over(client).heartbeat()
    assert got["sinks"] == {"telegram": False, "push_service_account": False,
                            "healthchecks": False}
    assert got["sinks"] is not None

    # An agent build predating the field publishes {} — still not None, and
    # still not a claim that nothing is armed.
    client2 = _FakeRedis()
    await _publish(client2)
    got2 = await _reader_over(client2).heartbeat()
    assert got2["sinks"] == {}


@pytest.mark.asyncio
async def test_no_heartbeat_at_all_reads_as_absent():
    got = await _reader_over(_FakeRedis()).heartbeat()
    assert got["present"] is False
    assert got.get("sinks") is None


def test_the_notifier_reports_the_values_it_actually_gates_on():
    """Drive the real Notifier, so the answer cannot drift from the behaviour.

    A second copy of "is Telegram configured" is what /alerts would have needed
    otherwise, and the web container does not even receive AGENT_TELEGRAM_*.
    """
    from app.agent.notifier import Notifier

    armed = Notifier(bot_token="t", chat_id="c", healthchecks_url="https://hc")
    assert armed.armed_sinks()["telegram"] is True
    assert armed.armed_sinks()["healthchecks"] is True

    # Either half missing sends nothing.
    assert Notifier(bot_token="t", chat_id="").armed_sinks()["telegram"] is False
    assert Notifier(bot_token="", chat_id="c").armed_sinks()["telegram"] is False
    assert Notifier(bot_token="", chat_id="").armed_sinks()["healthchecks"] is False

    # No token ever leaves the method.
    values = armed.armed_sinks().values()
    assert all(isinstance(v, bool) for v in values)


@pytest.mark.asyncio
async def test_the_alerts_page_reads_the_sinks_the_agent_really_published():
    """End to end, through the real reader — the check that was missing.

    The existing /alerts tests monkeypatch `heartbeat()` and choose its shape,
    so they pass whether or not the reader carries the field. This one publishes
    through the agent's own function and asserts on the rendered page.
    """
    from fastapi.testclient import TestClient

    from app.data_sources import agent_alerts as aa
    from app.main import app
    from tests.test_panel_readability import _login

    client = _FakeRedis()
    await _publish(client, sinks={"telegram": True, "push_service_account": False,
                                  "healthchecks": False})
    real_heartbeat = AgentAlertsReader.heartbeat

    async def _hb(self):
        return await real_heartbeat(_reader_over(client))

    original = aa.AgentAlertsReader.heartbeat
    aa.AgentAlertsReader.heartbeat = _hb
    try:
        with TestClient(app) as tc:
            _login(tc)
            body = tc.get("/alerts").text
    finally:
        aa.AgentAlertsReader.heartbeat = original

    assert "Nothing pages you" not in body, (
        "Telegram is armed and the agent said so — this is the live 2026-08-19 state"
    )
    assert "DELIVERY NOT REPORTED" not in body, (
        "the sinks were published and must not be dropped in transit"
    )
    assert "ALERTS DELIVERED" in body


@pytest.mark.asyncio
async def test_push_is_graded_on_the_agents_service_account_not_this_containers():
    """The half of the delivery path that lives in the other container.

    Found on the live box 2026-08-19, after the Telegram fix: `/alerts` read
    "no FIREBASE_SERVICE_ACCOUNT is configured" while the agent reported
    `push_service_account: true`. Compose passes that variable to the
    monitoring-agent service and NOT to this one, so the web container's copy
    is empty on a box where push is perfectly armed. Grading a copy of another
    process's config is the same defect as asserting a delivery path.

    Both halves are needed and each is read where it can be observed: whether a
    send is possible comes from the agent, whether it has anywhere to go comes
    from the device registry this container owns.
    """
    from fastapi.testclient import TestClient

    from app.data_sources import agent_alerts as aa
    from app.main import app
    from tests.test_panel_readability import _login

    client = _FakeRedis()
    await _publish(client, sinks={"telegram": False, "push_service_account": True,
                                  "healthchecks": False})
    real = AgentAlertsReader.heartbeat

    async def _hb(self):
        return await real(_reader_over(client))

    original = aa.AgentAlertsReader.heartbeat
    aa.AgentAlertsReader.heartbeat = _hb
    try:
        with TestClient(app) as tc:
            _login(tc)
            # This container has NO service account — exactly the live config.
            object.__setattr__(app.state.settings, "fcm_service_account", "")
            app.state.device_registry.count = lambda: 2   # type: ignore[method-assign]
            body = tc.get("/alerts").text
    finally:
        aa.AgentAlertsReader.heartbeat = original

    assert "ALERTS DELIVERED" in body
    assert "2 registered devices" in body
    assert "no <code>FIREBASE_SERVICE_ACCOUNT</code>" not in body, (
        "the agent said it has one — this container's empty copy is not the answer"
    )


@pytest.mark.asyncio
async def test_an_agent_that_says_nothing_falls_back_and_labels_it():
    """Silent fallbacks are mirrors nobody knows are mirrors."""
    from fastapi.testclient import TestClient

    from app.data_sources import agent_alerts as aa
    from app.main import app
    from tests.test_panel_readability import _login

    client = _FakeRedis()
    await _publish(client)          # no sinks key at all
    real = AgentAlertsReader.heartbeat

    async def _hb(self):
        return await real(_reader_over(client))

    original = aa.AgentAlertsReader.heartbeat
    aa.AgentAlertsReader.heartbeat = _hb
    try:
        with TestClient(app) as tc:
            _login(tc)
            object.__setattr__(app.state.settings, "fcm_service_account", "")
            app.state.device_registry.count = lambda: 0   # type: ignore[method-assign]
            body = tc.get("/alerts").text
    finally:
        aa.AgentAlertsReader.heartbeat = original

    assert "DELIVERY NOT REPORTED" in body
    assert "Nothing pages you" not in body
