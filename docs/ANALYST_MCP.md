# Analyst MCP server (`/mcp`)

The hourly AI analyst (360-v2 `docs/AI_ANALYST_SPEC_2026_11_19.md`, step 3)
reaches the engine through this MCP server. Approved by the owner 2026-11-19.

## What it can do

The tool table in `app/routes/mcp.py` is the whole scope. Each tool forwards one
engine diagnostic-catalog key; the engine decides what that key may do.

| Tool | Engine catalog key | Writes |
|---|---|---|
| `market_context` | `read.analyst_market` | no |
| `market_scan` | `read.analyst_scan` | no |
| `chart(symbol, tf, bars)` | `read.analyst_chart` | no |
| `watchlist` | `read.analyst_watchlist` | no |
| `pair_snapshot(symbol)` | `read.analyst_pair` | no |
| `recent_plans(limit)` | `read.analyst_plans` | no |
| `plan_outcomes` | `read.analyst_outcomes` | no |
| `derivatives_status` | `read.deriv_history` | no |
| `submit_plan(plan)` | `action.analyst_submit_plan` | appends to the engine's analyst ledger |

`submit_plan` reaches no subscriber, signal queue or order. The engine asserts
this per catalog entry, by AST. It needs `DIAG_ACTIONS_ENABLED` on the engine.

Not yet tools (the engine does not publish them yet): book state, the unlock
calendar as its own view.

## Credentials

- **Mint:** Control → Access → *Analyst machine tokens*. It is shown once and
  only its hash is stored. Choose 7, 30 or 90 days.
- **Use:** `Authorization: Bearer lmt_…` on `POST https://ops.luminapp.org/mcp`.
  The server is stateless JSON-RPC over Streamable HTTP, protocol `2025-06-18`.
- **Revoke:** on the same card. It takes effect on the next call.
- **Audit:** every `tools/call` is audited, actor `machine:<label>`. It shows in
  the Access page's log.

For a Claude Code cloud session, store the token as an **environment network
secret** for host `ops.luminapp.org` (header `Authorization`, prefix `Bearer`).
The proxy then injects it, so it never appears in a repo file or an environment
variable. Point the session's MCP config at `https://ops.luminapp.org/mcp` with
no header.

## Smoke test

```bash
curl -s https://ops.luminapp.org/mcp -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"market_context","arguments":{}}}'
```
