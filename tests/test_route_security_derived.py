"""Route-level security properties, derived from the app rather than listed.

Three properties the control doctrine states and nothing enforced
(2026-09-26 audit):

* **Every ``/api/v1`` route needs an app token.** The session middleware
  exempts ``/api/v1`` wholesale and each route attaches
  ``Depends(require_app_token)`` itself, so a new route that forgets it is a
  public control-plane endpoint — reachable by anyone, with nothing failing.
* **The middleware's prefix exemptions match segments, not strings.**
  ``startswith("/static")`` would have served a future ``/statistics`` page to
  an unauthenticated caller.
* **Every control action is audited.** The doctrine says so; this reads each
  mutating route's call graph (within its own module) for ``audit.record``.
"""
from __future__ import annotations

import ast
import inspect
import json
import re
import sys
from pathlib import Path

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.main import app
from app.auth_mw import _under

#: The only /api/v1 route an anonymous caller may reach.
V1_PUBLIC = {("POST", "/api/v1/auth/login")}

#: Mutating routes that write nothing to the audit log, each with the reason.
AUDIT_EXEMPT: dict[tuple[str, str], str] = {
    ("POST", "/control/users/lookup"): "a user SEARCH sent as POST — reads, changes nothing",
}

#: GET routes that start work on production and so must be audited like a POST.
GET_JOB_TRIGGERS = {("GET", "/exit-backtest/run-now")}


def _api_routes():
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                yield method, route.path, route


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


# ---------------------------------------------------------------------------
# /api/v1 needs a token
# ---------------------------------------------------------------------------


def test_every_api_v1_route_refuses_a_missing_token() -> None:
    v1 = [(m, p) for m, p, _ in _api_routes() if _under(p, "/api/v1")]
    assert len(v1) >= 20
    open_routes = []
    with TestClient(app, raise_server_exceptions=False) as client:
        for method, path in v1:
            if (method, path) in V1_PUBLIC:
                continue
            body = {} if method in ("POST", "PUT", "PATCH", "DELETE") else None
            status = client.request(method, _concrete(path), json=body).status_code
            if status != 401:
                open_routes.append(f"{method} {path} -> {status}")
    assert not open_routes, (
        "/api/v1 route(s) answer without an app token — add "
        "dependencies=[Depends(require_app_token)]:\n  " + "\n  ".join(open_routes)
    )


def test_every_api_v1_route_refuses_a_forged_token() -> None:
    with TestClient(app, raise_server_exceptions=False) as client:
        for method, path, _ in _api_routes():
            if not _under(path, "/api/v1") or (method, path) in V1_PUBLIC:
                continue
            r = client.request(
                method, _concrete(path),
                json={} if method != "GET" else None,
                headers={"Authorization": "Bearer not-a-real-token"},
            )
            assert r.status_code == 401, f"{method} {path}"


# ---------------------------------------------------------------------------
# Prefix exemptions are segment matches
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,prefix,expected", [
    ("/static", "/static", True),
    ("/static/style.css", "/static", True),
    ("/statistics", "/static", False),
    ("/staticx/../control", "/static", False),
    ("/api/v1", "/api/v1", True),
    ("/api/v1/pulse", "/api/v1", True),
    ("/api/v1-admin", "/api/v1", False),
    ("/api/v10/x", "/api/v1", False),
])
def test_under_is_a_segment_match(path, prefix, expected) -> None:
    assert _under(path, prefix) is expected


