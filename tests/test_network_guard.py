"""Pins that the suite cannot reach the network (see conftest.py).

The guard is only worth having if deleting it fails CI: without these tests,
removing it would make the suite slower and production-coupled again while
every other test stayed green.
"""
from __future__ import annotations

import socket

import httpx
import pytest

from tests import conftest


def test_the_engine_base_is_never_production() -> None:
    from app.config import load_settings

    settings = load_settings()
    for url in (settings.engine_api_base, settings.monitor_logs_base_url,
                settings.binance_futures_rest_base):
        assert url.endswith(".invalid"), url
        assert "luminapp.org" not in url


async def test_an_async_request_to_production_is_refused_not_sent() -> None:
    before = len(conftest.BLOCKED_REQUESTS)
    async with httpx.AsyncClient() as client:
        with pytest.raises(httpx.ConnectError, match="network disabled in tests"):
            await client.get("https://api.luminapp.org/api/pulse")
    assert conftest.BLOCKED_REQUESTS[before:] == ["GET https://api.luminapp.org/api/pulse"]


def test_a_sync_request_is_refused() -> None:
    with httpx.Client() as client:
        with pytest.raises(httpx.ConnectError):
            client.post("https://fapi.binance.com/fapi/v1/order")


def test_a_raw_socket_is_refused() -> None:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(ConnectionRefusedError, match="network disabled"):
            s.connect(("93.184.216.34", 443))
    finally:
        s.close()


def test_the_in_process_app_still_answers() -> None:
    """Control: the guard must not block TestClient, or every test would be
    measuring the guard."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200


async def test_an_engine_page_renders_its_unreachable_state_offline() -> None:
    """With the engine unreachable by construction, the client reports the
    failure in its own words rather than raising into the route."""
    from app.config import load_settings
    from app.data_sources.engine_api import EngineApiClient

    client = EngineApiClient(load_settings())
    out = await client.pulse()
    assert isinstance(out, dict) and out.get("error")
