"""Machine tokens — the analyst's door into ops (2026-11-19, owner-approved).

The hourly AI analyst runs as a scheduled Claude Code session. It reaches the
engine through ops' MCP server (``app/routes/mcp.py``) holding a **machine
token**: a Bearer credential minted and revoked on Control → Access.

What a machine token can do is fixed by ``app/routes/mcp.py``'s tool table and
nothing else: a handful of named engine catalog READS and exactly one catalog
ACTION, ``action.analyst_submit_plan``. That action validates a plan and
appends it to the engine's analyst ledger; nothing it does reaches a
subscriber, the signal queue or an order (asserted engine-side by AST, per
catalog entry). There is deliberately no scope parameter: one token kind, one
tool table, so the blast radius is argued once.

Same storage rules as ``guest_access.py`` and ``app_tokens.py``:

* only the SHA-256 hash is persisted, and the token is shown once at mint;
* revocation and expiry are checked on **every request**;
* ``uses`` / ``last_used`` / ``denials`` ride with the record, so the Access
  page says what a token has been doing rather than only that it exists.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import tempfile
import threading
import time
from typing import Any

logger = logging.getLogger("ops.machine_tokens")

PREFIX = "lmt_"
TTL_CHOICES: list[tuple[str, str, int]] = [
    ("7d", "7 days", 7 * 86400),
    ("30d", "30 days", 30 * 86400),
    ("90d", "90 days", 90 * 86400),
]
TTLS = {k: s for k, _l, s in TTL_CHOICES}


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class MachineTokenStore:
    def __init__(self, path: str) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._recs: dict[str, dict[str, Any]] = self._load()

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self._path, encoding="utf-8") as fh:
                data = json.load(fh)
            recs = data.get("tokens", {}) if isinstance(data, dict) else {}
            return {k: v for k, v in recs.items() if isinstance(v, dict)}
        except FileNotFoundError:
            return {}
        except Exception:  # noqa: BLE001 - unreadable store = no tokens, loudly
            logger.exception("machine token store unreadable: %s", self._path)
            return {}

    def _save(self) -> None:
        d = os.path.dirname(self._path) or "."
        os.makedirs(d, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=d, prefix=".mt-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"schema": 1, "tokens": self._recs}, fh)
            os.replace(tmp, self._path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def issue(self, *, label: str, ttl_sec: float) -> tuple[str, str]:
        token = PREFIX + secrets.token_urlsafe(32)
        tid = secrets.token_hex(6)
        now = time.time()
        with self._lock:
            self._recs[_hash(token)] = {
                "id": tid, "label": (label or "analyst").strip()[:64] or "analyst",
                "created_at": now, "expires_at": now + float(ttl_sec),
                "revoked_at": None, "uses": 0, "last_used": None, "denials": 0,
            }
            self._save()
        return token, tid

    def verify(self, token: str) -> dict[str, Any] | None:
        """The live record for *token*, touched, or None. Checked every call."""
        if not token or not token.startswith(PREFIX):
            return None
        h = _hash(token)
        now = time.time()
        with self._lock:
            rec = self._recs.get(h)
            if rec is None or rec.get("revoked_at") or now >= float(rec["expires_at"]):
                return None
            rec["uses"] = int(rec.get("uses", 0)) + 1
            rec["last_used"] = now
            try:
                self._save()
            except Exception:  # noqa: BLE001 - a counter must not break auth
                logger.exception("machine token counter not persisted")
            return dict(rec)

    def record_denial(self, token_id: str) -> None:
        with self._lock:
            for rec in self._recs.values():
                if rec.get("id") == token_id:
                    rec["denials"] = int(rec.get("denials", 0)) + 1
                    self._save()
                    return

    def revoke(self, token_id: str) -> bool:
        with self._lock:
            for rec in self._recs.values():
                if rec.get("id") == token_id and not rec.get("revoked_at"):
                    rec["revoked_at"] = time.time()
                    self._save()
                    return True
        return False

    def list_tokens(self) -> list[dict[str, Any]]:
        now = time.time()
        out = []
        for rec in self._recs.values():
            state = ("revoked" if rec.get("revoked_at")
                     else "expired" if now >= float(rec["expires_at"]) else "live")
            out.append({**rec, "state": state,
                        "seconds_remaining": max(0, int(float(rec["expires_at"]) - now))})
        return sorted(out, key=lambda r: -float(r["created_at"]))
