"""The thin status strip under the header, on every page (outside audit,
2026-11-25: degraded feeds surfaced as broken tables on whichever page you
happened to be on, with nothing site-wide saying so).

It shows only what is free to know on every request:

- the Binance REST breaker, which lives in this process — when it is open,
  every page that replays candles is degraded, not just the one you are on;
- the engine's SUSTAINED liveness alerts, read from feature_liveness.json on a
  short cache (the file is rewritten every 5 minutes, so 30s costs nothing).

Nothing here calls the engine: a strip that adds an engine round trip to
every page would make the panel slower exactly when the engine is struggling.
An unreadable liveness file adds no item — the Pulse page grades that state;
the strip only raises what is positively known to be wrong.
"""
from __future__ import annotations

import time
from typing import Any

_CACHE: dict[str, Any] = {"at": 0.0, "alerts": []}
_TTL_S = 30.0


def _liveness_alerts(state: Any) -> list[str]:
    now = time.monotonic()
    if now - _CACHE["at"] < _TTL_S:
        return _CACHE["alerts"]
    alerts: list[str] = []
    try:
        lv = state.data_volume.feature_liveness()
        if isinstance(lv, dict) and isinstance(lv.get("alerts"), list):
            alerts = [str(a.get("feature")) for a in lv["alerts"] if isinstance(a, dict)]
    except Exception:  # noqa: BLE001 — a strip must never break a page
        alerts = []
    _CACHE.update(at=now, alerts=alerts)
    return alerts


def status_strip(request: Any) -> list[dict[str, str]]:
    state = getattr(getattr(request, "app", None), "state", None)
    if state is None:
        return []
    items: list[dict[str, str]] = []
    klines = getattr(state, "binance_klines", None)
    if klines is not None and getattr(klines, "circuit_open", False):
        mins = int(getattr(klines, "ban_seconds_remaining", 0) // 60) + 1
        items.append({
            "tone": "bad",
            "text": f"Binance REST cooldown — candle replays paused, ~{mins} min left. "
                    "Held-to-stop and Dark signals show degraded rows until it lifts.",
            "href": "/dark-signals",
        })
    if getattr(state, "data_volume", None) is not None:
        alerts = _liveness_alerts(state)
        if alerts:
            shown = ", ".join(alerts[:3]) + (f" +{len(alerts) - 3} more" if len(alerts) > 3 else "")
            items.append({
                "tone": "warn",
                "text": f"{len(alerts)} sustained liveness alert{'s' if len(alerts) != 1 else ''}: {shown}",
                "href": "/",
            })
    return items
