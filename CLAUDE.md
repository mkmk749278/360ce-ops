# CLAUDE.md — 360 CE Ops

Guidance for Claude Code sessions working in this repository.

## Companion repo

This dashboard is a consumer of — and the control plane for — `mkmk749278/360-v2` (the engine). Before working here, read in order:

1. `OWNER_BRIEF.md` in 360-v2 — operating contract, role boundaries, business rules
2. `ACTIVE_CONTEXT.md` in 360-v2 — current engine state, open queue
3. `docs/360CE_OPS_PLAN.md` in 360-v2 — design document for this app

## Your role here

Same as in 360-v2: CTE with full technical ownership. Read briefs every session. Update `ACTIVE_CONTEXT.md` (in 360-v2) at session end if anything here changed materially.

For every change, ask: **"how does this make signals more profitable for paid subscribers?"** If the answer is engineering polish without a measurable effect on subscriber-visible quality, defer.

## Scope of this repo

Diagnostic dashboard **and the engine control plane** (promoted 2026-06-20). Diagnostic-first, but no longer read-only: the owner's manual control of the engine (auto-mode flips, kill switch, and — over time — manual close / per-user settings) lives here. Monitoring of signals/positions/PnL stays primarily in the Lumin app; **ops owns control**.

> **Correction 2026-07-25: Telegram is NOT banned in India — it works.** This doc
> previously gave "Telegram is banned in-region" as the *reason* ops owns control.
> That premise was false. The decision stands on its own — ops is the auditable,
> owner-gated, PRG-confirmed control surface and that is where control belongs —
> but it is a standing owner decision, not a consequence of Telegram being
> unreachable. Do not re-derive product or routing choices from the old premise.

Three deliverables live in this repo:

- **The web dashboard + control plane** (`app/`) — FastAPI + HTMX, deployed at `ops.luminapp.org`.
- **The 24/7 monitoring agent** (`app/agent/`) — a separate container (`python -m app.agent.runner`, 60s poll cycle) running the Tier-0 safety detectors (naked position, signing-service down, engine/Redis stale, …). Alert state is a Redis-backed dedup/escalation FSM (`alert:state:{fingerprint}`, in-memory fallback); it pages via FCM push (Telegram notifier retained in code) and pings a healthchecks.io heartbeat after each clean cycle. The web app's `/alerts` page reads the same Redis state.
- **The native ops mobile app** (`mobile/`) — owner-only Flutter Android app over the ops `/api/v1` JSON surface (see `mobile/README.md` and `docs/OPS_MOBILE_APP_PLAN.md`). Its CI (`.github/workflows/mobile-apk.yml`) generates the Android scaffolding with `flutter create`, so `mobile/android/` is not committed.

**This repo is also where "dark" becomes visible.** Money-path work in the engine
ships dark — invisible to users, *live to the owner* (see `CLAUDE.md § Project
Phase` in 360-v2). The measurement runs from day one; ops is where the owner
actually reads it. So a dark engine change is not finished until its ops surface
exists: a panel, a table, or a truth-report section readable the same day.
"Measured but nowhere to look" is an unfinished change, and building that surface
is this repo's job.

Control doctrine (non-negotiable for every write surface):
- **Owner-gated end to end.** Writes call the engine's owner-gated endpoints; the dashboard's static Bearer token is owner-tier on the engine. Everything stays behind the auth gate (password + TOTP when enrolled).
- **Audited.** Every control action is appended to the audit log (`app/audit.py` → `OPS_AUDIT_LOG`). Audit writes are best-effort and never block the action — being able to hit the kill switch matters more than logging it.
- **PRG + confirm.** Control routes use POST→redirect→GET so a refresh can't re-fire an action; destructive actions (engage kill switch, switch to LIVE) require an explicit confirm.
- **The engine is the source of truth.** Ops never holds control state locally; it reads it back from the engine after every write.

## Change-management protocol (mirrors 360-v2's)

Every change ships via PR. Fresh topic branch off `main`. Design-summary in the PR body before code review. Never push to `main` directly — auto-deploy on `main` push ships in ~60s and bypasses review.

## What the Strategy Lab is (and what it must not do)

`/strategy-lab` is the owner-facing surface of the engine's **Autonomous Portfolio**
(Layers A–G — see `OWNER_BRIEF.md § 3.11` in 360-v2). It renders the
Strategy×Context edge matrix, the per-gate KEEP/TUNE/DROP audit, the allocator's
would-do panel, and the counterfactual measurement arms.

Two rules, both learned the hard way:

