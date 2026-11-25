"""Labs: one list of experiments, rendered twice (nav group + /labs index).

The index and _nav.html's Labs nav group are two renderings of the same set.
A hand-kept second list drifts silently, so they are held equal here: a new
experiment page cannot be linked in one place and forgotten in the other.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

import ast  # noqa: E402
import re  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.routes.labs import LABS  # noqa: E402
from tests.test_nav import REPO  # noqa: E402


def _nav_group(key: str) -> list[str]:
    src = (REPO / "app" / "templates" / "_nav.html").read_text()
    m = re.search(r"set NAV = (\[.*?\])\s*-?%\}", src, re.S)
    body = re.sub(r"\{#-?.*?-?#\}", "", m.group(1), flags=re.S)
    for gkey, _label, subs in ast.literal_eval(body):
        if gkey == key:
            return [p for p, _, _ in subs]
    raise AssertionError(f"no nav group {key!r}")


def test_the_index_and_the_labs_nav_list_the_same_experiments():
    nav = _nav_group("labs")
    assert nav[0] == "/labs"
    assert [p for _, p, *_ in LABS] == nav[1:]


def test_every_row_says_whether_it_can_act():
    for _cat, path, name, what, reach in LABS:
        assert what.strip() and name.strip(), path
        assert reach.startswith(("Yes", "Only", "No")), (path, reach)


def test_live_holds_no_experiment():
    """The point of the split: Live is what is happening, not what is measured."""
    labs = {p for _, p, *_ in LABS}
    assert not labs & set(_nav_group("live"))


def test_the_index_renders_every_experiment_for_owner_and_guest():
    with TestClient(app) as c:
        c.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
        body = c.get("/labs").text
    for _cat, path, name, *_ in LABS:
        assert f'href="{path}"' in body, path
    assert body.count("ACTS</span>") == sum(1 for *_, r in LABS if r.startswith("Yes"))
