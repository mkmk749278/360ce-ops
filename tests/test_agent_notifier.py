"""Tests for the monitoring agent's notification delivery leg.

The detectors were already pinned as pure functions, but the module that
actually *delivers* a Tier-0 page (``app/agent/notifier.py``) had no
tests — a silently broken notifier means a naked-position alert fires
into the void, which is exactly the "output flat-lines without paging"
failure the agent exists to prevent.  Pinned here:

* message formatting (severity emoji, fingerprint display, recovery);
* Telegram send: correct bot URL + HTML payload, unconfigured token
  drops the message instead of raising, non-200 and transport errors
  are logged not raised;
* FCM push fan-out: sends to every registered device, prunes tokens the
  sender reports dead, disabled-safe no-op without an FCM sink, and an
  FCM failure never propagates (best-effort contract);
* heartbeat ping: skipped when unconfigured, errors swallowed;
* ``aclose`` closes both sinks and tolerates an FCM close failure.

All HTTP is faked at the ``_get_client`` seam — no network.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.agent.alert_state import AlertAction, ResolvedAction
from app.agent.detectors import DetectorResult
from app.agent.notifier import Notifier, _format_alert, _format_recovery


def _action(severity: str = "HIGH", fingerprint: str = "naked_position:BTCUSDT") -> AlertAction:
    return AlertAction(
        should_notify=True,
        is_first=True,
        count=3,
        first_seen="12:00:00 UTC",
        severity=severity,
        result=DetectorResult(
            severity=severity,  # type: ignore[arg-type]
            fingerprint=fingerprint,
            description="OPEN position with no stop for 90s",
        ),
    )


def _fake_http(status_code: int = 200) -> MagicMock:
    client = MagicMock()
    response = MagicMock()
    response.status_code = status_code
    response.text = "ok"
    client.post = AsyncMock(return_value=response)
    client.get = AsyncMock(return_value=response)
    return client


def _notifier(http=None, **kwargs) -> Notifier:
    n = Notifier(
        bot_token=kwargs.pop("bot_token", "tok"),
        chat_id=kwargs.pop("chat_id", "42"),
        **kwargs,
    )
    if http is not None:
        n._client = http
    return n


class TestFormatting:
    def test_high_alert_uses_siren_and_readable_fingerprint(self):
        text = _format_alert(_action())
        assert "🚨" in text
        assert "naked_position · BTCUSDT" in text
        assert "OPEN position with no stop" in text
        assert "Cycle #3" in text

    def test_warn_alert_uses_warning_emoji(self):
        text = _format_alert(_action(severity="WARN"))
        assert "⚠️" in text
        assert "🚨" not in text

    def test_recovery_message(self):
        text = _format_recovery(
            ResolvedAction(fingerprint="engine_stale:snapshot", first_seen="11:59:00 UTC")
        )
        assert "Recovered" in text
        assert "engine_stale · snapshot" in text
        assert "11:59:00 UTC" in text


class TestTelegramSend:
    async def test_posts_to_bot_url_with_html_payload(self):
        http = _fake_http()
        n = _notifier(http)
        await n.send_alert(_action())
        http.post.assert_awaited_once()
        url = http.post.call_args.args[0]
        payload = http.post.call_args.kwargs["json"]
        assert url == "https://api.telegram.org/bottok/sendMessage"
        assert payload["chat_id"] == "42"
        assert payload["parse_mode"] == "HTML"
        assert "naked_position" in payload["text"]

    async def test_unconfigured_token_drops_message_without_error(self):
        http = _fake_http()
        n = _notifier(http, bot_token="", chat_id="")
        await n.send_alert(_action())
        http.post.assert_not_awaited()

    async def test_non_200_is_logged_not_raised(self):
        http = _fake_http(status_code=403)
        n = _notifier(http)
        await n.send_alert(_action())  # must not raise

    async def test_transport_error_is_logged_not_raised(self):
        http = _fake_http()
        http.post = AsyncMock(side_effect=ConnectionError("telegram unreachable"))
        n = _notifier(http)
        await n.send_alert(_action())  # must not raise


class TestFcmPush:
    def _sinks(self, tokens=("t1", "t2"), dead=()):
        fcm = MagicMock()
        fcm.send = AsyncMock(return_value=list(dead))
        devices = MagicMock()
        devices.all_tokens.return_value = list(tokens)
        return fcm, devices

    async def test_alert_pushes_to_all_registered_devices(self):
        fcm, devices = self._sinks()
        n = _notifier(_fake_http(), fcm=fcm, device_registry=devices)
        await n.send_alert(_action())
        fcm.send.assert_awaited_once()
        assert fcm.send.call_args.args[0] == ["t1", "t2"]
        assert fcm.send.call_args.kwargs["data"]["kind"] == "alert"
        devices.prune.assert_not_called()

    async def test_dead_tokens_are_pruned(self):
        fcm, devices = self._sinks(dead=["t2"])
        n = _notifier(_fake_http(), fcm=fcm, device_registry=devices)
        await n.send_alert(_action())
        devices.prune.assert_called_once_with(["t2"])

    async def test_recovery_pushes_with_recovery_kind(self):
        fcm, devices = self._sinks()
        n = _notifier(_fake_http(), fcm=fcm, device_registry=devices)
        await n.send_recovery(
            ResolvedAction(fingerprint="engine_stale:snapshot", first_seen="x")
        )
        assert fcm.send.call_args.kwargs["data"]["kind"] == "recovery"

    async def test_no_fcm_sink_is_a_noop(self):
        n = _notifier(_fake_http())
        await n.send_alert(_action())  # must not raise

    async def test_no_registered_devices_skips_send(self):
        fcm, devices = self._sinks(tokens=())
        n = _notifier(_fake_http(), fcm=fcm, device_registry=devices)
        await n.send_alert(_action())
        fcm.send.assert_not_awaited()

    async def test_fcm_failure_never_blocks_the_telegram_leg(self):
        # Both sinks are best-effort: FCM blowing up must not raise (and
        # the Telegram send has already happened by then).
        fcm, devices = self._sinks()
        fcm.send = AsyncMock(side_effect=RuntimeError("fcm quota"))
        http = _fake_http()
        n = _notifier(http, fcm=fcm, device_registry=devices)
        await n.send_alert(_action())
        http.post.assert_awaited_once()


class TestHeartbeat:
    async def test_unconfigured_url_skips_ping(self):
        http = _fake_http()
        n = _notifier(http, healthchecks_url="")
        await n.ping_heartbeat()
        http.get.assert_not_awaited()

    async def test_pings_configured_url(self):
        http = _fake_http()
        n = _notifier(http, healthchecks_url="https://hc-ping.example/uuid")
        await n.ping_heartbeat()
        http.get.assert_awaited_once()
        assert http.get.call_args.args[0] == "https://hc-ping.example/uuid"

    async def test_ping_failure_is_swallowed(self):
        http = _fake_http()
        http.get = AsyncMock(side_effect=ConnectionError("hc down"))
        n = _notifier(http, healthchecks_url="https://hc-ping.example/uuid")
        await n.ping_heartbeat()  # must not raise


class TestClose:
    async def test_aclose_closes_http_and_fcm(self):
        http = _fake_http()
        http.aclose = AsyncMock()
        fcm = MagicMock()
        fcm.aclose = AsyncMock()
        n = _notifier(http, fcm=fcm)
        await n.aclose()
        http.aclose.assert_awaited_once()
        fcm.aclose.assert_awaited_once()
        assert n._client is None

    async def test_fcm_close_failure_is_swallowed(self):
        http = _fake_http()
        http.aclose = AsyncMock()
        fcm = MagicMock()
        fcm.aclose = AsyncMock(side_effect=RuntimeError("already closed"))
        n = _notifier(http, fcm=fcm)
        await n.aclose()  # must not raise
