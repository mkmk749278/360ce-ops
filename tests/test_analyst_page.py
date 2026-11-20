"""`/analyst` — the AI analyst's plans and dark outcomes (2026-11-20)."""
from __future__ import annotations

import os
import sys

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import analyst  # noqa: E402
from tests.engine_repo import ABSENT as ENGINE_ABSENT  # noqa: E402
from tests.engine_repo import ENGINE_REPO  # noqa: E402

OUTCOMES = {"ok": True, "error": "", "result": {
    "readable": True, "arms_not_built": ["existing_signals"], "note": "n first",
    "by_arm": {
        "analyst": {"n": 12, "win_rate": 0.5, "avg_net_pct": 0.31, "total_net_pct": 3.72,
                    "without_top_1pct": {"dropped": 1, "n": 11, "avg_net_pct": 0.12},
                    "open": 2, "insufficient": 0, "unfilled_scored_at_market": 3},
        "fixed_rule": {"n": 12, "win_rate": 0.42, "avg_net_pct": 0.05, "total_net_pct": 0.6,
                       "without_top_1pct": {"dropped": 1, "n": 11, "avg_net_pct": -0.02},
                       "open": 0, "insufficient": 0, "unfilled_scored_at_market": 1},
        "brand_new_arm": {"n": 0, "open": 0, "insufficient": 0,
                          "unfilled_scored_at_market": 0},
    }}}
PLANS = {"ok": True, "error": "", "result": {
    "readable": True, "totals": {"setups": 2, "accepted": 1},
    "rejections_by_reason": {"stop_distance": 1},
    "plans": [{"plan_id": "p1", "submitted_at": 1_795_000_000, "bias": "risk_on",
               "notes": "test", "setups": [
                   {"symbol": "ETHUSDT", "side": "long", "archetype": "trend_pullback",
                    "status": "accepted", "reason": None, "detail": None,
                    "stop_atr": 1.2, "tp1_pct": 1.5},
                   {"symbol": "SOLUSDT", "side": "short", "archetype": "range_fade",
                    "status": "rejected", "reason": "stop_distance",
                    "detail": "3.10 x 1h-ATR outside [0.8, 2.5]",
                    "stop_atr": 3.1, "tp1_pct": 0.9}]}]}}


def _get(monkeypatch, outcomes=OUTCOMES, plans=PLANS) -> str:
    async def fake(self, key, args=None):
        return outcomes if key == "read.analyst_outcomes" else plans
    monkeypatch.setattr(EngineApiClient, "diag_run", fake)
    with TestClient(app) as c:
        c.post("/login", data={"password": "test-token"}, follow_redirects=False)
        r = c.get("/analyst")
        assert r.status_code == 200
        return r.text


def test_arms_render_beside_each_other_with_n_and_the_trim(monkeypatch):
    body = _get(monkeypatch)
    assert "Fixed rule — same menu, mechanical levels" in body
    assert "Analyst — limit at its first entry level" in body
    assert "Not built yet, so not zero: existing_signals" in body


def test_an_arm_ops_has_never_heard_of_renders_badged_not_dropped(monkeypatch):
    body = _get(monkeypatch)
    assert "brand_new_arm" in body and "unclassified" in body


def test_rejections_carry_the_engines_reason(monkeypatch):
    body = _get(monkeypatch)
    assert "stop_distance" in body and "outside [0.8, 2.5]" in body


@pytest.mark.parametrize("payload,state", [
    ({"ok": False, "error": "unknown catalog entry", "known": []}, "not_reported"),
    ({"ok": False, "error": "the engine did not answer within 25.0s"}, "failed"),
    ({"endpoint": "/x", "error": ""}, "failed"),
    ({"ok": True, "error": "", "result": {"readable": False, "reason": "no_ledger_yet"}},
     "no_ledger"),
])
def test_four_states_never_pooled(payload, state):
    assert analyst.classify(payload)[0] == state


def test_a_blank_transport_error_still_names_itself():
    assert analyst.classify({"endpoint": "/x", "error": ""})[1]


def test_no_ledger_renders_its_cause_not_an_empty_table(monkeypatch):
    nl = {"ok": True, "error": "", "result": {"readable": False, "reason": "no_ledger_yet"}}
    body = _get(monkeypatch, outcomes=nl, plans=nl)
    assert "No plan has been submitted yet" in body and "no_ledger_yet" in body


def test_contract_the_engine_publishes_what_this_page_reads(tmp_path, monkeypatch):
    """Drive the engine's REAL summary over its REAL ledger shape."""
    if not ENGINE_REPO.exists():
        pytest.skip(ENGINE_ABSENT)
    sys.path.insert(0, str(ENGINE_REPO))
    try:
        from src import analyst_plan as P  # type: ignore
        from src import analyst_walk as W  # type: ignore
        led = P.Ledger(str(tmp_path / "plans.sqlite"))
        led._connect().close()
        real = W.summary(path=led.path)
        recent = led.recent()
    finally:
        sys.path.remove(str(ENGINE_REPO))
    assert real["readable"] is True and "arms_not_built" in real
    rows = analyst.arm_rows(real["by_arm"])
    assert {r["key"] for r in rows} == set(real["by_arm"])
    assert all(r["known"] for r in rows), "an engine arm has no label here"
    assert {"plans", "totals", "rejections_by_reason"} <= set(recent)
