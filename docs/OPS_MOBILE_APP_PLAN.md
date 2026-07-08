# 360 CE Ops — Native Android App Plan

*Owner-only native control + telemetry app for the 360 engine, delivered as a
sideloaded signed APK with FCM push. This is the reliable in-region replacement
for the Telegram control/alert channel, which is dead in-region (and, as of the
2026-07-08 incident, was additionally down via a Telegram-side 502 outage).*

Status: **Phase 0 — design (this document).** Build proceeds in the phased PRs below.

---

## Why this exists

Telegram is the only owner control/alert channel the engine historically used,
and it is unreliable in-region:

- **Banned/degraded in-region** — the documented reason `ops.luminapp.org` and the
  GitHub-issue liveness paging exist.
- **2026-07-08:** Telegram's Bot API returned a persistent `502 Bad Gateway` for the
  bot for hours. Engine was healthy the whole time (single container, `Up 18h`,
  fresh scanner heartbeat, breaker healthy) but *every* Bot API call — `getUpdates`
  (commands in) and `sendMessage` (alerts out) — failed. The owner found out only by
  manually probing. No engine-side fix is possible for a Telegram outage.

The ops **web** dashboard already covers most telemetry, but (a) it is not an
installable app, (b) it cannot push a notification, and (c) some admin control
actions still live only in Telegram. A native app closes all three gaps and, via
**FCM push**, gives a Telegram-independent alert path.

## Decisions (owner-confirmed 2026-07-08)

| Decision | Choice | Rationale |
|---|---|---|
| Screens | **Fully-native Flutter widgets** | Owner chose native over a WebView shell. Best polish/offline; every ops page is (re)built as a native screen against a JSON API. |
| Distribution | **Sideloaded signed APK (owner-only)** | No Play Store review/listing/tester rules for a single-owner control tool. CI builds + signs, owner installs from a link. |
| Alerts | **FCM push** | The actual Telegram replacement: reliable, in-region, no bot dependency. |
| Stack | **Flutter** (match Lumin: Dart ≥3.4, Flutter ≥3.24) | Reuses Lumin's toolchain, Firebase integration, and `build-apk.yml` signing CI. |

## Architecture

```
┌──────────────────────────┐        HTTPS + Bearer(ops app-token)
│  Ops Android app (Flutter)│ ───────────────────────────────────────┐
│  native screens · biometric│                                        ▼
│  FCM receiver             │                            ┌───────────────────────────┐
└──────────────────────────┘                            │  360ce-ops (FastAPI)       │
        ▲  FCM push                                      │  /api/v1/* JSON (read)     │
        │                                                │  /api/v1/control/* (write, │
┌───────┴────────────┐   Firebase Admin (send)           │      owner-gated, audited) │
│ Firebase Cloud     │ ◀──────────────────────────────── │  app-token issue/verify    │
│ Messaging (FCM)    │                                    │  device-token registry     │
└────────────────────┘                                    │  EngineApiClient (Bearer)  │
        ▲                                                 └───────────┬───────────────┘
        │ alert relay                                                 │ Bearer (owner-tier)
        │                                                             ▼
        └──────────────────────────────────────────────  360-v2 engine REST /api/*
```

**Key invariants (inherited from `360ce-ops/CLAUDE.md` control doctrine):**

- **The app never holds the engine's owner-tier token.** A sideloaded APK is
  extractable; embedding the engine Bearer would leak owner-tier engine access.
  The app authenticates to **ops** and holds only an **ops-issued app-token**. Ops
  continues to call the engine server-side with its existing Bearer.
- **Writes go only through ops's owner-gated, audited endpoints.** Every control
  action the app performs hits `/api/v1/control/*`, which reuses the existing
  audited engine-control path (`app/audit.py`). No new engine mutation surface.
- **Engine is source of truth.** The app reads control state back from ops after
  every write (no local control state), mirroring the web dashboard.
- **Destructive actions confirm.** Engage kill switch / switch to LIVE require an
  explicit in-app confirm, matching the web PRG+confirm rule.

