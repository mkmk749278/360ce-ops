"""The analyst's MCP server and machine tokens (2026-11-19)."""
from __future__ import annotations

import json
import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.machine_tokens import MachineTokenStore  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import mcp  # noqa: E402


class FakeEngine:
    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply

    async def diag_run(self, key, args=None):
        self.calls.append((key, dict(args or {})))
        if self.reply is not None:
            return self.reply
        return {"ok": True, "key": key, "error": "", "result": {"echo": key}}


@pytest.fixture
def ctx(tmp_path):
    with TestClient(app) as c:
        real = app.state.engine_api
        app.state.machine_tokens = MachineTokenStore(str(tmp_path / "mt.json"))
        app.state.engine_api = FakeEngine()
        token, tid = app.state.machine_tokens.issue(label="hourly", ttl_sec=3600)
        try:
            yield c, token, tid
        finally:
            app.state.engine_api = real


def _rpc(c, token, method, params=None, id_=1):
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return c.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"})


def test_no_token_is_401_json_not_a_login_redirect(ctx):
    c, _, _ = ctx
    r = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
               follow_redirects=False)
    assert r.status_code == 401 and "machine token" in r.json()["error"]


def test_an_app_or_guest_style_token_is_not_a_machine_token(ctx):
    c, _, _ = ctx
    assert _rpc(c, "4JKC6-64ME8-6JE1D-9YVFJ", "ping").status_code == 401


def test_initialize_and_tools_list(ctx):
    c, token, _ = ctx
    init = _rpc(c, token, "initialize", {"protocolVersion": mcp.PROTOCOL_VERSION})
    assert init.json()["result"]["capabilities"] == {"tools": {"listChanged": False}}
    names = {t["name"] for t in _rpc(c, token, "tools/list").json()["result"]["tools"]}
    assert names == set(mcp.TOOLS)


def test_a_notification_is_acknowledged_without_a_body(ctx):
    c, token, _ = ctx
    r = c.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"},
               headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 202


def test_each_tool_forwards_exactly_its_catalog_key(ctx):
    c, token, _ = ctx
    r = _rpc(c, token, "tools/call", {"name": "pair_snapshot",
                                      "arguments": {"symbol": "ethusdt"}})
    res = r.json()["result"]
    assert res["isError"] is False and json.loads(res["content"][0]["text"]) == {
        "echo": "read.analyst_pair"}
    assert app.state.engine_api.calls == [("read.analyst_pair", {"symbol": "ETHUSDT"})]


def test_submit_plan_carries_the_token_label_as_source_and_is_audited(ctx, tmp_path):
    c, token, _ = ctx
    plan = {"bias": "stand_aside", "setups": []}
    _rpc(c, token, "tools/call", {"name": "submit_plan", "arguments": {"plan": plan}})
    assert app.state.engine_api.calls == [
        ("action.analyst_submit_plan", {"plan": plan, "source": "mcp:hourly"})]
    from app import audit
    rows = [e for e in audit.tail(app.state.settings.audit_log_path, limit=50)
            if e.get("action") == "mcp_tool_call"]
    assert rows and rows[0]["actor"] == "machine:hourly"


def test_the_scope_is_the_tool_table_and_nothing_else(ctx):
    c, token, tid = ctx
    r = _rpc(c, token, "tools/call", {"name": "kill_switch", "arguments": {}})
    assert r.json()["error"]["code"] == -32602
    assert app.state.engine_api.calls == []
    assert app.state.machine_tokens.list_tokens()[0]["denials"] == 1
    # Exactly one tool writes, and it is the plan submit.
    writers = {n for n, (k, _d, _s) in mcp.TOOLS.items() if not k.startswith("read.")}
    assert writers == {"submit_plan"}


def test_a_bad_symbol_never_reaches_the_engine(ctx):
    c, token, _ = ctx
    r = _rpc(c, token, "tools/call", {"name": "pair_snapshot",
                                      "arguments": {"symbol": "../etc"}})
    assert r.json()["result"]["isError"] is True and app.state.engine_api.calls == []


def test_an_engine_failure_is_an_error_result_not_a_success(ctx):
    c, token, _ = ctx
    app.state.engine_api = FakeEngine(reply={"endpoint": "/x", "error": ""})
    res = _rpc(c, token, "tools/call", {"name": "watchlist"}).json()["result"]
    assert res["isError"] is True
    assert json.loads(res["content"][0]["text"])["transport_error"] == "no answer"


def test_revoke_is_effective_on_the_next_call(ctx):
    c, token, tid = ctx
    assert _rpc(c, token, "ping").status_code == 200
    assert app.state.machine_tokens.revoke(tid)
    assert _rpc(c, token, "ping").status_code == 401


def test_expired_token_is_refused(tmp_path):
    store = MachineTokenStore(str(tmp_path / "m.json"))
    token, _ = store.issue(label="x", ttl_sec=-1)
    assert store.verify(token) is None


