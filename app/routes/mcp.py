"""``/mcp`` — the hourly analyst's MCP server (2026-11-19, owner-approved).

A stateless MCP server over Streamable HTTP: each ``POST /mcp`` carries one
JSON-RPC 2.0 message and gets one JSON response. It is the only door the
scheduled analyst session uses, and it is authenticated by a **machine token**
(``app/machine_tokens.py``), never by the owner session or the engine Bearer.

**The tool table below is the whole scope.** Each tool maps to ONE engine
diagnostic-catalog key; ops forwards the key and the engine decides what it may
do (``360-v2/src/diag_catalog.py``, AST-asserted per entry to reach no order,
secret or kill switch). Exactly one tool writes anything — ``submit_plan``,
which validates a level-id plan and appends it to the engine's analyst ledger.
Nothing here reaches a subscriber, the signal queue or an order.

Every ``tools/call`` is audited (actor ``machine:<label>``), success or not.
"""
from __future__ import annotations

import json
import re
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from app import audit

router = APIRouter()

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "360ce-ops-analyst", "version": "1"}
_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,20}USDT$")

_SYMBOL_SCHEMA = {"type": "object", "properties": {
    "symbol": {"type": "string", "description": "Binance USD-M perp, e.g. ETHUSDT"}},
    "required": ["symbol"], "additionalProperties": False}
_EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}

#: name -> (catalog key, description, input schema). The whole scope.
TOOLS: dict[str, tuple[str, str, dict]] = {
    "market_context": (
        "read.analyst_market",
        "Read first. BTC regime per timeframe, BTC 24h/7d change and ATR, 7d altcoin "
        "breadth vs BTC (n, stale and missing counted), UTC session, and macro-calendar "
        "readability (not wired: treat as UNKNOWN, never as 'no events').", _EMPTY),
    "market_scan": (
        "read.analyst_scan",
        "Every universe coin (~75), one compact row each: price, change 1h/4h/24h/7d, "
        "1h-ATR %, 24h volume surge vs the 7-day mean, position in the 7-day range, "
        "funding and 24h OI change (None = no history yet, never zero). Use it to "
        "triage the WHOLE market yourself.", _EMPTY),
    "chart": (
        "read.analyst_chart",
        "Raw CLOSED candles for one coin: [open_time_ms, open, high, low, close, volume, "
        "taker_buy_share], oldest first, plus 48h of hourly OI / funding / premium. No "
        "indicators: read the structure yourself (swings, higher highs/lows, ranges, "
        "breaks, volume). Read top-down: 1d, 4h, 1h, then 15m for the trigger.",
        {"type": "object", "required": ["symbol"], "additionalProperties": False,
         "properties": {"symbol": {"type": "string"},
                        "tf": {"enum": ["15m", "1h", "4h", "1d"]},
                        "bars": {"type": "integer", "minimum": 10, "maximum": 300}}}),
    "watchlist": (
        "read.analyst_watchlist",
        "Pairs within 1.5x 1h-ATR of a level scoring >= 60, best first, with refusals "
        "counted by reason.", _EMPTY),
    "pair_snapshot": (
        "read.analyst_pair",
        "One pair: price, bar freshness, ATR, trend per timeframe, 24h/7d change, "
        "relative strength vs BTC, value area, unlocks in the veto window, and the "
        "LEVEL MENU. Setups must reference these level ids, never prices.", _SYMBOL_SCHEMA),
    "recent_plans": (
        "read.analyst_plans",
        "Your last plans and every setup's status: accepted, or rejected with a named "
        "reason. Read before planning so you do not repeat a rejected setup.",
        {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1,
                                                    "maximum": 20}},
         "additionalProperties": False}),
    "plan_outcomes": (
        "read.analyst_outcomes",
        "How your past accepted setups did, walked forward dark on 5m bars, per arm "
        "(your limit entry, your tranches, market at submit, no trade): n, win rate, "
        "average net of a 0.07% round trip, with and without the top 1%. Unfilled "
        "limits are scored at market. Read n before any average.", _EMPTY),
    "derivatives_status": (
        "read.deriv_history",
        "State of the hourly OI/funding/premium/taker recorder (it may be switched off).",
        _EMPTY),
    "submit_plan": (
        "action.analyst_submit_plan",
        "Submit this hour's plan. Give each setup EITHER your own prices from your chart "
        "read (entry_prices, stop_price, target_prices: each must lie within the 7-day "
        "traded range +/-1 ATR) OR level ids from pair_snapshot's menu, plus a scenario "
        "and p_target (your probability of reaching the first target; it is scored for "
        "calibration). The engine validates and records each setup, accepted or rejected "
        "with a reason. Measured dark: "
        "nothing reaches a subscriber or an order. 'stand_aside' with no setups is a "
        "valid and common plan.",
        {"type": "object", "required": ["plan"], "additionalProperties": False,
         "properties": {"plan": {
             "type": "object", "required": ["bias", "setups"],
             "properties": {
                 "bias": {"enum": ["risk_on", "risk_off", "neutral", "stand_aside"]},
                 "notes": {"type": "string", "maxLength": 500},
                 "setups": {"type": "array", "maxItems": 5, "items": {
                     "type": "object",
                     "required": ["symbol", "side", "archetype", "invalidation"],
                     "properties": {
                         "symbol": {"type": "string"},
                         "side": {"enum": ["long", "short"]},
                         "archetype": {"enum": ["level_reclaim", "breakout_retest",
                                                "range_fade", "trend_pullback",
                                                "sweep_reversal"]},
                         "entry_level_ids": {"type": "array", "minItems": 1, "maxItems": 2,
                                             "items": {"type": "string"}},
                         "tranche_weights": {"type": "array", "items": {"type": "number"}},
                         "stop_level_id": {"type": "string"},
                         "target_level_ids": {"type": "array", "minItems": 1,
                                              "maxItems": 3, "items": {"type": "string"}},
                         "entry_prices": {"type": "array", "minItems": 1, "maxItems": 2,
                                          "items": {"type": "number"}},
                         "stop_price": {"type": "number"},
                         "target_prices": {"type": "array", "minItems": 1, "maxItems": 3,
                                           "items": {"type": "number"}},
                         "scenario": {"type": "string", "maxLength": 500},
                         "p_target": {"type": "number", "exclusiveMinimum": 0,
                                      "exclusiveMaximum": 1},
                         "expiry_hours": {"type": "number", "minimum": 1, "maximum": 24},
                         "invalidation": {"type": "string", "maxLength": 500}}}}}}}}),
}


