"""The two destructive control routes: full signal reset and force-close.

``test_control.py`` covers the auth gate, the auto-mode flip and the kill
switch.  These two were uncovered — and they are the ones that destroy state:
``/control/reset-signals`` clears every active signal, all history, stats,
invalidation records and paper broker state for all users;
``/control/close-signal`` force-closes one live OPEN signal.

The control doctrine is what is under test here, not the happy path:

* **Audited** — the audit row must be written on the *failure* path too.  An
  action that fails silently and unaudited is the one you cannot reconstruct
  afterwards, and "the engine said no" is exactly what you need in the log.
* **PRG** — POST→redirect→GET on every outcome, so a refresh cannot re-fire a
  full reset.  A non-303 here means a browser refresh replays a destructive
  action.
* **No open redirect** — ``redirect_to`` is attacker-influenceable form input
  on an owner-authenticated surface; anything not starting with ``/`` must
  fall back to an in-app path.
* **The engine is the source of truth** — ops reports back what the engine
  returned, and the three-way ``closed`` result (True / None-queued / False-
  already-closed) must not be collapsed into "done".
"""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import control as control_route  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


@pytest.fixture
def wired(monkeypatch):
    """Patch the engine reads/writes and capture audit rows."""
    recorded: list[dict] = []
    monkeypatch.setattr(control_route.audit, "tail", lambda *a, **k: [])
    monkeypatch.setattr(
        control_route.audit, "record", lambda *a, **k: recorded.append(k)
    )

    async def fake_auto_mode(self):
        return {"mode": "paper"}

    async def fake_ks(self):
        return {"engaged": False, "initialised": True}

    async def fake_glob(self):
        return {"enabled": True, "initialised": True}

    async def fake_billing(self):
        return {"enabled": True, "configured": True, "initialised": True}

    monkeypatch.setattr(EngineApiClient, "auto_mode", fake_auto_mode)
    monkeypatch.setattr(EngineApiClient, "kill_switch_state", fake_ks)
    monkeypatch.setattr(EngineApiClient, "auto_trade_global_state", fake_glob)
    monkeypatch.setattr(EngineApiClient, "billing_enabled_state", fake_billing)
    return recorded


def _set_reset(monkeypatch, payload):
    async def fake_reset(self):
        return payload
    monkeypatch.setattr(EngineApiClient, "reset_signals", fake_reset)


def _set_close(monkeypatch, payload, seen=None):
    async def fake_close(self, signal_id):
        if seen is not None:
            seen.append(signal_id)
        return payload
    monkeypatch.setattr(EngineApiClient, "close_signal", fake_close)


# ---------------------------------------------------------------------------
# Auth — destructive routes are behind the gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,data",
    [
        ("/control/reset-signals", {}),
        ("/control/close-signal", {"signal_id": "S1"}),
    ],
)
def test_destructive_routes_require_auth(path, data):
    with TestClient(app) as client:
        r = client.post(path, data=data, follow_redirects=False)
        assert r.status_code in (302, 303)
        assert r.headers["location"] == "/login"


def test_unauthed_destructive_post_never_reaches_the_engine(monkeypatch, wired):
    """The gate must stop the call, not just redirect after making it."""
    called = {"n": 0}

    async def fake_reset(self):
        called["n"] += 1
        return {}

    monkeypatch.setattr(EngineApiClient, "reset_signals", fake_reset)
    with TestClient(app) as client:
        client.post("/control/reset-signals", follow_redirects=False)
    assert called["n"] == 0


# ---------------------------------------------------------------------------
# Full reset
# ---------------------------------------------------------------------------


def test_reset_calls_engine_audits_and_redirects(monkeypatch, wired):
    _set_reset(monkeypatch, {
        "cleared_active_signals": 4, "cleared_history": 120,
        "paper_positions_closed": 2,
    })
    with TestClient(app) as client:
        _login(client)
        r = client.post("/control/reset-signals", follow_redirects=False)

    assert r.status_code == 303           # PRG — refresh cannot re-fire
    assert r.headers["location"] == "/control"
    assert len(wired) == 1
    assert wired[0]["action"] == "signal_reset_full"
    assert wired[0]["ok"] is True


def test_reset_reports_the_engine_counts_back(monkeypatch, wired):
    """The engine is the source of truth — ops echoes its numbers."""
    _set_reset(monkeypatch, {
        "cleared_active_signals": 7, "cleared_history": 55,
        "paper_positions_closed": 3,
    })
    with TestClient(app) as client:
        _login(client)
        client.post("/control/reset-signals", follow_redirects=False)
        page = client.get("/control").text

    assert "7 active signals" in page
    assert "55 history" in page
    assert "3 paper positions" in page


def test_reset_discloses_a_queued_engine_propagation(monkeypatch, wired):
    """"Queued" is not "done" — the flash must say the reset is in flight."""
    _set_reset(monkeypatch, {
        "cleared_active_signals": 1, "cleared_history": 0,
        "paper_positions_closed": 0, "engine_reset_queued": True,
    })
    with TestClient(app) as client:
        _login(client)
        client.post("/control/reset-signals", follow_redirects=False)
        page = client.get("/control").text

    assert "engine reset queued" in page


def test_failed_reset_is_still_audited(monkeypatch, wired):
    """The failure path is the one you need in the log afterwards."""
    _set_reset(monkeypatch, {"error": "engine unreachable"})
    with TestClient(app) as client:
        _login(client)
        r = client.post("/control/reset-signals", follow_redirects=False)

    assert r.status_code == 303
    assert len(wired) == 1
    assert wired[0]["ok"] is False
    assert wired[0]["action"] == "signal_reset_full"


