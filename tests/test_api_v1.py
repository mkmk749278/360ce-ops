"""Tests for the native-app JSON API (Phase 1 of the ops Android app).

Covers the app-token store (issue / verify / revoke / persistence / hashing)
and the ``/api/v1`` surface: password→token login, the Bearer auth gate
returning 401 JSON (not the web's 302→/login), a token-gated read reusing the
engine client, and revoke-all invalidating live tokens.
"""
from __future__ import annotations

import os
import tempfile

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")
os.environ.setdefault(
    "OPS_APP_TOKENS_PATH", os.path.join(tempfile.gettempdir(), "ops_app_tokens_test.json")
)

from fastapi.testclient import TestClient  # noqa: E402

from app.app_tokens import AppTokenStore  # noqa: E402
from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.main import app  # noqa: E402


# ---- app-token store ----------------------------------------------------


def test_token_store_issue_verify_revoke(tmp_path):
    p = str(tmp_path / "toks.json")
    store = AppTokenStore(p)
    tok = store.issue(label="phone")
    assert store.verify(tok)
    assert not store.verify("bogus")
    assert not store.verify("")
    # Survives reload from disk (owner not forced to re-login on restart).
    assert AppTokenStore(p).verify(tok)
    assert store.revoke_all() == 1
    assert not store.verify(tok)
    # Revocation is durable across reload.
    assert not AppTokenStore(p).verify(tok)


def test_token_store_persists_only_hashes(tmp_path):
    p = str(tmp_path / "toks.json")
    store = AppTokenStore(p)
    tok = store.issue()
    with open(p, "r", encoding="utf-8") as fh:
        raw = fh.read()
    # The raw token must never be written to disk — only its SHA-256.
    assert tok not in raw


def test_token_store_unwritable_path_still_works():
    # A control tool must not break because the token volume is unwritable;
    # issue()/verify() fall back to the in-memory copy.
    store = AppTokenStore("/proc/cannot/write/here.json")
    tok = store.issue()
    assert store.verify(tok)


# ---- /api/v1 auth -------------------------------------------------------


def _login(client: TestClient) -> str:
    r = client.post("/api/v1/auth/login", json={"password": "test-token"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


def test_login_rejects_wrong_password():
    with TestClient(app) as client:
        r = client.post("/api/v1/auth/login", json={"password": "wrong"})
        assert r.status_code == 401


def test_login_issues_token_and_whoami_accepts_it():
    with TestClient(app) as client:
        tok = _login(client)
        assert tok
        r = client.get("/api/v1/auth/whoami", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert r.json()["ok"] is True


def test_read_without_token_is_401_json_not_redirect():
    with TestClient(app) as client:
        r = client.get("/api/v1/pairs", follow_redirects=False)
        assert r.status_code == 401
        # Must NOT be the web session redirect.
        assert "location" not in {k.lower() for k in r.headers}
        assert r.json()["detail"]


def test_read_with_token_returns_engine_json(monkeypatch):
    async def fake_pairs(self):
        return {"pairs": ["BTCUSDT", "ETHUSDT"]}

    monkeypatch.setattr(EngineApiClient, "pairs", fake_pairs)
    with TestClient(app) as client:
        tok = _login(client)
        r = client.get("/api/v1/pairs", headers={"Authorization": f"Bearer {tok}"})
        assert r.status_code == 200
        assert r.json() == {"pairs": ["BTCUSDT", "ETHUSDT"]}


def test_malformed_authorization_header_is_401():
    with TestClient(app) as client:
        _login(client)
        for bad in ("", "Bearer", "Basic abc", "Bearer   "):
            r = client.get("/api/v1/auth/whoami", headers={"Authorization": bad})
            assert r.status_code == 401, bad


def test_revoke_all_invalidates_live_token():
    with TestClient(app) as client:
        tok = _login(client)
        h = {"Authorization": f"Bearer {tok}"}
        assert client.get("/api/v1/auth/whoami", headers=h).status_code == 200
        assert client.post("/api/v1/auth/revoke-all", headers=h).status_code == 200
        # Token is dead after the lost-phone switch.
        assert client.get("/api/v1/auth/whoami", headers=h).status_code == 401