def _rpc(id_: Any, *, result: Any = None, error: tuple[int, str] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"jsonrpc": "2.0", "id": id_}
    if error:
        body["error"] = {"code": error[0], "message": error[1]}
    else:
        body["result"] = result
    return JSONResponse(body)


def _auth(request: Request) -> dict[str, Any] | None:
    store = getattr(request.app.state, "machine_tokens", None)
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if store is None or scheme.lower() != "bearer":
        return None
    return store.verify(token.strip())


def _tool_args(name: str, args: Any, label: str) -> dict[str, Any]:
    """The engine args for *name*, or ValueError. Ops passes no free-form target."""
    args = args if isinstance(args, dict) else {}
    if name == "pair_snapshot":
        sym = str(args.get("symbol") or "").upper()
        if not _SYMBOL_RE.match(sym):
            raise ValueError("symbol must look like XXXUSDT")
        return {"symbol": sym}
    if name == "chart":
        sym = str(args.get("symbol") or "").upper()
        if not _SYMBOL_RE.match(sym):
            raise ValueError("symbol must look like XXXUSDT")
        tf = args.get("tf", "1h")
        if tf not in ("15m", "1h", "4h", "1d"):
            raise ValueError("tf must be one of 15m, 1h, 4h, 1d")
        bars = args.get("bars", 120)
        if not isinstance(bars, int) or not 10 <= bars <= 300:
            raise ValueError("bars must be an integer 10-300")
        return {"symbol": sym, "tf": tf, "bars": bars}
    if name == "recent_plans":
        lim = args.get("limit", 5)
        if not isinstance(lim, int) or not 1 <= lim <= 20:
            raise ValueError("limit must be an integer 1-20")
        return {"limit": lim}
    if name == "submit_plan":
        plan = args.get("plan")
        if not isinstance(plan, dict):
            raise ValueError("plan must be an object")
        if len(json.dumps(plan)) > 20_000:
            raise ValueError("plan too large")
        return {"plan": plan, "source": f"mcp:{label}"}
    return {}


