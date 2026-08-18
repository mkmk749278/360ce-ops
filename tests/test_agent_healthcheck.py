"""The monitoring agent's container healthcheck — the one it never had.

Both ops services run the same image, and that image's single ``HEALTHCHECK``
fetches ``http://localhost:8000/healthz``. Right for the web container,
impossible for this one: it overrides the command to ``python -m
app.agent.runner`` and serves no HTTP. So the agent reported UNHEALTHY by
construction — on the box 2026-08-18, ``failing streak 14`` eight minutes after
a deploy — and nothing restarted, because it is not autoheal-labelled.

**A health signal that can never be true is not a false alarm, it is a dead
instrument.** Red on it means nothing, so red on it gets ignored, on the one
container whose failure is otherwise silent. It had been sitting in
``docker ps`` unread until `/system` rendered the healthcheck's own output.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
from datetime import datetime, timedelta, timezone

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.agent import healthcheck  # noqa: E402
from app.agent.heartbeat import KEY  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]


class _FakeRedis:
    def __init__(self, value=None, raises=False):
        self._value = value
        self._raises = raises

    def get(self, key):
        assert key == KEY
        if self._raises:
            raise ConnectionError("nope")
        return self._value


def _install(monkeypatch, fake):
    import sys
    import types

    module = types.SimpleNamespace(from_url=lambda *a, **k: fake)
    monkeypatch.setitem(sys.modules, "redis", module)


def _stamp(age_sec: float, **extra) -> str:
    when = datetime.now(timezone.utc) - timedelta(seconds=age_sec)
    return json.dumps({"at": when.isoformat(), **extra})


class TestCheck:
    def test_a_fresh_heartbeat_passes(self, monkeypatch, capsys):
        _install(monkeypatch, _FakeRedis(_stamp(20)))
        assert healthcheck.check() == 0
        assert "last cycle" in capsys.readouterr().out

    def test_a_stale_heartbeat_fails_and_says_how_stale(self, monkeypatch, capsys):
        _install(monkeypatch, _FakeRedis(_stamp(healthcheck.STALE_SEC + 60)))
        assert healthcheck.check() == 1
        assert "over the" in capsys.readouterr().out

    def test_no_heartbeat_fails_with_a_named_reason(self, monkeypatch, capsys):
        _install(monkeypatch, _FakeRedis(None))
        assert healthcheck.check() == 1
        assert "no heartbeat stamped yet" in capsys.readouterr().out

    def test_an_unreachable_store_is_not_graded_as_a_dead_agent(self, monkeypatch, capsys):
        """The alert store being unreachable is not the agent being dead — it
        keeps detecting with in-memory alert state. Failing here would restart
        a working agent because an optional dependency blinked."""
        _install(monkeypatch, _FakeRedis(raises=True))
        assert healthcheck.check() == 0
        assert "not graded" in capsys.readouterr().out

    def test_a_failed_cycle_is_not_a_failed_healthcheck(self, monkeypatch):
        """A cycle that ran and reported failures is the agent WORKING.
        Restarting on it would kill the one process able to say what failed.
        `cycle_ok` is published and rendered; it is not a restart condition."""
        _install(monkeypatch, _FakeRedis(_stamp(10, cycle_ok=False)))
        assert healthcheck.check() == 0

    @pytest.mark.parametrize("raw", ["not json", '{"at": "yesterday"}', "{}"])
    def test_an_unreadable_stamp_fails_rather_than_passing(self, monkeypatch, raw):
        """A stamp we cannot parse is not a fresh one. The tempting fallback —
        treat unparseable as fine — is how a broken writer reads as healthy."""
        _install(monkeypatch, _FakeRedis(raw))
        assert healthcheck.check() == 1

    def test_the_reason_goes_to_stdout_where_docker_keeps_it(self, monkeypatch, capsys):
        """Docker captures stdout into `State.Health.Log`, which is where
        /system reads it from. A reason in a log nobody can reach from a phone
        is the defect this whole surface exists to repair."""
        _install(monkeypatch, _FakeRedis(None))
        healthcheck.check()
        assert capsys.readouterr().out.strip()


class TestComposeWiring:
    """Defining the check is not using it — pin the call site, not the method."""

    def test_the_agent_service_overrides_the_images_http_healthcheck(self):
        compose = (REPO / "docker-compose.yml").read_text()
        agent = compose.split("monitoring-agent:", 1)[1].split("\n  360ce-ops-redis:", 1)[0]
        assert "app.agent.healthcheck" in agent, (
            "the agent service must override the image's /healthz probe — it "
            "serves no HTTP, so that probe can never pass"
        )
        assert "start_period" in agent, (
            "a fresh container has no heartbeat until its first cycle "
            "completes; without a start period it fails instead of starting"
        )

    def test_the_image_healthcheck_is_still_http_for_the_web_service(self):
        """The override is scoped. If the image ever stops probing /healthz the
        web container silently loses its own health signal, and this test is
        the only thing that would say so."""
        dockerfile = (REPO / "Dockerfile").read_text()
        assert re.search(r"HEALTHCHECK[\s\S]{0,200}healthz", dockerfile)
