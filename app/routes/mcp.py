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
        "triage the WHOLE market yourself. Pairs with stale 1h bars are listed under "
        "'stale', not in rows: a setup on one is refused as data_stale.", _EMPTY),
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
    "chart_image": (
        "read.analyst_chart_image",
        "The same closed candles as chart, drawn as a candlestick PNG with volume, so "
        "you can READ the chart as a trader does: swings, trend, ranges, wicks, "
        "compression, failed breaks. Optional 'levels' (up to 10 prices) are drawn as "
        "dashed lines. The image is content[0] (base64 PNG): decode it to a file and "
        "view it. Take exact prices from the chart tool's numbers, not from the image.",
        {"type": "object", "required": ["symbol"], "additionalProperties": False,
         "properties": {"symbol": {"type": "string"},
                        "tf": {"enum": ["15m", "1h", "4h", "1d"]},
                        "bars": {"type": "integer", "minimum": 20, "maximum": 200},
                        "levels": {"type": "array", "maxItems": 10,
                                   "items": {"type": "number"}}}}),
    "pair_flow": (
        "read.analyst_flow",
        "Who is trading one coin: long and short liquidations (15m/1h/4h) and their "
        "price clusters; aggressive buy/sell volume by price over ~2h (tick footprint, "
        "40 pairs); 15m CVD changes; live book depth and imbalance (40 pairs); volume "
        "profile (POC, value area); funding and OI. Each block says whether it could be "
        "read: unreadable = unknown, never zero. Context at a level, never the trigger.",
        _SYMBOL_SCHEMA),
    "pair_profile": (
        "read.analyst_profile",
        "How one coin behaves, computed by the engine from stored candles: beta and "
        "correlation to BTC (1h, 4h, on BTC up vs down bars, last 48h) and whether it "
        "has decoupled now; volatility percentile and wick share; trending vs "
        "mean-reverting and breakout follow-through; returns by BTC regime; liquidity; "
        "funding z-score and OI; unlocks. Every figure carries its n.", _SYMBOL_SCHEMA),
    "pair_book": (
        "read.analyst_book",
        "Your memory of one coin: every level you marked before and how price treated "
        "it (respected / broke / swept_reclaimed / untested / pending), your own earlier "
        "pair_notes (hypotheses, not facts), and your last setups there. Read it before "
        "charting a coin you have traded.", _SYMBOL_SCHEMA),
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
        "(your limit entry, your tranches, a ladder, market at submit, a fixed rule, no "
        "trade): n, win rate, average net of a 0.07% round trip, with and without the "
        "top 1%. REALIZED per setup posted: a limit never filled earns 0.00%. by_setup "
        "(archetype|side) and by_bias (bias|side) split it per cell, with market and "
        "fixed_rule on the same setups. Also: analyst_split (filled vs scored-at-market, "
        "and how filled setups ended: target / stop / 12h time exit), active_book (your "
        "setups still pending or running, by side: read it BEFORE planning), and "
        "recent_closed (your last 10 closed setups with p_target, R:R, entry distance, "
        "7d-range position, exit, MFE and MAE). Read n before any average.", _EMPTY),
    "plan_feed": (
        "read.analyst_feed",
        "Your accepted setups one by one, newest first (limit 1-300): symbol, side, "
        "archetype, entry type, levels, scenario, p_target, state (pending / active / "
        "tp_hit / sl_hit / time_exit / not_filled), and beside each the market and "
        "fixed_rule arms on the SAME setup. Use it to look at a specific day or coin; "
        "plan_outcomes is the aggregate.",
        {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1,
                                                    "maximum": 300}},
         "additionalProperties": False}),
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
                 "prompt_version": {"type": "string", "maxLength": 40},
                 "bias": {"enum": ["risk_on", "risk_off", "neutral", "stand_aside"]},
                 "notes": {"type": "string", "maxLength": 500},
                 "pair_notes": {"type": "object", "maxProperties": 6,
                                "description": "SYMBOL -> your thesis, key levels and what "
                                               "you are waiting for; stored as a hypothesis "
                                               "and shown back in pair_book",
                                "additionalProperties": {"type": "string",
                                                         "maxLength": 300}},
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
                         "p_stop": {"type": "number", "minimum": 0, "maximum": 1},
                         "expiry_hours": {"type": "number", "minimum": 1, "maximum": 24},
                         "invalidation": {"type": "string", "maxLength": 500},
                         "entry_type": {"enum": ["pullback", "immediate", "watch"],
                                        "description": "pullback = resting limit at "
                                        "entry_prices (default); immediate = enter at "
                                        "market now (omit entry_prices); watch = wait for "
                                        "price to reach 'watch.zone' and enter only on the "
                                        "confirming close (omit entry_prices)"},
                         "watch": {"type": "object", "properties": {
                             "zone": {"type": "array", "minItems": 2, "maxItems": 2,
                                      "items": {"type": "number"}},
                             "confirm": {"type": "object", "properties": {
                                 "rule": {"enum": ["close_beyond_zone", "rejection_wick"]},
                                 "tf": {"enum": ["5m", "15m"]},
                                 "min_wick_ratio": {"type": "number", "minimum": 0.2,
                                                    "maximum": 0.9}}},
                             "cancel": {"type": "array", "maxItems": 3, "items": {
                                 "type": "object", "properties": {
                                     "rule": {"enum": ["close_through", "btc_beyond"]},
                                     "level": {"type": "number"},
                                     "tf": {"enum": ["5m", "15m"]},
                                     "price": {"type": "number"},
                                     "dir": {"enum": ["above", "below"]}}}}}}}}}}}}}),
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
    if name in ("pair_snapshot", "pair_flow", "pair_profile", "pair_book"):
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
    if name == "chart_image":
        sym = str(args.get("symbol") or "").upper()
        if not _SYMBOL_RE.match(sym):
            raise ValueError("symbol must look like XXXUSDT")
        tf = args.get("tf", "1h")
        if tf not in ("15m", "1h", "4h", "1d"):
            raise ValueError("tf must be one of 15m, 1h, 4h, 1d")
        bars = args.get("bars", 120)
        if not isinstance(bars, int) or not 20 <= bars <= 200:
            raise ValueError("bars must be an integer 20-200")
        levels = args.get("levels", [])
        if (not isinstance(levels, list) or len(levels) > 10
                or not all(isinstance(x, (int, float)) and not isinstance(x, bool)
                           for x in levels)):
            raise ValueError("levels must be a list of at most 10 numbers")
        return {"symbol": sym, "tf": tf, "bars": bars, "levels": levels}
    if name == "plan_feed":
        lim = args.get("limit", 50)
        if not isinstance(lim, int) or not 1 <= lim <= 300:
            raise ValueError("limit must be an integer 1-300")
        return {"limit": lim}
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
    return _rpc(id_, result={"isError": not ok, "content": _content(payload)})


def _content(payload: Any) -> list[dict[str, Any]]:
    """MCP content blocks. A rendered chart travels as an image block, first,
    with its metadata (minus the base64) as text beside it, so a client that
    shows images shows the chart and the text stays readable."""
    if isinstance(payload, dict) and isinstance(payload.get("png_base64"), str):
        meta = {k: v for k, v in payload.items() if k != "png_base64"}
        return [{"type": "image", "data": payload["png_base64"],
                 "mimeType": payload.get("mime_type") or "image/png"},
                {"type": "text", "text": json.dumps(meta, default=str)}]
    return [{"type": "text", "text": json.dumps(payload, default=str)}]
