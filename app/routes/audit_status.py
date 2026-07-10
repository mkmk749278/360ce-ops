"""Audit board — what from the 2026-07-10 institutional audit is actually
implemented, what's partial, what's open, and what waits on the owner.

Read-only statement of record backed by ``app.audit_findings`` (maintained
at session end, same discipline as ACTIVE_CONTEXT.md). Sorted so the work
that still needs attention is on top: open → partial → owner → done, and by
severity inside each group.
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from app.audit_findings import (
    EXTRAS,
    FINDINGS,
    SEVERITY_ORDER,
    STATUS_ORDER,
    summary,
)

router = APIRouter()


@router.get("/audit")
async def audit_board(request: Request, status: str | None = Query(None)):
    findings = list(FINDINGS)
    if status:
        findings = [f for f in findings if f["status"] == status]
    findings.sort(
        key=lambda f: (
            STATUS_ORDER.get(f["status"], 9),
            SEVERITY_ORDER.get(f["severity"], 9),
            f["id"],
        )
    )
    return request.app.state.templates.TemplateResponse(
        "audit_status.html",
        {
            "request": request,
            "active": "audit",
            "findings": findings,
            "extras": EXTRAS,
            "summary": summary(list(FINDINGS)),
            "filter_status": status,
        },
    )
