"""Layer G — the autonomous emission controller, and what its action space costs.

Layer G (engine: ``src/emission_controller.py``) is the outer loop of the
Autonomous Portfolio: it reads the measured gate verdicts and the Strategy×Context
edge matrix and moves the per-strategy emission overrides **itself**, with no
human in the loop, inside a bounded envelope. It has been live and self-promoting
on the money path since S72 — and until this page it had **no ops surface at
all**. That is the gap this route closes, and it is not a small one: a
money-path tuner nobody can watch is exactly the "measured but nowhere to look"
state the doctrine calls an unfinished change.

It is also how the bug this page leads with went unnoticed for 279 cycles.

**The routability finding (engine #806, measured 2026-07-27).** The controller
keys its *inputs* by **matrix** strategy — which carries the measurement arms
(``X@ATR``, ``X@FIXED``, …) and the shadow-only ``SHADOW_*`` units — but its
*output* is read by ``resolve_min_samples(setup_class)``, which the scanner only
ever calls with a live ``SetupClass`` value. So an override stored under any
other key is unreachable by construction: persisted, logged as applied, listed
below as an "active override", and read by nothing. At the time of the finding
**9 of 18** persisted overrides were dead keys and **23 of 40** lifetime
promotions had gone to them.

The phantoms do not merely leak — they *outcompete* the real rows for a
2-per-cycle budget. An arm never emits, so its ``n_emitted`` stays 0, the
strategy-health signal never reaches its sample floor, and the auto-tighten brake
that holds back a real strategy can never fire on it. And every ``min_samples``
candidate carries no EV, so they all tie in the blast-radius sort and the tie
resolves alphabetically — where ``QUIET_COMPRESSION_BREAK@ATR`` sits ahead of
``RANGE_FADE``.

Two rules this page follows, both from the ops brief:

* **Ops renders the engine's classification; it never re-derives it.** Every
  ledger row carries a ``routable`` flag stamped by the engine. A mirrored suffix
  list here would be the same drift class that inflated the Strategy Lab rollup
  for a week — so there is deliberately no suffix constant in this module.
* **Every count is measured on the population shown beside it.** The lifetime
  attribution is computed over the ledger rows the table renders; the latest-cycle
  panel is explicitly labelled as one cycle. Rows the engine could not classify
  are reported as ``unclassified`` rather than silently folded into either side —
  a blank needs a cause before it gets a caption.

Read-only, like every diagnostic surface. Enforcement is flipped from Control →
Signal gating (``emission_controller_routable_live``), never from here.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()

# Statuses mirror the engine's emission_controller module.
STATUS_PROMOTED = "PROMOTED"
STATUS_PENDING = "PENDING"
STATUS_PRUNED = "PRUNED"


def _rows(payload: Any, key: str) -> list[dict]:
    """A list-of-dicts member of the store payload, defensively.

    The engine REST/volume surface is the source of truth and this dashboard
    adapts to it, so a shape drift yields an empty section rather than a 500.

    Every row is normalised to carry an **explicit** ``routable`` key. That is not
    cosmetic: in Jinja, ``row.routable`` on a dict that lacks the key yields
    ``Undefined``, and ``Undefined is none`` is **False** — so a
    ``none``/truthy/else chain in the template silently lands on the else branch
    and renders a pre-stamp row as *unroutable*. That shipped, and it asserted the
    opposite of the truth for real strategies (``MOVER_AVWAP_SCALP``,
    ``DIVERGENCE_CONTINUATION`` all read "no"). Normalising here fixes it once for
    every consumer instead of relying on three template predicates staying right.
    """
    if not isinstance(payload, dict):
        return []
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    out = []
    for r in value:
        if not isinstance(r, dict):
            continue
        r = dict(r)
        r["routable"] = r.get("routable")   # explicit None, never Undefined
        out.append(r)
    return out


def _mapping(payload: Any, key: str) -> dict:
    if not isinstance(payload, dict):
        return {}
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def classify_ledger(rows: list[dict]) -> dict:
    """Lifetime promotion attribution over exactly the rows handed in.

    Split by the engine-stamped ``routable`` flag. Rows written before the
    engine began stamping it carry ``None``; they are counted as
    **unclassified** and never guessed at — inventing a classification here is
    how ops would start disagreeing with the engine.

    Prunes are counted separately: a prune removes a dead override rather than
    changing live policy, so folding it into "promotions" would overstate both
    the budget spent and the cleanup done.
    """
    counts = {"routable": 0, "unroutable": 0, "unclassified": 0, "pruned": 0}
    by_key: dict[str, int] = {}
    for row in rows:
        if str(row.get("status") or "") == STATUS_PRUNED:
            counts["pruned"] += 1
            continue
        flag = row.get("routable")
        if flag is None:
            counts["unclassified"] += 1
        elif flag:
            counts["routable"] += 1
        else:
            counts["unroutable"] += 1
            name = str(row.get("strategy") or "?")
            by_key[name] = by_key.get(name, 0) + 1
    promotions = counts["routable"] + counts["unroutable"] + counts["unclassified"]
    wasted_pct = (counts["unroutable"] / promotions * 100.0) if promotions else 0.0
    return {
        "counts": counts,
        "promotions": promotions,
        "wasted_pct": wasted_pct,
        "unroutable_by_key": dict(sorted(by_key.items(), key=lambda kv: (-kv[1], kv[0]))),
        # Honest denominator: how much of the split rests on rows the engine
        # actually stamped. A high unclassified count means the window predates
        # the measurement, not that the controller is behaving well.
        "classified": counts["routable"] + counts["unroutable"],
    }


def split_overrides(active: dict, dead: dict, *, measuring: bool = True) -> dict:
    """Active overrides split by whether the policy can read them.

    ``dead`` comes from the engine's routability report, so membership is the
    engine's judgement, not a re-derivation. While the engine *is* measuring, an
    override absent from ``dead`` is live — failing toward "this is real" keeps ops
    from quietly hiding a genuine override behind a classification bug here.

    **When the engine is not measuring there is no basis to claim either.** The
    first cut of this reducer had two states and defaulted to "live", so with no
    routability block every override — including the nine known dead keys — landed
    under a heading reading *"Active overrides the policy actually reads"*. Every
    number on it was defensible and the sentence above it was false, which is the
    "copy is part of the measurement" failure. So the split has three states, and
    ``unknown`` is one of them.
    """
    live: dict[str, Any] = {}
    unroutable: dict[str, Any] = {}
    unknown: dict[str, Any] = {}
    for name, params in sorted((active or {}).items()):
        if not measuring:
            unknown[name] = params
        elif name in (dead or {}):
            unroutable[name] = params
        else:
            live[name] = params
    return {
        "live": live,
        "unroutable": unroutable,
        "unknown": unknown,
        "live_n": len(live),
        "unroutable_n": len(unroutable),
        "unknown_n": len(unknown),
        "total_n": len(live) + len(unroutable) + len(unknown),
    }


def summarise(payload: Any) -> dict:
    """Everything the page renders, reduced from the store payload.

    ``error`` is surfaced rather than swallowed: a missing volume file in dev and
    a controller that has never run are different states and must read
    differently on screen.
    """
    if isinstance(payload, dict) and payload.get("error"):
        return {"error": str(payload["error"])}

    routability = _mapping(payload, "routability")
    ledger = _rows(payload, "ledger")
    pending = _rows(payload, "pending")
    state = _mapping(payload, "state")

    dead = routability.get("dead_overrides")
    dead = dead if isinstance(dead, dict) else {}

    measuring = bool(routability)
    enforced = bool(routability.get("enforced")) if measuring else False

    return {
        "error": None,
        # A controller that has never published a routability block is *not*
        # measuring — say so, rather than rendering zeros that look like health.
        "measuring": measuring,
        "enforced": enforced,
        "cycle": state.get("cycle"),
        "latest": {
            "routable_candidates": routability.get("routable_candidates"),
            "unroutable_candidates": routability.get("unroutable_candidates"),
            "promoted_unroutable": routability.get("promoted_unroutable") or [],
            "starved_routable": routability.get("starved_routable") or [],
            "pruned": routability.get("pruned") or [],
            "wasted_promotions": routability.get("wasted_promotions") or 0,
        },
        "overrides": split_overrides(
            _mapping(payload, "active_overrides"), dead, measuring=measuring,
        ),
        "dead_overrides": dict(sorted(dead.items())),
        "lifetime": classify_ledger(ledger),
        "ledger": list(reversed(ledger))[:60],   # newest first
        "pending": pending,
        "ledger_n": len(ledger),
    }


@router.get("/emission-controller")
async def emission_controller(request: Request):
    payload = request.app.state.data_volume.emission_controller()
    summary = summarise(payload)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "emission_controller.html",
        {
            "request": request,
            "s": summary,
            "active": "emission_controller",
        },
    )
