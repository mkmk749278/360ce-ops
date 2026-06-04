# Monitoring Agent — Design Specification

**Status:** Draft — sign-off items 1–3 resolved; item 4 (healthchecks.io setup) pending owner action  
**Scope:** Tier 0 (deterministic reflexes) + Tier 2 (dead-man heartbeat)  
**Repo:** `360ce-ops`  
**Implementation PR will follow** this spec PR after owner approval.

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
- **Page behaviour:** Pages **immediately on first detection** — no 2-cycle escalation. Every second a naked position exists is a live loss risk.
- **Why 90s grace:** The FSM transitions through ENTRY before SL placement; a fresh signal is legitimately without a confirmed SL for up to ~30s. 90s is conservative but avoids false pages on every entry.

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
  (docker.sock is already mounted in ops for the diag runner)
- **Trigger:** Container `360scalp-v2-signing` not in `(healthy)` status, **or** container absent
- **Fingerprint:** `signing_service:{status}`
- **Page behaviour:** Immediate. No signing service = no order execution = positions can't be managed.

#### D4 — BlastRadiusTripwireDetector  `severity: HIGH`

- **Source:** `EngineApiClient.pulse()` or dedicated `/api/tripwires` endpoint (if available); fallback: `DiagRunner` log grep
- **Trigger:** Any `tripwire_triggered` event in the last `AGENT_TRIPWIRE_WINDOW_SEC` (default: 300)
- **Fingerprint:** `tripwire:{event_type}:{timestamp_bucket}` (bucket = floor to nearest 5 min)
- **Page behaviour:** Immediate. A tripwire means blast-radius cap hit — requires owner awareness.

#### D5 — HeartbeatAgeDetector  `severity: WARN → HIGH`

- **Source:** `EngineApiClient.pulse()` → `uptime_seconds` delta / truth snapshot age
- **Trigger:**
  - `age > AGENT_HEARTBEAT_WARN_SEC` (default: 120) → WARN
  - `age > AGENT_HEARTBEAT_HIGH_SEC` (default: 300) → HIGH (escalated in alert_state, see §6)
- **Fingerprint:** `heartbeat_stale`
- **Rationale:** The engine heartbeat is written every scan cycle (~15s). Age > 120s means at least 8 consecutive missed cycles.

#### D6 — WSFeedDetector  `severity: WARN`

- **Source:** `monitor_logs.py` last-fetched log content; or engine log markers via `DiagRunner`
- **Trigger:** `ws_rest_fallback_activated` marker present, or WS reconnect count > `AGENT_WS_RECONNECT_THRESHOLD` (default: 3) in the last hour
- **Fingerprint:** `ws_feed:fallback` or `ws_feed:reconnect_storm`
- **Rationale:** REST fallback means reduced price fidelity. Not an immediate money risk but warrants owner awareness.

#### D7 — SignalSilenceDetector  `severity: WARN`

- **Source:** `EngineApiClient.activity()` + `pulse()` (`signals_today`) + `auto_mode()`
- **Trigger:** Zero signals emitted in `AGENT_SIGNAL_SILENCE_WINDOW_H` (default: 4) rolling hours  
  AND engine is not in an explicitly suppressed state (auto-mode off, kill switch active)
- **Fingerprint:** `signal_silence:{window_h}h`
- **Rationale:** Silent scanner during a live market is either a scanner bug or complete gate suppression. Not a fire, but not normal.
- **Important caveat:** Only fires if auto-mode is ON. If the owner has intentionally disabled auto-mode, silence is expected — no alert.

#### D8 — RedisStalenessDetector  `severity: WARN`

- **Source:** `DiagRunner` → `docker exec 360scalp-v2-redis redis-cli TTL snapshot:engine`
  (already part of the diag runner repertoire). Reads the **engine's** Redis; does not write to it.
- **Trigger:** Most recent snapshot key age > `AGENT_REDIS_STALE_SEC` (default: 45)  
  (SnapshotWriter pushes every ~15s scan cycle; 45s = 3 missed cycles)
- **Fingerprint:** `redis_stale`
- **Rationale:** Stale snapshots mean the API container is serving outdated state to any connected dashboard or app.

---

## 6. Alert State (dedicated ops Redis)

**CTE decision (sign-off item 2):** the agent uses a **dedicated `360ce-ops-redis`** container for alert state — *not* the engine's `360scalp-v2-redis`. Writing alert-state keys to the engine's Redis would breach the "no writes to engine state" charter; a separate instance keeps that boundary clean and still gives persistent dedup across agent restarts (no double-paging on deploys). Falls back to an in-memory dict if Redis is unreachable, same pattern as the engine.

```
Key schema:   alert:state:{fingerprint}
Value (JSON): {
  "first_seen": <iso8601>,
  "last_seen":  <iso8601>,
  "count":      <int>,       # consecutive triggered cycles
  "severity":   "WARN"|"HIGH",
  "paged":      <bool>,      # true once Telegram message sent
  "last_paged": <iso8601>|null
}
TTL: AGENT_ALERT_EXPIRY_SEC (default: 3600)
     Auto-resets when condition clears — no manual cleanup needed.
```

