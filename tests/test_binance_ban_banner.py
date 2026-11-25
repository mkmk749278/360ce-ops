"""The Binance IP-ban banner renders on BOTH pages that replay candles.

/dark-signals carried it; /profit replays through the same breaker and showed
only "N row(s) degraded" with no cause (outside audit, 2026-11-25). The
banner is one partial now, so the two cannot drift, and this pins both.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


class _OpenBreaker:
    """Wraps the real client; only the breaker state is forced open."""

    def __init__(self, real):
        self._real = real

    circuit_open = True
    ban_seconds_remaining = 300.0
    ban_reason = "-1003 Way too many requests"

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.mark.parametrize("page", ["/dark-signals", "/profit"])
def test_open_breaker_is_named_on_the_page(page):
    with TestClient(app) as c:
        real = app.state.binance_klines
        app.state.binance_klines = _OpenBreaker(real)
        try:
            c.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
            body = c.get(page).text
        finally:
            app.state.binance_klines = real
    assert "Binance IP-ban cooldown active" in body
    assert "-1003 Way too many requests" in body


@pytest.mark.parametrize("page", ["/dark-signals", "/profit"])
def test_closed_breaker_renders_no_banner(page):
    with TestClient(app) as c:
        c.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
        body = c.get(page).text
    assert "Binance IP-ban cooldown active" not in body
