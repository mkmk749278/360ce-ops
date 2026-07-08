"""FCM device-token registry for ops push (Phase 4).

Stores the FCM registration tokens of the owner's device(s) so the monitoring
agent can push alerts to them. Unlike app-tokens (which are secrets and stored
hashed), FCM tokens are *addressing* handles — the agent needs them in
plaintext to send — so they're stored as-is.

Cross-process by design: the **web app** registers/unregisters tokens
(`/api/v1/devices`) while the **monitoring agent** reads them to send. They run
in separate processes sharing the writable volume, so every operation reads the
file fresh (no long-lived in-memory cache) and mutations are read-modify-write
under a lock. The file is tiny (a handful of tokens), so re-reading is cheap.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("ops.devices")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DeviceRegistry:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()

    def _read(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _write(self, tokens: dict[str, dict[str, Any]]) -> None:
        try:
            parent = os.path.dirname(self._path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=parent or ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(tokens, fh, separators=(",", ":"))
                os.replace(tmp, self._path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as exc:
            logger.error("device registry write failed: %s", exc)

    def register(self, token: str, platform: str = "android") -> None:
        if not token:
            return
        with self._lock:
            tokens = self._read()
            existing = tokens.get(token, {})
            tokens[token] = {
                "platform": platform,
                "registered_at": existing.get("registered_at", _now()),
                "last_seen": _now(),
            }
            self._write(tokens)

    def unregister(self, token: str) -> bool:
        with self._lock:
            tokens = self._read()
            if token in tokens:
                del tokens[token]
                self._write(tokens)
                return True
            return False

    def prune(self, invalid: list[str]) -> int:
        """Remove tokens FCM reported as unregistered/invalid. Returns count."""
        if not invalid:
            return 0
        with self._lock:
            tokens = self._read()
            removed = 0
            for t in invalid:
                if t in tokens:
                    del tokens[t]
                    removed += 1
            if removed:
                self._write(tokens)
            return removed

    def all_tokens(self) -> list[str]:
        return list(self._read().keys())

    def count(self) -> int:
        return len(self._read())
