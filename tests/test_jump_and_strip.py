"""Ctrl+K jump index and the site-wide status strip (2026-11-25)."""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from types import SimpleNamespace  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import guest_scope, status_strip  # noqa: E402
from app.main import app  # noqa: E402
from tests.test_guest_access import _guest, _mint  # noqa: E402
from tests.test_nav import _nav_entries  # noqa: E402


def _paths(groups):
    return [s[0] for _g, _l, subs in groups for s in subs]


def test_the_owner_index_is_the_whole_nav():
    with TestClient(app) as c:
        c.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
        groups = c.get("/_partial/jump-index").json()
    assert _paths(groups) == [p for p, _, _ in _nav_entries()]


def test_a_guest_is_never_offered_a_page_the_gate_refuses():
    with TestClient(app) as c:
        _guest(c, _mint(c))
        groups = c.get("/_partial/jump-index").json()
    paths = _paths(groups)
    assert paths and "/control" not in paths
    assert set(paths) <= guest_scope.GUEST_READ_ROUTES


def test_the_index_is_not_embedded_in_pages():
    """Fetched on first open, not inlined: inlined, every "this URL is absent"
    assertion on an owner page would start matching the palette's list."""
    with TestClient(app) as c:
        c.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
        body = c.get("/labs").text
    assert "/control/access" not in body
    assert 'id="palette"' in body


def _req(breaker=None, liveness=None):
    status_strip._CACHE.update(at=0.0, alerts=[])
    dv = SimpleNamespace(feature_liveness=lambda: liveness)
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        binance_klines=breaker, data_volume=dv)))


def test_strip_is_empty_when_nothing_is_known_to_be_wrong():
    assert status_strip.status_strip(_req(liveness={"alerts": []})) == []
    # An unreadable manifest adds nothing: Pulse grades that state.
    assert status_strip.status_strip(_req(liveness={"error": "missing"})) == []


def test_strip_names_an_open_breaker_and_sustained_alerts():
    breaker = SimpleNamespace(circuit_open=True, ban_seconds_remaining=125.0)
    items = status_strip.status_strip(_req(
        breaker, {"alerts": [{"feature": "cohort_edge_gate"}, {"feature": "depth_feed"}]}))
    assert [i["tone"] for i in items] == ["bad", "warn"]
    assert "~3 min" in items[0]["text"]
    assert "2 sustained" in items[1]["text"] and "cohort_edge_gate" in items[1]["text"]
