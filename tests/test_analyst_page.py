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


# ── /signals/analyst — the setups as a signal feed (2026-11-22) ──────────────

T = 1_795_369_000.0


def _row(sid, state, side="long", **analyst):
    return {"setup_id": sid, "plan_id": "p", "submitted_at": T, "expires_at": T + 4 * 3600,
            "symbol": "ETHUSDT", "side": side, "archetype": "trend_pullback",
            "bias": "neutral", "plan_notes": "regime read", "entries": [{"price": 100.0,
                                                                          "weight": 1.0}],
            "stop": 98.0, "targets": [104.0, 106.0], "stop_atr": 1.2, "tp1_pct": 4.0,
            "rr_tp1": 2.0, "invalidation": "loses 98", "scenario": "pullback holds",
            "p_target": 0.4, "state": state, "analyst": analyst or None,
            "market": {"status": "resolved", "net_pct": 0.5},
            "fixed_rule": {"status": "resolved", "net_pct": -0.2, "exit_reason": "stop"}}


FEED = {"ok": True, "error": "", "result": {
    "readable": True, "n_accepted_total": 4, "limit": 300, "generated_at": T + 600,
    "walk_step_s": 300.0, "note": "dark",
    "rows": [
        _row("p:0", "tp_hit", entry=100.0, exit=104.0, net_pct=3.93, exit_reason="target",
             mfe_pct=4.1, mae_pct=0.3, exit_at=T + 300, updated_at=T + 600),
        _row("p:1", "active", entry=100.0, status="open", updated_at=T + 600),
        _row("p:2", "not_filled", unfilled=1, net_pct=-1.1, status="resolved",
             updated_at=T + 600),
        _row("p:3", "brand_new_state", side="short"),
    ]}}


def _feed(monkeypatch, payload=FEED, prices=None, query="") -> str:
    async def fake(self, key, args=None):
        return payload
    monkeypatch.setattr(EngineApiClient, "diag_run", fake)

    async def marks(self, **kw):
        return {"ETHUSDT": 101.0} if prices is None else prices
    from app.data_sources.binance_klines import BinanceKlinesClient
    monkeypatch.setattr(BinanceKlinesClient, "fetch_all_prices", marks)
    with TestClient(app) as c:
        c.post("/login", data={"password": "test-token"}, follow_redirects=False)
        r = c.get("/signals/analyst" + query)
        assert r.status_code == 200
        return r.text


def test_feed_renders_each_setup_as_a_card(monkeypatch):
    body = _feed(monkeypatch)
    assert "DARK · OPS ONLY" in body
    assert "TP1 hit" in body and "3.93% net" in body
    assert "pullback holds" in body and "loses 98" in body
    # The live mark is unrealised and on the running card only.
    assert "1.00% unrealised" in body


def test_not_filled_is_never_presented_as_a_fill(monkeypatch):
    body = _feed(monkeypatch)
    assert "Limit never reached" in body and "At market it would have made" in body
    s = analyst.feed_summary(analyst.feed_rows(FEED["result"]["rows"], {}, T))
    assert s["n_filled_closed"] == 1 and s["avg_net_pct"] == 3.93
    assert s["n_not_filled"] == 1 and s["not_filled_avg_market_pct"] == -1.1


def test_an_unknown_state_is_badged_not_dropped(monkeypatch):
    body = _feed(monkeypatch)
    assert "brand_new_state" in body and "unclassified" in body


def test_filter_counts_exclude_their_own_filter(monkeypatch):
    body = _feed(monkeypatch, query="?state=tp_hit")
    assert "1 cards" in body
    # The state dropdown still counts every state (side filter is unset).
    assert "Active (1)" in body


def test_missing_marks_blank_the_column_and_say_so(monkeypatch):
    body = _feed(monkeypatch, prices={})
    assert "Live marks unavailable" in body and "% unrealised" not in body


def test_a_running_card_the_engine_stopped_advancing_is_stale():
    rows = analyst.feed_rows([_row("p:1", "active", entry=100.0, updated_at=T)], {}, T + 3600)
    assert rows[0]["walk_stale"] is True
    rows = analyst.feed_rows([_row("p:1", "active", entry=100.0, updated_at=T)], {}, T + 60)
    assert rows[0]["walk_stale"] is False


def test_feed_not_reported_names_the_deploy_question(monkeypatch):
    body = _feed(monkeypatch, payload={"ok": False, "error": "unknown catalog entry"})
    assert "NOT REPORTED" in body and "read.analyst_feed" in body


