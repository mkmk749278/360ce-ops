"""The client must not give up before the server it is talking to does.

`/internal/diag/catalog/run` is a QUEUE-AND-POLL path: the api container hands
the catalog key to the engine over Redis and polls for the answer up to the
engine's own `DIAG_POLL_TIMEOUT_SEC` (25.0s), because the engine drains that
queue on its monitor loop, a bounded number per cycle. Ops' default client
timeout is 10s — shorter than that deadline — so every read the engine took
10-25s to answer was abandoned here and reported as a failure while the engine
was still working on it.

Measured on 2026-09-03, six consecutive loads of `/signals/ai-governor`:
**four rendered (1.3-7.2s) and two failed, both at ~10.8s.** A third of the
loads of a page whose whole job is to be read.

It was invisible until the same day's fix taught that page to say WHICH failure
it had: before, an abandoned call rendered as *"the engine has no
read.ai_governor catalog entry — an engine predating this page, so it is a
deploy question"*. The instrument found the fault underneath the one it was
built for.
"""
from __future__ import annotations

import inspect
import pathlib

from app.data_sources import engine_api

ENGINE_CONFIG = pathlib.Path(__file__).resolve().parents[2] / "360-v2/config/__init__.py"


def test_the_diag_timeout_exceeds_the_engines_own_poll_deadline():
    """Read the engine's declared deadline rather than trusting a number typed
    here — if the engine ever raises it, this fails instead of silently going
    back to abandoning reads."""
    if not ENGINE_CONFIG.exists():
        import pytest

        pytest.skip("engine repo not checked out beside ops")

    text = ENGINE_CONFIG.read_text()
    marker = 'DIAG_POLL_TIMEOUT_SEC: float = _safe_float("DIAG_POLL_TIMEOUT_SEC", '
    assert marker in text, "the engine no longer declares this deadline as expected"
    engine_deadline = float(text.split(marker)[1].split(")")[0])

    assert engine_api.DIAG_RUN_TIMEOUT_SEC > engine_deadline, (
        f"ops gives up at {engine_api.DIAG_RUN_TIMEOUT_SEC}s while the engine "
        f"keeps working until {engine_deadline}s — every answer in between is "
        f"thrown away and rendered as a failure"
    )


def test_diag_run_actually_passes_the_timeout():
    """A constant nothing reads is the scaffold this repo bans. Pinned on the
    call site, not the definition."""
    src = inspect.getsource(engine_api.EngineApiClient.diag_run)
    assert "DIAG_RUN_TIMEOUT_SEC" in src


def test_the_longer_timeout_is_scoped_to_the_diag_bridge_alone():
    """10s stays correct for every endpoint the api container answers itself.
    This is not "raise the timeout"; it is one path whose server-side deadline
    is longer than the client's."""
    assert "httpx.Timeout(10.0)" in inspect.getsource(engine_api.EngineApiClient)
    for name in ("data_intake", "router_delivery", "host_resources"):
        fn = getattr(engine_api.EngineApiClient, name, None)
        if fn is None:
            continue
        assert "DIAG_RUN_TIMEOUT_SEC" not in inspect.getsource(fn)


async def test_the_timeout_reaches_httpx():
    """Driven through the real `_post`, because a test that reads the source
    asserts the author's intent and not the client's behaviour."""
    seen = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {"ok": True}

    class _Client:
        async def post(self, path, **kw):
            seen.update(kw)
            return _Resp()

    class _Api(engine_api.EngineApiClient):
        def __init__(self):
            pass

        @property
        def client(self):
            return _Client()

    await _Api().diag_run("read.ai_governor", {})
    assert seen.get("timeout") == engine_api.DIAG_RUN_TIMEOUT_SEC


async def test_an_ordinary_post_still_uses_the_client_default():
    seen = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {}

    class _Client:
        async def post(self, path, **kw):
            seen.update(kw)
            return _Resp()

    class _Api(engine_api.EngineApiClient):
        def __init__(self):
            pass

        @property
        def client(self):
            return _Client()

    await _Api()._post("/api/anything", {"a": 1})
    assert "timeout" not in seen
