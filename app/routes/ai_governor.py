"""`/signals/ai-governor` — what the governor decided, and what it refused.

Engine side: `src/execution/ai_governor.py` in `mkmk749278/360-v2`, design of
record `docs/PLAN_AI_TRADE_GOVERNOR.md`. Shipped 2026-09-02 with the
measurement flag ON and the effect flag OFF, which is precisely why this page
exists on the same day: **dark work must be observable, or it is not dark, it
is just off.** A lane measured with nowhere to look is an unfinished change,
and this repo has paid for that under several names.

Everything here is read from the ENGINE, through the diagnostic catalog
(`read.ai_governor`). Ops computes nothing about the governor. The api
container has never evaluated a candidate and cannot see the arms or the
position index, so a version of this assembled locally would report a healthy
zero — the `INDEX COLD` defect, and the promotion census before it.

Three rules this page carries, each already in this file's siblings:

* **Refusals and throttles are never pooled.** `cooldown` means the lane found
  an arm it was willing to evaluate and deliberately did not — positive
  evidence it is working. Bucketed with the refusals it reads as a blocked
  governor, which is #816 arriving from the display side.
* **There is no blended cross-arm figure**, and a route test asserts none
  appears. Three of the four arms are decidable from the closed-signal record
  and one is not, so a single number over all four would move with the SL arm's
  refusal rate rather than with the mechanism.
* **`panic_armed: false` is a STATE, rendered, never an absent row.** The panic
  arm refuses while its position ceiling is unset, and a missing row would read
  as an arm that is simply quiet.
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Request

router = APIRouter()

#: What a reading means when it is not a reading. Mirrors `firestore_cost`'s
#: three states because the failure modes are the same ones, and the next move
#: differs for each: a network check, a deploy, or a shrug.
STATE_UNREACHABLE = "unreachable"
STATE_NOT_REPORTED = "not_reported"
STATE_EMPTY = "empty"
STATE_OK = "ok"

#: Copy for each refusal the engine can emit. Looked up FROM THE ENGINE'S
#: PAYLOAD rather than iterated — rendering `for reason in REFUSAL_COPY` would
#: be silent by construction on the next reason the engine adds, which is the
#: drifting-mirror defect wearing yet another hat. A reason ops has never heard
#: of renders under its raw name, badged `unclassified`.
REFUSAL_COPY: Dict[str, str] = {
    "disabled": "The measurement flag is off. Nothing is being recorded.",
    "not_configured": "No provider API key is set, so no call is made. A "
                      "decision nobody has taken yet — not a failure.",
    "kill_switch": "The kill switch is engaged. Asking a model whether to act "
                   "is the first half of acting, so the lane stands down and "
                   "existing protection stays exactly where it is.",
    "index_cold": "The in-process position index could not answer. Deliberately "
                  "NOT falling back to Firestore — that read is the one that "
                  "scales with the subscriber count.",
    "budget_exhausted_calls": "The per-signal or per-hour call bound was hit.",
    "budget_exhausted_spend": "The daily USD cap was hit. The governor "
                              "degrades to MAINTAIN rather than spending on.",
    "no_series": "No usable candle series for the signal's trigger timeframe.",
    "tf_unknown": "The setup declares no trigger timeframe, so it is refused "
                  "rather than defaulted to 5m.",
    "stale_verdict": "The verdict aged out before it could be applied. The "
                     "world has moved on; applying it late is worse than "
                     "doing nothing.",
    "unknown_choice": "The model named a level that was not in this position's "
                      "menu. Bounded by construction — this is the refusal "
                      "that makes a hallucinated key harmless.",
    "unknown_signal": "The model answered about a position we did not ask "
                      "about.",
    "unknown_action": "The model returned a verdict outside the four allowed.",
    "not_monotone": "The choice would have widened a stop or moved a target "
                    "further away. Refused in code, never trusted to the prompt.",
    "arm_off": "That arm is not in the armed set.",
    "apply_off": "The effect flag is off. The verdict is recorded and applied "
                 "to nothing — this is what dark means.",
    "panic_ceiling_unset": "The panic arm refuses while its position ceiling is "
                           "0. A blast-radius cap that falls back to unbounded "
                           "is not a cap.",
    "panic_ceiling_hit": "More positions than the ceiling allows. Refused and "
                         "named rather than truncated to the first N — closing "
                         "an arbitrary subset of a correlated book is a "
                         "different action from the one asked for.",
    "apply_paced": "Deferred to the next tick by the exchange-call budget. The "
                   "existing protection stays exactly where it is while it "
                   "waits.",
    "trail_governed": "A trail mechanism already owns that stop. Two modules "
                      "must never move one stop.",
    "verdict_queue_full": "The apply path is not keeping up. A fault to "
                          "surface, not a depth to absorb.",
}

#: The throttles. Kept in their OWN table, not merged into the one above.
THROTTLE_COPY: Dict[str, str] = {
    "cooldown": "An arm was eligible and deliberately not evaluated. Positive "
                "evidence the lane is running — never a refusal.",
}


def classify(payload: Any) -> str:
    """Grade the diag result without asserting a cause we cannot observe."""
    if not isinstance(payload, dict):
        return STATE_UNREACHABLE
    if payload.get("ok") is False:
        # Checked BEFORE the generic error key: the engine ANSWERED and refused
        # the key, which means a build predating the entry. Reading that as
        # unreachable sends the operator to check a network that is fine.
        return STATE_NOT_REPORTED
    if payload.get("error"):
        return STATE_UNREACHABLE
    out = payload.get("result") if "result" in payload else payload
    if not isinstance(out, dict):
        return STATE_UNREACHABLE
    if "measure_enabled" not in out:
        return STATE_NOT_REPORTED
    return STATE_OK


def _unwrap(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    out = payload.get("result") if "result" in payload else payload
    return out if isinstance(out, dict) else {}


def annotate(counts: Any, copy: Dict[str, str]) -> List[Dict[str, Any]]:
    """Pair each count the ENGINE reported with its sentence.

    Iterates the payload, never `copy` — so a reason this page has never heard
    of appears under its raw name rather than vanishing. One writer, one
    reader, and the drift is visible instead of absent.
    """
    if not isinstance(counts, dict):
        return []
    rows = []
    for name, n in sorted(counts.items(), key=lambda kv: (-int(kv[1] or 0), kv[0])):
        rows.append({
            "name": name,
            "count": int(n or 0),
            "copy": copy.get(name, ""),
            "unclassified": name not in copy,
        })
    return rows


def lane_state(diag: dict) -> str:
    """Which of the lane's worlds we are in, in the order an operator reads.

    Deliberately more than a boolean: "measuring but the provider is not
    configured" is the state this lane ships in, and it is neither working nor
    broken. Collapsing it into either would send the owner to fix the wrong
    thing.
    """
    if not diag:
        return "unknown"
    if not diag.get("measure_enabled"):
        return "off"
    if not diag.get("provider_configured"):
        return "not_configured"
    if diag.get("apply_enabled"):
        return "enforcing"
    return "measuring"


@router.get("/signals/ai-governor")
async def ai_governor(request: Request):
    api = request.app.state.engine_api
    try:
        raw = await api.diag_run("read.ai_governor", {})
    except Exception as exc:  # pragma: no cover - defensive
        raw = {"error": f"{type(exc).__name__}: {exc}"}

    diag = _unwrap(raw)
    health = diag.get("health") if isinstance(diag.get("health"), dict) else {}
    bounds = diag.get("bounds") if isinstance(diag.get("bounds"), dict) else {}

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "ai_governor.html",
        {
            "request": request,
            "active": "ai_governor",
            "state": classify(raw),
            "diag": diag,
            "lane": lane_state(diag),
            "health": health,
            "bounds": bounds,
            "arms": diag.get("arms") or [],
            "refusals": annotate(health.get("refusals"), REFUSAL_COPY),
            "throttles": annotate(health.get("throttles"), THROTTLE_COPY),
            "actions": annotate(health.get("by_action"), {}),
            "provider_status": annotate(health.get("provider_status"), {}),
            "served_models": health.get("served_models") or {},
        },
    )
