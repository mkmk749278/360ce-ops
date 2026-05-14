"""Signals 'Created' column + /positions diag view.

Shipped 2026-05-14 alongside 360-v2 PR #386's WS data-flow watchdog.
Two concerns in one PR because they share the same forensic context: the
operator's view of position state (Created timestamps + per-row
sl_breach_distance_pct) is how we tell apart "engine still alive but
WS-silent for some symbols" from "engine emitting but those signals are
actually invalidating".
"""
from __future__ import annotations

import os
from datetime import datetime, timezone, timedelta

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.routes.signals import (  # noqa: E402
    _format_relative,
    _normalize_signal,
    _parse_iso_timestamp,
)
from app.routes.positions import _classify_row  # noqa: E402


# ---------------------------------------------------------------------------
# Signal normaliser — timestamp now populates Created column
# ---------------------------------------------------------------------------


class TestSignalNormaliserTimestamp:
    def test_timestamp_field_populates_created(self):
        norm = _normalize_signal({
            "signal_id": "sig-1",
            "symbol": "BTCUSDT",
            "timestamp": "2026-05-13T12:54:49+00:00",
        })
        assert norm["created_at_relative"] is not None
        assert "ago" in norm["created_at_relative"]
        # Absolute form ends in UTC for consistency with the Lumin app
        assert "UTC" in (norm["created_at_absolute"] or "")
        # Legacy field kept for back-compat
        assert norm["created_at"] is not None

    def test_dispatch_timestamp_used_when_timestamp_absent(self):
        norm = _normalize_signal({
            "signal_id": "sig-1",
            "symbol": "BTCUSDT",
            "dispatch_timestamp": "2026-05-13T12:55:00+00:00",
        })
        assert norm["created_at_relative"] is not None

    def test_missing_timestamp_falls_back_to_none(self):
        norm = _normalize_signal({"signal_id": "sig-1", "symbol": "BTCUSDT"})
        assert norm["created_at_relative"] is None
        # No timestamp at all → all timestamp fields are None
        assert norm["created_at_absolute"] is None
        assert norm["created_at"] is None

    def test_unparseable_timestamp_preserves_raw(self):
        norm = _normalize_signal({
            "signal_id": "sig-1",
            "symbol": "BTCUSDT",
            "timestamp": "not-a-date",
        })
        # No relative form, but raw is preserved for the template fallback
        assert norm["created_at_relative"] is None
        assert norm["created_at_raw"] == "not-a-date"


class TestParseIsoTimestamp:
    def test_iso_with_z_suffix(self):
        ts = _parse_iso_timestamp("2026-05-13T12:54:49Z")
        assert ts is not None
        assert ts.tzinfo is not None

    def test_iso_with_offset(self):
        ts = _parse_iso_timestamp("2026-05-13T12:54:49+00:00")
        assert ts is not None

    def test_numeric_epoch(self):
        # Pick an epoch we can recompute
        ts = _parse_iso_timestamp(1715600000)
        assert ts is not None
        assert ts.year >= 2024

    def test_none_returns_none(self):
        assert _parse_iso_timestamp(None) is None

    def test_garbage_returns_none(self):
        assert _parse_iso_timestamp("yesterday") is None


class TestFormatRelative:
    """Render timestamps in compact "Xm ago" / "Xh ago" / "Xd ago" form."""

    def test_seconds_ago(self):
        now = datetime.now(timezone.utc)
        assert _format_relative(now - timedelta(seconds=5), now=now) == "5s ago"

    def test_minutes_ago(self):
        now = datetime.now(timezone.utc)
        assert _format_relative(now - timedelta(minutes=18), now=now) == "18m ago"

    def test_hours_ago(self):
        now = datetime.now(timezone.utc)
        assert _format_relative(now - timedelta(hours=5), now=now) == "5h ago"

    def test_days_ago(self):
        now = datetime.now(timezone.utc)
        assert _format_relative(now - timedelta(days=3), now=now) == "3d ago"

    def test_just_now_for_future(self):
        # Clock skew between dashboard pod and engine pod can produce
        # "future" timestamps; render them as "just now" rather than
        # negative time.
        now = datetime.now(timezone.utc)
        assert _format_relative(now + timedelta(seconds=2), now=now) == "just now"

    def test_none_returns_none(self):
        assert _format_relative(None) is None


# ---------------------------------------------------------------------------
# Positions view — row classification + endpoint smoke
# ---------------------------------------------------------------------------


class TestPositionsRowClassification:
    """``_classify_row`` decides which CSS class a row receives based on
    the diag fields shipped in 360-v2 PR #385."""

    def test_active_with_negative_sl_breach_is_sl_breached(self):
        # Wick past SL but status still ACTIVE = monitor-evaluation gap
        row = {"status": "ACTIVE", "sl_breach_distance_pct": -0.28, "candle_1m_age_sec": 5.0}
        assert _classify_row(row) == "sl_breached"

    def test_active_with_zero_sl_breach_is_sl_breached(self):
        # Exactly-at-SL also counts — the engine should have closed it
        row = {"status": "ACTIVE", "sl_breach_distance_pct": 0.0, "candle_1m_age_sec": 5.0}
        assert _classify_row(row) == "sl_breached"

    def test_active_with_positive_sl_breach_and_fresh_candle_is_ok(self):
        row = {"status": "ACTIVE", "sl_breach_distance_pct": 0.42, "candle_1m_age_sec": 5.0}
        assert _classify_row(row) == "ok"

    def test_stale_candle_is_feed_stale(self):
        row = {"status": "ACTIVE", "sl_breach_distance_pct": 0.42, "candle_1m_age_sec": 300.0}
        assert _classify_row(row) == "feed_stale"

    def test_none_candle_age_is_feed_stale(self):
        # No kline ever recorded for this symbol — definitely stale
        row = {"status": "ACTIVE", "sl_breach_distance_pct": 0.5, "candle_1m_age_sec": None}
        assert _classify_row(row) == "feed_stale"

    def test_sl_breached_overrides_feed_stale_priority(self):
        # If both conditions are true, sl_breached wins because it indicates
        # a monitor-evaluation gap that the operator must act on.
        row = {"status": "ACTIVE", "sl_breach_distance_pct": -0.5, "candle_1m_age_sec": 300.0}
        assert _classify_row(row) == "sl_breached"


