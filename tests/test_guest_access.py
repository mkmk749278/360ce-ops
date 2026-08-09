"""The temporary read-only access layer.

Ops is a control plane, so a second door into it is a security change, and the
tests here are about the properties rather than the happy path:

* **The classification is total.** Every registered route is classified exactly
  once. A new page fails this test until somebody decides whether a guest may
  read it — the alternative is a deny-list, which is silent by construction on
  the next route and would hand tomorrow's page to every live code the day it
  ships.
* **Writes are structurally impossible**, not merely unlisted: the method check
  precedes the route lookup, so a guest cannot POST even to a route it may GET.
* **Revocation is immediate.** The session carries the grant id and the grant is
  re-read per request, so a cookie minted before the revoke dies on the next
  click. A test that only checked the login would pass on a build where a
  revoked code kept working for hours.
* **The nav does not lie.** A guest's nav is filtered from the same set the gate
  enforces, so it cannot offer a link that 403s.
* **A GET is not a proof of safety.** `/exit-backtest/run-now` is a GET that
  starts a `docker exec` job; it is pinned owner-only here, because it is the
  exact route a method-only gate would have handed over.
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import guest_scope  # noqa: E402
from app.guest_access import GuestAccessStore, normalise_code  # noqa: E402
from app.main import app  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _mint(client: TestClient, *, ttl_sec: float = 3600, label: str = "test-agent") -> str:
    """Mint a code straight from the live store the app is using."""
    code, _gid = app.state.guest_access.issue(label=label, ttl_sec=ttl_sec)
    return code


def _guest(client: TestClient, code: str):
    return client.post("/guest", data={"code": code}, follow_redirects=False)


@pytest.fixture
def client(tmp_path, monkeypatch):
    with TestClient(app) as c:
        # Isolate the store per test so grants never leak between them.
        app.state.guest_access = GuestAccessStore(str(tmp_path / "grants.json"))
        yield c


# ---------------------------------------------------------------------------
# the classification is total — the CI guard
# ---------------------------------------------------------------------------
def _registered_route_paths() -> set[str]:
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        if path is None or methods is None:
            continue  # Mounts (e.g. /static) are handled before the role check
        paths.add(path)
    return paths


def test_every_registered_route_is_classified():
    """A new route must be classified guest-readable or owner-only.

    This is the guard that keeps the scope table from becoming a deny-list. If
    it fails, add the route to `GUEST_READ_ROUTES` (a reader may see it) or to
    `OWNER_ONLY` with a reason (it may not) — do not delete the assertion.
    """
    classified = guest_scope.GUEST_READ_ROUTES | set(guest_scope.OWNER_ONLY)
    unclassified = _registered_route_paths() - classified - guest_scope.PUBLIC_PATHS
    assert not unclassified, (
        "unclassified route(s) — decide whether a read-only guest may see them: "
        f"{sorted(unclassified)}"
    )


def test_classification_has_no_overlap():
    both = guest_scope.GUEST_READ_ROUTES & set(guest_scope.OWNER_ONLY)
    assert not both, f"routes classified twice: {sorted(both)}"


def test_owner_only_reasons_are_populated():
    """A withheld route has a written reason. 'Why can't the agent see this'
    must have an answer in the table, not an absence."""
    blank = [p for p, why in guest_scope.OWNER_ONLY.items() if not (why or "").strip()]
    assert not blank, f"owner-only routes with no stated reason: {blank}"


def test_no_control_route_is_guest_readable():
    """Structural restatement of the owner's own boundary: the control panel is
    excluded. Checked by prefix so a future /control/* page cannot be added to
    the readable set by accident."""
    leaked = [p for p in guest_scope.GUEST_READ_ROUTES if p.startswith("/control")]
    assert not leaked, f"control routes exposed to guests: {leaked}"


def test_api_v1_is_never_guest_readable():
    leaked = [p for p in guest_scope.GUEST_READ_ROUTES if p.startswith("/api/v1")]
    assert not leaked, f"app-token surface exposed to guests: {leaked}"


def test_every_mutating_route_is_owner_only():
    """Derived, not listed: any route registering a non-GET method must be
    owner-only. Rule 1 already blocks the request, but a write route sitting in
    the readable set would be a live contradiction waiting for rule 1 to move."""
    offenders = []
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        path = getattr(route, "path", None)
        if path and (methods - {"GET", "HEAD", "OPTIONS"}):
            if path in guest_scope.GUEST_READ_ROUTES and path not in guest_scope.PUBLIC_PATHS:
                offenders.append((path, sorted(methods)))
    assert not offenders, f"mutating routes in the guest-readable set: {offenders}"


def test_get_with_side_effects_is_owner_only():
    """`/exit-backtest/run-now` is a GET that starts a docker-exec job on the
    production engine. It is the concrete reason this module classifies routes
    instead of trusting the HTTP method, so it is pinned rather than assumed."""
    assert "/exit-backtest/run-now" in guest_scope.OWNER_ONLY
    assert "/exit-backtest/run-now" not in guest_scope.GUEST_READ_ROUTES


# ---------------------------------------------------------------------------
# the store
# ---------------------------------------------------------------------------
def test_code_is_not_recoverable_from_the_store(tmp_path):
    store = GuestAccessStore(str(tmp_path / "g.json"))
    code, _ = store.issue(label="x", ttl_sec=60)
    raw = (tmp_path / "g.json").read_text()
    assert code not in raw
    assert normalise_code(code) not in raw


def test_code_survives_typing_variants(tmp_path):
    store = GuestAccessStore(str(tmp_path / "g.json"))
    code, _ = store.issue(label="x", ttl_sec=60)
    mangled = code.lower().replace("-", " ")
    assert store.redeem(mangled) is not None


def test_expired_grant_is_refused(tmp_path):
    store = GuestAccessStore(str(tmp_path / "g.json"))
    code, gid = store.issue(label="x", ttl_sec=60)
    # issue() floors the TTL at 60s, so expire it by moving the record.
    for rec in store._grants.values():
        rec["expires_at"] = time.time() - 1
    assert store.redeem(code) is None
    assert store.lookup(gid) is None


def test_revoked_grant_is_refused_by_id(tmp_path):
    store = GuestAccessStore(str(tmp_path / "g.json"))
    code, gid = store.issue(label="x", ttl_sec=3600)
    assert store.redeem(code) is not None
    assert store.lookup(gid) is not None
    assert store.revoke(gid) is True
    assert store.lookup(gid) is None
    assert store.redeem(code) is None


def test_revocation_survives_a_restart(tmp_path):
    path = str(tmp_path / "g.json")
    store = GuestAccessStore(path)
    code, gid = store.issue(label="x", ttl_sec=3600)
    store.revoke(gid)
    # A fresh store over the same file — a revoke that lived only in memory
    # would silently come back on the next deploy.
    reloaded = GuestAccessStore(path)
    assert reloaded.lookup(gid) is None
    assert reloaded.redeem(code) is None


def test_failed_codes_lock_the_guest_door_but_not_the_owner(tmp_path):
    store = GuestAccessStore(str(tmp_path / "g.json"), max_failures=3, lockout_sec=60)
    good, _ = store.issue(label="x", ttl_sec=3600)
    for _ in range(3):
        assert store.redeem("WRONG-WRONG-WRONG-WRONG") is None
    assert store.locked_out() > 0
    # Even a valid code is refused while locked out — the throttle is on the
    # door, not on the code.
    assert store.redeem(good) is None


def test_purge_does_not_change_an_access_decision(tmp_path):
    store = GuestAccessStore(str(tmp_path / "g.json"))
    _code, gid = store.issue(label="x", ttl_sec=3600)
    assert store.purge(older_than_sec=0) >= 0
    assert store.lookup(gid) is not None  # a live grant is never purged


# ---------------------------------------------------------------------------
# the gate, end to end
# ---------------------------------------------------------------------------
def test_guest_login_page_is_public(client):
    assert client.get("/guest").status_code == 200


def test_bad_code_is_rejected_generically(client):
    r = client.post("/guest", data={"code": "NOPE1-NOPE2-NOPE3-NOPE4"}, follow_redirects=False)
    assert r.status_code == 401
    # The message must not distinguish wrong / expired / revoked.
    assert "expired or been revoked" in r.text


def test_guest_can_read_a_measurement_page(client, monkeypatch):
    code = _mint(client)
    assert _guest(client, code).status_code == 303
    r = client.get("/track-record")
    assert r.status_code == 200


def test_guest_cannot_reach_the_control_panel(client):
    code = _mint(client)
    _guest(client, code)
    r = client.get("/control", follow_redirects=False)
    assert r.status_code == 403
    assert "owner-only" in r.text


def test_guest_cannot_post_even_to_a_page_it_may_read(client):
    """Rule 1 precedes rule 2: the method is refused before the path is looked
    up, so a readable page is still not a writable one."""
    code = _mint(client)
    _guest(client, code)
    r = client.post("/control/kill-switch", data={"action": "engage"}, follow_redirects=False)
    assert r.status_code == 403
    r2 = client.post("/signals/sar/clear", follow_redirects=False)
    assert r2.status_code == 403


def test_guest_cannot_trigger_the_backtest_job(client):
    code = _mint(client)
    _guest(client, code)
    assert client.get("/exit-backtest/run-now", follow_redirects=False).status_code == 403


def test_guest_cannot_read_subscriber_tables(client):
    code = _mint(client)
    _guest(client, code)
    for path in ("/control/users", "/control/referrals", "/trials"):
        assert client.get(path, follow_redirects=False).status_code == 403, path


def test_guest_cannot_see_or_mint_access_grants(client):
    """The access panel is the thing that hands out access. A read-only holder
    reaching it would see every live grant and could mint another — so it is
    owner-only on the same footing as the kill switch, and moving it to its own
    sub-tab must not have loosened that."""
    code = _mint(client)
    _guest(client, code)
    assert client.get("/control/access", follow_redirects=False).status_code == 403
    assert client.post(
        "/control/access/issue", data={"label": "x", "ttl": "7d"}, follow_redirects=False
    ).status_code == 403
    assert client.post(
        "/control/access/revoke", data={"scope": "all"}, follow_redirects=False
    ).status_code == 403


def test_guest_cannot_run_the_diag_runner(client):
    code = _mint(client)
    _guest(client, code)
    assert client.get("/diag/geometry", follow_redirects=False).status_code == 403


def test_guest_cannot_walk_the_raw_data_volume(client):
    code = _mint(client)
    _guest(client, code)
    assert client.get("/data/raw/anything.json", follow_redirects=False).status_code == 403


def test_guest_is_refused_on_the_app_token_surface(client):
    code = _mint(client)
    _guest(client, code)
    r = client.get("/api/v1/pulse", follow_redirects=False)
    assert r.status_code in (401, 403)


def test_revoke_ends_a_live_session_on_the_next_request(client):
    """The property the owner asked for: 'I can disable that access too'. A
    login-time check would leave the cookie working until it expired."""
    store = app.state.guest_access
    code, gid = store.issue(label="agent", ttl_sec=3600)
    _guest(client, code)
    assert client.get("/track-record").status_code == 200

    store.revoke(gid)

    r = client.get("/track-record", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"].startswith("/guest")


def test_revoke_all_ends_every_session(client):
    store = app.state.guest_access
    code_a, _ = store.issue(label="a", ttl_sec=3600)
    code_b, _ = store.issue(label="b", ttl_sec=3600)
    _guest(client, code_a)
    assert store.revoke_all() == 2
    assert client.get("/track-record", follow_redirects=False).status_code == 302
    # And the other code cannot start a new session either.
    assert client.post("/guest", data={"code": code_b}, follow_redirects=False).status_code == 401


def test_guest_nav_offers_nothing_it_cannot_reach(client):
    """A nav link that 403s teaches the reader the app is broken. The nav is
    filtered from the same set the gate enforces; this asserts they agree by
    walking every link the guest is actually shown."""
    import re

    code = _mint(client)
    _guest(client, code)
    body = client.get("/track-record").text
    links = set(re.findall(r'href="(/[^"#?]*)"', body))
    for href in links:
        if href.startswith("/static") or href in ("/guest/logout", "/logout"):
            continue
        r = client.get(href, follow_redirects=False)
        assert r.status_code != 403, f"nav offered {href} to a guest and the gate refused it"


def test_guest_nav_hides_the_control_group(client):
    code = _mint(client)
    _guest(client, code)
    body = client.get("/track-record").text
    assert 'href="/control"' not in body
    assert "READ-ONLY" in body


def test_owner_session_is_unchanged(client):
    """The owner's door is untouched by any of this — including while a guest
    lockout is in force, which must never be able to lock the owner out."""
    app.state.guest_access._locked_until = time.time() + 3600
    r = client.post("/login", data={"password": "test-token"}, follow_redirects=False)
    assert r.status_code == 302
    assert client.get("/control", follow_redirects=False).status_code == 200


def test_unauthenticated_still_redirects_to_owner_login(client):
    r = client.get("/track-record", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# the owner's access sub-tab
# ---------------------------------------------------------------------------
def _owner(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


def test_access_page_renders_and_is_in_the_control_nav(client):
    """The panel moved off the engine page onto its own sub-tab (2026-08-06).
    A page that exists but is not linked is the nav defect this repo has already
    paid for, so the link is asserted, not assumed."""
    _owner(client)
    r = client.get("/control/access")
    assert r.status_code == 200
    assert 'href="/control/access"' in r.text
    # …and it is no longer a card on the engine page.
    assert "Temporary read-only access" not in client.get("/control").text


def test_minted_code_is_shown_once_and_not_again(client):
    """The code rides to its render through process memory, not the session
    cookie, so a refresh cannot re-display it — and neither can anyone who later
    reads the cookie."""
    _owner(client)
    r = client.post(
        "/control/access/issue", data={"label": "agent", "ttl": "1h"}, follow_redirects=True
    )
    assert r.status_code == 200
    import re

    m = re.search(
        r'<code id="new-code">([0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5}-[0-9A-Z]{5})</code>',
        r.text,
    )
    assert m, "the new code was not rendered"
    code = m.group(1)
    assert code not in client.get("/control/access").text
    # It still works — shown once is not issued once.
    assert app.state.guest_access.redeem(code) is not None


def test_the_minted_code_can_be_copied_in_one_click(client):
    """The code is displayed exactly once and only its hash is stored, so a
    partial hand-selection costs a whole grant. The button copies the value the
    server rendered — asserted against that value, not against the button's
    existence, because a copy control wired to the wrong string is the failure
    this is guarding."""
    _owner(client)
    import re

    r = client.post(
        "/control/access/issue", data={"label": "agent", "ttl": "1h"},
        follow_redirects=True,
    )
    code = re.search(
        r'<code id="new-code">([0-9A-Z-]+)</code>', r.text
    ).group(1)
    assert f'id="copy-code"' in r.text
    assert f'data-code="{code}"' in r.text
    # …and the link the holder actually opens, so the hand-off is one paste.
    assert 'id="copy-url"' in r.text
    assert "/guest" in r.text


def test_the_copy_control_has_a_fallback_when_the_clipboard_is_unavailable(client):
    """``navigator.clipboard`` is undefined over plain HTTP and in some embedded
    browsers. A button that appears to work and does nothing is worse than no
    button when the thing it copies cannot be shown again, so the failure path
    selects the text and says so."""
    _owner(client)
    r = client.post(
        "/control/access/issue", data={"label": "agent", "ttl": "1h"},
        follow_redirects=True,
    )
    assert "Clipboard unavailable" in r.text
    assert "getSelection" in r.text


def test_access_actions_flash_on_their_own_page(client):
    """Separate flash key: an engine action and an access action sharing one key
    means a result can render on a page the operator did not come from."""
    _owner(client)
    r = client.post(
        "/control/access/revoke", data={"scope": "all"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/control/access"


def test_unknown_ttl_is_refused_not_defaulted(client):
    _owner(client)
    before = app.state.guest_access.live_count()
    client.post("/control/access/issue", data={"label": "x", "ttl": "99y"}, follow_redirects=True)
    assert app.state.guest_access.live_count() == before


# ---------------------------------------------------------------------------
# The nav was filtered; the CONTROLS were not (2026-08-07).
#
# `test_guest_nav_offers_nothing_it_cannot_reach` walked links on ONE page and
# looked at `href` only. `/exit-backtest` is guest-readable and rendered, to a
# read-only session, a `POST /exit-backtest/run` form and a
# `GET /exit-backtest/run-now` job trigger — with the copy "Button not
# responding? Use the plain link" between them. The gate held (both 403), so
# this was never a security defect; it is the nav's own rule stopping one layer
# short, and a control that 403s is indistinguishable from a broken page.
#
# The requirement is DERIVED — every guest-readable page is rendered and every
# actionable target in it is driven — rather than written as a list of controls
# to hide, which would be silent on the next one.
# ---------------------------------------------------------------------------


def _guest_readable_pages() -> list[str]:
    """Literal HTML pages a guest may read, straight off the scope table."""
    from app import guest_scope

    skip = {"/guest", "/guest/logout", "/logout", "/login"}
    return sorted(
        p for p in guest_scope.GUEST_READ_ROUTES
        if "{" not in p
        and not p.startswith("/_partial")
        and not p.endswith((".csv", ".json", ".md"))
        and p not in skip
    )


def test_no_guest_page_renders_a_control_the_guest_cannot_use(client, monkeypatch):
    import re

    code = _mint(client)
    _guest(client, code)

    offending: list[str] = []
    for page in _guest_readable_pages():
        r = client.get(page)
        if r.status_code != 200:
            continue
        body = r.text
        # Every actionable target the page hands the reader: form actions
        # (including htmx's hx-post/hx-get) and plain links.
        targets: set[tuple[str, str]] = set()
        for m in re.finditer(r'<form[^>]*>', body):
            tag = m.group(0)
            action = re.search(r'action="(/[^"]*)"', tag)
            method = re.search(r'method="([a-zA-Z]+)"', tag)
            if action:
                targets.add((action.group(1), (method.group(1) if method else "GET").upper()))
        for m in re.finditer(r'hx-(post|put|delete|patch)="(/[^"]*)"', body):
            targets.add((m.group(2), m.group(1).upper()))
        for href in re.findall(r'href="(/[^"#?]*)"', body):
            if href.startswith("/static") or href in ("/guest/logout", "/logout"):
                continue
            targets.add((href, "GET"))

        for path, method in sorted(targets):
            resp = client.request(method, path, follow_redirects=False)
            if resp.status_code == 403:
                offending.append(f"{page} offers {method} {path}")

    assert not offending, (
        "guest-readable pages rendered controls the gate refuses:\n  "
        + "\n  ".join(offending)
        + "\nHide them with may_use(request, <path>, <method>) — the scope table "
          "is the one writer; do not add a second list."
    )


def test_exit_backtest_hides_its_trigger_from_a_guest_and_says_why(client):
    """The specific page the derived check was written for.

    Pinned separately because a future refactor could satisfy the sweep above by
    removing the page from the guest scope entirely — which would lose the
    results, and the results are data.
    """
    code = _mint(client)
    _guest(client, code)
    body = client.get("/exit-backtest").text

    assert 'action="/exit-backtest/run"' not in body
    assert "/exit-backtest/run-now" not in body
    assert "Button not responding?" not in body
    # …and the reader is told it is a tier limit, not a missing feature.
    assert "<strong>cannot start a run</strong>" in body
    # The results themselves are still on the page.
    assert "Exit-method backtest" in body


def test_the_owner_still_sees_the_trigger(client):
    """`may_use` decides what to RENDER; it must never take a control away from
    the owner, whose session is the one that runs the job."""
    client.post("/login", data={"password": "test-token"}, follow_redirects=False)
    body = client.get("/exit-backtest").text
    assert 'action="/exit-backtest/run"' in body
    assert "/exit-backtest/run-now" in body
    assert "cannot start a run" not in body
    assert "Button not responding?" in body
