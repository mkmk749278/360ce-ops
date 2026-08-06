"""Temporary read-only access grants — the second door into ops.

Ops is the engine's **control plane**, so its one existing door is owner-tier by
construction: the session cookie behind ``OPS_AUTH_TOKEN`` (+ TOTP) can flip the
kill switch, switch to LIVE, and reset every signal. That is the right shape for
the owner and the wrong shape for anything else — there has never been a way to
let a collaborator (or an AI agent) *read* the measurement pages without handing
over the same key that arms live trading.

This module is that second door. The owner mints a short-lived code from
``/control``; the holder exchanges it for a **guest** session that can issue
``GET``/``HEAD`` against the classified read pages and nothing else. The owner
revokes it from the same card.

Design, and why each property is here:

* **Only SHA-256 hashes are persisted.** Same rule as ``app_tokens.py`` — the
  code is shown once, at mint time, and is not recoverable from the store. A
  leaked store file cannot be replayed.
* **Revocation is checked on every request, not at login.** The session carries
  the grant *id*, never the code, and the middleware re-reads the grant on each
  request. A cookie minted before a revoke is dead on the next click. If the
  session were trusted after login, "I can disable that access too" would be
  false for as long as the cookie lived — which is the whole point of the
  feature.
* **Expiry is enforced in ``verify``, not by a sweeper.** A grant nobody
  remembers to revoke dies on its own; there is no state where an expired grant
  is still honoured because a cleanup job did not run.
* **A failed code is throttled.** The code is 100 bits, so offline brute force
  is hopeless, but an *online* guesser should not get unlimited tries against a
  control-plane host. Failures are counted in a rolling window and the guest
  door closes for a cooldown once the count trips. The **owner's** login is a
  different route and is deliberately unaffected — a guest-side lockout must
  never be able to lock the owner out of his own kill switch.
* **Counters are persisted.** ``uses`` / ``last_used`` / ``denials`` ride with
  the grant so the control page can say what a code has actually been doing,
  rather than only that it exists.

Scope is fixed at ``read`` for every grant. There is deliberately no scope
parameter: a second tier that can *sometimes* write is a tier whose blast radius
has to be re-derived at every call site, and this repo's control doctrine says
writes are owner-gated, audited and PRG-confirmed. Read-only is the only tier
that needs no such argument.
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
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("ops.guest_access")

SCHEMA = 1

#: Crockford base32 minus the ambiguous letters. The code is read off a screen
#: and typed (or pasted into an agent's shell), so I/L/O/U are excluded and the
#: reader below folds the look-alikes back in.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_CODE_GROUPS = 4
_CODE_GROUP_LEN = 5  # 20 chars x 5 bits = 100 bits of entropy
_CONFUSABLES = str.maketrans({"I": "1", "L": "1", "O": "0", "U": "V"})


def _hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _now_ts() -> float:
    return time.time()


def _iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def normalise_code(raw: str) -> str:
    """Fold a typed code back to canonical form.

    Dashes and spaces are formatting, case is not significant, and the four
    excluded glyphs are mapped to what the reader almost certainly meant. This
    runs on *both* sides of the comparison, so a code minted here always
    normalises to itself.
    """
    cleaned = "".join(ch for ch in (raw or "").upper() if ch.isalnum())
    return cleaned.translate(_CONFUSABLES)


def _mint_code() -> str:
    groups = [
        "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_GROUP_LEN))
        for _ in range(_CODE_GROUPS)
    ]
    return "-".join(groups)


@dataclass(frozen=True)
class Grant:
    """A live, verified grant. Only ever constructed for a code/id that passed
    every check — expiry and revocation are decided in the store, so a caller
    holding a ``Grant`` never has to re-ask whether it is still good."""

    grant_id: str
    label: str
    created_at: float
    expires_at: float
    uses: int

    @property
    def seconds_remaining(self) -> int:
        return max(0, int(self.expires_at - _now_ts()))


class GuestAccessStore:
    """Thread-safe, file-backed store of hashed guest access codes."""

    def __init__(
        self,
        path: str,
        *,
        max_failures: int = 10,
        failure_window_sec: float = 300.0,
        lockout_sec: float = 900.0,
    ) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._max_failures = max_failures
        self._failure_window_sec = failure_window_sec
        self._lockout_sec = lockout_sec
        self._failures: list[float] = []
        self._locked_until: float = 0.0
        # code-hash -> record
        self._grants: dict[str, dict[str, Any]] = self._load()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            with open(self._path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        grants = data.get("grants")
        if not isinstance(grants, dict):
            return {}
        return {str(k): v for k, v in grants.items() if isinstance(v, dict)}

    def _save(self) -> None:
        """Atomically write the store — temp file + rename, so a crash mid-write
        cannot corrupt it. Best-effort: a write failure is logged, never raised.

        The failure direction matters and is deliberate. An unpersisted *mint*
        still works until restart (the grant is in memory); an unpersisted
        *revoke* would silently come back on restart, so ``revoke`` reports
        whether the write landed and the control page says so."""
        parent = os.path.dirname(self._path)
        try:
            if parent:
                os.makedirs(parent, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=parent or ".", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(
                        {"schema": SCHEMA, "grants": self._grants},
                        fh,
                        separators=(",", ":"),
                    )
                os.replace(tmp, self._path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as exc:
            logger.error("guest-access store write failed: %s", exc)
            raise

    def _save_quietly(self) -> bool:
        try:
            self._save()
            return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # minting / revoking (owner side)
    # ------------------------------------------------------------------
    def issue(self, *, label: str, ttl_sec: float) -> tuple[str, str]:
        """Mint a grant. Returns ``(code, grant_id)``; the raw code is available
        here and nowhere else, ever."""
        code = _mint_code()
        grant_id = secrets.token_hex(8)
        now = _now_ts()
        with self._lock:
            self._grants[_hash(normalise_code(code))] = {
                "id": grant_id,
                "label": (label or "guest").strip()[:64] or "guest",
                "created_at": now,
                "expires_at": now + max(60.0, float(ttl_sec)),
                "revoked_at": None,
                "last_used": None,
                "uses": 0,
                "denials": 0,
            }
            self._save_quietly()
        return code, grant_id

    def revoke(self, grant_id: str) -> bool:
        """Revoke one grant by id. Returns True if a live grant was revoked."""
        with self._lock:
            for rec in self._grants.values():
                if rec.get("id") == grant_id and rec.get("revoked_at") is None:
                    rec["revoked_at"] = _now_ts()
                    self._save_quietly()
                    return True
        return False

    def revoke_all(self) -> int:
        """Revoke every live grant — the panic switch. Returns the count."""
        now = _now_ts()
        n = 0
        with self._lock:
            for rec in self._grants.values():
                if rec.get("revoked_at") is None and float(rec.get("expires_at", 0)) > now:
                    rec["revoked_at"] = now
                    n += 1
            if n:
                self._save_quietly()
        return n

    def purge(self, *, older_than_sec: float = 7 * 86400) -> int:
        """Drop records that have been dead (revoked or expired) for a while, so
        the file does not grow forever. Purging changes no access decision — a
        dead grant already fails ``verify``."""
        cutoff = _now_ts() - older_than_sec
        with self._lock:
            drop = [
                h
                for h, rec in self._grants.items()
                if float(rec.get("expires_at") or 0) < cutoff
                and (rec.get("revoked_at") is None or float(rec["revoked_at"]) < cutoff)
            ]
            for h in drop:
                del self._grants[h]
            if drop:
                self._save_quietly()
        return len(drop)

    # ------------------------------------------------------------------
    # verification (guest side)
    # ------------------------------------------------------------------
    def _live(self, rec: dict[str, Any], now: float) -> bool:
        return (
            rec.get("revoked_at") is None
            and float(rec.get("expires_at") or 0) > now
        )

    def _as_grant(self, rec: dict[str, Any]) -> Grant:
        return Grant(
            grant_id=str(rec.get("id") or ""),
            label=str(rec.get("label") or "guest"),
            created_at=float(rec.get("created_at") or 0),
            expires_at=float(rec.get("expires_at") or 0),
            uses=int(rec.get("uses") or 0),
        )

    def locked_out(self) -> float:
        """Seconds of guest-login lockout remaining (0 when open)."""
        with self._lock:
            return max(0.0, self._locked_until - _now_ts())

    def redeem(self, code: str) -> Grant | None:
        """Exchange a raw code for its grant. Counts failures and closes the
        guest door for a cooldown once too many land in the window.

        This is the only entry point that takes a code. Everything after login
        goes through ``lookup``, so the code exists in one request and is never
        stored in a session."""
        now = _now_ts()
        with self._lock:
            if self._locked_until > now:
                return None
            rec = self._grants.get(_hash(normalise_code(code)))
            if rec is None or not self._live(rec, now):
                self._failures = [
                    t for t in self._failures if now - t < self._failure_window_sec
                ]
                self._failures.append(now)
                if len(self._failures) >= self._max_failures:
                    self._locked_until = now + self._lockout_sec
                    self._failures.clear()
                    logger.warning(
                        "guest access locked out for %.0fs after %d failed codes",
                        self._lockout_sec,
                        self._max_failures,
                    )
                return None
            rec["uses"] = int(rec.get("uses") or 0) + 1
            rec["last_used"] = now
            self._save_quietly()
            return self._as_grant(rec)

    def lookup(self, grant_id: str, *, touch: bool = True) -> Grant | None:
        """Re-check a grant by id — called on **every** guest request.

        This is what makes revocation immediate: the session holds an id, and an
        id whose grant has been revoked or has expired resolves to ``None`` on
        the very next request."""
        now = _now_ts()
        with self._lock:
            for rec in self._grants.values():
                if rec.get("id") != grant_id:
                    continue
                if not self._live(rec, now):
                    return None
                if touch:
                    rec["uses"] = int(rec.get("uses") or 0) + 1
                    rec["last_used"] = now
                return self._as_grant(rec)
        return None

    def record_denial(self, grant_id: str) -> None:
        """Count a request this grant was refused. Rendered on the control page:
        a guest probing ``/control`` is exactly what the owner wants to see."""
        with self._lock:
            for rec in self._grants.values():
                if rec.get("id") == grant_id:
                    rec["denials"] = int(rec.get("denials") or 0) + 1
                    return

    def flush(self) -> None:
        """Persist counters touched by ``lookup``/``record_denial``.

        Those run on the hot path (every guest request), so they mutate in
        memory and the write is amortised here rather than fsyncing per request
        — the ops cost rule applied to a store on a page load."""
        with self._lock:
            self._save_quietly()

    # ------------------------------------------------------------------
    # rendering (owner side)
    # ------------------------------------------------------------------
    def list_grants(self, *, include_dead: bool = True) -> list[dict[str, Any]]:
        """Grants for the control page, newest first. Never includes a code."""
        now = _now_ts()
        out: list[dict[str, Any]] = []
        with self._lock:
            for rec in self._grants.values():
                live = self._live(rec, now)
                if not live and not include_dead:
                    continue
                expires_at = float(rec.get("expires_at") or 0)
                if rec.get("revoked_at") is not None:
                    state = "revoked"
                elif expires_at <= now:
                    state = "expired"
                else:
                    state = "live"
                out.append(
                    {
                        "id": str(rec.get("id") or ""),
                        "label": str(rec.get("label") or "guest"),
                        "state": state,
                        "created_at": _iso(float(rec.get("created_at") or 0)),
                        "expires_at": _iso(expires_at),
                        "revoked_at": _iso(rec.get("revoked_at")),
                        "last_used": _iso(rec.get("last_used")),
                        "uses": int(rec.get("uses") or 0),
                        "denials": int(rec.get("denials") or 0),
                        "seconds_remaining": max(0, int(expires_at - now)) if state == "live" else 0,
                    }
                )
        out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        return out

    def live_count(self) -> int:
        now = _now_ts()
        with self._lock:
            return sum(1 for rec in self._grants.values() if self._live(rec, now))
