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
#: The engine answered and the READ failed — a stalled bridge, a mid-cycle
#: timeout, an entry that raised. Split out of `not_reported` on 2026-09-03,
#: when a diag-bridge timeout rendered as *"the engine has no
#: `read.ai_governor` catalog entry … an engine predating this page, so it is a
#: deploy question"* over an engine that had the entry, was answering, and
#: showed it in the console one tab away. The engine's own error string said
#: exactly what happened and this page threw it away to print a cause it cannot
#: observe — `/invalidations`' WRITER STALE and `/dark-signals`' hardcoded ban
#: cause, arriving at the newest lane. It is INTERMITTENT, which is worse: a
#: reload shows data and the reader concludes nothing was ever wrong.
STATE_ENGINE_ERROR = "engine_error"
STATE_EMPTY = "empty"
STATE_OK = "ok"

#: The one error text that genuinely means "an engine predating this page".
#: `src/diag_catalog.run` writes it verbatim for a key it does not know; every
#: other `ok: false` is the engine failing to answer a key it HAS.
UNKNOWN_ENTRY_MARKER = "unknown catalog entry"

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


#: Why a thesis could not be graded. Same discipline as `REFUSAL_COPY`: looked
#: up FROM the engine's payload, never iterated here, so a reason the engine
#: adds tomorrow renders under its raw name instead of vanishing.
UNDECIDABLE_COPY: Dict[str, str] = {
    "still_open_or_undelivered": "No closed-signal record yet — the trade is "
                                 "open, or the router never delivered it. A "
                                 "wait, not a fault.",
    "no_pnl": "The record carries no readable PnL, so the row is counted and "
              "excluded rather than clamped to zero.",
    "no_excursion_stamp": "The excursion was never stamped, so 'would the "
                          "nearer target have been reached' is unanswerable. "
                          "Refused by name — unreached and unknown remove "
                          "opposite ends of the distribution.",
    "choice_not_in_menu": "The chosen key is not in the menu stored with that "
                          "verdict. A ledger fault, not a row to drop quietly.",
    "candidate_has_no_distance": "The stored candidate carries no distance, so "
                                 "nothing can be compared against the excursion.",
    "arm_undecidable_while_dark": "Nothing was applied, so the record shows what "
                                  "happened WITHOUT this arm. Deciding it needs a "
                                  "live window; for the SL arm two of its cases "
                                  "are not in the record at all.",
}


def blindness_state(block: Any) -> str:
    """Three states, never two.

    `unmeasured` is not `0% blind`. A lane that has been asked nothing has no
    blindness reading, and rendering zero there reports a fully-informed
    governor on an empty one — the flattering direction of the error, which is
    the dangerous one on a money-path panel.
    """
    if not isinstance(block, dict) or not block:
        return STATE_NOT_REPORTED
    return "measured" if block.get("measured") else "unmeasured"


def classify(payload: Any) -> str:
    """Grade the diag result without asserting a cause we cannot observe.

    There are exactly TWO producers and they are told apart by the `ok` key,
    never by whether `error` is truthy:

    * the ENGINE's own catalog envelope (`src/diag_catalog.run`) always carries
      `ok`, and always carries `error` — **empty on success**;
    * ops' transport wrapper (`engine_api._get` / `_post`) carries `error` and
      `endpoint` and no `ok` at all.

    Reading `if payload.get("error")` cannot separate them, and on 2026-09-03 it
    did not: an ops-side read timeout produced `{"error": "", "endpoint": …}`,
    whose empty message is falsy, so the payload sailed past the unreachable
    branch and was graded on its SHAPE — which, lacking `measure_enabled`,
    reads as an engine that has never heard of this entry. The page then told
    the owner it was a deploy question, beside a console listing the entry.
    """
    if not isinstance(payload, dict):
        return STATE_UNREACHABLE

    if "ok" in payload:
        # The engine answered — so this is never "unreachable". WHICH failure
        # it is comes from the engine's own words, never from what this page
        # assumes: only an unknown key means a build predating the entry.
        if payload.get("ok") is False:
            if UNKNOWN_ENTRY_MARKER in str(payload.get("error") or ""):
                return STATE_NOT_REPORTED
            return STATE_ENGINE_ERROR
    elif "error" in payload:
        # Ops' own client failed. Key presence, not truthiness — a timeout
        # carries no message and an empty cause is still a failure.
        return STATE_UNREACHABLE

    out = payload.get("result") if "result" in payload else payload
    if not isinstance(out, dict):
        return STATE_UNREACHABLE
    if "measure_enabled" not in out:
        return STATE_NOT_REPORTED
    return STATE_OK


