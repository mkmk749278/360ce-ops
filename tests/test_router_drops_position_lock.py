"""`correlation_lock` could not say whether it was tight or stale.

Owner, 2026-08-20: *"we enable lsr to go live but nothing reached live feed
and where they going what's happening"*.

The dark lane promoted **30** rows (25 `LIQUIDITY_SWEEP_REVERSAL`, 5
`MOVER_AVWAP_SCALP`) and **0** reached a subscriber: 26 died on
`correlation_lock`, 1 on `same_direction_throttle`.  On the same box that gate
had taken **309 of 332** dequeued candidates (93.1%) in one 13h process while
**2** signals were ACTIVE, and six of the locked symbols had no delivered
trade at all in the 30-day recorded book.

93% is this gate working when the locked symbols hold positions and a silent
outage when they do not, and this page had been asserting the first reading
over a book living the second — *"a caption that is true about the wrong axis
reads as reassurance"*, on the gate that drops the most.

The engine block these tests reduce is produced by
**360-v2 `src/signal_router.py::SignalRouter.position_lock_health`** and
published inside `delivery_stats()`.  `_ENGINE_BLOCK` below was **captured
from that real producer**, not typed from the reader's assumption — the
`zone_distance_atr` / price-action-card defect is a fixture agreeing with you
about a shape nothing produces.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.routes.router_drops import reduce_position_lock  # noqa: E402


#: Captured verbatim from 360-v2's real `delivery_stats()["position_lock"]`.
#: If a key here stops existing, this repo renders a blank where a verdict
#: belongs — which is why the names are pinned rather than read defensively.
_ENGINE_BLOCK = {
    "active_signals": 0,
    "active_symbols": 0,
    "direction_corrected_at_restore": 0,
    "locked": 1,
    "missing_added_at_restore": 0,
    "orphaned_now": 1,
    "orphaned_sample": ["GHOSTUSDT"],
    "orphans_dropped_at_restore": 4,
    "unlocked_now": 0,
    "unlocked_sample": [],
}


@contextmanager
def _client(payload, monkeypatch):
    """Drive the real app through its lifespan — `app.state.engine_api` is
    built at startup, so assigning to it beforehand assigns to something about
    to be replaced.  Mirrors `test_router_drops.py`'s helper."""
    from fastapi.testclient import TestClient

    from app.main import app

    async def _fake(self):
        return payload

    monkeypatch.setattr(EngineApiClient, "router_delivery", _fake)
    with TestClient(app) as c:
        c.post("/login", data={"password": "test-token"})
        yield c


def _payload(**overrides):
    block = dict(_ENGINE_BLOCK)
    block.update(overrides)
    return {"schema": 1, "position_lock": block}


class TestTheFourStates:
    def test_an_orphaned_lock_reads_stale_not_tight(self):
        """The outage's own shape: symbols locked, nothing behind them."""
        out = reduce_position_lock(_payload())
        assert out["state"] == "orphaned"
        assert out["orphaned"] == 1
        assert out["orphaned_sample"] == ["GHOSTUSDT"]

    def test_agreement_reads_tight(self):
        out = reduce_position_lock(_payload(
            locked=2, active_signals=2, active_symbols=2, orphaned_now=0,
            orphaned_sample=[],
        ))
        assert out["state"] == "healthy"

    def test_an_unlocked_active_signal_outranks_an_orphan(self):
        """Under-blocking is the worse fault: a second position can open on a
        symbol that already has one.  A page leading with the orphan would
        bury it, so the state machine ranks it first even when both hold."""
        out = reduce_position_lock(_payload(
            orphaned_now=3, orphaned_sample=["A", "B", "C"],
            unlocked_now=1, unlocked_sample=["ETHUSDT"],
        ))
        assert out["state"] == "unlocked"
        assert out["unlocked_sample"] == ["ETHUSDT"]

    def test_an_engine_without_the_block_is_not_reported_never_healthy(self):
        """"Nothing to report" and "nothing reported" are the conflation this
        repo keeps paying for — an older engine must not render as TIGHT."""
        assert reduce_position_lock({"schema": 1})["state"] == "not_reported"
        assert reduce_position_lock({})["state"] == "not_reported"
        assert reduce_position_lock(
            {"position_lock": None}
        )["state"] == "not_reported"


class TestTheRepairCountIsNotAFault:
    def test_restore_repairs_are_carried_but_do_not_set_the_state(self):
        """What the boot reconcile dropped is the only evidence the skew ever
        happened; it is history, not a live fault."""
        out = reduce_position_lock(_payload(
            locked=1, active_signals=1, active_symbols=1,
            orphaned_now=0, orphaned_sample=[],
            orphans_dropped_at_restore=26,
        ))
        assert out["state"] == "healthy"
        assert out["repaired"] == 26


class TestItRenders:
    """A reducer with no panel is #817 with the arrow reversed — the defect
    that shipped the price-action lane card one PR late."""

    @pytest.mark.parametrize("block,expect", [
        (_ENGINE_BLOCK, "STALE"),
        ({**_ENGINE_BLOCK, "orphaned_now": 0, "orphaned_sample": []}, "TIGHT"),
        (None, "NOT REPORTED"),
    ])
    def test_the_page_states_the_verdict(self, monkeypatch, block, expect):
        payload = {"schema": 1, "processed": 10, "delivered": 1, "dropped": 9,
                   "drops_by_reason": {"correlation_lock": 9},
                   "drops_by_reason_setup": {},
                   "position_lock": block}
        with _client(payload, monkeypatch) as c:
            r = c.get("/signals/router-drops")
        assert r.status_code == 200
        assert expect in r.text