- **Measurement arms are not strategies.** `@FIXED`/`@ATR`/`@TUNED`/`@DSV2`/`@GOV`/
  `@SARBASE`/`@SAREXIT` are stamped from the *same candidates* as the real rows, so
  counting them in the per-strategy rollup double-counts the candidate. The list
  lives in `strategy_lab.MEASUREMENT_SUFFIXES` and mirrors the engine's
  `geometry_ab._VARIANT_SUFFIXES` — **keep them in sync.** They drifted once
  (@TUNED/@DSV2/@GOV shipped engine-side and ops never learned about them) and
  quietly inflated the rollup for a week.
- **Ops ports the engine's math, it does not invent it.** Reducers here are ports of
  engine functions; the thresholds are mirrored and displayed in the UI footer for
  honesty. If a number here disagrees with the truth report, ops is wrong. Mirror
  the engine's **denominators** too: a rate divided by "all bucketed rows" where the
  engine divides by "resolved rows" agrees only until the two populations differ.
- **A panel must be measured on the population the page is showing.** A summary
  computed over the whole ledger while the table beside it is filtered is not a
  summary of anything the owner is looking at. `/signals/sar` shipped a split panel
  reading all 267 rows above a table showing 149 (#90, fixed #91) — pooling
  delivered, router-dropped and gate-killed candidates into the one number an
  adoption decision reads. Every count is measured with **every filter applied
  except its own**; a selector applied to its own counts makes each option describe
  only itself.
- **Disclose concentration; don't silently average it.** Overlapping entries into
  one move resolve at the same exit price and are not independent evidence — three
  BUSDT rows stamped 00:04 / 00:47 / 01:34 all exited at 0.1959 and carried 3/8 of a
  bucket. Show the distinct-outcome count beside the trade count. De-duplicating is
  a judgement call; counting them silently is not.
- **Copy is part of the measurement.** A panel asserting "a near-deterministic loss"
  directly above a bucket reading 38% win rate is wrong on screen even when every
  number is right, and explanatory text that names a *cause* for missing rows must
  be true of those rows. Say whether an R is gross or net — #90 predicted a
  cost-inclusive figure and then measured a cost-free one.

## Data sources (one-line each)

| Source | Module |
|---|---|
| Live engine REST API (`/api/pulse`, `/api/signals`, …) | `app/data_sources/engine_api.py` |
| Engine `data/*.json` mounted at `/engine-data` (read-only) | `app/data_sources/data_volume.py` |
| `monitor-logs` branch artifacts (TTL cached) | `app/data_sources/monitor_logs.py` |
| `docker exec engine python /app/scripts/diag_*` | `app/data_sources/diag_runner.py` |
| Monitoring agent's active-alert Redis state | `app/data_sources/agent_alerts.py` |
| Binance Futures public 1m klines (no key, read-only) | `app/data_sources/binance_klines.py` |
| "Held to stop" free-run replay (Profit tab) | `app/data_sources/free_run.py` |
| Scaled-exit what-if simulator (Profit tab) | `app/data_sources/exit_sim.py` |

## Conventions

- All runtime config via `app/config.py` env-overridable settings.
- FastAPI async everywhere — no blocking HTTP in routes.
- HTMX partials are routes prefixed `/_partial/...` returning HTML fragments.
- Templates extend `base.html`; `login.html` is the one exception (standalone).
- Owner-only auth — password gate via `OPS_AUTH_TOKEN` + optional TOTP second factor via `OPS_TOTP_SECRET` (enroll with `python scripts/generate_totp_secret.py`; audit F-08).
- Templates render unknown payload shapes via `tojson(indent=2)` rather than crashing on shape drift — the engine REST surface is the source of truth, this dashboard adapts to it. (The mobile app's screens follow the same rule: pull fields defensively, fall back to a raw-JSON card.)
- The `/api/v1` JSON surface (for the native app) authenticates with **ops-issued app-tokens** (`app/app_tokens.py`, stored hashed; `revoke-all` is the lost-phone switch). The engine's owner-tier Bearer must **never** ship inside the APK.
- Push alerts go through `app/fcm.py` (FCM HTTP v1 via `httpx` + `google-auth` — deliberately no `firebase-admin`) to tokens in `app/device_registry.py` (plain-file registry shared across the web and agent containers; read fresh, mutate under a lock). The whole push path is disabled-safe: no `FIREBASE_SERVICE_ACCOUNT`, no-op.
- Agent detectors are pure functions — all inputs arrive as parameters, no hidden I/O — so their tests pass plain dicts without live network or Docker.

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

# Docker build + run (web app + monitoring-agent + redis)
docker compose up --build

# Monitoring agent locally
python -m app.agent.runner

# Native ops app (mobile/)
cd mobile && flutter pub get && flutter test
flutter run                                                  # against ops.luminapp.org
flutter run --dart-define=OPS_BASE_URL=http://10.0.2.2:8000  # against local ops
```