def test_contract_the_engine_feed_carries_what_the_cards_read(tmp_path, monkeypatch):
    """Drive the engine's REAL feed over its REAL ledger and walk."""
    if not ENGINE_REPO.exists():
        pytest.skip(ENGINE_ABSENT)
    import json as _json
    sys.path.insert(0, str(ENGINE_REPO))
    try:
        from src import analyst_plan as P  # type: ignore
        from src import analyst_walk as W  # type: ignore
        from src.historical_data import HistoricalDataStore  # type: ignore
        led = P.Ledger(str(tmp_path / "plans.sqlite"))
        monkeypatch.setattr(P, "_ledger", led)
        con = led._connect()
        with con:
            con.execute("INSERT INTO plans (plan_id, submitted_at, bias) VALUES ('p',?,'n')", (T,))
            con.execute(
                "INSERT INTO setups (setup_id, plan_id, idx, symbol, side, status, entries_json,"
                " stop_price, targets_json, expires_at, submitted_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                ("p:0", "p", 0, "X", "long", "accepted",
                 _json.dumps([{"id": "e", "price": 99.0, "weight": 1.0}]), 97.0,
                 _json.dumps([{"id": "t", "price": 103.0}]), T + 4 * 3600, T))
        con.close()
        st = HistoricalDataStore()
        for i, b in enumerate([(100, 100.5, 99.5, 100), (100, 100, 98.8, 99.2),
                               (99.5, 103.5, 99.4, 103)]):
            st.update_candle("X", "5m", {"open": b[0], "high": b[1], "low": b[2], "close": b[3],
                                         "volume": 1, "open_time": (T + 300 * i) * 1000})
        W.step(type("E", (), {"data_store": st})(), now=T + 3000)
        real = W.feed(path=led.path, now=T + 3000)
        states = set(W.FEED_STATES)
    finally:
        sys.path.remove(str(ENGINE_REPO))
    assert states == set(analyst.FEED_STATE_COPY), "an engine state has no label here"
    row = real["rows"][0]
    for k in ("state", "symbol", "side", "entries", "stop", "targets", "analyst", "market",
              "fixed_rule", "submitted_at", "expires_at", "scenario", "p_target", "rr_tp1"):
        assert k in row, k
    assert row["state"] == "tp_hit"
    rows = analyst.feed_rows(real["rows"], {}, T + 3000, real["walk_step_s"])
    assert analyst.feed_summary(rows)["n_filled_closed"] == 1


def test_a_row_not_yet_walked_renders_instead_of_500ing(monkeypatch):
    """Live, 2026-11-25: the engine sends `market: {}` / `fixed_rule: {}` for a
    setup the walk has not reached. The template read `m.net_pct`, which is
    Undefined there and `is not none`, so the `pct` filter raised and ONE row
    took the whole page to 500. The fixture above always carried a full block —
    a fixture chooses a shape and then agrees with you about it."""
    row = _row("p:9", "not_walked")
    row["market"] = {}
    row["fixed_rule"] = {}
    payload = {"ok": True, "error": "", "result": dict(FEED["result"], rows=[row])}
    body = _feed(monkeypatch, payload)
    assert "Controls: market at submit —" in body


def test_the_pct_filter_reads_undefined_as_not_reported():
    from app.main import templates
    t = templates.env.from_string("{{ d.missing | pct }}|{{ d.missing | price }}")
    assert t.render(d={}) == "—|—"
# ── 2026-11-25: the split, the open book, no-valid-trade ─────────────────────

UPGRADED = {"ok": True, "error": "", "result": {
    **OUTCOMES["result"],
    "by_arm": {**OUTCOMES["result"]["by_arm"],
               "market": {"n": 12, "open": 0, "insufficient": 0,
                          "unfilled_scored_at_market": 0, "no_valid_trade": 3}},
    "analyst_split": {"fill_rate": 0.48,
                      "filled": {"n": 37, "avg_net_pct": 0.41},
                      "scored_at_market": {"n": 40, "avg_net_pct": 1.7},
                      "filled_by_exit": {"horizon": {"n": 20, "share": 0.54,
                                                     "avg_net_pct": 0.2},
                                         "target": {"n": 11, "share": 0.3,
                                                    "avg_net_pct": 2.9}}},
    "active_book": {"n": 2, "by_side": {"long": 0, "short": 2},
                    "active_by_side": {"long": 0, "short": 1}, "symbols": ["A", "B"],
                    "setups": [{"symbol": "SOLUSDT", "side": "short", "state": "pending",
                               "age_h": 1.5, "entry": 70.1, "stop": 71.0, "tp1": 68.0,
                               "p_target": 0.33, "mfe_pct": None, "mae_pct": None}]},
    "recent_closed": []}}


