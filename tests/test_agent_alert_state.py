"""Unit tests for AlertStateStore.

Uses a simple in-memory dict (no Redis) via the memory fallback path.
"""
from __future__ import annotations

import pytest

from app.agent.alert_state import AlertStateStore
from app.agent.detectors import DetectorResult


def _warn(fp: str = "test_fp") -> DetectorResult:
    return DetectorResult(severity="WARN", fingerprint=fp, description="test warn")


def _high(fp: str = "test_fp") -> DetectorResult:
    return DetectorResult(severity="HIGH", fingerprint=fp, description="test high")


class TestAlertStateStore:
    async def test_warn_not_paged_first_cycle(self):
        store = AlertStateStore(dedup_sec=1800)
        action = await store.process(_warn())
        assert not action.should_notify
        assert action.count == 1

    async def test_warn_paged_second_cycle(self):
        store = AlertStateStore(dedup_sec=1800)
        await store.process(_warn())
        action = await store.process(_warn())
        assert action.should_notify
        assert action.count == 2

    async def test_high_paged_immediately(self):
        store = AlertStateStore(dedup_sec=1800)
        action = await store.process(_high())
        assert action.should_notify
        assert action.count == 1
        assert action.severity == "HIGH"

    async def test_dedup_suppresses_repeat(self):
        store = AlertStateStore(dedup_sec=9999)
        await store.process(_high())   # paged
        action = await store.process(_high())  # within dedup window
        assert not action.should_notify

    async def test_dedup_zero_allows_repeat(self):
        store = AlertStateStore(dedup_sec=0)
        await store.process(_high())
        action = await store.process(_high())
        assert action.should_notify

    async def test_escalation_repages(self):
        store = AlertStateStore(dedup_sec=9999)
        # First page as WARN (2 cycles)
        await store.process(_warn())
        action1 = await store.process(_warn())
        assert action1.should_notify
        assert action1.severity == "WARN"
        # Now escalate to HIGH
        action2 = await store.process(_high())
        assert action2.should_notify
        assert action2.severity == "HIGH"

    async def test_resolve_removes_state(self):
        store = AlertStateStore(dedup_sec=1800)
        await store.process(_high())  # paged
        resolved = await store.resolve("test_fp")
        assert resolved is not None
        assert resolved.fingerprint == "test_fp"
        # After resolve, next trigger starts fresh
        action = await store.process(_high())
        assert action.should_notify  # pages again immediately

    async def test_resolve_unpaged_returns_none(self):
        store = AlertStateStore(dedup_sec=1800)
        await store.process(_warn())  # not yet paged (count=1)
        resolved = await store.resolve("test_fp")
        assert resolved is None

    async def test_resolve_nonexistent_returns_none(self):
        store = AlertStateStore()
        assert await store.resolve("does_not_exist") is None

    async def test_active_fingerprints_tracked(self):
        store = AlertStateStore()
        await store.process(_warn("fp_a"))
        await store.process(_warn("fp_b"))
        active = await store.active_fingerprints()
        assert "fp_a" in active
        assert "fp_b" in active

    async def test_active_fingerprints_cleared_on_resolve(self):
        store = AlertStateStore()
        await store.process(_warn("fp_a"))
        await store.resolve("fp_a")
        active = await store.active_fingerprints()
        assert "fp_a" not in active

    async def test_count_increments_each_cycle(self):
        store = AlertStateStore(dedup_sec=0)
        for expected_count in range(1, 6):
            action = await store.process(_high())
            assert action.count == expected_count

    async def test_different_fingerprints_independent(self):
        store = AlertStateStore(dedup_sec=9999)
        action_a = await store.process(_high("fp_a"))
        action_b = await store.process(_high("fp_b"))
        assert action_a.should_notify
        assert action_b.should_notify
