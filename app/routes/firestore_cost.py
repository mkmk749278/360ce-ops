"""`/system/firestore` — what the read allowance is being spent on, and what it
becomes at 1,000 members (2026-09-02).

On 2026-09-02 Firestore answered ``RESOURCE_EXHAUSTED`` at 00:41 UTC on 53,000
document reads against a 50,000/day allowance, and every Firestore-backed path
failed together: the keystore (so every signal fanned out to zero users), the
kill switch (so the emergency stop 503'd — the stop and the thing it stops
failing at once), the runtime tunables and the dispatch log.

**That budget is a hard ceiling, not a bill.**  Past it a project whose billing
account is not in good standing is refused, so this page is a safety surface
rather than a finance one, and it belongs beside the container X-rays.

The owner's auto-trade target is **1,000 members**, which is the number this
page exists for: every per-user read is invisible at one user and linear in
subscribers, so nothing in the console, the bill or the census says a word
about it until the subscribers arrive.  The engine's own projection is
rendered here rather than recomputed — ops ports the engine's arithmetic, it
does not invent it, and a second implementation of a cost model is a mirror
that drifts silently in the flattering direction.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()

#: What a reading means when it is not a reading.  Three states with three
#: different next moves, never pooled into "no data": the engine is unreachable
#: (a network question), the endpoint is absent (a deploy question), or the
#: census ran and has nothing (a quiet process, which after a restart is the
#: correct and uninteresting answer).
STATE_UNREACHABLE = "unreachable"
STATE_NOT_REPORTED = "not_reported"
STATE_EMPTY = "empty"
STATE_OK = "ok"


def classify(payload: Any) -> str:
    """Grade one diag result without asserting a cause we cannot observe."""
    if not isinstance(payload, dict):
        return STATE_UNREACHABLE
    if "ok" in payload:
        # The ENGINE's own envelope. It answered, so this is never
        # "unreachable" — an unknown catalog key means a build predating the
        # entry, which is a deploy question, and reading it as unreachable
        # sends the operator to check a network that is fine.
        if payload.get("ok") is False:
            return STATE_NOT_REPORTED
    elif "error" in payload:
        # Ops' own transport wrapper (`engine_api._get` / `_post`), which
        # carries no `ok`. KEY PRESENCE, not truthiness: `str(ReadTimeout())`
        # is the empty string, so the old check read a timed-out call as a
        # clean one and this page then graded it on shape — landing on EMPTY,
        # "running, nothing recorded", which is the benign caption for an
        # outage. Same defect `/signals/ai-governor` shipped, one page over;
        # the engine envelope carries `error` on success too, which is why
        # only `ok` can tell the two producers apart.
        return STATE_UNREACHABLE
    out = payload.get("result") if "result" in payload else payload
    if not isinstance(out, dict):
        return STATE_UNREACHABLE
    if not out.get("sites"):
        return STATE_EMPTY
    return STATE_OK


def _unwrap(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    out = payload.get("result") if "result" in payload else payload
    return out if isinstance(out, dict) else {}


async def _run(api, key: str, args: dict | None = None) -> Any:
    """One catalog call, converted to a payload rather than an exception.

    A failure here must render as a named state, never as a 500 — this page is
    read when something is already wrong.
    """
    try:
        return await api.diag_run(key, args or {})
    except Exception as exc:  # pragma: no cover - defensive
        return {"error": f"{type(exc).__name__}: {exc}"}


@router.get("/system/firestore")
async def system_firestore(request: Request):
    api = request.app.state.engine_api
    try:
        members = int(request.query_params.get("members") or 1000)
    except (TypeError, ValueError):
        members = 1000
    members = max(1, min(members, 1_000_000))
    try:
        current = int(request.query_params.get("current") or 1)
    except (TypeError, ValueError):
        current = 1
    current = max(1, current)

    projection_raw = await _run(
        api, "read.firestore_projection",
        {"members": members, "current_members": current},
    )
    census_raw = await _run(api, "read.firestore_reads")
    gen_raw = await _run(api, "read.control_generation")

    projection = _unwrap(projection_raw)
    census = _unwrap(census_raw)
    generation = _unwrap(gen_raw)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "system_firestore.html",
        {
            "request": request,
            "active": "system_firestore",
            "members": members,
            "current": current,
            "projection": projection,
            "projection_state": classify(projection_raw),
            "census": census,
            "census_state": classify(census_raw),
            "generation": generation,
            # The generation block has no `sites` key, so it is graded on
            # reachability alone rather than on the census classifier — a
            # shared classifier applied to a different shape is how a healthy
            # panel reads as empty.
            "generation_ok": isinstance(generation, dict)
            and "documents" in generation,
        },
    )