def test_the_split_and_the_open_book_render(monkeypatch):
    body = _get(monkeypatch, outcomes=UPGRADED)
    assert "The analyst arm, taken apart" in body and "scored at market n" in body
    assert "<code>horizon</code>" in body
    assert "short 2" in body and "SOLUSDT short" in body


def test_an_engine_predating_the_split_says_so(monkeypatch):
    body = _get(monkeypatch)
    assert "predates the split" in body and "predates the open book" in body


def test_contract_split_book_and_no_valid_trade_are_published(tmp_path):
    if not ENGINE_REPO.exists():
        pytest.skip(ENGINE_ABSENT)
    sys.path.insert(0, str(ENGINE_REPO))
    try:
        from src import analyst_plan as P  # type: ignore
        from src import analyst_walk as W  # type: ignore
        led = P.Ledger(str(tmp_path / "plans.sqlite"))
        led._connect().close()
        real = W.summary(path=led.path)
    finally:
        sys.path.remove(str(ENGINE_REPO))
    assert {"analyst_split", "active_book", "recent_closed"} <= set(real)
    assert {"filled", "scored_at_market", "fill_rate", "filled_by_exit"} <= set(
        real["analyst_split"])
    assert {"n", "by_side", "setups"} <= set(real["active_book"])
    assert all("no_valid_trade" in a for a in real["by_arm"].values())


# ── walk v2 (2026-11-27): realized headline, cost model, what-ifs, days ─────


def _walked_engine_summary(tmp_path):
    """The engine's REAL summary over a ledger with one filled and one
    unfilled setup, walked by the engine's real step."""
    import json as _json
    sys.path.insert(0, str(ENGINE_REPO))
    try:
        from src import analyst_plan as P  # type: ignore
        from src import analyst_walk as W  # type: ignore
        from src.historical_data import HistoricalDataStore  # type: ignore
        led = P.Ledger(str(tmp_path / "plans.sqlite"))
        con = led._connect()
        v2 = getattr(W, "WALK_VERSION", 1) >= 2
        t0 = 1_800_000_000.0
        with con:
            con.execute("INSERT INTO plans (plan_id, submitted_at) VALUES ('p', ?)", (t0,))
            for i, (entry, stop) in enumerate(((99.0, 97.0), (90.0, 88.0))):
                row = {"setup_id": f"p:{i}", "plan_id": "p", "idx": i, "symbol": "X",
                       "side": "long", "status": "accepted",
                       "entries_json": _json.dumps([{"id": "e", "price": entry, "weight": 1.0}]),
                       "stop_price": stop,
                       "targets_json": _json.dumps([{"id": "t", "price": 103.0},
                                                    {"id": "t2", "price": 106.0}]),
                       "expires_at": t0 + 3600, "submitted_at": t0, "p_target": 0.4}
                if v2:  # p_stop arrived with walk v2
                    row["p_stop"] = 0.35
                con.execute(f"INSERT INTO setups ({', '.join(row)}) VALUES "
                            f"({', '.join('?' * len(row))})", tuple(row.values()))
        con.close()
        st = HistoricalDataStore()
        bars = [(100, 100.5, 99.5, 100), (100, 100, 98.8, 99.2)] + \
            [(99.5, 100, 99.4, 99.8)] * 12 + [(100, 106.5, 99.9, 106)]
        for i, (o, h, lo, c) in enumerate(bars):
            st.update_candle("X", "5m", {"open": o, "high": h, "low": lo, "close": c,
                                         "volume": 1, "open_time": (t0 + 300 * i) * 1000})
        eng = type("E", (), {"data_store": st})()
        now = t0 + 200 * 300
        if v2:
            W.step(eng, now=now, path=led.path, deriv_path="")
        else:
            W.step(eng, now=now, path=led.path)
        return W.summary(path=led.path, now=now)
    finally:
        sys.path.remove(str(ENGINE_REPO))


def test_contract_walk_v2_blocks_are_where_this_page_reads_them(tmp_path):
    """Asserts whichever contract the engine at hand publishes. A v1 engine
    (no walk_version) must read as OLD here, never as realized; a v2 engine
    must carry every block this page reads. Never a skip: the contracts job
    fails on one, and either branch is a real assertion."""
    if not ENGINE_REPO.exists():
        pytest.skip(ENGINE_ABSENT)
    real = _walked_engine_summary(tmp_path)
    rows = {r["key"]: r for r in analyst.arm_rows(real["by_arm"])}
    assert all(r["known"] for r in rows.values()), "an engine arm has no label here"
    a = rows["analyst"]
    if real.get("walk_version") is None:
        assert a["basis"] is None, "a v1 engine must not look realized"
        assert a["filled_n"] is None and a["missed_n"] is None
        return
    assert a["basis"] and a["basis"].startswith("realized")
    # One limit filled, one never reached: realized counts the second at 0.00%.
    assert a["n"] == 2 and a["filled_n"] == 1 and a["missed_n"] == 1
    assert a["avg_net_pct"] < a["old_avg"], "the old headline scored the miss at market"
    assert a["cost_n"] is not None and a["cost_unknown"] is not None
    assert rows["market"]["missed_n"] is None, "a market entry never misses"
    assert a["n_days"] == 1
    assert {"baseline", "expiry_2h", "expiry_4h", "cancel_if_tp1_first"} <= set(
        real["analyst_whatif"])
    assert "three_outcome" in real["calibration"]
    days = analyst.day_rows(real["by_arm"])
    assert days and days[0]["analyst"]["n"] == 2


