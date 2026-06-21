"""Alerts panel — surfaces the monitoring agent's live safety alerts.

Telegram was the agent's only page channel (naked position, signing down,
engine/Redis stale).  With Telegram banned in-region (2026-06-20) this panel
is the alert surface: it reads the agent's ``alert:state:*`` keys from the
shared Redis and renders active alerts, HIGH first.

Pull-based by design (owner's call) — there is no push.  The page is meant
to be checked alongside the control plane, and the nav badge could later
carry an unread count.
"""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/alerts")
async def alerts_page(request: Request):
    reader = request.app.state.agent_alerts
    templates = request.app.state.templates
    payload = await reader.active_alerts()
    alerts = payload.get("alerts", [])
    return templates.TemplateResponse(
        "alerts.html",
        {
            "request": request,
            "active": "alerts",
            "alerts": alerts,
            "error": payload.get("error"),
            "high_count": sum(1 for a in alerts if a.get("severity") == "HIGH"),
        },
    )