# ---------------------------------------------------------------------------
# Smoke: /positions renders + degrades gracefully on engine errors
# ---------------------------------------------------------------------------


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


def test_positions_route_requires_auth():
    with TestClient(app) as client:
        r = client.get("/positions", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


def test_positions_route_handles_engine_error_gracefully(monkeypatch):
    """When engine API returns its error-envelope, the page must still render
    (with the error message displayed) rather than 500ing the dashboard."""
    with TestClient(app) as client:
        _login(client)

        async def _fake_diag(self):
            return {"error": "connection refused", "endpoint": "/internal/diag/positions"}

        from app.data_sources.engine_api import EngineApiClient
        monkeypatch.setattr(EngineApiClient, "positions_diag", _fake_diag, raising=True)
        r = client.get("/positions")
        assert r.status_code == 200
        assert "connection refused" in r.text


def test_positions_route_renders_rows(monkeypatch):
    """Happy path — engine returns a diag payload, page shows the rows
    sorted by class (sl_breached first, then feed_stale, then ok)."""
    with TestClient(app) as client:
        _login(client)

        async def _fake_diag(self):
            return {
                "items": [
                    {
                        "signal_id": "ok-1", "symbol": "OKUSDT", "direction": "LONG",
                        "status": "ACTIVE", "setup_class": "SR_FLIP_RETEST",
                        "entry": 100.0, "stop_loss": 99.0,
                        "tp1": 101.0, "tp2": 102.0, "tp3": None,
                        "current_price": 100.5, "pnl_pct": 0.5,
                        "max_favorable_excursion_pct": 0.6, "max_adverse_excursion_pct": -0.1,
                        "best_tp_hit": 0, "pre_tp_hit": False,
                        "candle_1m_high": 100.7, "candle_1m_low": 99.9, "candle_1m_age_sec": 4.0,
                        "sl_breach_distance_pct": 0.9, "minutes_open": 8,
                        "timestamp": "2026-05-13T13:00:00+00:00",
                        "dispatch_timestamp": None,
                        "first_sl_touch_timestamp": None,
                        "first_tp_touch_timestamp": None,
                        "terminal_outcome_timestamp": None,
                    },
                    {
                        "signal_id": "sl-1", "symbol": "AVAXUSDT", "direction": "LONG",
                        "status": "ACTIVE", "setup_class": "DIVERGENCE_CONTINUATION",
                        "entry": 10.0, "stop_loss": 9.9,
                        "tp1": 10.2, "tp2": 10.4, "tp3": None,
                        "current_price": 9.85, "pnl_pct": -1.5,
                        "max_favorable_excursion_pct": 0.0, "max_adverse_excursion_pct": -1.5,
                        "best_tp_hit": 0, "pre_tp_hit": False,
                        "candle_1m_high": 9.95, "candle_1m_low": 9.85, "candle_1m_age_sec": 3.0,
                        "sl_breach_distance_pct": -0.5, "minutes_open": 12,
                        "timestamp": "2026-05-13T13:05:00+00:00",
                        "dispatch_timestamp": None,
                        "first_sl_touch_timestamp": None,
                        "first_tp_touch_timestamp": None,
                        "terminal_outcome_timestamp": None,
                    },
                ],
                "total": 2,
                "monitor_running": True,
                "generated_at": "2026-05-13T13:10:00+00:00",
            }

        from app.data_sources.engine_api import EngineApiClient
        monkeypatch.setattr(EngineApiClient, "positions_diag", _fake_diag, raising=True)
        r = client.get("/positions")
        assert r.status_code == 200
        body = r.text
        # sl-breached row should appear before the ok row in the rendered HTML
        assert body.index("AVAXUSDT") < body.index("OKUSDT")
        # Smoking-gun column is visible with negative value
        assert "-0.500" in body
        # monitor_running propagates
        assert "monitor_running: <strong>yes" in body


def test_positions_route_renders_empty_state(monkeypatch):
    with TestClient(app) as client:
        _login(client)

        async def _fake_diag(self):
            return {
                "items": [], "total": 0,
                "monitor_running": False,
                "generated_at": "2026-05-13T13:10:00+00:00",
            }

        from app.data_sources.engine_api import EngineApiClient
        monkeypatch.setattr(EngineApiClient, "positions_diag", _fake_diag, raising=True)
        r = client.get("/positions")
        assert r.status_code == 200
        assert "No active positions." in r.text
        assert "monitor_running: <strong>NO" in r.text


def test_positions_link_in_nav():
    """Adding the route also means the nav link must be reachable."""
    with TestClient(app) as client:
        _login(client)
        r = client.get("/")
        # Nav appears on every authenticated page
        assert 'href="/positions"' in r.text