def test_v2_payload_renders_realized_and_the_missed_card(monkeypatch, tmp_path):
    if not ENGINE_REPO.exists():
        pytest.skip(ENGINE_ABSENT)
    real = _walked_engine_summary(tmp_path)
    body = _get(monkeypatch, outcomes={"ok": True, "error": "", "result": real})
    if real.get("walk_version") is None:  # a v1 engine renders as one
        assert "OLD ENGINE" in body and "Realized %" not in body
        return
    assert "Realized %" in body and "OLD ENGINE" not in body
    assert "What a missed limit would have made" in body
    assert "rules that only remove fills" in body
    assert "Half at TP1" in body or "half at TP1" in body


def test_an_engine_without_a_basis_is_badged_old_not_read_as_realized(monkeypatch):
    body = _get(monkeypatch)
    assert "OLD ENGINE" in body and "Avg net %" in body
    assert "Realized %" not in body
    assert "predates walk v2" in body


# ── analyst vs the existing system (2026-11-27) ─────────────────────────────


def test_comparison_without_the_engine_block_is_a_deploy_question(monkeypatch):
    assert analyst.comparison(OUTCOMES["result"]) is None
    body = _get(monkeypatch)
    assert "it predates the comparison" in body


def test_contract_the_comparison_is_where_this_page_reads_it(tmp_path, monkeypatch):
    if not ENGINE_REPO.exists():
        pytest.skip(ENGINE_ABSENT)
    real = _walked_engine_summary(tmp_path)
    if real.get("walk_version") is None or "existing_signals" not in real:
        # An engine predating the comparison renders as one, never as empty.
        assert analyst.comparison(real) is None
        return
    cmp = analyst.comparison(real)
    assert cmp is not None and cmp["rows"][0]["label"].startswith("Existing system")
    assert {r["label"] for r in cmp["rows"][1:]} >= {ARM for ARM in (
        analyst.ARM_LABELS["analyst"], analyst.ARM_LABELS["market"])}
    assert "by_prompt_version" in real and "analyst_by_archetype_side" in real
    body = _get(monkeypatch, outcomes={"ok": True, "error": "", "result": real})
    assert "Analyst vs the existing system" in body and "By prompt version" in body


def test_existing_rows_render_first_with_their_numbers(monkeypatch):
    out = dict(OUTCOMES["result"])
    out["existing_signals"] = {
        "readable": True, "since": 1_795_000_000, "n": 404, "win_rate": 0.35,
        "avg_net_pct": 0.165, "total_net_pct": 66.7,
        "without_top_1pct": {"dropped": 4, "n": 400, "avg_net_pct": 0.09},
        "by_day": {"n_days": 7, "days_positive": 4, "days": [
            {"day": "2026-11-26", "n": 56, "total_pct": 0.3}]},
        "by_setup_side": [{"key": "QUIET_COMPRESSION_BREAK SHORT", "n": 40,
                           "win_rate": 0.35, "avg_net_pct": 0.4, "total_net_pct": 16.0,
                           "without_top_1pct": {"avg_net_pct": 0.3}}]}
    cmp = analyst.comparison(out)
    assert cmp["rows"][0]["n"] == 404 and cmp["rows"][0]["days_pos"] == 4
    days = analyst.day_rows(out["by_arm"], existing=out["existing_signals"])
    assert days[0]["existing"]["n"] == 56
    body = _get(monkeypatch, outcomes={"ok": True, "error": "", "result": out})
    assert "Existing system — signals users received" in body
    assert "QUIET_COMPRESSION_BREAK SHORT" in body


def test_an_unreadable_existing_record_names_its_cause(monkeypatch):
    out = dict(OUTCOMES["result"])
    out["existing_signals"] = {"readable": False, "reason": "unreadable"}
    body = _get(monkeypatch, outcomes={"ok": True, "error": "", "result": out})
    assert "could not be read" in body and "unreadable" in body
