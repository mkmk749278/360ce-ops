"""Telegram notifier and healthchecks.io heartbeat ping.

Uses httpx (already a project dependency) for all outbound HTTP.
The healthchecks.io URL is never logged or committed — it lives in .env only.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx

from app.agent.alert_state import AlertAction, ResolvedAction

log = logging.getLogger("agent.notifier")

_TG_API = "https://api.telegram.org"


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S UTC")


def _severity_emoji(severity: str) -> str:
    return "🚨" if severity == "HIGH" else "⚠️"


def _format_alert(action: AlertAction) -> str:
    r = action.result
    emoji = _severity_emoji(action.severity)
    lines = [
        f"{emoji} <b>{action.severity} — {r.fingerprint.replace(':', ' · ')}</b>",
        r.description,
        f"First seen: {action.first_seen}  |  Cycle #{action.count}",
    ]
    return "\n".join(lines)


def _format_recovery(action: ResolvedAction) -> str:
    fp_display = action.fingerprint.replace(":", " · ")
    return f"✅ <b>Recovered — {fp_display}</b>\nCondition cleared. First seen: {action.first_seen}"


class Notifier:
    def __init__(
        self,
        *,
        bot_token: str,
        chat_id: str,
        healthchecks_url: str = "",
        fcm: Any = None,
        device_registry: Any = None,
    ) -> None:
        self._token = bot_token
        self._chat_id = chat_id
        self._hc_url = healthchecks_url
        # Optional FCM push sink (Phase 4) — the reliable in-region alert path
        # when Telegram is dead. Both sinks are best-effort; a failure in one
        # never blocks the other.
        self._fcm = fcm
        self._devices = device_registry
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        if self._fcm is not None:
            try:
                await self._fcm.aclose()
            except Exception:
                pass

    async def send_alert(self, action: AlertAction) -> None:
        text = _format_alert(action)
        await self._send_telegram(text)
        await self._push(
            title=f"{_severity_emoji(action.severity)} {action.severity} — "
            f"{action.result.fingerprint.split(':')[0]}",
            body=action.result.description,
            data={"fingerprint": action.result.fingerprint, "kind": "alert"},
        )

    async def send_recovery(self, action: ResolvedAction) -> None:
        text = _format_recovery(action)
        await self._send_telegram(text)
        await self._push(
            title=f"✅ Recovered — {action.fingerprint.split(':')[0]}",
            body="Condition cleared.",
            data={"fingerprint": action.fingerprint, "kind": "recovery"},
        )

    def armed_sinks(self) -> dict[str, bool]:
        """Which delivery paths this notifier can actually send through.

        Read off the same values `_send_telegram` and `_push` gate on, so the
        answer cannot drift from the behaviour — the alternative is a second
        copy of "is Telegram configured" in the web container, which is how
        `/alerts` came to print *"Nothing pages you"* over a working Telegram
        sink (2026-08-19). Booleans only; no token leaves this method.

        `telegram` needs both a bot token and a chat id — either alone sends
        nothing. `push` is deliberately NOT graded here: a service account
        makes a send possible and a registered device makes it arrive, and the
        registry lives on the web side, so that half stays where it is
        observable. This reports what the AGENT holds.
        """
        return {
            "telegram": bool(self._token and self._chat_id),
            "push_service_account": self._fcm is not None and self._fcm.enabled,
            "healthchecks": bool(self._hc_url),
        }

    async def _push(self, *, title: str, body: str, data: dict[str, str]) -> None:
        """Best-effort FCM push to every registered device; prune dead tokens."""
        if self._fcm is None or self._devices is None:
            return
        try:
            tokens = self._devices.all_tokens()
            if not tokens:
                return
            dead = await self._fcm.send(tokens, title=title, body=body, data=data)
            if dead:
                self._devices.prune(dead)
        except Exception as exc:
            log.warning("FCM push failed: %s", exc)

    async def ping_heartbeat(self) -> None:
        if not self._hc_url:
            return
        try:
            await self._get_client().get(self._hc_url, timeout=5.0)
        except Exception as exc:
            log.warning("healthchecks.io ping failed: %s", exc)

    async def _send_telegram(self, text: str) -> None:
        if not self._token or not self._chat_id:
            log.warning("Telegram not configured — dropping message: %s", text[:80])
            return
        url = f"{_TG_API}/bot{self._token}/sendMessage"
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            r = await self._get_client().post(url, json=payload, timeout=10.0)
            if r.status_code != 200:
                log.warning("Telegram API returned %s: %s", r.status_code, r.text[:200])
        except Exception as exc:
            log.error("Telegram send failed: %s", exc)
