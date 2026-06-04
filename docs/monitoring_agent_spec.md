# Monitoring Agent — Design Specification

**Status:** Approved — all sign-off items resolved. Implementation in progress.  
**Scope:** Tier 0 (deterministic reflexes) + Tier 2 (dead-man heartbeat)  
**Repo:** `360ce-ops`  
**Prerequisite PR:** `feat/diag-tasks-endpoint` in 360-v2 (D2 dependency)  
**Implementation PR:** `feat/monitoring-agent` in 360ce-ops

---

## 1. Problem Statement

The 24/7 engine process has no persistent external observer. When the background task loop dies, a position opens without a stop, the signing service crashes, or the WS feed silently stops delivering prices, the only detection path is the owner manually checking the dashboard. The JTOUSDT incident ($50+ loss) is the canonical example: a naked position sat OPEN for hours before it was noticed.

This spec defines a **lightweight, always-on monitoring agent** that runs in the ops stack, observes the engine from the outside (no writes to engine state, ever), and pages the owner on Telegram the moment a money-path condition triggers.

---

## 2. Non-Goals

- The agent **never writes to engine state**. It cannot flip the kill switch, cancel orders, or modify positions. Observation only.
- No multi-tenant, no external access, no new auth surface.
- No LLM layer in this phase. That is Tier 1, a separate PR.
- Does not replace the existing `monitor-logs` branch telemetry — it runs alongside it.

---

## 3. Architecture Overview

```
360ce-ops Docker stack
┌─────────────────────────────────────────────────────────────────────┐
│  monitoring-agent  (new container, this spec)                       │
│                                                                     │
│  runner.py (60s poll loop)                                          │
│       ↓                                                             │
│  Detectors (Tier 0) ──→ alert_state.py (Redis dedup/escalation)    │
│       ↓                      ↓                                      │
│  data sources           notifier.py ──→ Telegram                   │
│  · EngineApiClient          ↓                                       │
│  · DataVolumeReader    healthchecks.io ping  (Tier 2)              │
│  · DiagRunner                                                       │
└─────────────────────────────────────────────────────────────────────┘
        ↑                         ↑                    ↑
  engine-data volume (ro)   docker.sock (ro use)   360ce-ops-redis (alert state)
```

The agent is a **new Docker service** (`monitoring-agent`) added to the existing `docker-compose.yml`. It reuses all four existing data-source modules — no new collection plumbing is needed. Alert state lives in a **dedicated ops Redis** (see §6) — never the engine's Redis.

---

## 4. Module Layout

```
360ce-ops/
  app/
    agent/
      __init__.py
      runner.py          # main async poll loop; healthchecks.io ping
      detectors.py       # all Tier 0 predicate classes
      alert_state.py     # Redis-backed dedup + escalation FSM
      notifier.py        # Telegram formatter + healthchecks.io client
    config.py            # AGENT_* tunables added here (all env-overridable)
  tests/
    test_agent_detectors.py   # unit tests — no network required
    test_agent_alert_state.py
```

Entry point: `python -m app.agent.runner` (new Docker CMD).

---

## 5. Tier 0 Detectors

All detectors implement a common protocol:

```python
@dataclass
class DetectorResult:
    triggered: bool
    severity: Literal["HIGH", "WARN"]
    fingerprint: str      # stable identity for dedup; e.g. "naked_position:BTCUSDT:uid42"
    description: str      # human-readable Telegram message fragment
    raw: dict             # raw data used, for Tier 1 context later

class Detector(Protocol):
    async def check(self) -> list[DetectorResult]: ...
```

Each detector returns a **list** — multiple simultaneous instances of the same condition (e.g., two naked positions) produce independent results with distinct fingerprints.

### 5.1 Detector registry — money-path-first priority order

#### D1 — NakedPositionDetector  `severity: HIGH`

- **Source:** `EngineApiClient.positions()` + `EngineApiClient.positions_diag()` (`/internal/diag/positions`)
- **Trigger:** Any position with `state == "OPEN"` where:
  - No associated live SL order ID in the FSM state, **or**
  - The signal's recorded `sl_price` is absent / zero
  - `age_seconds > AGENT_NAKED_POSITION_GRACE_SEC` (default: 90)
- **Fingerprint:** `naked_position:{symbol}:{signal_id}`
- **Page behaviour:** Pages **immediately on first detection** — no 2-cycle escalation.
- **Why 90s grace:** The FSM transitions through ENTRY before SL placement; a fresh signal is legitimately without a confirmed SL for up to ~30s. 90s avoids false pages on every entry.

#### D2 — BackgroundTaskDetector  `severity: HIGH`

- **Source:** **New** `/internal/diag/tasks` engine endpoint (see §14 — prerequisite PR). Returns the names from `asyncio.all_tasks()`. The current `/api/pulse` does **not** expose this.
- **Trigger:** Any of the following task names missing:
  - `trade_monitor`
  - `reconciler`
  - `mark_price_feed`
  - `funding_exit_watcher`
  - `pretp_dispatcher`
