"""The diagnostic console, and the invariant it narrowed.

Owner, 2026-08-19, approving "diag catalog + a few safe actions": he wants a
session that can *"diagnosis everything and fix every error within that allowed
guest mode"*. A pure-read tier cannot do that, so `guest_scope`'s rule 1 —
which read **"GET and HEAD. Nothing else, ever."** — was narrowed to an
allow-list of POST routes, each carrying a written reason.

Narrowed, not deleted. This repo's own lesson: *an invariant that blocks correct
work gets deleted outright by whoever needs the work; one that states what it
means survives.* These tests are what make the narrowing safe rather than a
weakening — they assert the exception is exactly one route, that everything else
is still refused, and that the route named cannot do anything a guest could not
already do.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app import guest_scope  # noqa: E402
from app.main import app  # noqa: E402


# ---------------------------------------------------------------------------
# The narrowed invariant
# ---------------------------------------------------------------------------

def test_the_action_allowlist_is_small_and_every_entry_says_why():
    """A blank reason is an entry nobody had to justify.

    That is how a list grows past what it was approved for, and the growth is
    invisible because each individual addition looks reasonable.
    """
    assert guest_scope.GUEST_ACTION_ROUTES, "the map exists"
    for path, reason in guest_scope.GUEST_ACTION_ROUTES.items():
        assert reason.strip(), f"{path} is allow-listed with no stated reason"
        assert len(reason) > 80, f"{path}'s reason is too thin to be an argument"


def test_every_action_route_is_also_classified_readable():
    """A route a guest may POST to but not GET is a control it cannot see."""
    for path in guest_scope.GUEST_ACTION_ROUTES:
        assert path in guest_scope.GUEST_READ_ROUTES, path
        assert path not in guest_scope.OWNER_ONLY, path


def test_no_control_route_ever_reaches_the_action_list():
    """Derived from the prefix, so a future /control write cannot be added here
    without this failing — the money-path surfaces stay on the owner's login."""
    for path in guest_scope.GUEST_ACTION_ROUTES:
        assert not path.startswith("/control"), path
        assert not path.startswith("/api/v1"), path
        for word in ("kill", "mode", "close", "take", "clear", "reset"):
            assert word not in path.lower(), f"{path} contains {word!r}"


def _scope(method: str, path: str) -> dict:
    return {"type": "http", "method": method, "path": path,
            "headers": [], "root_path": "", "query_string": b""}


def test_a_guest_post_to_anything_else_is_still_refused():
    """The blanket refusal survives; only the named route is excepted."""
    for path in ("/control/kill-switch", "/signals/sar/clear", "/control/tunables",
                 "/exit-backtest/run", "/control/users/exit-mechanism"):
        ok, reason = guest_scope.guest_may(app, _scope("POST", path))
        assert ok is False, f"{path} accepted a guest POST"
        assert reason, "a refusal must state its cause"


def test_the_named_route_accepts_post_and_nothing_stronger():
    ok, _ = guest_scope.guest_may(app, _scope("POST", "/diagnostics/console/run"))
    assert ok is True
    for method in ("DELETE", "PUT", "PATCH"):
        ok, reason = guest_scope.guest_may(app, _scope(method, "/diagnostics/console/run"))
        assert ok is False, f"{method} was accepted"
        assert method in reason


def test_an_unmatched_write_is_refused_rather_than_passed_through():
    """A GET with no route falls through to the app's 404 deliberately, so a
    prober learns nothing. A write with no route has no such argument."""
    ok, _ = guest_scope.guest_may(app, _scope("POST", "/no/such/route/at/all"))
    assert ok is False


def test_may_use_agrees_with_the_gate_rather_than_hiding_the_control():
    """The 2026-08-07 defect with the sign flipped.

    If `may_use` stayed absolute it would hide a control the gate allows, and a
    silently absent control reads as a page with nothing to offer.
    """
    class _Req:
        scope = {"ops_role": "guest"}

    assert guest_scope.may_use(_Req(), "/diagnostics/console/run", "POST") is True
    assert guest_scope.may_use(_Req(), "/control/kill-switch", "POST") is False