### Escalation FSM

```
[NOT EXISTS]
    │  detector triggers (WARN)
    ▼
[WARN, count=1, paged=false]
    │  next cycle: still triggered
    ▼
[WARN, count=2, paged=true]  ──→  send WARN Telegram message
    │  still triggered after AGENT_DEDUP_SEC (default: 1800)
    ▼
                              ──→  send escalation reminder

[NOT EXISTS] ◄── condition cleared ──  send "✅ recovered" Telegram message
```

**HIGH-severity detectors bypass the 2-cycle wait.** On first trigger (count=1), `paged` is set true and the Telegram message fires immediately.

**Dedup window (`AGENT_DEDUP_SEC`):** Once a fingerprint has been paged, it will not re-page for `AGENT_DEDUP_SEC` seconds even if it keeps triggering. Exception: severity escalates (WARN→HIGH) — that always re-pages regardless of dedup timer.

---

## 7. Notifier

**CTE/owner decision (sign-off item 1):** reuse the **engine's existing Telegram bot token**. Alerts go to a **distinct chat/topic** (`AGENT_TELEGRAM_CHAT_ID`) so operational alerts stay visually separate from subscriber signal traffic. No new bot to register.

### Telegram format

```
🚨 HIGH — Naked Position
Symbol: BTCUSDT  Signal: sig_abc123
Age: 4m 32s  No SL order on record.
First seen: 14:03:21 UTC  (this cycle: #1)
```

For WARN:
```
⚠️ WARN — Heartbeat Stale
Last heartbeat: 187s ago (threshold: 120s)
Persisting for 2 cycles.
```

Recovery:
```
✅ Recovered — Heartbeat Stale
Condition cleared after 4m 15s.
```

### healthchecks.io (Tier 2)

At the **end of each successful poll cycle** (all detectors ran without exception):

```python
GET {AGENT_HEALTHCHECKS_URL}  # env var; empty string = disabled
timeout: 5s
fail silently (log warning, don't raise)
```

healthchecks.io configuration:
- **Period:** 60s (matches `AGENT_POLL_INTERVAL_S`)
- **Grace:** 120s (2 missed cycles before alarm — tolerates one transient failure)
- **Notification channels:** email (always) + Telegram webhook (via healthchecks.io integration)

A hung detector that blocks the loop also stops the heartbeat ping → Tier 2 catches an agent hang as well as an agent crash.

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
                cycle_ok = False  # don't ping healthchecks — cycle was incomplete
        if cycle_ok:
            await notifier.ping_heartbeat()
        await asyncio.sleep(settings.agent_poll_interval_s)
```

Each detector is independently try/excepted. A broken detector does not silence the others. A detector exception does NOT count as an alert (it would create noise during deployments); instead it suppresses the Tier 2 heartbeat ping for that cycle so healthchecks.io fires if the problem persists.

---

## 9. Config Additions to `config.py`

All new fields go into the existing `Settings` dataclass with `_env_int` / `_env` helpers:

| Env var | Default | Description |
|---|---|---|
| `AGENT_POLL_INTERVAL_S` | `60` | Seconds between full detector passes |
| `AGENT_TELEGRAM_BOT_TOKEN` | — (reuse engine bot) | Bot token for agent alerts |
| `AGENT_TELEGRAM_CHAT_ID` | — (required) | Distinct chat/topic for system alerts |
| `AGENT_REDIS_URL` | `redis://360ce-ops-redis:6379/0` | Dedicated ops Redis for alert state |
| `AGENT_HEALTHCHECKS_URL` | `""` | healthchecks.io ping URL; empty = disabled |
| `AGENT_DEDUP_SEC` | `1800` | Suppress re-page for same fingerprint |
| `AGENT_ALERT_EXPIRY_SEC` | `3600` | Redis key TTL for resolved alerts |
| `AGENT_NAKED_POSITION_GRACE_SEC` | `90` | Grace before NakedPosition fires |
| `AGENT_HEARTBEAT_WARN_SEC` | `120` | Heartbeat age WARN threshold |
| `AGENT_HEARTBEAT_HIGH_SEC` | `300` | Heartbeat age HIGH threshold |
| `AGENT_WS_RECONNECT_THRESHOLD` | `3` | WS reconnects/hour before WARN |
| `AGENT_SIGNAL_SILENCE_WINDOW_H` | `4` | Rolling hours for silence detector |
| `AGENT_REDIS_STALE_SEC` | `45` | Snapshot age WARN threshold |
| `AGENT_TRIPWIRE_WINDOW_SEC` | `300` | Look-back window for tripwire events |

---

## 10. Docker Service Additions

```yaml
# docker-compose.yml additions
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
    # Ephemeral by design — alert state is short-lived (TTL 1h). No persistence
    # needed; a restart at worst re-pages an active alert once.
```

