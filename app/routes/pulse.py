"""Pulse: engine health + auto-mode + heartbeat."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/")
async def pulse(request: Request):
    api = request.app.state.engine_api
    logs = request.app.state.monitor_logs
    pulse_data = await api.pulse()
    auto = await api.auto_mode()
    heartbeat = await logs.heartbeat()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "pulse.html",
        {
            "request": request,
            "pulse": pulse_data,
            "auto_mode": auto,
            "heartbeat": heartbeat,
            "active": "pulse",
        },
    )


@router.get("/_partial/pulse")
async def pulse_partial(request: Request):
    api = request.app.state.engine_api
    pulse_data = await api.pulse()
    auto = await api.auto_mode()
    templates = request.app.state.templates
    return templates.TemplateResponse(
        "_pulse_card.html",
        {"request": request, "pulse": pulse_data, "auto_mode": auto},
    )
