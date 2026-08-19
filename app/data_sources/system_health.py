"""Is the system actually alive — containers, the Redis bridge, and the chain between them.

Why this module exists
----------------------
On 2026-08-18 the owner was getting a HIGH page, repeatedly, that read:

    redis_unreachable — Could not read snapshot:tickers idletime from the
    engine redis container … Probe said: no_output · rc=0 · exited 0 and
    printed nothing.

…immediately followed by a recovery, over and over. Beside it he could not tell
whether the engine was live at all, because the dashboard's engine-backed pages
had stopped answering.

**Both symptoms are one event, and the alert names the wrong container.**
``docker exec`` exited **0**. The container was up, the daemon answered, redis
answered. What redis said was *nil* — and ``redis-cli`` prints a nil bulk reply
as nothing at all when stdout is not a TTY. So ``no_output`` does not mean
"redis is unreachable"; it means **the key was not there**.

And the key not being there is a fact about the *engine*: every ``snapshot:*``
key carries a TTL of twice its write interval (``snapshot:tickers`` is written
every ~15s with a 60s TTL — 360-v2 ``src/api/snapshot_store.py``). Miss four
write cycles and the key evicts itself. In isolated mode the API container reads
those keys and nothing else, so the same stall that expires the key is exactly
what makes the dashboard stop answering.

So the questions this module answers are, in order:

1. **Is each container there, and how many times has it been restarted?**
   The engine carries ``autoheal=true`` and a healthcheck that tests *work*
   (scanner heartbeat freshness), so a wedged scan loop restarts the container.
   ``RestartCount`` is the one number that distinguishes "quiet" from "in a
   restart loop", and nothing in either repo rendered it.
2. **Is redis reachable, separately from whether a key is present?** These are
   different faults with different owners and they had one alert between them.
3. **Where in the chain does liveness stop?** engine container → scanner
   heartbeat → SnapshotWriter → redis keys → api container → ops. A single
   verdict at the end cannot say which link broke.

Rules this module holds to
--------------------------
* **A named refusal, never a blank.** Every probe reports *how* it failed —
  ``timeout`` / ``exec_error`` / ``no_output`` / ``exception`` / ``not_run`` —
  and the page renders the name. "Blank needs a cause before it gets a caption"
  is the rule this whole module is a repair for.
* **Presence is enumerated, absence is declared.** ``docker ps`` can only show
  what exists, so a container that vanished is invisible in it. ``ROSTER``
  declares what should be there; anything running and undeclared renders under
  its raw name badged ``unexpected`` rather than being dropped. A list of what
  is present is silent by construction about what is missing.
* **The redis key census is SCANNED, not mirrored.** The engine owns its key
  schema. Ops asks redis what is actually there rather than keeping a second
  copy of ``snapshot_store.py`` that drifts — the fix for a drifting mirror is
  not a second mirror. ``OPS_DEPENDS_ON`` is a fact about *ops*, not a copy of
  the engine's list: it says which keys, if absent, break a page here.
* **Ops does not grade the engine on ops' clock.** Container ages come from
  Docker's own ``StartedAt``; engine uptime comes from the engine's own pulse;
  key ages come from redis' own ``TTL``/``IDLETIME``. Where ops must time
  something itself — its own HTTP round trip — the number is labelled as ops'.
* **Every collector is independently degradable.** A failed ``docker stats``
  must not empty the container table; each section carries its own ``error``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("ops.system_health")

#: Hard ceiling on any one probe. The page is fetched on demand and the reader
#: is usually asking *because* something is wrong, so a slow answer beats a
#: hung one — but a probe that cannot answer in this long is itself the finding.
PROBE_TIMEOUT_S = 12.0

#: `docker inspect` output is bounded (one JSON doc per container) but a
#: corrupted daemon could stream. Refuse rather than hold it all in memory.
MAX_PROBE_BYTES = 4 * 1024 * 1024


# ---------------------------------------------------------------------------
# The roster — what SHOULD be running, and what breaks when it is not
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RosterEntry:
    name: str
    stack: str
    role: str
    #: What stops working when this container is down. Written out because
    #: "container_down" with no consequence beside it does not tell the owner
    #: whether to get out of bed.
    impact: str
    #: True when the container's absence means money-path protection is gone,
    #: as opposed to a measurement or a page going dark.
    critical: bool


#: Declared rather than discovered, deliberately — see the module docstring.
#: Names match `container_name:` in 360-v2/docker-compose.yml and
#: 360ce-ops/docker-compose.yml.
ROSTER: tuple[RosterEntry, ...] = (
    RosterEntry(
        "360scalp-v2-engine", "engine", "Scanner, FSM, monitor loop, SnapshotWriter",
        "No signals are generated, no position is monitored, and every snapshot "
        "key expires within 60s — which takes the whole dashboard dark with it.",
        critical=True,
    ),
    RosterEntry(
        "360scalp-v2-redis", "engine", "Snapshot bridge between engine and api",
        "The engine cannot publish and the api container has nothing to read. "
        "Pages do not go stale, they go empty.",
        critical=True,
    ),
    RosterEntry(
        "360scalp-v2-api", "engine", "HTTP surface (isolated mode)",
        "Ops, the Lumin app and every /api consumer stop answering. The engine "
        "keeps trading — this is a visibility outage, not a trading one.",
        critical=True,
    ),
    RosterEntry(
        "360scalp-v2-signing", "engine", "Order signing (Unix socket, separate blast radius)",
        "No order can be signed: no entry, no stop amend, no exit. Positions "
        "keep whatever protection is already resting on Binance.",
        critical=True,
    ),
    RosterEntry(
        "360scalp-v2-autoheal", "engine", "Restarts any container that reports unhealthy",
        "A wedged engine stays wedged until a human notices. Nothing breaks "
        "immediately; recovery stops being automatic.",
        critical=False,
    ),
    RosterEntry(
        "360scalp-v2-watchdog", "engine", "Minutes-level supervisor (blind positions, breaker trips)",
        "The invariants a container healthcheck cannot express stop being "
        "checked, and the external dead-man's switch stops being pinged.",
        critical=True,
    ),
    RosterEntry(
        "360ce-ops", "ops", "This dashboard and the control plane",
        "If you are reading this, it is up.",
        critical=False,
    ),
    RosterEntry(
        "360ce-ops-agent", "ops", "24/7 monitoring agent — the Tier-0 detectors",
        "Nothing pages. A naked position would go unannounced until someone "
        "opens this dashboard.",
        critical=True,
    ),
    RosterEntry(
        "360ce-ops-redis", "ops", "Alert dedup/escalation state for the agent",
        "The agent falls back to in-memory alert state, so it keeps detecting "
        "but re-pages on every cycle and /alerts goes blank.",
        critical=False,
    ),
)

ROSTER_BY_NAME = {e.name: e for e in ROSTER}


# ---------------------------------------------------------------------------
# Keys OPS depends on. Not a copy of the engine's schema — a list of what
# breaks HERE when a key is missing, which is a fact about this repo.
# ---------------------------------------------------------------------------

OPS_DEPENDS_ON: dict[str, str] = {
    "snapshot:tickers": "Pairs, and the agent's redis-liveness probe reads this key",
    "snapshot:signals_all": "the Signals feed and every page that joins to it",
    "snapshot:engine_state": "Pulse, auto-mode, and the task census",
    "snapshot:positions_diag": "Positions, and the naked-position detector",
    "snapshot:data_intake": "Diagnostics → Data intake",
    "snapshot:router_delivery": "Signals → Router drops",
    "snapshot:trail_governor": "Signals → Trail governor",
    "snapshot:dark_promotion": "Control → Promotions (the refusal census)",
    "snapshot:alerts": "the engine's own alert list on Pulse",
    "snapshot:activity_all": "the activity stream",
    "snapshot:agents_all": "per-evaluator agent stats",
}


# ---------------------------------------------------------------------------
# Probe plumbing
# ---------------------------------------------------------------------------

@dataclass
class ProbeResult:
    """What a subprocess probe did — never merely whether it worked.

    ``ok`` means it ran and produced output. It says nothing about whether the
    output is *good*; grading that is the caller's job. Same split the agent's
    ``RedisProbe`` makes, and for the same reason: not-measured and
    measured-fine must not share a return value.
    """

    ok: bool
    stdout: str = ""
    #: "" when ok, else timeout | exec_error | no_output | exception | not_run
    cause: str = ""
    returncode: int | None = None
    detail: str = ""
    elapsed_ms: int = 0

    def summary(self) -> str:
        if self.ok:
            return f"ok in {self.elapsed_ms}ms"
        bits = [self.cause or "unknown"]
        if self.returncode is not None:
            bits.append(f"rc={self.returncode}")
        if self.detail:
            bits.append(self.detail[:160])
        return " · ".join(bits)


async def _run(cmd: tuple[str, ...], timeout: float = PROBE_TIMEOUT_S) -> ProbeResult:
    """Run *cmd*, capture it, and report how it failed if it did.

    ``cmd`` is always a fixed tuple assembled from module constants and roster
    names — no request data reaches it, which is why there is no shell and no
    argument sanitiser here. Keep it that way.
    """
    started = time.monotonic()
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError):
        await _kill(proc)
        return ProbeResult(
            ok=False, cause="timeout",
            detail=f"no response within {timeout:.0f}s",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except FileNotFoundError:
        return ProbeResult(
            ok=False, cause="exception",
            detail="docker binary not found in the ops container",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        await _kill(proc)
        return ProbeResult(
            ok=False, cause="exception", detail=f"{type(exc).__name__}: {exc}",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )

    elapsed = int((time.monotonic() - started) * 1000)
    out = stdout[:MAX_PROBE_BYTES].decode("utf-8", errors="replace")
    err = stderr[:4096].decode("utf-8", errors="replace").strip()
    rc = proc.returncode
    if rc != 0:
        # stdout rides out WITH the failure. `docker inspect a b c` exits 1
        # when any one name is missing and still prints the ones it found, so
        # discarding stdout here turned "one container is gone" into "we are
        # blind to all nine" — and `_inspect_all`'s own comment said the
        # opposite was happening. A docstring asserting a property the code
        # beneath it does not have, for the sixth time in these two repos;
        # caught by reading the diff, not by a test.
        return ProbeResult(
            ok=False, cause="exec_error", returncode=rc, stdout=out,
            detail=err or "no stderr", elapsed_ms=elapsed,
        )
    if not out.strip():
        # Deliberately its own cause and NOT an error. A command can exit 0 and
        # print nothing perfectly legitimately — `redis-cli` does exactly that
        # for a nil reply, which is the whole 2026-08-18 defect.
        return ProbeResult(
            ok=False, cause="no_output", returncode=rc,
            detail="exited 0 and printed nothing", elapsed_ms=elapsed,
        )
    return ProbeResult(ok=True, stdout=out, returncode=rc, elapsed_ms=elapsed)


async def _kill(proc: Any) -> None:
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.kill()
        await proc.wait()
    except Exception:
        pass


def _parse_iso(value: str | None) -> datetime | None:
    """Docker stamps RFC3339 with nanoseconds, which `fromisoformat` refuses."""
    if not value or value.startswith("0001-01-01"):
        return None
    raw = value.replace("Z", "+00:00")
    if "." in raw:
        head, _, tail = raw.partition(".")
        frac = tail
        offset = ""
        for sep in ("+", "-"):
            if sep in tail:
                frac, _, off = tail.partition(sep)
                offset = sep + off
                break
        raw = f"{head}.{frac[:6]}{offset}"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # Always aware. Docker stamps `Z` so this is defensive — but a naive
    # datetime here subtracts against an aware `now()` and raises TypeError,
    # which would 500 the one page you open when things are already broken.
    # A page whose whole job is to answer during an outage must not have a
    # shape of input that can take it down.
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _age_sec(when: datetime | None) -> float | None:
    if when is None:
        return None
    return (datetime.now(timezone.utc) - when).total_seconds()


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

async def _inspect_all() -> tuple[dict[str, dict], ProbeResult]:
    """`docker inspect` every roster container, one JSON document per line.

    Inspecting by explicit name (rather than listing and then inspecting) means
    a container that does not exist comes back as an *error on that name* while
    the others still answer — which is the state the roster exists to surface.
    ``--format {{json .}}`` keeps parsing line-oriented so one malformed
    document cannot take the whole census with it.
    """
    probe = await _run((
        "docker", "inspect", "--format", "{{json .}}", *[e.name for e in ROSTER],
    ))
    # `docker inspect` exits non-zero when ANY name is missing, and still prints
    # the ones it found. That is the common case on a partial stack, so the
    # stdout is parsed regardless of the exit code.
    text = probe.stdout or ""
    found: dict[str, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = (doc.get("Name") or "").lstrip("/")
        if name:
            found[name] = doc
    return found, probe


async def _docker_ps() -> tuple[dict[str, dict], ProbeResult]:
    """Everything the daemon is running, so an UNDECLARED container is visible."""
    probe = await _run((
        "docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}",
    ))
    rows: dict[str, dict] = {}
    for line in (probe.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = doc.get("Names") or ""
        if name:
            rows[name] = doc
    return rows, probe


async def _docker_stats() -> tuple[dict[str, dict], ProbeResult]:
    """CPU and memory per container.

    Best-effort and separately degradable: the engine is capped at 2.5 cores
    and has been measured at 130% of a 1.5-core cap, so this is the column that
    says whether a restart loop is resource pressure — but a daemon too busy to
    answer `docker stats` must not empty the container table.
    """
    probe = await _run((
        "docker", "stats", "--no-stream", "--format", "{{json .}}",
    ))
    rows: dict[str, dict] = {}
    for line in (probe.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = doc.get("Name") or ""
        if name:
            rows[name] = doc
    return rows, probe


def _container_row(entry: RosterEntry | None, name: str,
                   inspect: dict | None, ps: dict | None,
                   stats: dict | None) -> dict[str, Any]:
    """One container, graded. Every field can be absent and says so."""
    state = (inspect or {}).get("State") or {}
    health = state.get("Health") or {}
    started = _parse_iso(state.get("StartedAt"))
    finished = _parse_iso(state.get("FinishedAt"))
    status = (state.get("Status") or "").lower()

    # The last healthcheck the daemon ran, and what it printed. This is the
    # evidence behind an autoheal restart and it has never been on a screen.
    log_entries = health.get("Log") or []
    last_health = None
    if log_entries:
        tail = log_entries[-1]
        last_health = {
            "exit_code": tail.get("ExitCode"),
            "output": (tail.get("Output") or "").strip()[:600],
            "start": tail.get("Start"),
            "end": tail.get("End"),
        }

    present = inspect is not None or ps is not None
    running = bool(state.get("Running")) if inspect is not None else (
        (ps or {}).get("State") == "running"
    )

    # Four states, not two — a container that never existed, one that exists and
    # is stopped, one running and unhealthy, one running and fine all have
    # different next moves, and "down" pools the first two.
    if not present:
        verdict = "absent"
    elif status == "restarting":
        verdict = "restarting"
    elif not running:
        verdict = "stopped"
    elif health.get("Status") == "unhealthy":
        verdict = "unhealthy"
    elif health.get("Status") == "starting":
        verdict = "starting"
    else:
        verdict = "running"

    return {
        "name": name,
        "declared": entry is not None,
        "stack": entry.stack if entry else "unknown",
        "role": entry.role if entry else "",
        "impact": entry.impact if entry else "",
        "critical": bool(entry and entry.critical),
        "present": present,
        "verdict": verdict,
        "status_text": (ps or {}).get("Status") or state.get("Status") or "",
        # `None` when the container carries no healthcheck at all, which is a
        # different fact from a healthcheck that has not run yet.
        "health": health.get("Status") if health else None,
        "health_failing_streak": health.get("FailingStreak") if health else None,
        "last_health": last_health,
        "restart_count": (inspect or {}).get("RestartCount"),
        "started_at": state.get("StartedAt") if started else None,
        "uptime_sec": _age_sec(started) if running else None,
        "stopped_for_sec": _age_sec(finished) if (not running and finished) else None,
        "exit_code": state.get("ExitCode") if not running else None,
        "oom_killed": bool(state.get("OOMKilled")) if inspect else None,
        "image": ((inspect or {}).get("Config") or {}).get("Image") or (ps or {}).get("Image"),
        "restart_policy": (((inspect or {}).get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name"),
        "cpu_pct": (stats or {}).get("CPUPerc"),
        "mem_usage": (stats or {}).get("MemUsage"),
        "mem_pct": (stats or {}).get("MemPerc"),
    }


#: Start times inside one minute are treated as the same event. A `compose up`
#: brings a stack up over a few seconds, so second-granularity would read an
#: ordinary deploy as six independent restarts; a minute is coarse enough to
#: absorb that and far finer than the thing being detected.
_COHORT_BUCKET_SEC = 60


def _mark_restart_cohorts(rows: list[dict[str, Any]]) -> None:
    """Flag a container that restarted AFTER its stack was last deployed.

    Added 2026-08-18 because ``RestartCount`` cannot see the events worth
    catching: Docker increments it for restarts made by the container's restart
    *policy*, while autoheal issues a manual restart (which does not) and a
    `compose` recreate builds a new container (which starts the count over). On
    the box the engine was up 1h48m beside three stack-mates up 5h16m — plainly
    restarted on its own — and its count read 0.

    **The first cut of this function cried wolf, and reading the live page an
    hour later is what caught it.** It flagged any container alone in its
    minute-bucket that had *any* older stack-mate — so `360scalp-v2-redis`, up
    21 days, was badged "went down by itself" purely because `autoheal` had been
    up 25. On a long-lived stack that rule eventually flags everything except
    the single oldest container. A test guarded the oldest; nothing guarded the
    second-oldest.

    The rule that needs no invented threshold: **the stack's largest
    start-cohort is the deploy.** A `compose up` restarts most of the stack
    within the same minute, so the modal bucket dates the last deployment.
    Containers *younger* than it came back after that deploy — that is a
    restart. Containers *older* than it were simply not touched by the deploy
    (redis and autoheal carry no config that changed), which is history, not an
    event.

    When no bucket holds two containers there is no identifiable deploy, so this
    flags nothing rather than guessing — a refusal, not a clamp.
    """
    by_stack: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("uptime_sec") is None:
            continue
        by_stack.setdefault(row["stack"], []).append(row)

    for stack_rows in by_stack.values():
        buckets: dict[int, list[dict[str, Any]]] = {}
        for row in stack_rows:
            buckets.setdefault(int(row["uptime_sec"] // _COHORT_BUCKET_SEC), []).append(row)

        # The deploy cohort: most members wins; on a tie the more RECENT one,
        # because that is the deployment a reader is asking about.
        deploy_key = max(
            (k for k, members in buckets.items() if len(members) >= 2),
            key=lambda k: (len(buckets[k]), -k),
            default=None,
        )
        if deploy_key is None:
            continue
        deploy_uptime = min(r["uptime_sec"] for r in buckets[deploy_key])

        for row in stack_rows:
            if row["uptime_sec"] >= deploy_uptime:
                continue          # older than the deploy — it was never restarted
            row["restarted_alone"] = True
            row["deploy_cohort_size"] = len(buckets[deploy_key])
            row["deploy_uptime_sec"] = deploy_uptime


async def collect_containers() -> dict[str, Any]:
    inspect_map, inspect_probe = await _inspect_all()
    ps_map, ps_probe = await _docker_ps()
    stats_map, stats_probe = await _docker_stats()

    blind = not inspect_map and not ps_map
    rows: list[dict[str, Any]] = [
        _container_row(ROSTER_BY_NAME[e.name], e.name,
                       inspect_map.get(e.name), ps_map.get(e.name),
                       stats_map.get(e.name))
        for e in ROSTER
    ]
    _mark_restart_cohorts(rows)
    # Running and undeclared. Not a fault — but a stray container on this box is
    # something the owner should see under its own name rather than not at all.
    unexpected = [
        _container_row(None, name, inspect_map.get(name), doc, stats_map.get(name))
        for name, doc in sorted(ps_map.items())
        if name not in ROSTER_BY_NAME
    ]

    return {
        # `blind` is the docker_ps_unavailable state: reporting every container
        # absent would be a lie about which thing broke.
        "blind": blind,
        "blind_reason": inspect_probe.summary() if blind else "",
        "rows": rows,
        "unexpected": unexpected,
        "absent": [r["name"] for r in rows if not r["present"]],
        "not_running": [r["name"] for r in rows if r["present"] and r["verdict"] not in {"running", "starting"}],
        "unhealthy": [r["name"] for r in rows if r["verdict"] == "unhealthy"],
        "restarted_alone": [r["name"] for r in rows if r.get("restarted_alone")],
        "probes": {
            "inspect": inspect_probe.summary(),
            "ps": ps_probe.summary(),
            "stats": stats_probe.summary(),
        },
        "stats_available": bool(stats_map),
    }


# ---------------------------------------------------------------------------
# Redis — reachability and the key census, kept apart on purpose
# ---------------------------------------------------------------------------

REDIS_CONTAINER = os.getenv("ENGINE_REDIS_CONTAINER", "360scalp-v2-redis")

#: One shell invocation, assembled from constants only, that prints
#: "<key> <ttl> <idletime>" per snapshot key. A key per `docker exec` would be
#: a dozen process spawns on a page the owner opens when the box is already
#: struggling. Nothing here interpolates request data — if that ever changes,
#: this becomes an injection surface and must be rewritten.
_REDIS_CENSUS_SH = (
    "redis-cli --scan --pattern 'snapshot:*' | sort | while read -r k; do "
    "printf '%s\\t%s\\t%s\\n' \"$k\" \"$(redis-cli TTL \"$k\")\" "
    "\"$(redis-cli OBJECT IDLETIME \"$k\")\"; done"
)


async def collect_redis() -> dict[str, Any]:
    """Reachability and key presence, measured and reported separately.

    They were one alert until 2026-08-18 and the alert named the wrong
    container: a nil reply for a missing key exits 0 and prints nothing, which
    the agent classified as ``redis_unreachable``.
    """
    ping, census, info = await asyncio.gather(
        _run(("docker", "exec", REDIS_CONTAINER, "redis-cli", "PING")),
        _run(("docker", "exec", REDIS_CONTAINER, "sh", "-c", _REDIS_CENSUS_SH)),
        _run(("docker", "exec", REDIS_CONTAINER, "redis-cli", "INFO")),
    )

    reachable = ping.ok and ping.stdout.strip().upper() == "PONG"

    # Named ``key_rows`` and not ``keys``: Jinja resolves ``payload.keys`` to
    # the dict's own ``.keys`` METHOD before it looks for an item of that name,
    # so a template iterating it silently gets a builtin. Anything reached from
    # a template must not collide with a dict method.
    key_rows: list[dict[str, Any]] = []
    for line in (census.stdout or "").splitlines():
        parts = line.strip().split("\t")
        if len(parts) != 3:
            continue
        key, ttl_raw, idle_raw = parts

        def _int(v: str) -> int | None:
            try:
                return int(v.strip())
            except (TypeError, ValueError):
                return None

        ttl = _int(ttl_raw)
        key_rows.append({
            "key": key,
            # -1 = no TTL set, -2 = key gone between scan and read. Both are
            # facts, not errors, and pooling them into "no ttl" loses which.
            "ttl": ttl,
            "persistent": ttl == -1,
            "vanished": ttl == -2,
            "idle_sec": _int(idle_raw),
            "ops_needs": OPS_DEPENDS_ON.get(key, ""),
            "declared": key in OPS_DEPENDS_ON,
        })

    present = {k["key"] for k in key_rows}
    missing = [
        {"key": k, "ops_needs": why}
        for k, why in sorted(OPS_DEPENDS_ON.items())
        if k not in present
    ]

    facts: dict[str, str] = {}
    for line in (info.stdout or "").splitlines():
        if ":" in line and not line.startswith("#"):
            name, _, value = line.partition(":")
            facts[name.strip()] = value.strip()

    return {
        "reachable": reachable,
        "ping": ping.summary(),
        "ping_cause": ping.cause,
        "census_cause": census.cause,
        "census_ran": census.ok or census.cause == "no_output",
        "key_rows": key_rows,
        "missing": missing,
        "snapshot_key_count": len(key_rows),
        "info": {
            k: facts.get(k)
            for k in (
                "redis_version", "uptime_in_seconds", "connected_clients",
                "used_memory_human", "maxmemory_human", "maxmemory_policy",
                "evicted_keys", "expired_keys", "rejected_connections",
                "total_commands_processed", "instantaneous_ops_per_sec",
            )
        },
        "info_available": bool(facts),
    }


# ---------------------------------------------------------------------------
# Host — the resources every container is sharing
# ---------------------------------------------------------------------------

def collect_host(engine_data_dir: str) -> dict[str, Any]:
    """Disk and load, read with no subprocess at all.

    Load average inside a container is the *host's* — containers share the
    kernel — so this is the real box, and it is the number that says whether a
    `docker exec` timeout was redis being gone or the host being too busy to
    start a process.
    """
    out: dict[str, Any] = {"disks": [], "loadavg": None, "cpu_count": os.cpu_count()}
    try:
        out["loadavg"] = list(os.getloadavg())
    except OSError:
        out["loadavg"] = None

    #: Three mounts, and on this box all three are the SAME filesystem — the
    #: first live read rendered 35.0 GiB / 144.3 GiB three times, which reads
    #: as three volumes with three independent headrooms. It is one, and the
    #: engine filling it takes the audit log and the dashboard with it. The
    #: device id says so; a repeated number does not.
    seen_devices: dict[int, str] = {}
    for label, path, why in (
        ("Engine data volume", engine_data_dir,
         "every ledger, the closed-signal record and the SQLite user store"),
        ("Ops data volume", "/data", "the audit log, app tokens and device registry"),
        ("Ops container root", "/", "the dashboard process itself"),
    ):
        try:
            usage = shutil.disk_usage(path)
            device = os.stat(path).st_dev
        except OSError as exc:
            out["disks"].append({
                "label": label, "path": path, "why": why,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        out["disks"].append({
            "label": label, "path": path, "why": why,
            "total": usage.total, "used": usage.used, "free": usage.free,
            "used_pct": round(usage.used / usage.total * 100, 1) if usage.total else None,
            # The first path on a device owns the reading; the rest name it and
            # do not repeat the figures.
            "shares_with": seen_devices.get(device),
            "error": None,
        })
        seen_devices.setdefault(device, label)
    out["distinct_filesystems"] = len(seen_devices)
    return out


# ---------------------------------------------------------------------------
# The liveness chain — where does "alive" actually stop?
# ---------------------------------------------------------------------------

#: Every hop between "the engine process is running" and "this page can show
#: you a number", in order. A single green/red verdict cannot say which link
#: broke, and on 2026-08-18 the owner had exactly that: a dashboard that had
#: stopped answering and one alert blaming the wrong container.
#:
#: ``key`` is what the collector fills in; ``owner`` is which repo fixes it
#: when it breaks — because the fix for link 3 is not in the same codebase as
#: the fix for link 5.
CHAIN_STEPS: tuple[tuple[str, str, str, str], ...] = (
    ("engine_container", "Engine container running", "360-v2",
     "Docker says the engine process is up. Nothing about whether it is doing work."),
    ("engine_healthcheck", "Engine healthcheck passing", "360-v2",
     "The container's own healthcheck tests the scanner heartbeat, not just the "
     "process — so unhealthy means the scan loop is wedged, and autoheal will "
     "restart it."),
    ("redis_container", "Redis reachable", "360-v2",
     "PING answered. Measured on its own so that a missing KEY can never be "
     "reported as a missing container again."),
    ("snapshot_keys", "Engine publishing snapshots", "360-v2",
     "The keys ops depends on are present. Each carries a TTL of twice its "
     "write interval, so an absent key means the SnapshotWriter missed several "
     "cycles — this is an engine fault, never a redis one."),
    ("api_container", "API container running", "360-v2",
     "The container that serves HTTP in isolated mode."),
    ("api_reachable", "Engine API answering ops", "360ce-ops",
     "This dashboard's own HTTP round trip to /api/health. The one number on "
     "this page measured on ops' clock, and it is labelled as such."),
    ("engine_working", "Scanner producing", "360-v2",
     "The engine's own pulse: uptime, scanned pairs and today's signal count, "
     "read from the engine rather than inferred here."),
    ("agent", "Monitoring agent cycling", "360ce-ops",
     "The 24/7 detectors. If this stops, nothing pages — and the failure is "
     "silent by construction, because a dead pager sends no message."),
)


def _step(key: str, state: str, detail: str, evidence: str = "") -> dict[str, Any]:
    """One link. ``state`` is one of ok | broken | degraded | unknown.

    ``unknown`` is not ``ok``. A link ops could not measure is its own state,
    because "we could not ask" and "we asked and it is fine" have different
    next moves and the first one is how a check quietly stops running.
    """
    return {"key": key, "state": state, "detail": detail, "evidence": evidence}


def build_chain(containers: dict[str, Any], redis: dict[str, Any],
                api: dict[str, Any], agent: dict[str, Any]) -> list[dict[str, Any]]:
    by_name = {r["name"]: r for r in containers.get("rows", [])}
    blind = containers.get("blind")
    steps: dict[str, dict[str, Any]] = {}

    def container_step(key: str, name: str) -> None:
        row = by_name.get(name)
        if blind or row is None:
            steps[key] = _step(key, "unknown", "docker could not be asked",
                               containers.get("blind_reason", ""))
            return
        if not row["present"]:
            steps[key] = _step(key, "broken", "not present on this box")
            return
        if row["verdict"] in {"stopped", "restarting"}:
            steps[key] = _step(key, "broken", row["status_text"] or row["verdict"])
            return
        restarts = row.get("restart_count")
        note = row["status_text"] or "up"
        if isinstance(restarts, int) and restarts > 0:
            note += f" · restarted {restarts}×"
        steps[key] = _step(key, "ok", note)

    container_step("engine_container", "360scalp-v2-engine")
    container_step("api_container", "360scalp-v2-api")
    # Deliberately NOT `container_step("redis_container", …)`. Redis is graded
    # on its own PING below, which is strictly better evidence than what Docker
    # thinks — a container can be `Up` with a wedged server inside it, and PING
    # is the reachability question actually being asked. The first cut called
    # both and let the second overwrite the first, which is a computation whose
    # result is discarded; a test caught it.

    # -- healthcheck ------------------------------------------------------
    engine = by_name.get("360scalp-v2-engine")
    if blind or engine is None or not engine.get("present"):
        steps["engine_healthcheck"] = _step("engine_healthcheck", "unknown",
                                            "the container could not be inspected")
    elif engine.get("health") is None:
        steps["engine_healthcheck"] = _step(
            "engine_healthcheck", "unknown",
            "this container reports no healthcheck at all",
            "not the same as passing — nothing is testing the scan loop",
        )
    elif engine["health"] == "healthy":
        steps["engine_healthcheck"] = _step("engine_healthcheck", "ok", "healthy")
    elif engine["health"] == "starting":
        steps["engine_healthcheck"] = _step(
            "engine_healthcheck", "degraded",
            "starting — inside the 480s boot grace",
            "the engine re-seeds 75 pairs over REST on every boot; unhealthy "
            "here would be normal and is deliberately not an alert",
        )
    else:
        last = engine.get("last_health") or {}
        steps["engine_healthcheck"] = _step(
            "engine_healthcheck", "broken",
            f"{engine['health']} · failing streak {engine.get('health_failing_streak')}",
            (last.get("output") or "").strip(),
        )

    # -- redis reachability -----------------------------------------------
    #
    # `blind` first, and this ordering is the whole point of the module. Every
    # probe here is a `docker exec`, so when the DAEMON is unreachable the PING
    # fails for a reason that has nothing to do with redis — and calling that
    # "redis is down" would be this page committing, one layer up, exactly the
    # misclassification it was built to end. Caught by rendering the page, not
    # by a test: the first cut read "Redis reachable — broken" on a box with no
    # docker socket at all.
    if redis.get("reachable"):
        steps["redis_container"] = _step("redis_container", "ok", "PONG")
    elif blind:
        steps["redis_container"] = _step(
            "redis_container", "unknown",
            "the docker daemon could not be reached, so the PING proves nothing",
            containers.get("blind_reason", ""),
        )
    elif redis.get("ping_cause") in {"exec_error", "timeout", "exception"}:
        # Docker's view of the container is EVIDENCE here rather than the
        # verdict: a stopped container and a busy host produce the same failed
        # PING and have nothing else in common.
        redis_row = by_name.get("360scalp-v2-redis") or {}
        note = redis_row.get("status_text") or ""
        steps["redis_container"] = _step(
            "redis_container", "broken",
            f"PING did not answer ({redis.get('ping_cause')})",
            " · ".join(x for x in (redis.get("ping", ""), f"docker says: {note}" if note else "") if x),
        )
    else:
        steps["redis_container"] = _step("redis_container", "unknown",
                                         "PING could not be run", redis.get("ping", ""))

    # -- snapshot keys ----------------------------------------------------
    missing = redis.get("missing") or []
    if blind:
        steps["snapshot_keys"] = _step(
            "snapshot_keys", "unknown",
            "the docker daemon could not be reached, so no key could be read",
            containers.get("blind_reason", ""),
        )
    elif not redis.get("census_ran"):
        steps["snapshot_keys"] = _step("snapshot_keys", "unknown",
                                       "the key census could not be run",
                                       redis.get("census_cause", ""))
    elif not redis.get("reachable"):
        # Do not grade keys we could not have read. An empty census behind a
        # dead PING says nothing about the writer.
        steps["snapshot_keys"] = _step("snapshot_keys", "unknown",
                                       "redis did not answer, so key absence proves nothing")
    elif missing:
        steps["snapshot_keys"] = _step(
            "snapshot_keys", "broken",
            f"{len(missing)} of {len(OPS_DEPENDS_ON)} keys ops needs are absent",
            "Each has a TTL of twice its write interval, so this is the engine's "
            "SnapshotWriter having missed several cycles — not a redis fault.",
        )
    else:
        steps["snapshot_keys"] = _step(
            "snapshot_keys", "ok",
            f"all {len(OPS_DEPENDS_ON)} keys ops depends on are present "
            f"({redis.get('snapshot_key_count')} snapshot keys in total)",
        )

    # -- api round trip ---------------------------------------------------
    if api.get("error"):
        steps["api_reachable"] = _step("api_reachable", "broken",
                                       "ops could not reach the engine API",
                                       str(api["error"])[:200])
    else:
        steps["api_reachable"] = _step(
            "api_reachable", "ok",
            f"answered in {api.get('latency_ms')}ms (measured by ops)",
        )

    # -- engine doing work ------------------------------------------------
    pulse = api.get("pulse") or {}
    if api.get("error") or not pulse:
        steps["engine_working"] = _step("engine_working", "unknown",
                                        "no pulse to read")
    else:
        uptime = pulse.get("uptime_seconds")
        pairs = pulse.get("scanning_pairs")
        bits = []
        if uptime is not None:
            bits.append(f"engine up {float(uptime) / 60:.0f}m")
        if pairs is not None:
            bits.append(f"scanning {pairs} pairs")
        bits.append(f"{pulse.get('signals_today', '?')} signals today")
        # A young process is not a quiet market, and the two look identical in
        # a signal count. Say which one this is rather than letting the reader
        # infer it.
        state = "ok" if pairs else "degraded"
        steps["engine_working"] = _step(
            "engine_working", state, " · ".join(bits),
            "uptime under an hour: signal counts describe a young process, not "
            "a quiet market" if (uptime or 0) < 3600 else "",
        )

    # -- the agent --------------------------------------------------------
    steps["agent"] = agent_step(agent)

    return [
        {**steps.get(key, _step(key, "unknown", "not measured")),
         "label": label, "owner": owner, "why": why}
        for key, label, owner, why in CHAIN_STEPS
    ]


#: The agent polls every 60s, so two missed cycles is the point at which its
#: silence stops being ordinary jitter. Deliberately not one: a single slow
#: cycle on a busy host is not a dead pager.
AGENT_STALE_SEC = 150


def agent_step(agent: dict[str, Any]) -> dict[str, Any]:
    if agent.get("error"):
        return _step("agent", "unknown", "the agent's heartbeat could not be read",
                     str(agent["error"])[:200])
    age = agent.get("age_sec")
    if age is None:
        # An agent that predates the heartbeat is not a stopped agent. Ops
        # cannot tell them apart, so it says so instead of picking one.
        return _step(
            "agent", "unknown", "no heartbeat recorded",
            "either the agent has not completed a cycle since this feature "
            "shipped, or it is not running — /alerts and the container row "
            "above separate the two",
        )
    if age > AGENT_STALE_SEC:
        return _step("agent", "broken",
                     f"last cycle {age:.0f}s ago (bound {AGENT_STALE_SEC}s)",
                     "Nothing is paging while this is red.")
    detail = f"last cycle {age:.0f}s ago"
    if agent.get("cycle_ok") is False:
        # A cycle that ran and failed is not a cycle that did not run. The
        # agent skips its dead-man ping in this state, so say so here too.
        return _step("agent", "degraded", detail + " · that cycle reported failures",
                     "the external dead-man's switch was not pinged for it")
    return _step("agent", "ok", detail)


# ---------------------------------------------------------------------------
# Engine loop health — the numbers that decide whether the container survives
# ---------------------------------------------------------------------------

#: Verdicts for the scan-cycle card, ordered worst first. Never two states:
#: "we were not told" is not "healthy", and it is not "broken" either.
LOOP_STATES = ("not_reported", "past_deadline", "pressure", "ok")


def reduce_host_resources(payload: Any) -> dict[str, Any]:
    """Is the box big enough — and is the deployed config the running one.

    The owner asked this first (*"engine cpu 221% used is our vps not enough or
    what"*) and no surface could answer it, because the raw percentage is
    meaningless alone. 221% against a 250% quota is a process at its ceiling
    whose scan loop cannot meet its deadline however well it is written; 221%
    against an unlimited 4-core box is a busy machine with a core spare. Same
    number, opposite next move — so this grades CPU **against the quota** and
    says which of the two it is looking at.

    Four states, and `unknown` outranks convenience: a reading that could not
    be taken renders under its own name. A 0.0% CPU figure over a pinned engine
    is worse than a blank, because a blank prompts a question.
    """
    block: dict[str, Any] = {}
    if isinstance(payload, dict):
        raw = payload.get("host_resources")
        if isinstance(raw, dict):
            block = raw

    out: dict[str, Any] = {
        "reported": False,
        "state": "not_reported",
        "cpu": None,
        "memory": None,
        "load": None,
        "disk": None,
        "config": None,
        "note": "",
        "error": "",
    }
    if not block:
        out["note"] = (
            "This engine build does not publish host resources. It is NOT a "
            "claim that the box is fine — the numbers are not being sent."
        )
        return out
    if block.get("error"):
        out["state"] = "unknown"
        out["error"] = str(block["error"])
        out["note"] = "The engine tried to sample the box and the sample raised."
        return out

    out["reported"] = True
    cpu = block.get("cpu") if isinstance(block.get("cpu"), dict) else {}
    out["cpu"] = cpu
    out["memory"] = block.get("memory")
    out["load"] = block.get("load")
    out["disk"] = block.get("disk")
    out["config"] = block.get("effective_config")
    out["sampled_at"] = block.get("sampled_at")

    pct = cpu.get("pct_of_quota")
    quota = cpu.get("quota_cores")
    if pct is None:
        out["state"] = "unknown"
        out["note"] = cpu.get("reason") or (
            "CPU could not be measured. That is a statement about the reading, "
            "not about the load."
        )
        return out

    # The quota being the whole host is its own finding: it means nothing is
    # capping this container, so a pinned engine is competing with every other
    # container on the box rather than being throttled by a number we chose.
    uncapped = bool(cpu.get("quota_is_host"))
    if pct >= 90:
        out["state"] = "at_ceiling"
        out["note"] = (
            f"The engine is using {cpu.get('cores_used')} of its {quota} allotted "
            f"cores ({pct}%). At this level the scan loop cannot meet its deadline "
            "however well it is written — the next lever is more CPU or less work "
            "per cycle, not another optimisation."
        ) + (
            " Note the quota equals the host's core count, so nothing is capping "
            "this container: it is competing with every other container on the box."
            if uncapped else ""
        )
    elif pct >= 70:
        out["state"] = "tight"
        out["note"] = (
            f"{pct}% of a {quota}-core quota. Working, with little headroom for a "
            "busy market — worth watching beside the scan-cycle worst case rather "
            "than acting on alone."
        )
    else:
        out["state"] = "ok"
        out["note"] = (
            f"{cpu.get('cores_used')} of {quota} cores ({pct}%). CPU is not what is "
            "limiting the loop; read the scan-cycle card for what is."
        )
    return out


def _reduce_cycle_stages(scan: dict[str, Any]) -> dict[str, Any]:
    """Where a slow scan cycle actually went — three states, never two.

    ``not_reported`` is an engine predating the stamp; ``none_yet`` is an engine
    that has the stamp and has recorded no slow cycle, which is the HEALTHY case
    and must not render as the same blank. Collapsing them would say "we cannot
    see inside a slow cycle" on a box that simply has not had one — the caption
    naming a cause the page cannot observe, which this repo has paid for on
    /invalidations and /truth.

    The breakdown existed the whole time and was LOGGED and nowhere else, so on
    2026-08-19 the owner's grep for it returned nothing while the deadline
    warnings beside it came through — the one question that aims the next fix
    had no answer on any surface.
    """
    out: dict[str, Any] = {
        "state": "not_reported",
        "worst": [],
        "last_slow": [],
        "last_slow_sec": None,
        "last_slow_at": None,
    }
    if "worst_stages" not in scan:
        return out

    def _rows(raw: Any) -> list[dict[str, Any]]:
        if not isinstance(raw, dict) or not raw:
            return []
        # The engine already orders these worst-first; ops sorts anyway rather
        # than trusting dict order across a JSON round trip, and carries the
        # total so a reader can see the ratio without adding the column up.
        items = sorted(
            ((str(k), float(v)) for k, v in raw.items() if isinstance(v, (int, float))),
            key=lambda kv: -kv[1],
        )
        total = sum(v for _, v in items)
        return [
            {
                "stage": k,
                "sec": round(v, 2),
                "share": round(100.0 * v / total, 1) if total > 0 else None,
            }
            for k, v in items
        ]

    out["worst"] = _rows(scan.get("worst_stages"))
    out["last_slow"] = _rows(scan.get("last_slow_stages"))
    out["last_slow_sec"] = scan.get("last_slow_sec") or None
    out["last_slow_at"] = scan.get("last_slow_at") or None
    out["state"] = "reported" if out["worst"] else "none_yet"
    return out


def reduce_loop_health(payload: Any) -> dict[str, Any]:
    """Grade the engine's own loop counters.

    Added 2026-08-19 with engine ``/internal/diag/loop-health``. The number
    this card exists for is **scan-cycle wall-time**: the scanner touches its
    heartbeat file once at the end of a cycle, ``healthcheck.py`` fails when
    that file is older than 120s, and three consecutive failures make autoheal
    restart the container. Measured on the live box that day: cycles of 9.2s to
    402.5s against a 15s target, and a restart inside the measurement window.

    None of it was readable anywhere. ``telemetry.scan_latency_ms`` was
    computed every cycle and reached a log line and a Telegram command — so the
    quantity that was restarting the engine appeared on no page, no probe and
    no report, which is why the restarts had no explanation and the only alert
    firing named redis.

    **Every bound comes from the engine.** ``warn_sec`` and ``kill_sec`` are
    published in the block, not chosen here — ops inventing a staleness bound
    is exactly what made ``/truth`` read STALE for 23 hours a day. When the
    engine does not send them the card says so rather than substituting a
    number of its own.
    """
    block = {}
    if isinstance(payload, dict):
        raw = payload.get("loop_health")
        if isinstance(raw, dict):
            block = raw

    out: dict[str, Any] = {
        "reported": bool(block),
        "state": "not_reported",
        "scan": None,
        "writer": None,
        "edge": None,
        "cache": None,
        "note": "",
    }
    if not block:
        out["note"] = (
            "This engine build does not publish loop health. It is NOT a claim "
            "that the loop is fine — the numbers simply are not being sent."
        )
        return out

    scan = block.get("scan_cycle")
    out["writer"] = block.get("snapshot_writer")
    out["edge"] = block.get("strategy_edge")
    # Top-level, because that is where the engine writes it. Reading it off
    # `scan_cycle` would have been a field this repo reads and no repo writes
    # — the card would render empty forever and look like a quiet cache.
    cache = block.get("indicator_cache")
    out["cache"] = cache if isinstance(cache, dict) else None
    if not isinstance(scan, dict) or not scan.get("cycles"):
        out["note"] = (
            "The engine is reporting, but the scanner has not completed a cycle "
            "yet — a young process, not a stalled one."
        )
        out["state"] = "not_reported"
        return out

    out["scan"] = scan
    out["stages"] = _reduce_cycle_stages(scan)
    warn = scan.get("warn_sec")
    kill = scan.get("kill_sec")
    cycles = int(scan.get("cycles") or 0)
    over_warn = int(scan.get("over_warn") or 0)
    over_kill = int(scan.get("over_kill") or 0)
    out["over_warn_pct"] = round(100.0 * over_warn / cycles, 1) if cycles else None
    out["bounds_reported"] = warn is not None and kill is not None

    # Boot warm-up is a separate population and never folded into the verdict.
    # A cold start re-seeds every pair over REST and rebuilds the indicator
    # caches, so its first cycles legitimately run long — 74.5s / 131.2s / 72.8s
    # measured after a real deploy against a steady state of 8-47s, and
    # `healthcheck.py` holds its own grace for exactly that. Grading them made
    # this card read PAST THE DEADLINE for the whole life of a healthy boot,
    # which is a dead instrument rather than a warning.
    boot_kill = int(scan.get("over_kill_boot") or 0)
    boot_warn = int(scan.get("over_warn_boot") or 0)
    out["boot_over_kill"] = boot_kill
    out["boot_over_warn"] = boot_warn
    out["boot_reported"] = "over_kill_boot" in scan

    # A cycle that has NOT finished is graded first, because every counter
    # below it records completion only. On 2026-08-19 this card read
    # "0 past the deadline, last cycle 20.76s" while autoheal was restarting the
    # engine on a failing streak of 3 — healthy-looking precisely while the
    # container was being killed. `healthcheck.py` kills on heartbeat age, the
    # heartbeat is touched once per COMPLETED cycle, so a hung cycle appears in
    # none of over_warn / over_kill / worst_sec.
    in_flight = scan.get("in_flight_sec")
    beat_age = scan.get("heartbeat_age_sec")
    out["in_flight_sec"] = in_flight
    out["heartbeat_age_sec"] = beat_age
    # Partial stage sums for the cycle still running. The worst-cycle breakdown
    # is captured at COMPLETION, so a hung cycle contributes nothing to it —
    # these are the only stages a hang can report, and they name the await it
    # is stuck on.
    flight_stages = scan.get("in_flight_stages")
    out["in_flight_stages"] = [
        {"stage": str(k), "sec": round(float(v), 2)}
        for k, v in sorted(
            (flight_stages or {}).items(),
            key=lambda kv: -float(kv[1]),
        )
        if isinstance(v, (int, float))
    ]
    out["hang_reported"] = "in_flight_sec" in scan

    worst_age = max([v for v in (in_flight, beat_age) if v is not None], default=None)
    if kill is not None and worst_age is not None and worst_age > kill:
        out["state"] = "hanging"
        out["note"] = (
            f"A scan cycle has been running {in_flight}s and the heartbeat is "
            f"{beat_age}s old, against a {kill:.0f}s deadline — the container is "
            "being killed right now, or is about to be. None of the counters "
            "below can show this: they record cycles at completion, and this one "
            "has not completed."
        )
        return out

    if over_kill:
        out["state"] = "past_deadline"
        out["note"] = (
            f"{over_kill} scan cycle(s) have run past the {kill:.0f}s healthcheck "
            "deadline since this boot. Sustained across three checks that restarts "
            "the container — and every restart expires the snapshot:* keys, so the "
            "dashboard and the app feed go empty while it happens."
        )
    elif cycles >= 10 and over_warn / cycles > 0.5:
        out["state"] = "pressure"
        out["note"] = (
            f"{over_warn} of {cycles} cycles are past the {warn:.0f}s warn bound. "
            "Nothing has been restarted, but the deadline is one busy market away."
        )
    else:
        out["state"] = "ok"
        out["note"] = (
            f"Worst cycle {scan.get('worst_sec')}s against a {kill:.0f}s deadline."
            if kill is not None else ""
        )
        if boot_kill:
            out["note"] += (
                f" {boot_kill} cycle(s) ran long during boot warm-up and are "
                "counted apart — a cold start re-seeds every pair over REST, and "
                "the healthcheck holds its own grace for it."
            )

    return out
