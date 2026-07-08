"""Owner app-token store for the native ops app.

The native Android app authenticates *to ops* (not to the engine) and holds
only an **ops-issued token** — never the engine's owner-tier Bearer, which
must never ship inside a sideloaded, extractable APK (see
``docs/OPS_MOBILE_APP_PLAN.md``). The flow:

1. Owner enters the ops password once in the app.
2. Ops verifies it against ``OPS_AUTH_TOKEN`` and mints a random app-token.
3. The app stores the token behind biometric unlock and sends it as
   ``Authorization: Bearer <token>`` on every ``/api/v1`` request.

Design:

* **Only SHA-256 hashes are persisted** — never the raw token. A leaked store
  file can't be replayed against the API.
* **File-backed**, on the same writable volume as the audit log, so tokens
  survive a container restart (the owner isn't forced to re-login on every
  deploy).
* **Revoke-all** is the "lost phone" switch — it clears every issued token in
  one call.

This is owner-only, single-tenant by construction (matches the ops hard limit:
no multi-user). Every token minted is owner-tier; there are no scopes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("ops.app_tokens")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AppTokenStore:
    """Thread-safe, file-backed store of hashed app-tokens."""

    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        # hash -> {"created_at", "label", "last_used"}
        self._tokens: dict[str, dict[str, Any]] = self._load()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                # Defensive: only keep well-shaped entries.
                return {
                    str(k): v
                    for k, v in data.items()
                    if isinstance(v, dict)
                }
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _save(self) -> None:
        """Atomically write the store — temp file + rename so a crash mid-write
        can't corrupt the token file. Best-effort: a write failure is logged,
        never raised (an in-memory token still works until restart)."""
        try:
            parent = os.path.dirname(self._path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=parent or ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self._tokens, fh, separators=(",", ":"))
                os.replace(tmp, self._path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as exc:
            logger.error("app-token store write failed: %s", exc)

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------
    def issue(self, label: str = "") -> str:
        """Mint a new random app-token, persist its hash, return the raw token
        (the only time it is ever available in plaintext)."""
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[_hash(token)] = {
                "created_at": _now(),
                "label": label or "ops-app",
                "last_used": None,
            }
            self._save()
        return token

    def verify(self, token: str) -> bool:
        """Return True if the token is known. Updates ``last_used`` on hit."""
        if not token:
            return False
        h = _hash(token)
        with self._lock:
            meta = self._tokens.get(h)
            if meta is None:
                return False
            meta["last_used"] = _now()
            self._save()
            return True

    def revoke_all(self) -> int:
        """Revoke every issued token (the lost-phone switch). Returns the count
        of tokens cleared."""
        with self._lock:
            n = len(self._tokens)
            self._tokens.clear()
            self._save()
            return n

    def count(self) -> int:
        with self._lock:
            return len(self._tokens)