## Auth model

1. Owner opens the app, enters the ops password once (same `OPS_AUTH_TOKEN` gate).
2. Ops verifies and issues a signed, revocable **app-token** (JWT or opaque token
   in a small server-side store), scoped owner-tier, long-lived.
3. App stores the token in `flutter_secure_storage`, gated behind **biometric /
   device-PIN unlock** (`local_auth`) on every cold start.
4. App sends `Authorization: Bearer <app-token>` to `/api/v1/*`. Ops validates and
   maps it to the owner session; unknown/revoked tokens → 401.
5. Revocation: an ops endpoint (and web control page button) revokes all app-tokens
   — the "lost phone" switch.

No multi-user, no multi-tenant (unchanged hard limit): the token is owner-tier,
period.

## API contract (ops adds these; engine unchanged)

Read (`GET /api/v1/...`), JSON siblings of existing HTML pages, reusing
`EngineApiClient` + `data_sources`:

`pulse · signals · signals/{id} · positions · profit · performance · pairs ·
invalidations · diag · alerts · truth`

Control (`POST /api/v1/control/...`), owner-gated + audited, mirroring the Telegram
admin command set that is not yet on the web control page:

`auto_mode · kill_switch · exec_mode · tunables (leverage/risk/confidence_threshold/
free_channel_limit) · pause_channel · resume_channel · force_scan ·
reset_circuit_breaker · restart_engine · rollback_code`

Device registration for push:

`POST /api/v1/devices` (register FCM token) · `DELETE /api/v1/devices/{token}`

## Push (FCM)

- Firebase project already exists (used by the engine keystore + Lumin auth).
- Ops gains a **Firebase Admin** sender and a device-token registry (owner devices).
- Alert relay: engine already emits alert-worthy events (regime shift, macro,
  circuit-breaker trip, liveness, high-tier signal fire). Ops relays these to FCM —
  either via a lightweight engine→ops alert webhook or by ops polling the engine's
  existing alert feed. Notification taps deep-link to the relevant native screen.
- This is the path that makes Telegram optional for alerting.

## Phased delivery (each phase = one PR, lands working)

- **Phase 0 — this doc.** Architecture + roadmap.
- **Phase 1 — Ops backend foundation.** `/api/v1/*` read endpoints + app-token
  issue/verify/revoke + device registry, all behind the owner gate and audited.
  Pytest coverage. Independently usable (curl-testable) before any app exists.
- **Phase 2 — App shell + read screens.** Flutter project under `mobile/`, secure
  token storage, biometric unlock, Material-3 theme, bottom-nav, and the read
  screens (Pulse, Signals, Positions, Profit, Performance) wired to Phase 1. CI
  builds a **signed APK artifact** (mirrors Lumin `build-apk.yml`). A usable app.
- **Phase 3 — Control parity.** Native control screen → ops owner-gated audited
  POSTs, with confirm on destructive actions. Retires Telegram for control.
- **Phase 4 — FCM push.** Device registration, Firebase Admin send, engine alert
  relay → notifications. Retires Telegram for alerting.
- **Phase 5 — Polish.** Offline cache, pull-to-refresh, notification deep-links,
  app icon/splash, revoke-all-devices control.

## Location & CI

- App: `360ce-ops/mobile/` (Flutter). Client of the ops API, lives with ops, ships
  via ops's PR flow. Can be split to its own repo later without code change.
- Backend: additive routers in `360ce-ops/app/routes/` + `app/api_v1/`. No change to
  engine code (hard limit).
- CI: new `mobile-apk.yml` mirrors Lumin's signed-APK build; APK published as a
  workflow artifact / GitHub Release asset for sideloading. Signing keystore held as
  an ops Actions secret (never committed).

## Out of scope / non-goals

- No multi-user, no public endpoints (unchanged hard limits).
- No engine code modified from this repo.
- No Play Store release (owner-only sideload).
- The app does not hold Binance keys or the engine owner-tier token.
