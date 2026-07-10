"""Diag page: the paper-health tool (2026-07-10 frozen-paper investigation).

The route must call the allow-listed engine script with the bounded window
arg, and the allowlist must actually contain the new script so the runner
doesn't reject it.
"""
from __future__ import annotations

import os
from unittest.mock import AsyncMock

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.diag_runner import DiagResult, DiagRunner  # noqa: E402
from app.main import app  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


def test_paper_health_is_allowlisted():
    assert "diag_paper_health" in DiagRunner._ALLOWED_SCRIPTS


def test_diag_paper_runs_script_with_bounded_hours():
    with TestClient(app) as client:
        _login(client)
        runner = AsyncMock()
        runner.run.return_value = DiagResult(stdout="PAPER HEALTH", stderr="", returncode=0)
        app.state.diag_runner = runner
        r = client.post("/diag/paper", data={"hours": 9999})
        assert r.status_code == 200
        runner.run.assert_awaited_once_with("diag_paper_health", ["--hours", "720"])
        assert "PAPER HEALTH" in r.text


def test_diag_page_shows_paper_form():
    with TestClient(app) as client:
        _login(client)
        r = client.get("/diag/geometry")
        assert r.status_code == 200
        assert "/diag/paper" in r.text