- **Fingerprint:** `task_dead:{task_name}`
- **Page behaviour:** Immediate on first detection. A dead `trade_monitor` means no stop-loss enforcement.

#### D3 — SigningHealthDetector  `severity: HIGH`

- **Source:** `DiagRunner` → `docker ps --filter name=signing --format json`
- **Trigger:** Container `360scalp-v2-signing` not in `(healthy)` status, **or** container absent
- **Fingerprint:** `signing_service:{status}`
- **Page behaviour:** Immediate.

#### D4 — BlastRadiusTripwireDetector  `severity: HIGH`

- **Source:** `EngineApiClient.pulse()` or `/api/tripwires` if available; fallback: `DiagRunner` log grep
- **Trigger:** Any `tripwire_triggered` event in the last `AGENT_TRIPWIRE_WINDOW_SEC` (default: 300)
- **Fingerprint:** `tripwire:{event_type}:{timestamp_bucket}` (bucket = floor to nearest 5 min)
- **Page behaviour:** Immediate.

#### D5 — HeartbeatAgeDetector  `severity: WARN → HIGH`

- **Source:** `EngineApiClient.pulse()` → `uptime_seconds` delta / truth snapshot age
- **Trigger:** `age > AGENT_HEARTBEAT_WARN_SEC` (default: 120) → WARN; `age > AGENT_HEARTBEAT_HIGH_SEC` (default: 300) → HIGH
- **Fingerprint:** `heartbeat_stale`

#### D6 — WSFeedDetector  `severity: WARN`

- **Source:** `monitor_logs.py`; or `DiagRunner` log markers
- **Trigger:** `ws_rest_fallback_activated` marker present, or WS reconnect count > `AGENT_WS_RECONNECT_THRESHOLD` (default: 3) in the last hour
- **Fingerprint:** `ws_feed:fallback` or `ws_feed:reconnect_storm`

#### D7 — SignalSilenceDetector  `severity: WARN`

- **Source:** `EngineApiClient.activity()` + `pulse()` + `auto_mode()`
- **Trigger:** Zero signals emitted in `AGENT_SIGNAL_SILENCE_WINDOW_H` (default: 4) rolling hours AND auto-mode is ON
- **Fingerprint:** `signal_silence:{window_h}h`

#### D8 — RedisStalenessDetector  `severity: WARN`

- **Source:** `DiagRunner` → `docker exec 360scalp-v2-redis redis-cli TTL snapshot:engine` (reads engine Redis, never writes)
- **Trigger:** Snapshot key age > `AGENT_REDIS_STALE_SEC` (default: 45)
- **Fingerprint:** `redis_stale`

---

## 6. Alert State (dedicated ops Redis)

**Dedicated `360ce-ops-redis`** container — never the engine's Redis. Preserves the no-writes-to-engine charter. Falls back to in-memory dict if Redis is unreachable.

```
Key schema:   alert:state:{fingerprint}
Value (JSON): {
  "first_seen": <iso8601>,
  "last_seen":  <iso8601>,
  "count":      <int>,
  "severity":   "WARN"|"HIGH",
  "paged":      <bool>,
  "last_paged": <iso8601>|null
}
TTL: AGENT_ALERT_EXPIRY_SEC (default: 3600)
```

### Escalation FSM

- WARN, count=1: store, don't page
- WARN, count=2: page
- HIGH, count=1: page immediately (no wait)
- Condition clears: send `✅ recovered` message; delete key
- Re-page suppressed for `AGENT_DEDUP_SEC` (default: 1800) unless severity escalates

---

## 7. Notifier

**Telegram:** reuse engine bot token (`AGENT_TELEGRAM_BOT_TOKEN`), distinct chat/topic (`AGENT_TELEGRAM_CHAT_ID`).

**healthchecks.io:** ping URL stored in `AGENT_HEALTHCHECKS_URL` env var (never committed to repo). Ping at end of each successful cycle. Period=1min, Grace=2min → agent death detected within 3 minutes.

---

## 8. Poll Loop

```python
async def run():
    while True:
        cycle_ok = True
        for detector in ALL_DETECTORS:
            try:
                results = await detector.check()
                for result in results:
                    await alert_state.handle(result)
            except Exception:
                logger.exception(f"{detector.__class__.__name__} failed")
                cycle_ok = False
        if cycle_ok:
            await notifier.ping_heartbeat()
        await asyncio.sleep(settings.agent_poll_interval_s)
```

A broken detector doesn't silence others. A detector exception suppresses the heartbeat ping that cycle → healthchecks.io fires if it persists.

---

## 9. Config Additions to `config.py`

