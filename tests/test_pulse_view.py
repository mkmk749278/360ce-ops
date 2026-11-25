"""Pulse: tiles instead of two raw JSON blocks (outside audit, 2026-11-25).

Three things are pinned here:

- an unreadable payload renders em-dashes with a named cause, never zeros —
  including the engine's own synthetic ``mode="off"`` fallback, which says so
  with ``readable: false`` since engine 2026-11-25;
- liveness leads with what needs a look;
- every key the view reads is one the engine's REAL schemas publish, so a
  rename fails CI instead of leaving a tile at "—" forever.
"""
from __future__ import annotations

import os
import sys

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.pulse_view import liveness_view, pulse_view  # noqa: E402
from tests.engine_repo import ABSENT as ENGINE_ABSENT  # noqa: E402
from tests.engine_repo import ENGINE_REPO  # noqa: E402

PULSE = {
    "status": "Healthy", "mode": "paper", "regime": "QUIET",
    "regime_pct_trending": 12.5, "today_pnl_usd": -4.2, "today_pnl_pct": -0.42,
    "daily_loss_budget_usd": 30.0, "daily_loss_used_usd": 18.0,
    "open_positions": 0, "signals_today": 10, "uptime_seconds": 7300.0,
    "scanning_pairs": 129,
}
AUTO = {
    "mode": "paper", "open_positions": 0, "daily_pnl_usd": 0.0,
    "daily_loss_pct": 0.0, "daily_kill_tripped": False, "manual_paused": False,
    "current_equity_usd": 1000.0,
}


def test_a_reading_becomes_tiles():
    v = pulse_view(PULSE, AUTO)
    assert v["pulse_error"] is None and v["auto_error"] is None
    assert v["status_tone"] == "ok"
    assert v["mode"] == "paper"
    assert v["budget_frac"] == pytest.approx(0.6)
    assert v["budget_tone"] == "warn"
    assert v["pnl_tone"] == "neg"
    assert v["equity"] == 1000.0


def test_a_transport_failure_is_unreadable_not_zero():
    v = pulse_view({"endpoint": "/api/pulse", "error": ""}, AUTO)
    assert v["pulse_error"]                     # a blank error still names itself
    assert v["signals_today"] is None           # em-dash, never 0
    assert v["budget_frac"] is None


def test_the_engine_placeholder_is_not_read_as_off():
    """/api/auto-mode returns mode="off" with zeros when it cannot build the
    status. That payload must not render as an engine that is off."""
    placeholder = dict(AUTO, mode="off", current_equity_usd=0.0, readable=False)
    v = pulse_view(PULSE, placeholder)
    assert v["auto_error"]
    assert v["mode"] == "paper"                 # falls back to /api/pulse's own
    assert v["equity"] is None


def test_an_older_engine_without_readable_is_read_as_before():
    assert pulse_view(PULSE, AUTO)["auto_error"] is None


def test_liveness_leads_with_what_needs_a_look():
    lv = liveness_view({
        "features": {
            "a_ok": {"status": "ok", "detail": "x", "streak": 0},
            "b_unknown": {"status": "unknown", "detail": "probe read failed", "streak": 0},
            "c_violating": {"status": "violating", "detail": "flat", "streak": 9},
            "d_violating": {"status": "violating", "detail": "flat", "streak": 2},
        },
        "alerts": [{"feature": "c_violating", "detail": "flat", "streak": 9}],
    })
    assert [r["name"] for r in lv["needs_look"]] == ["c_violating", "d_violating", "b_unknown"]
    assert lv["n_ok"] == 1
    assert len(lv["all"]) == 4


def test_an_unreadable_manifest_says_so():
    assert liveness_view({"error": "missing: feature_liveness.json"})["state"] == "unreadable"
    assert liveness_view(None)["state"] == "unreadable"


def test_the_page_renders_tiles_and_hides_the_json():
    from app.main import app

    with TestClient(app) as c:
        c.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
        body = c.get("/").text
        partial = c.get("/_partial/pulse").text
    assert 'class="tiles"' in body
    # The raw payload is still reachable, but behind an explicit toggle.
    assert "Inspect raw JSON" in body
    assert 'hx-get="/_partial/pulse"' in partial and 'class="tiles"' in partial
    # The engine is unreachable in tests, so the tiles must say so rather
    # than render zeros.
    assert "UNREADABLE" in body


def _engine_fields(name: str) -> set[str]:
    if not ENGINE_REPO.exists():
        pytest.skip(ENGINE_ABSENT)
    sys.path.insert(0, str(ENGINE_REPO))
    try:
        from src.api import schemas  # type: ignore
        return set(getattr(schemas, name).model_fields)
    finally:
        sys.path.remove(str(ENGINE_REPO))


def test_every_pulse_key_the_view_reads_is_published_by_the_engine():
    assert set(PULSE) <= _engine_fields("PulseSnapshot")


def test_every_auto_mode_key_the_view_reads_is_published_by_the_engine():
    fields = _engine_fields("AutoModeStatus")
    assert set(AUTO) <= fields
    assert "readable" in fields, "the placeholder flag this view keys on"
