"""Labs index — every shadow, governor and A/B experiment on one page.

Outside audit, 2026-11-25: the Signals group held 17 sub-tabs, mixing the live
feed with measurement-only experiments, so the operator hunted for positions
among shadow telemetry. Live pages now sit under "Live"; experiments under
"Labs", with this page as their index. No URL moved.

**What this page deliberately does not do: grade each experiment's state.**
Every experiment page already grades its own freshness on the ENGINE's clock,
from its own ledger or diag read, under rules paid for one page at a time
(CLAUDE.md: "a surface cannot grade its own liveness on a clock it supplies").
A second implementation of fourteen state machines here would be a mirror, and
mirrors drift in the flattering direction. So the index says what each one
measures and whether it can change what users get — design facts, stable
across deploys — and links to the page that knows the state.

``LABS`` and base.html's Labs nav group are held equal by tests/test_labs.py:
one list of experiments, two renderings, no third.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()

#: (category, path, name, what it measures, can it change what users get)
LABS: list[tuple[str, str, str, str, str]] = [
    ("Exit governors", "/signals/trail-governor", "Trail governor",
     "The only module that moves a resting stop on a live position: which positions it governs and why the rest are not.",
     "Yes — for users opted in to a trailing exit, while its master switch is on."),
    ("Exit governors", "/signals/ai-governor", "AI governor",
     "A model critiques open positions; refusals, verdicts and their selection against the engine's own geometry.",
     "Only if its effect flag is switched on (off by default)."),
    ("Exit governors", "/signals/governor-live", "AI governor live",
     "Each governed signal walked twice on the same bars: geometry the verdict edited vs the evaluator's, paired.",
     "No — measurement only."),
    ("Exit governors", "/signals/sar-live", "SAR live",
     "Parabolic SAR stepped forward in the monitor loop, on delivered and dark signals, with both fills.",
     "No — measurement only."),
    ("Exit governors", "/signals/atr-live", "ATR trail live",
     "The Chandelier trail on the same arm engine as SAR, read against the risk it actually parks.",
     "No — measurement only."),
    ("Exit governors", "/signals/sar", "SAR signals",
     "Replayed SAR agreement at entry and the replay ledger's resolutions.",
     "No — measurement only."),
    ("Exit governors", "/sar-exit", "SAR exit A/B",
     "Paired SAR-exit vs baseline stamps, per strategy.",
     "No — measurement only."),
    ("Entry & structure", "/signals/entry-features", "Entry features",
     "Per-path entry stamps joined to outcomes, and the live entry-quality rules.",
     "Yes — the entry-quality gate is live, switchable per rule."),
    ("Entry & structure", "/signals/structural-veto", "Structural veto",
     "Which signals have a level ahead of the trade, and what vetoing them would have cost.",
     "Only if enforce is switched on (off by default)."),
    ("Entry & structure", "/signals/structural-snap", "Snap geometry",
     "Level-aware SL/TP1 snapping at the enqueue choke point, both arms kept apart.",
     "Only for paths on its apply allow-list (off by default)."),
    ("Shadow feeds", "/signals/dark-live", "Dark feed",
     "Signals the scanner would send with one gate loosened, diverted before the queue and walked forward.",
     "Only rows a promotion rule promotes (Control → Promotions)."),
    ("Shadow feeds", "/signals/price-action", "Price action",
     "Signals generated from structure alone: a level swept and reclaimed, delta-confirmed.",
     "No — never delivered."),
    ("Shadow feeds", "/signals/unlock-shorts", "Unlock shorts",
     "Insider cliff unlocks shorted T−14 → T+2, measured forward on public data.",
     "No — never reaches a user or an order."),
    ("Autonomy", "/strategy-lab", "Strategy Lab",
     "Every strategy measured per market context: the edge matrix, gate audit and allocator's would-do.",
     "No — the allocator is recommendation-only."),
    ("Autonomy", "/emission-controller", "Layer G",
     "The closed-loop emission controller: promotions, the routability stamp and its budget.",
     "Yes — live, self-promoting inside a bounded envelope."),
]


@router.get("/labs")
async def labs_index(request: Request):
    groups: dict[str, list[dict]] = {}
    for cat, path, name, what, reach in LABS:
        groups.setdefault(cat, []).append(
            {"path": path, "name": name, "what": what, "reach": reach,
             "tier": "acts" if reach.startswith("Yes") else "switchable" if reach.startswith("Only") else "measures"}
        )
    return request.app.state.templates.TemplateResponse(
        "labs.html",
        {"request": request, "groups": groups, "n": len(LABS), "active": "labs"},
    )
