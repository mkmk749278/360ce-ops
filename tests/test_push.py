"""Phase 4 push: device registry, FCM response classification, disabled sender.

None of these touch google-auth — the sender only imports it when a service
account is configured, so the suite runs without the FCM dependency wired.
"""
from __future__ import annotations

from app.device_registry import DeviceRegistry
from app.fcm import FcmSender, classify_send


# ---- device registry ----------------------------------------------------


def test_registry_register_all_unregister(tmp_path):
    p = str(tmp_path / "devices.json")
    reg = DeviceRegistry(p)
    reg.register("tok-a")
    reg.register("tok-b", platform="android")
    assert set(reg.all_tokens()) == {"tok-a", "tok-b"}
    assert reg.count() == 2
    # Re-register is idempotent (no duplicate).
    reg.register("tok-a")
    assert reg.count() == 2
    assert reg.unregister("tok-a") is True
    assert reg.unregister("tok-a") is False
    assert reg.all_tokens() == ["tok-b"]
    # Survives reload (cross-process: web writes, agent reads).
    assert DeviceRegistry(p).all_tokens() == ["tok-b"]


def test_registry_prune(tmp_path):
    p = str(tmp_path / "devices.json")
    reg = DeviceRegistry(p)
    for t in ("a", "b", "c"):
        reg.register(t)
    assert reg.prune(["a", "c", "missing"]) == 2
    assert reg.all_tokens() == ["b"]


def test_registry_empty_token_ignored(tmp_path):
    reg = DeviceRegistry(str(tmp_path / "d.json"))
    reg.register("")
    assert reg.count() == 0


# ---- FCM response classification (pure) ---------------------------------


def test_classify_send():
    assert classify_send(200, {"name": "projects/x/messages/1"}) == "ok"
    assert classify_send(404, {"error": {"status": "NOT_FOUND"}}) == "prune"
    assert classify_send(400, {"error": {"status": "UNREGISTERED"}}) == "prune"
    assert classify_send(400, {"error": {"status": "INVALID_ARGUMENT"}}) == "prune"
    assert classify_send(500, {"error": {"status": "INTERNAL"}}) == "error"
    assert classify_send(401, None) == "error"


# ---- disabled sender is a no-op -----------------------------------------


async def test_disabled_sender_is_noop():
    sender = FcmSender("")
    assert sender.enabled is False
    pruned = await sender.send(["tok"], title="t", body="b")
    assert pruned == []


async def test_enabled_flag_reflects_service_account():
    assert FcmSender('{"project_id":"x"}').enabled is True
    assert FcmSender("   ").enabled is False
