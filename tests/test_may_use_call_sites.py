"""``may_use`` must be called with the request, and the failure is silent.

`may_use(request, path, method="GET")` decides whether to RENDER an in-page
control for the current session. Two templates called it with two arguments:

    may_use('/diagnostics/console/run', 'POST')

Python binds that as `request='/diagnostics/console/run'`, `path='POST'`,
`method='GET'` — no `TypeError`, because the arity is legal. The body then
reads:

    if getattr(request, "scope", {}).get("ops_role") != "guest":
        return True

`getattr` on a **string** returns the `{}` default, `ops_role` is `None`,
`None != "guest"` is True, and the function takes the owner short-circuit and
returns **True for every session** — including a read-only guest.

So it fails in the permissive direction, which is why it survived: the control
renders, and a control rendering is what "working" looks like. `/exit-backtest`
cost this repo a session to exactly this defect in 2026-08-07 (a POST run form
and a `docker exec` job trigger rendered to a guest, with *"Button not
responding? Use the plain link"* between them). `may_use` was written to fix
that, and two later call sites reintroduced it by dropping one argument.

`tests/test_guest_access.py` cannot catch it: it renders every guest-readable
page, collects the controls it finds, and drives them — so a control that
renders *and* is allowed by the serving gate passes. The bug is only visible in
the gap between "what renders" and "what the session may use", and that gap
closes the moment the serving gate happens to allow the route.

Hence a **derived** guard rather than a behavioural one: parse every template
for `may_use(` and require `request` first. A hand-kept list of correct call
sites would be silent by construction on the next template.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "templates"

#: `may_use(` followed by its first argument, across line breaks.
_CALL = re.compile(r"may_use\(\s*([^,\)]+)")


def _call_sites():
    for path in sorted(_TEMPLATES.rglob("*.html")):
        body = path.read_text(encoding="utf-8")
        for m in _CALL.finditer(body):
            line = body.count("\n", 0, m.start()) + 1
            yield path.name, line, m.group(1).strip()


def test_there_are_call_sites_to_check():
    """A guard that silently checks nothing is worse than no guard.

    If `may_use` is ever renamed this test must fail rather than pass over an
    empty iterator — the dead-instrument shape.
    """
    assert list(_call_sites()), "no may_use( call sites found — was it renamed?"


@pytest.mark.parametrize("template,line,first_arg", list(_call_sites()))
def test_may_use_is_called_with_the_request(template, line, first_arg):
    """The first argument must be `request`, not the path.

    Passing the path first is legal Python and returns True unconditionally,
    so the control renders to a session that cannot use it — the 2026-08-07
    defect, reintroduced.
    """
    assert first_arg == "request", (
        f"{template}:{line} calls may_use({first_arg}, …) — the first argument "
        "must be `request`. Passing the path first binds it to `request`, "
        "`getattr(str, 'scope', {})` yields {}, and the function returns True "
        "for every session including a read-only guest."
    )


def test_the_permissive_failure_mode_is_real_and_not_a_theory():
    """Drive the real function with the broken shape and the fixed one.

    Reading the source produces a hypothesis about behaviour, never a
    measurement of it — so this calls `may_use` both ways and asserts the
    outcomes differ for a guest.
    """
    from app import guest_scope

    class _Req:
        scope = {"ops_role": "guest"}

    # The broken shape: path bound to `request`.
    assert guest_scope.may_use("/diagnostics/console/run", "POST") is True, (
        "the two-argument call returns True — this is the defect, and it is "
        "why a guest saw ten Run buttons on a page whose POST it could not "
        "issue"
    )

    # The correct shape, for a route a guest may NOT post to.
    assert guest_scope.may_use(_Req(), "/control/users/exit-mechanism", "POST") is False

    # …and an owner session is unaffected either way.
    class _Owner:
        scope = {"ops_role": "owner"}

    assert guest_scope.may_use(_Owner(), "/control/users/exit-mechanism", "POST") is True