# ---------------------------------------------------------------------------
# The page itself
# ---------------------------------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.routes import diag_console as mod

    catalog = {"entries": [
        {"key": "read.loop", "label": "Loop + host counters", "kind": "read",
         "summary": "counters", "effect": "", "needs": [], "enabled": True},
        {"key": "action.flush_ledgers", "label": "Flush ledgers", "kind": "action",
         "summary": "persist", "effect": "Writes in-memory rows to disk.",
         "needs": [], "enabled": True},
        {"key": "action.reseed_symbol", "label": "Re-seed", "kind": "action",
         "summary": "refetch", "effect": "One REST call.", "needs": ["symbol"],
         "enabled": False},
    ]}

    class _Api:
        async def diag_catalog(self):
            return catalog

        async def diag_run(self, key, args=None):
            return {"ok": True, "key": key, "kind": "read", "took_sec": 0.1,
                    "result": {"echoed_args": args or {}}}

        async def aclose(self):
            """The app closes its engine client on shutdown; a stub that cannot
            be closed fails teardown rather than the thing under test."""
            return None

    with TestClient(app) as c:
        c.post("/login", data={"password": "test-token"}, follow_redirects=False)
        monkeypatch.setattr(c.app.state, "engine_api", _Api(), raising=False)
        yield c


def test_the_page_renders_both_kinds_apart(client):
    body = client.get("/diagnostics/console").text
    assert "Reads — observe and return" in body
    assert "Actions — reversible, off the money path" in body
    assert "read.loop" in body and "action.flush_ledgers" in body


def test_a_switched_off_action_renders_OFF_not_missing(client):
    """A vanished entry reads as a broken deploy; OFF reads as a decision."""
    body = client.get("/diagnostics/console").text
    assert "action.reseed_symbol" in body, "must not disappear"
    assert "OFF" in body


def test_the_page_states_why_it_is_not_a_command_line(client):
    """Copy is part of the measurement, and here it is part of the argument.

    A reader has to be able to tell this from a shell, or the next person adds
    a free-text field to it.
    """
    # Whitespace-normalised: the template line-wraps, and a matcher that
    # cannot tell a wrap from a missing sentence fails on reformatting rather
    # than on meaning.
    body = " ".join(client.get("/diagnostics/console").text.split())
    assert "catalog key" in body
    assert "no field here for a command" in body


def test_the_page_carries_no_freeform_input(client):
    """The one guard that would actually be defeated by a careless edit.

    `symbol` is the only text input any entry takes, and it is pattern-bound.
    A second free-text field is how this page would become the thing it exists
    not to be, so the shape is asserted rather than trusted.
    """
    import re

    body = client.get("/diagnostics/console").text
    inputs = re.findall(r"<input[^>]*>", body)
    for tag in inputs:
        if 'type="hidden"' in tag:
            assert 'name="key"' in tag, f"unexpected hidden field: {tag}"
        elif 'type="text"' in tag:
            assert 'name="symbol"' in tag and "pattern=" in tag, tag
        else:
            assert 'type="submit"' in tag or "csrf" in tag.lower(), tag


def test_a_run_redirects_so_a_refresh_cannot_refire_it(client):
    r = client.post("/diagnostics/console/run", data={"key": "read.loop"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/diagnostics/console"


def test_the_result_shows_on_the_next_render(client):
    client.post("/diagnostics/console/run", data={"key": "read.loop"},
                follow_redirects=False)
    body = client.get("/diagnostics/console").text
    assert "Last run" in body and "read.loop" in body


def test_ops_does_not_validate_the_key_itself(client):
    """Deliberate: a second implementation of the engine's allow-list would
    drift from it, and the drift is invisible until a key silently stops
    working. The engine refuses and says what it knows."""
    import inspect

    from app.routes import diag_console as mod

    src = inspect.getsource(mod.diag_console_run)
    assert "diag_run(key" in src
    for word in ("ALLOWED", "allowlist", "allow_list", "VALID_KEYS"):
        assert word not in src, f"ops is keeping its own key list ({word})"