def test_failed_reset_surfaces_the_engine_error_text(monkeypatch, wired):
    _set_reset(monkeypatch, {"error": "engine unreachable"})
    with TestClient(app) as client:
        _login(client)
        client.post("/control/reset-signals", follow_redirects=False)
        page = client.get("/control").text

    assert "Full reset failed" in page
    assert "engine unreachable" in page


def test_non_dict_reset_result_does_not_crash(monkeypatch, wired):
    """Shape drift on the engine surface must degrade, not 500."""
    _set_reset(monkeypatch, "unexpected string")
    with TestClient(app) as client:
        _login(client)
        r = client.post("/control/reset-signals", follow_redirects=False)
    assert r.status_code == 303
    assert len(wired) == 1


# ---------------------------------------------------------------------------
# Force-close one signal
# ---------------------------------------------------------------------------


def test_close_signal_passes_the_id_and_audits_it(monkeypatch, wired):
    seen: list[str] = []
    _set_close(monkeypatch, {"closed": True, "pnl_pct": 1.25}, seen)
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/close-signal",
            data={"signal_id": "SIG-42", "redirect_to": "/signals"},
            follow_redirects=False,
        )

    assert r.status_code == 303
    assert seen == ["SIG-42"]
    assert wired[0]["action"] == "close_signal"
    assert wired[0]["params"] == {"signal_id": "SIG-42"}


def test_close_signal_trims_whitespace_from_the_id(monkeypatch, wired):
    seen: list[str] = []
    _set_close(monkeypatch, {"closed": True}, seen)
    with TestClient(app) as client:
        _login(client)
        client.post(
            "/control/close-signal",
            data={"signal_id": "  SIG-9  "},
            follow_redirects=False,
        )
    assert seen == ["SIG-9"]


def test_empty_signal_id_refuses_without_calling_the_engine(monkeypatch, wired):
    """A blank id must not reach the engine as a close request."""
    seen: list[str] = []
    _set_close(monkeypatch, {"closed": True}, seen)
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/close-signal",
            data={"signal_id": "   "},
            follow_redirects=False,
        )

    assert r.status_code == 303
    assert seen == []
    assert wired == []       # nothing happened, so nothing to audit


@pytest.mark.parametrize(
    "hostile",
    [
        "https://evil.example.com/phish",
        "//evil.example.com",
        "http://evil.example.com",
        "javascript:alert(1)",
        "evil.example.com",
    ],
)
def test_redirect_to_must_be_an_in_app_path(monkeypatch, wired, hostile):
    """Open-redirect guard on attacker-influenceable form input.

    ``//evil.example.com`` is the interesting one: it starts with "/" and is
    still protocol-relative, so a naive startswith("/") check lets it through.
    """
    _set_close(monkeypatch, {"closed": True})
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/close-signal",
            data={"signal_id": "S1", "redirect_to": hostile},
            follow_redirects=False,
        )

    dest = r.headers["location"]
    assert not dest.startswith("//"), f"protocol-relative redirect allowed: {dest}"
    assert "evil.example.com" not in dest
    assert "javascript:" not in dest


def test_valid_in_app_redirect_is_honoured(monkeypatch, wired):
    _set_close(monkeypatch, {"closed": True})
    with TestClient(app) as client:
        _login(client)
        r = client.post(
            "/control/close-signal",
            data={"signal_id": "S1", "redirect_to": "/positions"},
            follow_redirects=False,
        )
    assert r.headers["location"] == "/positions"


# ---------------------------------------------------------------------------
# The three-way close result must not collapse
# ---------------------------------------------------------------------------


def test_closed_true_reports_the_realised_pnl(monkeypatch, wired):
    """A confirmed close names the P/L — including a losing one."""
    _set_close(monkeypatch, {"closed": True, "pnl_pct": -2.5})
    with TestClient(app) as client:
        _login(client)
        client.post("/control/close-signal",
                    data={"signal_id": "S1", "redirect_to": "/control"},
                    follow_redirects=False)
        page = client.get("/control").text

    assert wired[0]["ok"] is True
    assert "-2.50%" in page


def test_closed_none_is_reported_as_queued_not_done(monkeypatch, wired):
    """Queued and closed are different states; collapsing them misleads.

    The owner refreshing on "Closed S1" when it is merely queued will believe
    a position is flat when it is not.
    """
    _set_close(monkeypatch, {"closed": None})
    with TestClient(app) as client:
        _login(client)
        r = client.post("/control/close-signal",
                        data={"signal_id": "S1", "redirect_to": "/control"},
                        follow_redirects=False)
        assert r.status_code == 303
        page = client.get("/control").text

    assert "queued" in page.lower()


def test_closed_false_says_already_closed_not_success(monkeypatch, wired):
    _set_close(monkeypatch, {"closed": False})
    with TestClient(app) as client:
        _login(client)
        client.post("/control/close-signal",
                    data={"signal_id": "S1", "redirect_to": "/control"},
                    follow_redirects=False)
        page = client.get("/control").text

    assert "already closed" in page.lower() or "not in the active book" in page.lower()


def test_failed_close_is_audited_with_ok_false(monkeypatch, wired):
    _set_close(monkeypatch, {"error": "no such signal"})
    with TestClient(app) as client:
        _login(client)
        r = client.post("/control/close-signal",
                        data={"signal_id": "S1", "redirect_to": "/control"},
                        follow_redirects=False)
        assert r.status_code == 303
        page = client.get("/control").text

    assert wired[0]["ok"] is False
    assert "no such signal" in page
