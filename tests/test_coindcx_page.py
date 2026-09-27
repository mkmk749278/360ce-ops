"""`/control/coindcx` — the CoinDCX venue, and the owner's real-account test.

``fixtures_coindcx.json`` is the ENGINE'S OWN output, byte-identical to 360-v2
``tests/venues/fixtures/coindcx/ops_contract.json``: the engine's test drives
its real ``status_snapshot`` and a real ``self_test.run`` (against its fake
exchange) and pins the file, so a shape change fails the engine's CI there and
this reader fails here. A fixture written here would choose a shape and then
agree with itself about it.

Pinned:
* the venue's liveness leads, in four states never pooled, graded on the
  engine's own published cycle;
* a missing report reads "never run", a newer request reads "requested", and
  an overdue one reads as a fault — never the previous run's verdict alone;
* ``fail_position_left_open`` is shouted, and an unknown verdict is badged;
* the self-test POST: confirm required, audited on every outcome, PRG, and
  the engine's refusal reaches the screen; it is owner-only.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources import coindcx as dcx  # noqa: E402
from app.data_sources.data_volume import DataVolumeReader  # noqa: E402
from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import coindcx as route  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
VECTOR = json.loads((HERE / "fixtures_coindcx.json").read_text())
T0 = 1_800_000_000.0


def _status(age: float = 5.0, **over) -> dict:
    s = copy.deepcopy(VECTOR["status"])
    s["written_at"] = T0 - age
    s["reconciler"]["last_cycle_at"] = T0 - age
    s.update(over)
    return s


def _report(verdict: str = "pass", started: float = T0 - 3600) -> dict:
    r = copy.deepcopy(VECTOR["self_test_pass"])
    r.update(verdict=verdict, started_at=started, finished_at=started + 60)
    return r


# ── status: four states, on the engine's clock ─────────────────────────

def test_the_engines_own_status_reads_running_on_its_own_cadence():
    g = dcx.grade_status(_status(), now=T0)
    assert g["state"] == "running"
    assert g["interval_sec"] == VECTOR["status"]["reconcile_interval_sec"]
    assert g["fallback_interval"] is False


def test_four_states_never_pooled():
    assert dcx.grade_status({"error": "missing: /engine-data/coindcx_status.json"}, now=T0)["state"] == "missing"
    assert dcx.grade_status({"error": "parse: Expecting value"}, now=T0)["state"] == "unreadable"
    assert dcx.grade_status(["x"], now=T0)["state"] == "unreadable"
    interval = VECTOR["status"]["reconcile_interval_sec"]
    stale = dcx.grade_status(_status(age=interval * dcx.STALE_CYCLES + 1), now=T0)
    assert stale["state"] == "stale"


def test_a_status_without_its_cadence_is_graded_on_a_named_fallback():
    s = _status()
    del s["reconcile_interval_sec"]
    g = dcx.grade_status(s, now=T0)
    assert g["fallback_interval"] is True and g["interval_sec"] == dcx.FALLBACK_INTERVAL_SEC


def test_execution_off_reads_as_nobody():
    v = dcx.execution_view(_status())
    assert v["enabled"] is False and "nobody" in v["who"]
    v = dcx.execution_view(_status(execution_enabled=True, allow_list={"active": False, "size": 0}))
    assert "EVERY" in v["who"]


def test_stream_enabled_without_a_snapshot_is_not_reported_not_zero():
    assert VECTOR["status"]["stream"] is None and VECTOR["status"]["stream_enabled"] is True
    assert dcx.stream_view(_status())["state"] == "not_reported"
    assert dcx.stream_view(_status(stream_enabled=False))["state"] == "off"


def test_counters_iterate_the_engines_keys():
    rows = dcx.counter_rows({"z_new_counter": 3, "a": 1})
    assert rows == [("a", 1), ("z_new_counter", 3)]


# ── self-test report ───────────────────────────────────────────────────

def test_the_engines_passing_report_reads_pass_with_every_gate_ok():
    st = dcx.grade_self_test(_report(), last_request_at=None, now=T0)
    assert st["verdict"] == "pass" and st["tone"] == "ok" and not st["unclassified"]
    gates = [s for s in st["steps"] if s["gate"]]
    assert gates and all(s["ok"] for s in gates)
    assert any(not s["gate"] for s in st["steps"]), "the informational step is marked"


def test_no_report_is_never_run_and_a_newer_request_is_pending():
    st = dcx.grade_self_test({"error": "missing: x"}, last_request_at=None, now=T0)
    assert st["state"] == "never_run" and st["pending"] is None
    st = dcx.grade_self_test(_report(), last_request_at=T0 - 30, now=T0)
    assert st["pending"] and st["pending"]["overdue"] is False
    st = dcx.grade_self_test(_report(), last_request_at=T0 - dcx.SELF_TEST_OVERDUE_SEC - 1, now=T0)
    assert st["pending"]["overdue"] is True
    # a report started after the request answers it
    st = dcx.grade_self_test(_report(started=T0 - 10), last_request_at=T0 - 30, now=T0)
    assert st["pending"] is None


def test_an_unknown_verdict_is_badged_not_dropped():
    st = dcx.grade_self_test(_report("brand_new_verdict"), last_request_at=None, now=T0)
    assert st["unclassified"] is True and st["verdict"] == "brand_new_verdict"


# ── the page ───────────────────────────────────────────────────────────

@pytest.fixture
def wired(monkeypatch):
    recorded: list[dict] = []
    state = {"status": _status(), "report": _report(), "tail": []}
    monkeypatch.setattr(route.time, "time", lambda: T0)
    monkeypatch.setattr(DataVolumeReader, "coindcx_status", lambda self: state["status"])
    monkeypatch.setattr(DataVolumeReader, "coindcx_self_test", lambda self: state["report"])
    monkeypatch.setattr(route.audit, "tail", lambda *a, **k: state["tail"])
    monkeypatch.setattr(route.audit, "record",
                        lambda *a, **k: None if k.get("action") == "login" else recorded.append(k))
    state["recorded"] = recorded
    return state


def _login(c):
    c.post("/login", data={"password": "test-token"})


def test_page_renders_the_engines_files(wired):
    with TestClient(app) as c:
        _login(c)
        html = c.get("/control/coindcx").text
    assert "RUNNING" in html and "OFF — dark" in html
    assert "PASS" in html and "stop_and_target_rest_together" in html
    assert "Run self-test" in html


def test_a_missing_status_says_not_running_not_quiet(wired):
    wired["status"] = {"error": "missing: /engine-data/coindcx_status.json"}
    with TestClient(app) as c:
        _login(c)
        html = c.get("/control/coindcx").text
    assert "NOT RUNNING" in html and "RUNNING</span>" not in html.replace("NOT RUNNING", "")


def test_a_position_left_open_is_shouted(wired):
    wired["report"] = _report("fail_position_left_open")
    with TestClient(app) as c:
        _login(c)
        html = c.get("/control/coindcx").text
    assert "A POSITION MAY STILL BE OPEN" in html and "flash flash-err" in html


def test_self_test_needs_confirm_and_audits_the_refusal(monkeypatch, wired):
    calls: list = []

    async def fake(self, uid, symbol, margin):
        calls.append((uid, symbol, margin))
        return {"queued": True, "request_id": "r"}

    monkeypatch.setattr(EngineApiClient, "coindcx_self_test", fake)
    with TestClient(app) as c:
        _login(c)
        r = c.post("/control/coindcx/self-test",
                   data={"uid": "ownerUid123", "symbol": "DOGEUSDT", "margin_currency": "USDT"},
                   follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/control/coindcx"
        assert calls == []
        assert wired["recorded"][-1]["ok"] is False
        r = c.post("/control/coindcx/self-test",
                   data={"uid": "ownerUid123", "symbol": "dogeusdt", "margin_currency": "INR",
                         "confirm": "yes"}, follow_redirects=False)
        assert r.status_code == 303
        assert calls == [("ownerUid123", "DOGEUSDT", "INR")]
        assert wired["recorded"][-1]["ok"] is True
        html = c.get("/control/coindcx").text
    assert "Self-test queued" in html


@pytest.mark.parametrize("uid,symbol", [("a b;rm", "DOGEUSDT"), ("ownerUid123", "DOGE/INR")])
def test_malformed_input_never_reaches_the_engine(monkeypatch, wired, uid, symbol):
    calls: list = []

    async def fake(self, *a):
        calls.append(a)
        return {"queued": True}

    monkeypatch.setattr(EngineApiClient, "coindcx_self_test", fake)
    with TestClient(app) as c:
        _login(c)
        c.post("/control/coindcx/self-test",
               data={"uid": uid, "symbol": symbol, "margin_currency": "USDT", "confirm": "yes"},
               follow_redirects=False)
    assert calls == [] and wired["recorded"][-1]["ok"] is False


def test_the_engines_refusal_reaches_the_screen(monkeypatch, wired):
    async def fake(self, *a):
        return {"error": "Engine bridge unavailable — try again.", "status_code": 503}

    monkeypatch.setattr(EngineApiClient, "coindcx_self_test", fake)
    with TestClient(app) as c:
        _login(c)
        # PRG: the redirect's GET is where the one-shot flash renders.
        html = c.post("/control/coindcx/self-test",
                      data={"uid": "ownerUid123", "symbol": "DOGEUSDT",
                            "margin_currency": "USDT", "confirm": "yes"}).text
    assert "HTTP 503" in html and "Engine bridge unavailable" in html
    assert wired["recorded"][-1]["ok"] is False


def test_a_newer_request_renders_requested_over_the_old_verdict(wired):
    wired["tail"] = [{"ts": "2027-01-15T07:59:30+00:00", "action": "coindcx_self_test",
                      "ok": True, "params": {}, "result": "ok"}]
    import datetime as _dt
    req = _dt.datetime.fromisoformat(wired["tail"][0]["ts"]).timestamp()
    wired["report"] = _report(started=req - 3600)
    with TestClient(app) as c:
        _login(c)
        html = c.get("/control/coindcx").text
    assert "REQUESTED" in html or "NO REPORT" in html
    assert "previous run" in html
