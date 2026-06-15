"""Signals tab prefers live engine state; monitor cache is fallback-only.

Regression for the '/reset_full ran but Signals tab still shows old rows' report
(2026-06-15): the monitor signals_last100 artefact is TTL-cached on the
monitor-logs branch and cannot be cleared engine-side, so merging it
unconditionally left phantom pre-reset rows. It must only be consulted when the
live API returns nothing.
"""
from __future__ import annotations

import asyncio
import types

from app.routes.signals import _build_rows


def _req(live_items, hist_items):
    logs_calls = {"n": 0}

    class _API:
        async def signals(self, status=None, setup_class=None):
            return live_items

    class _Logs:
        async def signals_last100(self):
            logs_calls["n"] += 1
            return hist_items

    req = types.SimpleNamespace(
        app=types.SimpleNamespace(
            state=types.SimpleNamespace(engine_api=_API(), monitor_logs=_Logs())
        )
    )
    return req, logs_calls


def test_live_present_ignores_monitor_cache():
    live = [{"signal_id": "LIVE1", "symbol": "BTCUSDT", "status": "OPEN"}]
    hist = [{"signal_id": "OLD1", "symbol": "ETHUSDT", "status": "SL_HIT"}]
    req, logs_calls = _req(live, hist)
    rows = asyncio.run(_build_rows(req, None, None))
    ids = {r["id"] for r in rows}
    assert ids == {"LIVE1"}          # phantom OLD1 not shown
    assert logs_calls["n"] == 0      # cache not even fetched when live present


def test_falls_back_to_cache_when_live_empty():
    hist = [{"signal_id": "OLD1", "symbol": "ETHUSDT", "status": "SL_HIT"}]
    req, logs_calls = _req([], hist)
    rows = asyncio.run(_build_rows(req, None, None))
    assert {r["id"] for r in rows} == {"OLD1"}   # fallback engaged
    assert logs_calls["n"] == 1


def test_reset_then_live_repopulates_clean():
    # After a reset the live API returns only the fresh post-reset signals;
    # the tab must show exactly those, never the stale cache.
    live = [{"signal_id": f"NEW{i}", "symbol": "X", "status": "INVALIDATED"} for i in range(3)]
    hist = [{"signal_id": "STALE", "symbol": "Y", "status": "PROFIT_LOCKED"}]
    req, _ = _req(live, hist)
    rows = asyncio.run(_build_rows(req, None, None))
    assert {r["id"] for r in rows} == {"NEW0", "NEW1", "NEW2"}
