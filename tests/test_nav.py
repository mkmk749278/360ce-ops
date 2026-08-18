"""Every page this app registers must be reachable from the nav.

Owner-caught 2026-08-06: `/signals/price-action` (the price-action LANE) and
`/signals/structural-veto` had no nav entry at all, so both could only be reached
by typing the URL — and both set ``active: "signals"``, which is the *Feed* tab's
key, so the Feed pill lit up on a page that was not the feed.

Meanwhile the label "Price action" sat on `/signals/structural-snap`, which is
the SL/TP1 geometry repair and a different thing entirely. Two pages, one name,
and the reachable one was the wrong one.

This is "dark work must be observable" arriving at the last hop. A panel that
renders perfectly on a page nobody can navigate to is exactly as useful as no
panel, and the whole session that produced those pages was about that rule.

**A hand-maintained nav is a floor** — it lists exactly the pages somebody
already typed and is silent by construction on the next one. So the requirement
here is DERIVED from the registered routes rather than written out again: a new
literal page under a nav-owning prefix fails this test until it is linked.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

REPO = pathlib.Path(__file__).resolve().parents[1]

#: Prefixes whose literal pages must appear in the nav. A page outside these is
#: not necessarily a fault (partials, exports, control POST targets), so the
#: rule is scoped rather than global.
NAV_OWNED_PREFIXES = ("/signals/", "/system")

#: Registered paths that are deliberately not nav destinations.
NAV_EXEMPT: set[str] = set()   # `{}` is a dict — this bit me writing the test.


def _nav_entries() -> list[tuple[str, str, str]]:
    """(path, label, active-key) parsed out of `base.html`'s own NAV literal."""
    src = (REPO / "app" / "templates" / "base.html").read_text()
    m = re.search(r"set NAV = (\[.*?\])\s*-?%\}", src, re.S)
    assert m, "could not find the NAV literal in base.html"
    # Strip Jinja comments before parsing — they are not Python.
    body = re.sub(r"\{#-?.*?-?#\}", "", m.group(1), flags=re.S)
    nav = ast.literal_eval(body)
    out: list[tuple[str, str, str]] = []
    for _group, _label, subs in nav:
        for path, label, key in subs:
            out.append((path, label, key))
    return out


def _registered_pages() -> set[str]:
    """Literal GET paths under a nav-owned prefix, from the route decorators."""
    found: set[str] = set()
    for f in (REPO / "app" / "routes").glob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            for dec in node.decorator_list:
                if (
                    isinstance(dec, ast.Call)
                    and isinstance(dec.func, ast.Attribute)
                    and dec.func.attr == "get"
                    and dec.args
                    and isinstance(dec.args[0], ast.Constant)
                    and isinstance(dec.args[0].value, str)
                ):
                    path = dec.args[0].value
                    if "{" in path or path.endswith(".csv"):
                        continue
                    if any(path.startswith(p) for p in NAV_OWNED_PREFIXES):
                        found.add(path)
    return found


def test_every_registered_signals_page_is_in_the_nav():
    """Derived, not listed. A new page under /signals/ fails here until linked —
    which is the whole point: a nav somebody maintains by hand is silent on the
    next page, and an unreachable page is an unfinished change."""
    nav_paths = {p for p, _, _ in _nav_entries()}
    missing = _registered_pages() - nav_paths - NAV_EXEMPT
    assert not missing, (
        f"registered but unreachable from the nav: {sorted(missing)}. "
        "Add an entry to base.html's NAV, or to NAV_EXEMPT with a reason."
    )


def test_no_two_nav_entries_share_a_label():
    """"Price action" pointed at the structural snap while the price-action lane
    had no entry — one name, two pages, and the reachable one was wrong."""
    labels: dict[str, list[str]] = {}
    for path, label, _ in _nav_entries():
        labels.setdefault(label, []).append(path)
    dupes = {k: v for k, v in labels.items() if len(v) > 1}
    assert not dupes, f"nav labels collide: {dupes}"


def test_no_two_nav_entries_share_an_active_key():
    """The active key picks which pill lights up. Two pages sharing one means a
    page highlights a tab that is not itself."""
    keys: dict[str, list[str]] = {}
    for path, _, key in _nav_entries():
        keys.setdefault(key, []).append(path)
    dupes = {k: v for k, v in keys.items() if len(v) > 1}
    assert not dupes, f"nav active keys collide: {dupes}"


def test_each_page_sets_its_own_active_key():
    """`/signals/price-action` and `/signals/structural-veto` both set
    ``active: "signals"`` — the *Feed* tab's key — so the Feed pill lit up on a
    page that was not the feed.

    Matched on the route DECORATOR, not on the path appearing anywhere in the
    file: the first cut tested `if path in src`, and `"/"` is a substring of
    every module, so it demanded that `auth.py` set `active="pulse"`. A
    substring search is not a call-site check — the same lesson as pinning the
    call site rather than the import.
    """
    by_path = {path: key for path, _, key in _nav_entries()}
    seen: set[str] = set()

    for f in (REPO / "app" / "routes").glob("*.py"):
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            paths = [
                d.args[0].value
                for d in node.decorator_list
                if isinstance(d, ast.Call)
                and isinstance(d.func, ast.Attribute)
                and d.func.attr == "get"
                and d.args
                and isinstance(d.args[0], ast.Constant)
                and isinstance(d.args[0].value, str)
                and d.args[0].value in by_path
            ]
            if not paths:
                continue
            actives = {
                v.value
                for n in ast.walk(node)
                if isinstance(n, ast.Dict)
                for k, v in zip(n.keys, n.values, strict=False)
                if isinstance(k, ast.Constant) and k.value == "active"
                and isinstance(v, ast.Constant) and isinstance(v.value, str)
            }
            if not actives:
                continue                      # renders no template of its own
            for path in paths:
                seen.add(path)
                assert by_path[path] in actives, (
                    f"{f.name} serves {path} setting active={actives} — "
                    f"the nav expects {by_path[path]!r}, so a different tab "
                    "would light up"
                )

    assert seen, "no nav page was checked — the AST walk found nothing"


def test_the_price_action_lane_and_the_snap_are_named_apart():
    """They measure different things and were both called "Price action". The
    lane generates signals from structure; the snap repairs SL/TP1 geometry on
    signals other paths generated."""
    by_path = {p: label for p, label, _ in _nav_entries()}
    assert by_path["/signals/price-action"] == "Price action"
    assert by_path["/signals/structural-snap"] != "Price action"


def test_every_nav_destination_answers():
    """The route list is not the authority — the request is. A literal page
    under a catch-all prefix can be registered and still 404 (#entry-features),
    so this drives real requests rather than reading `app.routes`."""
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as client:
        client.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]},
                    follow_redirects=False)
        for path, label, _ in _nav_entries():
            r = client.get(path, follow_redirects=False)
            assert r.status_code != 404, f"{label} ({path}) 404s from the nav"