No new image for the agent — same `Dockerfile`, different `CMD`. The dashboard image build also covers the agent.

---

## 11. Testing Strategy

All detector logic is tested with **plain dicts as inputs** — no live network, no Docker, no Redis required.

```python
# tests/test_agent_detectors.py  (example)
async def test_naked_position_triggers():
    fake_positions = [{"symbol": "BTCUSDT", "state": "OPEN",
                       "sl_order_id": None, "age_seconds": 150, "signal_id": "s1"}]
    detector = NakedPositionDetector(grace_sec=90)
    results = await detector.check(positions=fake_positions)
    assert len(results) == 1
    assert results[0].severity == "HIGH"
    assert results[0].fingerprint == "naked_position:BTCUSDT:s1"

async def test_naked_position_within_grace():
    fake_positions = [{"symbol": "BTCUSDT", "state": "OPEN",
                       "sl_order_id": None, "age_seconds": 30, "signal_id": "s1"}]
    detector = NakedPositionDetector(grace_sec=90)
    results = await detector.check(positions=fake_positions)
    assert results == []  # within grace period
```

`alert_state.py` is tested with `fakeredis` or an in-memory dict mock. The `notifier.py` Telegram call is mocked — we test the message *format*, not the network call.

---

## 12. What Tier 1 Slots Into Later

When the LLM layer is added, the seam is in `notifier.py`:

```python
# Current (Tier 0 only):
message = format_alert(result)
await send_telegram(message)

# Future (Tier 0 + Tier 1):
raw_context = {"result": result.raw, "recent_logs": ...}
diagnosis = await llm_diagnose(raw_context)   # Haiku, ~200ms, ~$0.0001/call
message = format_alert(result, diagnosis=diagnosis)
await send_telegram(message)
```

The structural shape of everything else — detectors, alert_state, poll loop, heartbeat — is unchanged. The LLM adds diagnosis text to the notification, not a new code path.

---

## 13. Sign-Off Items — Resolution Status

| # | Item | Resolution |
|---|---|---|
| 1 | Telegram bot | **Resolved (owner):** reuse existing engine bot token; alerts to a distinct chat/topic via `AGENT_TELEGRAM_CHAT_ID`. |
| 2 | Alert-state store | **Resolved (CTE):** dedicated ephemeral `360ce-ops-redis` container. Never the engine's Redis — preserves the no-writes-to-engine charter while giving persistent dedup across restarts. |
| 3 | `/api/pulse` task list | **Resolved (CTE):** confirmed *not* exposed today (`build_pulse` returns summary fields only). D2 depends on a new read-only `/internal/diag/tasks` engine endpoint — prerequisite PR, see §14. |
| 4 | healthchecks.io account | **Pending owner action.** Create the check, paste the ping URL into `.env` as `AGENT_HEALTHCHECKS_URL`. Step-by-step in §15. |

---

## 14. Prerequisite: `/internal/diag/tasks` engine endpoint (360-v2)

D2 (BackgroundTaskDetector) needs the engine's live asyncio task names. The current API does not expose them. Before the agent implementation lands, a small **read-only** endpoint ships to 360-v2:

```python
@app.get("/internal/diag/tasks", tags=["meta"])
async def diag_tasks() -> dict:
    """Owner-tier read-only task census. Returns the set of named
    asyncio tasks currently alive in the engine event loop."""
    names = sorted(
        t.get_name() for t in asyncio.all_tasks() if not t.done()
    )
    return {"tasks": names, "count": len(names)}
```

- Follows the existing `/internal/diag/*` owner-tier auth pattern (same Bearer token).
- Read-only, no engine state mutation — **not** on the owner-sign-off list (signing / FSM / evaluators / business-rules / routing).
- Ships as its own `feat/` PR to 360-v2, reviewed and merged before the agent's `feat/` PR here.

---

## 15. healthchecks.io setup — owner walkthrough (sign-off item 4)

Two minutes, free tier covers this:

1. Go to **https://healthchecks.io** → sign up (or log in).
2. Click **“Add Check”**. Name it `360ce-monitoring-agent`.
3. Set **Period** = `60 seconds`, **Grace** = `120 seconds`.
   *(period+grace = 3 min → if the agent stops pinging, you're alerted within 3 minutes.)*
4. Copy the **ping URL** it shows (looks like `https://hc-ping.com/<uuid>`).
5. Paste it into the VPS `.env` as:
   ```
   AGENT_HEALTHCHECKS_URL=https://hc-ping.com/<your-uuid>
   ```
6. (Optional but recommended) In healthchecks.io → **Integrations**, add **Email** (on by default) and **Telegram** so a dead agent pings you on the same channel as everything else.

That's all — leave `AGENT_HEALTHCHECKS_URL` empty and Tier 2 simply stays disabled (Tier 0 on-box alerting still runs). Tell me the URL is in `.env` and I'll confirm the config wiring in the implementation PR.
