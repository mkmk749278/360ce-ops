"""Binance kline ban-circuit — the pure backoff gate.

Tested without httpx/network: the breaker only ever reasons about a response
body + monotonic time, so a plain string body exercises the whole thing. Pins
the two rules that make it safe: it *extends* (never shrinks) an open window,
and it clears at Binance's real ``banned until`` deadline when one is present.
"""
from __future__ import annotations

import time

from app.data_sources.binance_klines import _BanCircuit


def test_starts_closed() -> None:
    c = _BanCircuit(cooldown_sec=120.0, max_sec=1800.0)
    assert c.open is False
    assert c.seconds_remaining() == 0.0


def test_fallback_cooldown_when_no_deadline() -> None:
    # A 429-style body with no "banned until" → fixed fallback cooldown.
    c = _BanCircuit(cooldown_sec=120.0, max_sec=1800.0)
    c.note_ban("Way too many requests")
    assert c.open is True
    # ~120s (allow scheduling slack).
    assert 110.0 <= c.seconds_remaining() <= 121.0


def test_parses_banned_until_deadline() -> None:
    c = _BanCircuit(cooldown_sec=120.0, max_sec=1800.0)
    now = 1_000_000.0
    ban_ms = int((now + 600.0) * 1000)  # 10 min out
    c.note_ban(
        f"Way too many requests; IP(1.2.3.4) banned until {ban_ms}.",
        now_wall=now,
    )
    assert c.open is True
    # deadline (600s) + 5s guard, well above the 120s fallback.
    assert 590.0 <= c.seconds_remaining() <= 610.0


def test_deadline_clamped_to_max() -> None:
    c = _BanCircuit(cooldown_sec=120.0, max_sec=300.0)
    now = 2_000_000.0
    ban_ms = int((now + 99_999.0) * 1000)  # absurdly long ban
    c.note_ban(f"banned until {ban_ms}", now_wall=now)
    # Never held open beyond max_sec — we re-probe instead of trusting it.
    assert c.seconds_remaining() <= 300.0 + 1.0


def test_past_deadline_falls_back_to_cooldown() -> None:
    c = _BanCircuit(cooldown_sec=90.0, max_sec=1800.0)
    now = 3_000_000.0
    ban_ms = int((now - 50.0) * 1000)  # already expired
    c.note_ban(f"banned until {ban_ms}", now_wall=now)
    # Expired deadline → fixed fallback, still opens (avoids instant re-hammer).
    assert c.open is True
    assert 80.0 <= c.seconds_remaining() <= 91.0


def test_window_only_extends_never_shrinks() -> None:
    c = _BanCircuit(cooldown_sec=120.0, max_sec=1800.0)
    now = 4_000_000.0
    c.note_ban(f"banned until {int((now + 600.0) * 1000)}", now_wall=now)
    far = c.seconds_remaining()
    # A subsequent nearer ban must not reopen the door early.
    c.note_ban(f"banned until {int((now + 30.0) * 1000)}", now_wall=now)
    assert c.seconds_remaining() >= far - 1.0


def test_closes_once_window_lapses() -> None:
    # The gate is monotonic-time based, not permanent: once the window passes,
    # the next probe sees it closed (half-open) and can re-hit Binance.
    c = _BanCircuit(cooldown_sec=120.0, max_sec=1800.0)
    c.note_ban("Way too many requests")
    assert c.open is True
    c._open_until = time.monotonic() - 1.0  # simulate the window having lapsed
    assert c.open is False
    assert c.seconds_remaining() == 0.0
