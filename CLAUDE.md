# CLAUDE.md — 360 CE Ops

Guidance for Claude Code sessions working in this repository.

## Companion repo

This dashboard is a read-only consumer of `mkmk749278/360-v2` (the engine). Before working here, read in order:

1. `OWNER_BRIEF.md` in 360-v2 — operating contract, role boundaries, business rules
2. `ACTIVE_CONTEXT.md` in 360-v2 — current engine state, open queue
3. `docs/360CE_OPS_PLAN.md` in 360-v2 — design document for this app

## Your role here

Same as in 360-v2: CTE with full technical ownership. Read briefs every session. Update `ACTIVE_CONTEXT.md` (in 360-v2) at session end if anything here changed materially.

For every change, ask: **"how does this make signals more profitable for paid subscribers?"** If the answer is engineering polish without a measurable effect on subscriber-visible quality, defer.

## Scope of this repo

Diagnostic dashboard **and the engine control plane** (promoted 2026-06-20). Diagnostic-first, but no longer read-only: Telegram is banned in-region, so the owner's manual control of the engine (auto-mode flips, kill switch, and — over time — manual close / per-user settings) now lives here. Monitoring of signals/positions/PnL stays primarily in the Lumin app; **ops owns control**.

Control doctrine (non-negotiable for every write surface):
- **Owner-gated end to end.** Writes call the engine's owner-gated endpoints; the dashboard's static Bearer token is owner-tier on the engine. Everything stays behind the auth gate (password + TOTP when enrolled).
- **Audited.** Every control action is appended to the audit log (`app/audit.py` → `OPS_AUDIT_LOG`). Audit writes are best-effort and never block the action — being able to hit the kill switch matters more than logging it.
- **PRG + confirm.** Control routes use POST→redirect→GET so a refresh can't re-fire an action; destructive actions (engage kill switch, switch to LIVE) require an explicit confirm.
- **The engine is the source of truth.** Ops never holds control state locally; it reads it back from the engine after every write.

## Change-management protocol (mirrors 360-v2's)

Every change ships via PR. Fresh topic branch off `main`. Design-summary in the PR body before code review. Never push to `main` directly — auto-deploy on `main` push ships in ~60s and bypasses review.

## Data sources (one-line each)

| Source | Module |
|---|---|
| Live engine REST API (`/api/pulse`, `/api/signals`, …) | `app/data_sources/engine_api.py` |
| Engine `data/*.json` mounted at `/engine-data` (read-only) | `app/data_sources/data_volume.py` |
| `monitor-logs` branch artifacts (TTL cached) | `app/data_sources/monitor_logs.py` |
| `docker exec engine python /app/scripts/diag_*` | `app/data_sources/diag_runner.py` |

## Conventions

- All runtime config via `app/config.py` env-overridable settings.
- FastAPI async everywhere — no blocking HTTP in routes.
- HTMX partials are routes prefixed `/_partial/...` returning HTML fragments.
- Templates extend `base.html`; `login.html` is the one exception (standalone).
- Owner-only auth — password gate via `OPS_AUTH_TOKEN` + optional TOTP second factor via `OPS_TOTP_SECRET` (enroll with `python scripts/generate_totp_secret.py`; audit F-08).
- Templates render unknown payload shapes via `tojson(indent=2)` rather than crashing on shape drift — the engine REST surface is the source of truth, this dashboard adapts to it.

## Hard limits

- No engine code is modified from this repo. (Engine *endpoints* are added in `mkmk749278/360-v2`; ops only *calls* them.)
- **Writes to engine state are allowed only through owner-gated engine endpoints, and only when audited** (see Control doctrine above). No direct mutation of engine data files, Redis, or SQLite from this repo — control flows through the engine's HTTP control surface, which owns the invariants (kill-switch write-through, FSM transitions, blast-radius caps). Never reach around the engine to flip state.
- No multi-user. No multi-tenant. No publicly-accessible endpoints (everything behind the auth gate). As a control plane this matters more, not less.
- The `docker.sock` mount is acceptable only because access is owner-only; never broaden user access without first replacing the diag runner with an engine-side endpoint.

## Commands

```bash
# Local dev
OPS_SESSION_SECRET=dev OPS_AUTH_TOKEN=dev uvicorn app.main:app --reload

# Tests
pytest -q

# Docker build + run
docker compose up --build
```