@router.get("/mcp")
async def mcp_get() -> Response:
    # Stateless server: no server-initiated stream.
    return Response(status_code=405, headers={"Allow": "POST"})


@router.post("/mcp")
async def mcp_post(request: Request) -> Response:
    rec = _auth(request)
    if rec is None:
        return JSONResponse({"error": "invalid, expired or revoked machine token"},
                            status_code=401,
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        msg = await request.json()
    except Exception:  # noqa: BLE001
        return _rpc(None, error=(-32700, "parse error"))
    if isinstance(msg, list):
        return _rpc(None, error=(-32600, "batches are not supported"))
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _rpc(None, error=(-32600, "invalid request"))
    method, id_ = msg.get("method"), msg.get("id")
    if id_ is None:  # a notification — acknowledged, no body
        return Response(status_code=202)

    if method == "initialize":
        return _rpc(id_, result={
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": (
                "You are the hourly setup analyst. Read market_context and market_scan, "
                "read the charts of your candidates top-down yourself, then submit one "
                "plan per hour with your own prices, a scenario and p_target. Most hours "
                "the right plan is stand_aside."),
        })
    if method == "ping":
        return _rpc(id_, result={})
    if method == "tools/list":
        return _rpc(id_, result={"tools": [
            {"name": n, "description": d, "inputSchema": s}
            for n, (_k, d, s) in TOOLS.items()]})
    if method != "tools/call":
        return _rpc(id_, error=(-32601, f"method not found: {method}"))

    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}
    name = str(params.get("name") or "")
    label = str(rec.get("label") or "analyst")
    settings = request.app.state.settings
    entry = TOOLS.get(name)
    if entry is None:
        request.app.state.machine_tokens.record_denial(rec["id"])
        audit.record(settings.audit_log_path, action="mcp_denied",
                     params={"tool": name}, result={}, ok=False, actor=f"machine:{label}")
        return _rpc(id_, error=(-32602, f"unknown tool: {name}"))
    key = entry[0]
    try:
        engine_args = _tool_args(name, params.get("arguments"), label)
    except ValueError as exc:
        return _rpc(id_, result={"isError": True,
                                 "content": [{"type": "text", "text": str(exc)}]})

    out = await request.app.state.engine_api.diag_run(key, engine_args)
    # The engine envelope always carries `ok`; ops' transport wrapper never does.
    # Key presence, never truthiness (CLAUDE.md 2026-09-03).
    if isinstance(out, dict) and "ok" in out:
        ok, payload = bool(out["ok"]), (out.get("result") if out["ok"] else out)
    else:
        ok, payload = False, {"transport_error": (out or {}).get("error") or "no answer",
                              "endpoint": (out or {}).get("endpoint")}
    audit.record(
        settings.audit_log_path, action="mcp_tool_call",
        params={"tool": name, "key": key,
                "symbol": engine_args.get("symbol"),
                "setups": len(engine_args.get("plan", {}).get("setups") or [])
                if name == "submit_plan" else None},
        result={"ok": ok} if name != "submit_plan" else
        {"ok": ok, "accepted": (payload or {}).get("accepted"),
         "plan_id": (payload or {}).get("plan_id"),
         "n_accepted": (payload or {}).get("n_accepted")},
        ok=ok, actor=f"machine:{label}")
    return _rpc(id_, result={"isError": not ok, "content": [
        {"type": "text", "text": json.dumps(payload, default=str)}]})
