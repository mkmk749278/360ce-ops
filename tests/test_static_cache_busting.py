"""Static assets are linked by content hash (2026-11-25).

Cloudflare caches /static/* for four hours. With an unversioned link, the
phase-2 deploy served new HTML over the old stylesheet and the Pulse tiles went
live unstyled. These pin the fix where it can regress: in the templates.
"""
from __future__ import annotations

import os
import pathlib
import re

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app, static_url  # noqa: E402

REPO = pathlib.Path(__file__).resolve().parents[1]


def test_no_template_links_an_unversioned_static_asset():
    bare = []
    for f in sorted((REPO / "app" / "templates").glob("*.html")):
        for m in re.finditer(r'(?:href|src)="/static/[^"]*"', f.read_text()):
            bare.append(f"{f.name}: {m.group(0)}")
    assert not bare, "link static assets with static_url(...):\n  " + "\n  ".join(bare)


def test_the_url_carries_the_file_hash_and_still_serves():
    url = static_url("style.css")
    assert re.fullmatch(r"/static/style\.css\?v=[0-9a-f]{12}", url)
    with TestClient(app) as c:
        r = c.get(url)
        login = c.get("/login").text
    assert r.status_code == 200 and "--bg" in r.text
    assert url in login


def test_an_unknown_asset_falls_back_to_its_plain_path():
    assert static_url("nope.css") == "/static/nope.css"