def classify_scorecard(payload: Any) -> str:
    """Grade the scorecard read on ITS OWN shape, not the lane's.

    `classify` keys on `measure_enabled`, which only the lane entry carries — so
    running a scorecard payload through it would grade every healthy scorecard
    as an engine predating the page. That is the shape-vs-path defect this file
    already records twice, and it was one line away from shipping again.

    The transport and envelope rules are identical, so they are reused rather
    than re-implemented; only the shape key differs.
    """
    if not isinstance(payload, dict):
        return STATE_UNREACHABLE
    if "ok" in payload:
        if payload.get("ok") is False:
            if UNKNOWN_ENTRY_MARKER in str(payload.get("error") or ""):
                return STATE_NOT_REPORTED
            return STATE_ENGINE_ERROR
    elif "error" in payload:
        return STATE_UNREACHABLE

    out = payload.get("result") if "result" in payload else payload
    if not isinstance(out, dict):
        return STATE_UNREACHABLE
    # The engine's own "I could not compute this" — a rendered state, not a
    # transport failure, and named apart because the next move differs.
    if "error" in out:
        return STATE_ENGINE_ERROR
    if "coverage" not in out:
        return STATE_NOT_REPORTED
    return STATE_OK


def engine_error(payload: Any) -> str:
    """What the engine said, for the page to quote rather than paraphrase.

    An empty string is "the engine gave no reason", which the banner names as
    such — a blank needs a cause before it gets a caption, and inventing one is
    the defect this function exists to stop.
    """
    if not isinstance(payload, dict):
        return ""
    return str(payload.get("error") or "")


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
            # NOT `copy`. Jinja resolves an attribute before an item, so
            # `row.copy` finds `dict.copy` — the throttle table rendered
            # `<built-in method copy of dict object at 0x…>` at the reader on
            # the day this page shipped, and the refusal table was one refusal
            # away from doing the same. Identical to the `redis.keys` collision
            # on `/system/redis`. A payload key must not collide with a dict
            # method, and `tests/test_ai_governor_page.py` now forbids the name.
            "meaning": copy.get(name, ""),
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

    # A SECOND call, on purpose. The scorecard parses the closed-signal record
    # off disk; folded into the entry above it made `read.ai_governor` blow its
    # 25s budget in production while every other catalog entry answered in
    # 0.0s. Two costs, two entries — and fetched separately here so a slow or
    # absent scorecard cannot take the arms, bounds and refusals down with it.
    # This page renders whatever half it got.
    try:
        raw_score = await api.diag_run("read.ai_governor_scorecard", {})
    except Exception as exc:  # pragma: no cover - defensive
        raw_score = {"error": f"{type(exc).__name__}: {exc}"}

    diag = _unwrap(raw)
    health = diag.get("health") if isinstance(diag.get("health"), dict) else {}
    bounds = diag.get("bounds") if isinstance(diag.get("bounds"), dict) else {}

    blindness = diag.get("blindness") if isinstance(diag.get("blindness"), dict) else {}
    scorecard = _unwrap(raw_score)
    # `scorecard.error` is the ENGINE saying it could not compute; a transport
    # failure is ops' own. Both render, and they are named apart because one is
    # a deploy or a data question and the other is a network one.
    score_state = classify_scorecard(raw_score)

    # Each arm's refusals annotated the same way the lane's own are: iterate the
    # ENGINE's counts and look the sentence up. An arm with no rows still
    # renders — a missing arm reads as one that never fired, and those are
    # opposite facts.
    score_arms = []
    for name, block in sorted((scorecard.get("arms") or {}).items()):
        if not isinstance(block, dict):
            continue
        score_arms.append({
            "arm": name,
            "block": block,
            "undecidable_rows": annotate(block.get("undecidable"), UNDECIDABLE_COPY),
        })

    templates = request.app.state.templates
    return templates.TemplateResponse(
        "ai_governor.html",
        {
            "request": request,
            "active": "ai_governor",
            "state": classify(raw),
            "engine_error": engine_error(raw),
            "diag": diag,
            "lane": lane_state(diag),
            "health": health,
            "bounds": bounds,
            "arms": diag.get("arms") or [],
            "refusals": annotate(health.get("refusals"), REFUSAL_COPY),
            "throttles": annotate(health.get("throttles"), THROTTLE_COPY),
            "actions": annotate(health.get("by_action"), {}),
            "provider_status": annotate(health.get("provider_status"), {}),
            # The counts say how many failed; these say WHAT the provider
            # objected to. `bad_json` alone covers a truncated answer, a
            # wrong-typed one and an error envelope — three different fixes,
            # and the vendor already told us which.
            "provider_failures": list(reversed(health.get("provider_failures") or [])),
            "served_models": health.get("served_models") or {},
            # How much context the recent verdicts actually had. The per-row
            # stamp existed from the day the lane shipped and nothing
            # aggregated it, so no surface could say whether a MAINTAIN was
            # informed or blind — which makes every verdict on this page
            # uninterpretable in EITHER direction, not merely unexplained.
            "blindness": blindness,
            "blindness_state": blindness_state(blindness),
            # Every thesis graded against the closed-signal record. Read the
            # coverage line before any delta: a scorecard over the rows that
            # happened to close is not a scorecard over the book.
            "scorecard": scorecard,
            "score_state": score_state,
            "score_arms": score_arms,
            "shadow_note": str(scorecard.get("shadow_note") or ""),
        },
    )
