"""Smoke tests — app starts, auth gate works, healthz responds."""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


def test_healthz_is_public():
    with TestClient(app) as client:
        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


def test_unauthenticated_root_redirects_to_login():
    with TestClient(app) as client:
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


def test_login_page_renders():
    with TestClient(app) as client:
        r = client.get("/login")
        assert r.status_code == 200
        assert "360 CE Ops" in r.text


def test_login_rejects_wrong_password():
    with TestClient(app) as client:
        r = client.post("/login", data={"password": "wrong"}, follow_redirects=False)
        assert r.status_code == 401
        assert "Invalid password" in r.text


def test_login_accepts_correct_password_and_redirects():
    with TestClient(app) as client:
        r = client.post("/login", data={"password": "test-token"}, follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/"


def test_logout_clears_session():
    with TestClient(app) as client:
        client.post("/login", data={"password": "test-token"})
        r = client.get("/logout", follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"
        # After logout, protected routes redirect again
        r2 = client.get("/", follow_redirects=False)
        assert r2.status_code == 302
        assert r2.headers["location"] == "/login"
