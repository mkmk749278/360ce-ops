"""Signup free trial — the owner's read of a dark money-path change.

The engine ships the 7-day trial (360-v2, 2026-07-25) dark: the measurement
flag is ON from the deploy and the user-visible flag is OFF, so every
eligible user is stamped into a cohort that nobody has been shown anything
about.  Per `CLAUDE.md § Scope of this repo`, that measurement is worthless
without somewhere to read it — this page is that place, and the engine change
is not finished without it.

Read-only.  Activation is an `.env` flip plus a redeploy (see
`docs/SIGNUP_TRIAL_ACTIVATION.md` in 360-v2), deliberately NOT a button here:
turning on 7 free days of server-side auto-execution for the whole eligible
base is an owner-sign-off decision that should cost a deploy, not a click.

Ops ports the engine's numbers, it does not invent them — every counter comes
from `GET /api/trial/admin/funnel`; the reducers below only bucket rows for
display.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Request

router = APIRouter()


def _is_error(result: object) -> bool:
    return isinstance(result, dict) and bool(result.get("error"))


def _parse_ts(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def classify(row: dict, *, now: datetime | None = None) -> str:
    """Bucket one funnel row for the table's status chip.

    Ordered most-specific first: a converted trialist is 'converted' even
    though their window may also still be running.
    """
    at = now or datetime.now(timezone.utc)
    if row.get("converted_at"):
        return "converted"
    if row.get("claimed_at"):
        expires = _parse_ts(row.get("expires_at"))
        return "active" if expires and expires > at else "lapsed"
    if row.get("offered_at"):
        return "offered"
    # Never offered anything — either the dark window, or a live-flag user
    # who has not opened the app since becoming eligible.
    return "dark" if row.get("shadow") else "eligible"


def days_left(row: dict, *, now: datetime | None = None) -> int | None:
    """Whole days remaining in a running trial, else None."""
    if not row.get("claimed_at"):
        return None
    expires = _parse_ts(row.get("expires_at"))
    if expires is None:
        return None
    remaining = (expires - (now or datetime.now(timezone.utc))).total_seconds()
    if remaining <= 0:
        return None
    return int(-(-remaining // 86400))


def phase(funnel: dict) -> str:
    """One word for what the reader is looking at.

    The distinction the panel exists to protect: a cohort of 400 means
    something entirely different depending on whether those users were ever
    actually offered anything.
    """
    if not funnel.get("measuring"):
        return "blind"
    return "live" if funnel.get("offer_live") else "dark"


@router.get("/trials")
async def trials_page(request: Request):
    api = request.app.state.engine_api
    templates = request.app.state.templates

    result = await api.trial_funnel()
    error = None
    funnel: dict = {}
    rows: list[dict] = []
    if _is_error(result):
        error = result.get("error")
    elif isinstance(result, dict):
        funnel = result
        now = datetime.now(timezone.utc)
        rows = [
            {
                **row,
                "status": classify(row, now=now),
                "days_left": days_left(row, now=now),
            }
            for row in funnel.get("trials", [])
            if isinstance(row, dict)
        ]

    return templates.TemplateResponse(
        "trials.html",
        {
            "request": request,
            "active": "trials",
            "funnel": funnel,
            "summary": funnel.get("summary") or {},
            "rows": rows,
            "phase": phase(funnel) if funnel else "unknown",
            "error": error,
        },
    )