| Env var | Default | Description |
|---|---|---|
| `AGENT_POLL_INTERVAL_S` | `60` | Seconds between detector passes |
| `AGENT_TELEGRAM_BOT_TOKEN` | required | Reuse engine bot token |
| `AGENT_TELEGRAM_CHAT_ID` | required | Distinct chat/topic for system alerts |
| `AGENT_REDIS_URL` | `redis://360ce-ops-redis:6379/0` | Dedicated ops Redis |
| `AGENT_HEALTHCHECKS_URL` | `""` | Ping URL — set in `.env`, never committed |
| `AGENT_DEDUP_SEC` | `1800` | Suppress re-page for same fingerprint |
| `AGENT_ALERT_EXPIRY_SEC` | `3600` | Redis key TTL |
| `AGENT_NAKED_POSITION_GRACE_SEC` | `90` | Grace before NakedPosition fires |
| `AGENT_HEARTBEAT_WARN_SEC` | `120` | Heartbeat WARN threshold |
| `AGENT_HEARTBEAT_HIGH_SEC` | `300` | Heartbeat HIGH threshold |
| `AGENT_WS_RECONNECT_THRESHOLD` | `3` | Reconnects/hour before WARN |
| `AGENT_SIGNAL_SILENCE_WINDOW_H` | `4` | Rolling hours for silence detector |
| `AGENT_REDIS_STALE_SEC` | `45` | Snapshot age WARN threshold |
| `AGENT_TRIPWIRE_WINDOW_SEC` | `300` | Tripwire look-back window |

---

## 10. Docker Service Additions

```yaml
  monitoring-agent:
    image: ghcr.io/mkmk749278/360ce-ops:latest
    build: .
    container_name: 360ce-ops-agent
    restart: unless-stopped
    command: python -m app.agent.runner
    environment:
      OPS_AUTH_TOKEN: ${OPS_AUTH_TOKEN}
      ENGINE_API_BASE: ${ENGINE_API_BASE:-https://api.luminapp.org}
      ENGINE_DATA_DIR: /engine-data
      ENGINE_CONTAINER_NAME: ${ENGINE_CONTAINER_NAME:-engine}
      AGENT_TELEGRAM_BOT_TOKEN: ${AGENT_TELEGRAM_BOT_TOKEN}
      AGENT_TELEGRAM_CHAT_ID: ${AGENT_TELEGRAM_CHAT_ID}
      AGENT_REDIS_URL: ${AGENT_REDIS_URL:-redis://360ce-ops-redis:6379/0}
      AGENT_HEALTHCHECKS_URL: ${AGENT_HEALTHCHECKS_URL:-}
      AGENT_POLL_INTERVAL_S: ${AGENT_POLL_INTERVAL_S:-60}
      LOG_LEVEL: ${LOG_LEVEL:-INFO}
    volumes:
      - engine-data:/engine-data:ro
      - /var/run/docker.sock:/var/run/docker.sock
    depends_on:
      - 360ce-ops-redis

  360ce-ops-redis:
    image: redis:7-alpine
    container_name: 360ce-ops-redis
    restart: unless-stopped
    command: redis-server --save "" --appendonly no --maxmemory 32mb --maxmemory-policy allkeys-lru
```

No new image — same `Dockerfile`, different `CMD`.

---

## 11. Testing Strategy

All detector logic tested with plain dicts — no live network, no Docker, no Redis required. `alert_state.py` tested with `fakeredis`. Telegram call mocked — test message format, not network.

---

## 12. Tier 1 Seam (future)

```python
# notifier.py — single call site for LLM diagnosis layer
diagnosis = await llm_diagnose({"result": result.raw, "recent_logs": ...})
message = format_alert(result, diagnosis=diagnosis)
```

Everything else unchanged.

---

## 13. Sign-Off Items — All Resolved

| # | Item | Resolution |
|---|---|---|
| 1 | Telegram bot | Reuse existing engine bot; distinct `AGENT_TELEGRAM_CHAT_ID` |
| 2 | Alert-state store | Dedicated ephemeral `360ce-ops-redis` (never engine Redis) |
| 3 | `/api/pulse` task list | Not exposed today; new `/internal/diag/tasks` endpoint in prerequisite PR |
| 4 | healthchecks.io | **Done.** Check created, Period=1min, Grace=2min. URL in VPS `.env`. |

---

## 14. Prerequisite: `/internal/diag/tasks` engine endpoint (360-v2)

Read-only owner-tier endpoint following the existing `/internal/diag/*` pattern:

```python
@app.get("/internal/diag/tasks", tags=["meta"])
async def diag_tasks() -> dict:
    names = sorted(t.get_name() for t in asyncio.all_tasks() if not t.done())
    return {"tasks": names, "count": len(names)}
```

Not on the owner-sign-off list. Ships as its own `feat/` PR to 360-v2 before the agent implementation lands.
