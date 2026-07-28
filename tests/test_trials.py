"""Tests for the signup free-trial panel (360-v2 ships the trial 2026-07-25).

Engine client monkeypatched, per the control-plane test convention.

The property this page exists to protect: while the engine's user-visible
flag is off, the panel must say so loudly and must never let a cohort of
measured-but-never-offered users read as an offer that is performing badly.
A rate with an empty denominator renders "not measured yet", not 0%.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.main import app  # noqa: E402
from app.routes.trials import classify, days_left, phase  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


# Frozen clock for the REDUCER tests. Those call classify/days_left/phase with
# an explicit ``now=_NOW``, so a fixed date is exactly right there: the input and
# the clock move together and the assertion is deterministic forever.
_NOW = datetime(2026, 7, 25, 12, 0, tzinfo=timezone.utc)


def _iso(delta: timedelta) -> str:
    return (_NOW + delta).isoformat()


# Wall clock for the ROUTE tests, and the distinction is not cosmetic.
#
# The /trials route renders through ``classify(row)`` with no ``now`` injected,
# so it reads ``datetime.now(timezone.utc)`` — the real one. Building its
# fixtures off the frozen ``_NOW`` gave the "still running" trial an
# ``expires_at`` of 2026-07-28T12:00Z, which meant the row silently reclassified
# itself from active to lapsed the moment real time passed that instant. The
# test went red on **2026-07-28 at 12:00 UTC** on a date, not on a commit: the
# last push to main before that was green and every run after it fails.
#
# A fixture whose meaning depends on when the suite happens to run is a time
# bomb, and the failure names the wrong thing when it goes off — it reads as a
# broken trials page. Anchor anything the route classifies to the real clock so
# "3 days from now" stays 3 days from now.
_REAL_NOW = datetime.now(timezone.utc)


def _riso(delta: timedelta) -> str:
    return (_REAL_NOW + delta).isoformat()


_DARK_FUNNEL = {
    "offer_live": False,
    "measuring": True,
    "days": 7,
    "tier": "auto",
    "max_account_age_days": 0,
    "summary": {
        "cohort": 3, "cohort_dark": 3, "cohort_live": 0,
        "offered": 0, "claimed": 0, "active": 0, "lapsed": 0, "converted": 0,
        "claim_rate": None, "conversion_rate": None,
    },
    "trials": [
        {
            "user_id": 5, "tier": "auto", "days": 7,
            "eligible_at": _riso(timedelta(days=-2)),
            "offered_at": None, "claimed_at": None, "expires_at": None,
            "converted_at": None, "shadow": 1,
        },
    ],
}

_LIVE_FUNNEL = {
    "offer_live": True,
    "measuring": True,
    "days": 7,
    "tier": "auto",
    "max_account_age_days": 7,
    "summary": {
        "cohort": 10, "cohort_dark": 3, "cohort_live": 7,
        "offered": 8, "claimed": 4, "active": 3, "lapsed": 1, "converted": 1,
        "claim_rate": 0.5, "conversion_rate": 0.25,
    },
    "trials": [
        {
            "user_id": 5, "tier": "auto", "days": 7,
            "eligible_at": _riso(timedelta(days=-6)),
            "offered_at": _riso(timedelta(days=-6)),
            "claimed_at": _riso(timedelta(days=-4)),
            "expires_at": _riso(timedelta(days=3)),
            "converted_at": None, "shadow": 0,
        },
    ],
}


# ---------------------------------------------------------------------------
# Reducers
# ---------------------------------------------------------------------------


def test_classify_puts_conversion_above_a_still_running_window():
    row = {
        "claimed_at": _iso(timedelta(days=-2)),
        "expires_at": _iso(timedelta(days=5)),
        "converted_at": _iso(timedelta(days=-1)),
    }
    assert classify(row, now=_NOW) == "converted"


def test_classify_distinguishes_running_from_lapsed():
    running = {
        "claimed_at": _iso(timedelta(days=-2)),
        "expires_at": _iso(timedelta(days=5)),
    }
    lapsed = {
        "claimed_at": _iso(timedelta(days=-9)),
        "expires_at": _iso(timedelta(days=-2)),
    }
    assert classify(running, now=_NOW) == "active"
    assert classify(lapsed, now=_NOW) == "lapsed"


def test_classify_never_calls_a_dark_row_offered():
    """The whole point of shadow=1: those users were counted, not offered."""
    row = {"offered_at": None, "claimed_at": None, "shadow": 1}
    assert classify(row, now=_NOW) == "dark"


def test_days_left_rounds_up_and_is_none_when_unclaimed():
    claimed = {
        "claimed_at": _iso(timedelta(days=-5)),
        "expires_at": _iso(timedelta(days=2, hours=3)),
    }
    assert days_left(claimed, now=_NOW) == 3
    assert days_left({"claimed_at": None}, now=_NOW) is None
    assert days_left(
        {"claimed_at": _iso(timedelta(days=-9)),
         "expires_at": _iso(timedelta(days=-1))}, now=_NOW,
    ) is None


def test_phase_separates_dark_from_blind():
    assert phase({"measuring": True, "offer_live": True}) == "live"
    assert phase({"measuring": True, "offer_live": False}) == "dark"
    assert phase({"measuring": False, "offer_live": False}) == "blind"


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------


def test_trials_page_requires_auth():
    with TestClient(app) as client:
        r = client.get("/trials", follow_redirects=False)
        assert r.status_code in (302, 303, 307)


def test_dark_phase_is_stated_and_rates_are_not_faked(monkeypatch):
    async def fake(self, limit=200):
        return _DARK_FUNNEL

    monkeypatch.setattr(EngineApiClient, "trial_funnel", fake)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/trials")
        assert r.status_code == 200
        assert "DARK" in r.text
        assert "none of them has been shown an offer" in r.text
        # An offer that never ran must not look like one that is failing.
        assert "not measured yet" in r.text
        assert "0.0%" not in r.text


def test_no_age_limit_warns_about_the_existing_free_base(monkeypatch):
    async def fake(self, limit=200):
        return _DARK_FUNNEL

    monkeypatch.setattr(EngineApiClient, "trial_funnel", fake)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/trials")
        assert "SIGNUP_TRIAL_MAX_ACCOUNT_AGE_DAYS" in r.text
        assert "every never-paid free user is eligible" in r.text


def test_live_phase_renders_the_funnel(monkeypatch):
    async def fake(self, limit=200):
        return _LIVE_FUNNEL

    monkeypatch.setattr(EngineApiClient, "trial_funnel", fake)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/trials")
        assert r.status_code == 200
        assert "LIVE" in r.text
        assert "50.0%" in r.text  # claim rate
        assert "25.0%" in r.text  # conversion
        assert "RUNNING" in r.text


def test_engine_read_failure_renders_instead_of_crashing(monkeypatch):
    async def fake(self, limit=200):
        return {"error": "connect timeout"}

    monkeypatch.setattr(EngineApiClient, "trial_funnel", fake)
    with TestClient(app) as client:
        _login(client)
        r = client.get("/trials")
        assert r.status_code == 200
        assert "connect timeout" in r.text
