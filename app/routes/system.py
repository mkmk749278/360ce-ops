"""`/system` — is the box alive, and if not, which part of it is not.

Built 2026-08-18 for the owner, who had two symptoms and no surface joining
them: a HIGH `redis_unreachable` page firing and recovering over and over, and
a dashboard whose engine-backed pages had stopped answering, with no way to
tell whether the engine was even running.

They are one event. `redis-cli` exits 0 and prints nothing for a **missing
key**, and every `snapshot:*` key carries a TTL of twice its write interval —
so an engine that stalls for a minute expires the keys, the api container has
nothing to serve, and the only alert raised blames redis. That misclassification
is fixed in `app/agent/detectors.py`; this is the surface that would have made
it obvious in the first place.

Three pages, because they answer three questions with three different next
moves, and one page pooling them is how the answer stops being findable on a
phone at 3am:

* **`/system`** — every container: is it there, is it healthy, and *how many
  times has it been restarted*. The restart count is the number that separates
  "quiet" from "in an autoheal loop", and neither repo rendered it anywhere.
* **`/system/liveness`** — the chain from engine process to this page, hop by
  hop, each with the repo that owns its fix. A single verdict cannot say which
  link broke.
* **`/system/redis`** — the snapshot bridge: reachability and key census kept
  deliberately apart, because conflating them is the whole defect.

Rules these pages hold to
-------------------------
* **Guest-readable, all three.** This is diagnosis, not control — there is no
  write surface here and nothing on it is subscriber data. The one page you
  want to be able to hand someone at 3am must not be the one behind the
  strictest gate.
* **`unknown` is not `ok`.** Every probe that could not run says so under its
  own name. A check that renders green when it could not be performed is worse
  than no check, because it is a claim.
* **The page names the container that can be fixed.** Every broken link carries
  the repo that owns it — the 2026-08-18 alert was accurate about everything
  except that, and that was the only part the owner needed.
* **Ops does not grade the engine on ops' clock.** Container ages come from
  Docker, engine uptime from the engine's pulse, key ages from redis. The one
  number ops times itself — its own HTTP round trip — is labelled as ops'.
"""
from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from app.data_sources import system_health

router = APIRouter()


async def _api_probe(request: Request) -> dict[str, Any]:
    """Ops' own round trip to the engine API, timed by ops and labelled so.

    Deliberately hits `/api/health` and `/api/pulse` rather than reading a
    cached payload: the question this page answers is whether the API is
    answering *now*, and a cache would answer it with a memory of when it last
    did — the `/signals/sar-live` mistake (a surface grading its own liveness
    on a clock it supplies), one repo over.
    """
    api = request.app.state.engine_api
    loop = asyncio.get_running_loop()
    started = loop.time()
    try:
        health, pulse = await asyncio.gather(api.health(), api.pulse())
    except Exception as exc:  # noqa: BLE001 — reported to the page, never raised
        return {
            "error": f"{type(exc).__name__}: {exc}",
            "latency_ms": int((loop.time() - started) * 1000),
            "health": {}, "pulse": {},
        }
    latency = int((loop.time() - started) * 1000)
    err = None
    if isinstance(health, dict) and health.get("error"):
        err = str(health["error"])
    if isinstance(pulse, dict) and pulse.get("error"):
        err = str(pulse["error"])
    return {
        "error": err,
        "latency_ms": latency,
        "health": health if isinstance(health, dict) else {},
        "pulse": pulse if isinstance(pulse, dict) else {},
    }


async def _loop_health_probe(request: Request) -> dict[str, Any]:
    """The engine's own loop counters — scan-cycle wall-time above all.

    Read from the engine rather than timed here, by the same rule that keeps
    every other number on these pages on its producer's clock. The bounds it is
    graded against (`warn_sec`, `kill_sec`) also come from the engine: ops
    inventing a threshold is what made /truth read STALE for 23 hours a day.
    """
    api = request.app.state.engine_api
    try:
        return await api.loop_health() or {}
    except Exception as exc:  # noqa: BLE001 — reported to the page, never raised
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _host_resources_probe(request: Request) -> dict[str, Any]:
    """CPU/memory/disk headroom, from the engine's own reading of its cgroup.

    Not measured here for the same reason the loop counters are not: this is a
    different container. A local sample would report ops' near-idle web process
    under the heading "engine" — a full-looking answer about the wrong thing.
    """
    api = request.app.state.engine_api
    try:
        return await api.host_resources() or {}
    except Exception as exc:  # noqa: BLE001 — reported to the page, never raised
        return {"error": f"{type(exc).__name__}: {exc}"}


async def _agent_probe(request: Request) -> dict[str, Any]:
    reader = getattr(request.app.state, "agent_alerts", None)
    if reader is None:
        return {"present": False, "error": "no agent-alerts reader configured", "age_sec": None}
    try:
        return await reader.heartbeat()
    except Exception as exc:  # noqa: BLE001
        return {"present": False, "error": f"{type(exc).__name__}: {exc}", "age_sec": None}


async def _gather(request: Request) -> dict[str, Any]:
    """One collection behind all three pages.

    Deliberately shared: three pages assembling their own view of the same box
    is three chances for them to disagree, and a reader moving between them
    cannot tell a real change from two collectors. Same rule the dark-feed and
    promotion pages follow with one reducer.
    """
    settings = request.app.state.settings
    containers, redis, api, agent, loop_raw, host_raw = await asyncio.gather(
        system_health.collect_containers(),
        system_health.collect_redis(),
        _api_probe(request),
        _agent_probe(request),
        _loop_health_probe(request),
        _host_resources_probe(request),
    )
    host = system_health.collect_host(settings.engine_data_dir)
    chain = system_health.build_chain(containers, redis, api, agent)
    return {
        "containers": containers,
        "redis": redis,
        "api": api,
        "agent": agent,
        "host": host,
        "loop": system_health.reduce_loop_health(loop_raw),
        # Measured in the ENGINE container and read here. Ops runs on a
        # different cgroup, so sampling locally would describe this process
        # while looking exactly like a reading of the engine.
        "resources": system_health.reduce_host_resources(host_raw),
        "chain": chain,
        # Keyed, so a page can render ONE link's verdict without re-deriving
        # it. Every card that asked its own question of the raw payload got a
        # different answer from the chain beside it — the Redis page called
        # redis "NO ANSWER · this one is the container" on a box whose docker
        # socket was simply absent, which is the misclassification this whole
        # change exists to end, committed by the template. One writer, one
        # reader: the verdict is computed in build_chain and nowhere else.
        "link": {s["key"]: s for s in chain},
        # The headline is derived from the chain rather than computed twice.
        # A summary that disagrees with the table under it is the defect this
        # repo has paid for under three different names.
        "broken": [s for s in chain if s["state"] == "broken"],
        "unknown": [s for s in chain if s["state"] == "unknown"],
        "degraded": [s for s in chain if s["state"] == "degraded"],
    }


@router.get("/system")
async def system_containers(request: Request):
    data = await _gather(request)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "system_containers.html",
        {"request": request, "active": "system_containers", **data},
    )


@router.get("/system/liveness")
async def system_liveness(request: Request):
    data = await _gather(request)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "system_liveness.html",
        {"request": request, "active": "system_liveness",
         "chain_steps": system_health.CHAIN_STEPS,
         "agent_stale_sec": system_health.AGENT_STALE_SEC, **data},
    )


@router.get("/system/redis")
async def system_redis(request: Request):
    data = await _gather(request)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "system_redis.html",
        {"request": request, "active": "system_redis",
         "ops_depends_on": system_health.OPS_DEPENDS_ON, **data},
    )
