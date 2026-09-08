"""`/path-scorecard` — is this path earning its place, and can we tell yet?

The question this answers is the owner's, 2026-09-08: *"we need to produce
good signals from every path."*

The context it answers it in. On 2026-08-30 the same-direction and
per-channel caps came off so every path could reach the feed rather than
`MOVER_TREND_PULLBACK` holding the slots by arithmetic. It worked — MVRTP's
share of the delivered book fell 76% to 59% and every other path is running
3-7x its old delivery rate. Over the ten days that followed, the delivered
book also went from **+0.506%/trade** (310 trades, 21 days) to
**-0.113%/trade** (374 trades, 10 days), and no surface in either repo could
say which path, if any, was responsible.

The August retirements were decided off exactly this analysis — n, distinct
symbols, net %, a symbol-clustered interval — run **by hand, once**. This is
the standing version.

Three things the page has to get right, and they are all about restraint:

* **The sample floor is applied BEFORE the interval, engine-side.** The
  worst-looking cell in the current window is three trades with an interval
  that excludes zero. Ranked on the interval it is the top row of the table
  and one click from retirement; ranked floor-first it reads INSUFFICIENT and
  names which bound it failed. That ordering is `FAILED_AUCTION_RECLAIM`'s
  +0.846R lesson turned into code, and this page must never re-sort past it.
* **`cells_drawn` renders beside the worst row, always.** "Best of N" is not
  a fact about the winner until N is on screen — and here the winner is a
  path somebody might retire.
* **A retired cell is FROZEN, not zero.** Retirement diverts to the dark
  lane, so a retired path stops producing delivered rows the moment it is
  armed: its n stops growing and its verdict stops being a reading about
  today. Frozen and fresh look identical in a table unless one of them says
  so.

Ops computes none of it. The interval, the floor, the verdict and the
candidate list are the engine's (`src/path_scorecard.py`), assembled in the
engine container and read through the diagnostic catalog — a second
implementation of a statistical rule is a mirror that drifts in the
flattering direction, and this one would drift toward retiring paths.
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Request

router = APIRouter()

#: Three states with three different next moves, never pooled into "no data".
#: Mirrors `firestore_cost.classify` because the failure modes are identical.
STATE_UNREACHABLE = "unreachable"
STATE_NOT_REPORTED = "not_reported"
STATE_EMPTY = "empty"
STATE_OK = "ok"

#: What a verdict means, in words, beside the verdict. The table iterates the
#: ENGINE's cells and looks each verdict up here; a verdict this page has
#: never heard of renders under its raw name badged `unclassified` rather than
#: being dropped or bucketed — iterating ops' own keys would be silent by
#: construction on the next verdict the engine adds.
VERDICT_COPY: Dict[str, str] = {
    "LOSES": (
        "Cleared the sample floor and the symbol-clustered 95% interval sits "
        "entirely below zero. This is the only state that names a retirement "
        "candidate."
    ),
    "EARNS": (
        "Cleared the sample floor and the interval sits entirely above zero."
    ),
    "UNDECIDED": (
        "Enough evidence to ask the question, and the interval still spans "
        "zero. Nothing to do but let it accumulate."
    ),
    "INSUFFICIENT": (
        "Below the sample floor, so the interval cannot produce a verdict "
        "however decisive it looks. The interval is still shown — hiding it "
        "would leave you wondering — but it is not a finding."
    ),
}


def classify(payload: Any) -> str:
    """Grade the diag result without asserting a cause we cannot observe.

    `ok` is the discriminator and `error` cannot be: the engine's catalog
    envelope carries `error` on SUCCESS (empty), so `str(ReadTimeout())` being
    the empty string once made a timed-out call read as a clean one. Key
    presence on `ok`, never truthiness on `error`.
    """
    if not isinstance(payload, dict):
        return STATE_UNREACHABLE
    if "ok" in payload:
        if payload.get("ok") is False:
            return STATE_NOT_REPORTED
    elif "error" in payload:
        return STATE_UNREACHABLE
    out = payload.get("result") if "result" in payload else payload
    if not isinstance(out, dict):
        return STATE_UNREACHABLE
    if "cells" not in out:
        return STATE_NOT_REPORTED
    if not out.get("cells"):
        # Reporting, nothing in the window. After a restart or on a fresh
        # ledger that is correct and uninteresting — not a fault.
        return STATE_EMPTY
    return STATE_OK


def _unwrap(payload: Any) -> dict:
    if not isinstance(payload, dict):
        return {}
    out = payload.get("result") if "result" in payload else payload
    return out if isinstance(out, dict) else {}


def annotate(cells: Any) -> List[Dict[str, Any]]:
    """Attach the sentence, keyed off the ENGINE's verdict."""
    rows: List[Dict[str, Any]] = []
    for cell in cells or []:
        if not isinstance(cell, dict):
            continue
        verdict = str(cell.get("verdict") or "")
        row = dict(cell)
        row["meaning"] = VERDICT_COPY.get(verdict, "")
        row["unclassified"] = verdict not in VERDICT_COPY
        rows.append(row)
    return rows


@router.get("/path-scorecard")
async def path_scorecard(request: Request):
    api = request.app.state.engine_api
    try:
        raw = await api.diag_run("read.path_scorecard", {})
    except Exception as exc:  # pragma: no cover - defensive
        raw = {"error": f"{type(exc).__name__}: {exc}"}

    out = _unwrap(raw)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "path_scorecard.html",
        {
            "request": request,
            "active": "path_scorecard",
            "state": classify(raw),
            "card": out,
            # Ordered by the ENGINE (worst mean first) and deliberately not
            # re-sorted here. Sorting by the interval would put the thinnest,
            # most extreme cell on the top line, which is the one arrangement
            # this page exists to avoid.
            "cells": annotate(out.get("cells")),
            "candidates": out.get("retirement_candidates") or [],
            "retired": out.get("already_retired") or [],
            # The legend is rendered FROM the same mapping the table looks
            # each verdict up in — one writer, one reader. A second hand-typed
            # legend is the drifting mirror this repo has paid for under
            # several names.
            "verdict_copy": VERDICT_COPY,
        },
    )
