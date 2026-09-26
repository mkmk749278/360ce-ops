"""Test-wide environment setup.

pytest imports conftest.py *before* collecting/importing any test module, so
this runs before ``app.main`` (and its module-level ``load_settings()``) is
first imported. That guarantees the file-backed stores (app-tokens, device
registry, audit log) point at a writable temp dir instead of the production
``/data`` volume — which isn't writable on CI runners. Without this, the store
paths are fixed at import time to ``/data`` and writes silently no-op.

**No test reaches the network (2026-09-26 audit).** ``ENGINE_API_BASE``
defaulted to ``https://api.luminapp.org`` — the PRODUCTION engine — and nothing
here overrode it, so any page render whose engine calls a test had not stubbed
went to production. One suite run measured **1,638 real requests**: 1,108 to
the live engine (including 9 ``POST /internal/diag/catalog/run``), 491 to
GitHub, 39 to Binance. They were answered 401 only because CI carries no
engine token — so the tests were coupled to production being up, took ~1.7s
each in network latency, and would have READ (and, on an unstubbed write,
WRITTEN) production the day an engine token reached the CI environment.

Two layers, because one of them alone is not enough:

* every external base URL points at an RFC-6761 ``.invalid`` host, so a
  forgotten stub cannot resolve anywhere;
* ``_block_network`` refuses every httpx request and socket connection to a
  non-local host with the same ``httpx.ConnectError`` an unreachable engine
  raises — which the pages already handle — and records it, so
  ``test_network_guard.py`` can pin that the guard is installed.
"""
from __future__ import annotations

import os
import socket
import tempfile

import httpx

_tmp = tempfile.mkdtemp(prefix="ops-test-")

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")
os.environ.setdefault("OPS_APP_TOKENS_PATH", os.path.join(_tmp, "app_tokens.json"))
os.environ.setdefault("OPS_DEVICE_TOKENS_PATH", os.path.join(_tmp, "devices.json"))
os.environ.setdefault("OPS_GUEST_ACCESS_PATH", os.path.join(_tmp, "guest_access.json"))
os.environ.setdefault("OPS_AUDIT_LOG", os.path.join(_tmp, "audit.jsonl"))
os.environ.setdefault("EXIT_BACKTEST_STATE_DIR", os.path.join(_tmp, "exit_backtest"))
# The cross-repo contract tests import the engine, whose logger writes to
# ./logs relative to the cwd — i.e. into this checkout. Keep it in the temp dir.
os.environ.setdefault("LOG_DIR", os.path.join(_tmp, "engine-logs"))
os.environ.setdefault("WS_TRACE_LOG_PATH", os.path.join(_tmp, "engine-logs", "ws_trace.log"))

# Never production. ``.invalid`` is reserved and never resolves.
os.environ["ENGINE_API_BASE"] = "https://engine.test.invalid"
os.environ["MONITOR_LOGS_BASE_URL"] = "https://monitor-logs.test.invalid"
os.environ["BINANCE_FUTURES_REST_BASE"] = "https://binance.test.invalid"

#: Hosts a test may talk to: the in-process ASGI app and loopback services.
LOCAL_HOSTS = frozenset({"testserver", "localhost", "127.0.0.1", "::1"})

#: Every blocked attempt, as ``"METHOD url"`` — read by test_network_guard.py.
BLOCKED_REQUESTS: list[str] = []

_orig_async_send = httpx.AsyncClient.send
_orig_sync_send = httpx.Client.send
_orig_connect = socket.socket.connect


def _refuse(request: httpx.Request) -> None:
    BLOCKED_REQUESTS.append(f"{request.method} {request.url}")
    raise httpx.ConnectError(
        f"network disabled in tests: {request.method} {request.url}", request=request,
    )


async def _guarded_async_send(self, request, *args, **kwargs):
    if request.url.host not in LOCAL_HOSTS:
        _refuse(request)
    return await _orig_async_send(self, request, *args, **kwargs)


def _guarded_sync_send(self, request, *args, **kwargs):
    if request.url.host not in LOCAL_HOSTS:
        _refuse(request)
    return _orig_sync_send(self, request, *args, **kwargs)


def _guarded_connect(self, address, *args, **kwargs):
    # Unix sockets (docker.sock) and loopback stay allowed; anything else is a
    # real outbound connection, which no test is entitled to make.
    if isinstance(address, tuple) and address:
        host = str(address[0])
        if host not in LOCAL_HOSTS and not host.startswith("127."):
            BLOCKED_REQUESTS.append(f"CONNECT {host}:{address[1] if len(address) > 1 else ''}")
            raise ConnectionRefusedError(f"network disabled in tests: {host}")
    return _orig_connect(self, address, *args, **kwargs)


# Installed at import, not in a fixture: EngineApiClient and friends are built
# when app.main is imported, before any fixture runs.
httpx.AsyncClient.send = _guarded_async_send
httpx.Client.send = _guarded_sync_send
socket.socket.connect = _guarded_connect
