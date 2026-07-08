"""Firebase Cloud Messaging sender (HTTP v1) for ops push alerts.

Deliberately avoids the heavy ``firebase-admin`` SDK: we already ship ``httpx``,
so we mint an OAuth token from the service account with ``google-auth`` and POST
to the FCM v1 endpoint directly.

Disabled-safe: when ``FIREBASE_SERVICE_ACCOUNT`` is unset the sender is a no-op
(the agent still alerts via Telegram / GitHub issues). This lets the whole push
path ship dark and light up the moment the owner adds the secret — the same
model as the release keystore.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

logger = logging.getLogger("ops.fcm")

_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def classify_send(status_code: int, body: dict[str, Any] | None) -> str:
    """Pure helper (unit-tested): map an FCM v1 response to an action.

    Returns ``"ok"``, ``"prune"`` (token is dead — drop it), or ``"error"``.
    """
    if status_code == 200:
        return "ok"
    # 404 UNREGISTERED or 400 with INVALID_ARGUMENT on the token → the token is
    # stale/garbage; prune it so we stop paying to send to it.
    err = ((body or {}).get("error") or {})
    status = err.get("status", "")
    if status_code == 404 or status == "UNREGISTERED":
        return "prune"
    if status_code == 400 and status == "INVALID_ARGUMENT":
        return "prune"
    return "error"


class FcmSender:
    def __init__(self, service_account_json: str) -> None:
        self._raw = service_account_json or ""
        self.enabled = bool(self._raw.strip())
        self._project_id: str | None = None
        self._creds: Any = None
        self._client: httpx.AsyncClient | None = None
        self._warned = False

    def _load_creds(self) -> None:
        # Imported lazily so environments without google-auth (and with push
        # disabled) don't need the dependency at import time.
        from google.oauth2 import service_account  # type: ignore

        info = json.loads(self._raw)
        self._project_id = info.get("project_id")
        self._creds = service_account.Credentials.from_service_account_info(
            info, scopes=[_SCOPE]
        )

    async def _access_token(self) -> str:
        if self._creds is None:
            self._load_creds()
        # google-auth refresh is synchronous (hits the token endpoint) — run it
        # off the event loop. It self-caches until expiry, so this is cheap.
        if not self._creds.valid:
            from google.auth.transport.requests import Request  # type: ignore

            await asyncio.to_thread(self._creds.refresh, Request())
        return self._creds.token

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0))
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(
        self,
        tokens: list[str],
        *,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> list[str]:
        """Send one notification to every token. Returns the tokens that should
        be pruned (dead/invalid). No-op (returns []) when disabled."""
        if not self.enabled:
            if not self._warned:
                logger.info("FCM disabled (no service account) — skipping push")
                self._warned = True
            return []
        if not tokens:
            return []
        try:
            access = await self._access_token()
        except Exception as exc:  # bad service account, network, etc.
            logger.error("FCM auth failed: %s", exc)
            return []

        url = f"https://fcm.googleapis.com/v1/projects/{self._project_id}/messages:send"
        headers = {"Authorization": f"Bearer {access}", "Content-Type": "application/json"}
        client = self._get_client()
        to_prune: list[str] = []

        for token in tokens:
            message = {
                "message": {
                    "token": token,
                    "notification": {"title": title, "body": body},
                    "data": {k: str(v) for k, v in (data or {}).items()},
                    "android": {"priority": "high"},
                }
            }
            try:
                r = await client.post(url, headers=headers, json=message)
                parsed = None
                try:
                    parsed = r.json()
                except Exception:
                    parsed = None
                verdict = classify_send(r.status_code, parsed)
                if verdict == "prune":
                    to_prune.append(token)
                elif verdict == "error":
                    logger.warning("FCM send %s: %s", r.status_code, (r.text or "")[:200])
            except Exception as exc:
                logger.warning("FCM send failed: %s", exc)
        return to_prune