@pytest.mark.parametrize("path", ["/staticx", "/statistics", "/api/v1x", "/api/v1-admin"])
def test_a_look_alike_path_hits_the_login_gate(path) -> None:
    """Before the fix these reached the router (404) without ever passing the
    auth gate; now they are sent to /login like every other private path."""
    with TestClient(app) as client:
        r = client.get(path, follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


def test_nothing_but_the_mount_lives_under_static() -> None:
    under_static = [getattr(r, "path", "") for r in app.routes
                    if _under(getattr(r, "path", ""), "/static")]
    assert under_static == ["/static"]


# ---------------------------------------------------------------------------
# Every control action is audited
# ---------------------------------------------------------------------------


def _module_functions(module) -> dict[str, ast.AST]:
    tree = ast.parse(inspect.getsource(module))
    return {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _calls_audit(fn: ast.AST, functions: dict[str, ast.AST], seen: set[str]) -> bool:
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        if isinstance(f, ast.Attribute) and f.attr == "record" and \
                isinstance(f.value, ast.Name) and f.value.id == "audit":
            return True
        name = f.id if isinstance(f, ast.Name) else None
        if name in functions and name not in seen:
            seen.add(name)
            if _calls_audit(functions[name], functions, seen):
                return True
    return False


def _audits(route: APIRoute) -> bool:
    module = sys.modules[route.endpoint.__module__]
    functions = _module_functions(module)
    fn = functions.get(route.endpoint.__name__)
    assert fn is not None, route.path
    return _calls_audit(fn, functions, {route.endpoint.__name__})


def test_every_mutating_route_writes_the_audit_log() -> None:
    unaudited = []
    checked = 0
    for method, path, route in _api_routes():
        mutating = method in ("POST", "PUT", "PATCH", "DELETE") or (method, path) in GET_JOB_TRIGGERS
        if not mutating or (method, path) in AUDIT_EXEMPT:
            continue
        checked += 1
        if not _audits(route):
            unaudited.append(f"{method} {path}")
    assert checked >= 30
    assert not unaudited, (
        "route(s) change state without writing the audit log — call audit.record "
        "or add a reasoned AUDIT_EXEMPT entry:\n  " + "\n  ".join(unaudited)
    )


def test_audit_exemptions_are_live_and_reasoned() -> None:
    registered = {(m, p) for m, p, _ in _api_routes()}
    assert set(AUDIT_EXEMPT) <= registered
    assert all(reason.strip() for reason in AUDIT_EXEMPT.values())


# ---------------------------------------------------------------------------
# The newly audited security events, driven for real
# ---------------------------------------------------------------------------


def _audit_lines() -> list[dict]:
    from app.config import load_settings

    path = Path(load_settings().audit_log_path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _new_entries(before: int) -> list[dict]:
    return _audit_lines()[before:]


def test_login_success_is_audited_and_failure_is_not() -> None:
    with TestClient(app) as client:
        before = len(_audit_lines())
        client.post("/login", data={"password": "wrong"}, follow_redirects=False)
        client.post("/api/v1/auth/login", json={"password": "wrong"})
        assert _new_entries(before) == [], "a failed login must not write the unbounded log"
        client.post("/login", data={"password": "test-token"}, follow_redirects=False)
        client.post("/api/v1/auth/login", json={"password": "test-token", "label": "phone"})
    entries = _new_entries(before)
    assert [(e["action"], e["params"]["channel"]) for e in entries] == [
        ("login", "web"), ("login", "app"),
    ]


def test_revoke_all_and_device_changes_are_audited_without_the_token() -> None:
    secret_token = "fcm-TOKEN-" + "x" * 140 + "tail1234"
    with TestClient(app) as client:
        app_token = client.post("/api/v1/auth/login", json={"password": "test-token"}).json()["token"]
        auth = {"Authorization": f"Bearer {app_token}"}
        before = len(_audit_lines())
        client.post("/api/v1/devices", json={"fcm_token": secret_token, "platform": "android"}, headers=auth)
        client.request("DELETE", "/api/v1/devices", json={"fcm_token": secret_token}, headers=auth)
        client.post("/api/v1/auth/revoke-all", headers=auth)
    entries = _new_entries(before)
    assert [e["action"] for e in entries] == [
        "device_register", "device_unregister", "app_tokens_revoke_all",
    ]
    assert entries[0]["params"]["token_tail"] == "tail1234"
    raw = json.dumps(entries)
    assert secret_token not in raw and "x" * 40 not in raw