def test_only_the_hash_is_persisted(tmp_path):
    p = tmp_path / "m.json"
    token, _ = MachineTokenStore(str(p)).issue(label="x", ttl_sec=60)
    assert token not in p.read_text()
    assert MachineTokenStore(str(p)).verify(token) is not None  # survives reload


def test_owner_can_mint_and_the_token_is_shown_once(ctx):
    c, _, _ = ctx
    c.post("/login", data={"password": "test-token"}, follow_redirects=False)
    r = c.post("/control/access/machine/issue", data={"label": "a", "ttl": "7d"},
               follow_redirects=False)
    assert r.status_code == 303
    first = c.get("/control/access").text
    assert "Analyst machine tokens" in first and 'id="new-machine-token">lmt_' in first
    assert "lmt_" not in c.get("/control/access").text  # never re-displayed


def test_a_guest_session_cannot_mint_or_reach_mcp_by_cookie(ctx):
    from app import guest_scope
    for path in ("/mcp", "/control/access/machine/issue", "/control/access/machine/revoke"):
        assert path in guest_scope.OWNER_ONLY


def test_chart_tool_bounds_its_arguments_before_the_engine(ctx):
    c, token, _ = ctx
    r = _rpc(c, token, "tools/call", {"name": "chart",
                                      "arguments": {"symbol": "ethusdt", "tf": "4h", "bars": 50}})
    assert r.json()["result"]["isError"] is False
    assert app.state.engine_api.calls[-1] == ("read.analyst_chart",
                                              {"symbol": "ETHUSDT", "tf": "4h", "bars": 50})
    for bad in ({"symbol": "x"}, {"symbol": "ETHUSDT", "tf": "3m"},
                {"symbol": "ETHUSDT", "bars": 9999}):
        n = len(app.state.engine_api.calls)
        res = _rpc(c, token, "tools/call", {"name": "chart", "arguments": bad}).json()["result"]
        assert res["isError"] is True and len(app.state.engine_api.calls) == n


def test_the_phase1_reads_forward_only_their_own_keys(ctx):
    c, token, _ = ctx
    for name, key in (("pair_flow", "read.analyst_flow"),
                      ("pair_profile", "read.analyst_profile"),
                      ("pair_book", "read.analyst_book")):
        r = _rpc(c, token, "tools/call", {"name": name, "arguments": {"symbol": "solusdt"}})
        assert r.json()["result"]["isError"] is False
        assert app.state.engine_api.calls[-1] == (key, {"symbol": "SOLUSDT"})
        n = len(app.state.engine_api.calls)
        bad = _rpc(c, token, "tools/call", {"name": name, "arguments": {"symbol": "x"}})
        assert bad.json()["result"]["isError"] is True and len(app.state.engine_api.calls) == n


def test_chart_image_is_bounded_and_returned_as_an_image_block(ctx):
    c, token, _ = ctx
    app.state.engine_api = FakeEngine(reply={"ok": True, "key": "read.analyst_chart_image",
                                             "error": "", "result": {
                                                 "readable": True, "mime_type": "image/png",
                                                 "png_base64": "iVBORw0KGgo=", "bars": 80}})
    r = _rpc(c, token, "tools/call", {"name": "chart_image", "arguments": {
        "symbol": "ethusdt", "tf": "4h", "bars": 80, "levels": [3000, 2950.5]}})
    res = r.json()["result"]
    assert res["isError"] is False
    assert res["content"][0] == {"type": "image", "data": "iVBORw0KGgo=",
                                 "mimeType": "image/png"}
    meta = json.loads(res["content"][1]["text"])
    assert "png_base64" not in meta and meta["bars"] == 80
    assert app.state.engine_api.calls[-1] == (
        "read.analyst_chart_image",
        {"symbol": "ETHUSDT", "tf": "4h", "bars": 80, "levels": [3000, 2950.5]})
    for bad in ({"symbol": "ETHUSDT", "bars": 300}, {"symbol": "ETHUSDT", "levels": ["x"]},
                {"symbol": "ETHUSDT", "levels": [1] * 11}, {"symbol": "ETHUSDT", "tf": "5m"}):
        n = len(app.state.engine_api.calls)
        res = _rpc(c, token, "tools/call", {"name": "chart_image",
                                            "arguments": bad}).json()["result"]
        assert res["isError"] is True and len(app.state.engine_api.calls) == n


def test_a_refused_chart_image_stays_a_text_block(ctx):
    c, token, _ = ctx
    app.state.engine_api = FakeEngine(reply={"ok": True, "key": "k", "error": "", "result": {
        "readable": False, "reason": "short_series"}})
    res = _rpc(c, token, "tools/call", {"name": "chart_image",
                                        "arguments": {"symbol": "ETHUSDT"}}).json()["result"]
    assert [b["type"] for b in res["content"]] == ["text"]
    assert json.loads(res["content"][0]["text"])["reason"] == "short_series"
