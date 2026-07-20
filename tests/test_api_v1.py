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
os.environ.setdefault(
    "OPS_DEVICE_TOKENS_PATH", os.path.join(tempfile.gettempdir(), "ops_device_tokens_test.json")
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


# ---- /api/v1 control writes (Phase 3) -----------------------------------


def test_control_write_requires_token():
    with TestClient(app) as client:
        r = client.post('/api/v1/control/auto-mode', json={'mode': 'paper'},
                        follow_redirects=False)
        assert r.status_code == 401
        assert 'location' not in {k.lower() for k in r.headers}


def test_control_auto_mode_rejects_invalid_mode():
    with TestClient(app) as client:
        tok = _login(client)
        r = client.post('/api/v1/control/auto-mode',
                        headers={'Authorization': f'Bearer {tok}'},
                        json={'mode': 'nonsense'})
        assert r.status_code == 422


def test_control_auto_mode_calls_engine_setter(monkeypatch):
    captured = []

    async def fake_set(self, mode):
        captured.append(mode)
        return {'ok': True, 'mode': mode}

    monkeypatch.setattr(EngineApiClient, 'set_auto_mode', fake_set)
    with TestClient(app) as client:
        tok = _login(client)
        r = client.post('/api/v1/control/auto-mode',
                        headers={'Authorization': f'Bearer {tok}'},
                        json={'mode': 'PAPER'})
        assert r.status_code == 200
        body = r.json()
        assert body['ok'] is True and body['action'] == 'auto_mode'
        # Normalized to lowercase before hitting the engine.
        assert captured == ['paper']


def test_control_kill_switch_engage_surfaces_engine_error(monkeypatch):
    async def fake_ks(self, engaged, reason=None):
        return {'error': 'engine unreachable'}

    monkeypatch.setattr(EngineApiClient, 'set_kill_switch', fake_ks)
    with TestClient(app) as client:
        tok = _login(client)
        r = client.post('/api/v1/control/kill-switch',
                        headers={'Authorization': f'Bearer {tok}'},
                        json={'engaged': True, 'reason': 'test'})
        assert r.status_code == 200
        body = r.json()
        assert body['ok'] is False
        assert 'unreachable' in body['detail']


def test_control_tunables_rejects_empty(monkeypatch):
    with TestClient(app) as client:
        tok = _login(client)
        r = client.post('/api/v1/control/tunables',
                        headers={'Authorization': f'Bearer {tok}'},
                        json={'values': {}})
        assert r.status_code == 422


# ---- /api/v1/devices (Phase 4 push registry) ----------------------------


def test_device_register_requires_token():
    with TestClient(app) as client:
        r = client.post('/api/v1/devices', json={'fcm_token': 'x'},
                        follow_redirects=False)
        assert r.status_code == 401


def test_device_register_and_unregister():
    with TestClient(app) as client:
        tok = _login(client)
        h = {'Authorization': f'Bearer {tok}'}
        reg = client.post('/api/v1/devices', headers=h,
                          json={'fcm_token': 'fcm-abc', 'platform': 'android'})
        assert reg.status_code == 200 and reg.json()['ok'] is True
        # Idempotent re-register.
        client.post('/api/v1/devices', headers=h, json={'fcm_token': 'fcm-abc'})
        dele = client.request('DELETE', '/api/v1/devices', headers=h,
                              json={'fcm_token': 'fcm-abc'})
        assert dele.status_code == 200 and dele.json()['removed'] is True


def test_device_register_rejects_empty_token():
    with TestClient(app) as client:
        tok = _login(client)
        r = client.post('/api/v1/devices',
                        headers={'Authorization': f'Bearer {tok}'},
                        json={'fcm_token': '   '})
        assert r.status_code == 422


# ---- analysis surfaces (Profit / Alerts / Truth / Invalidations) --------


def test_analysis_endpoints_require_token():
    with TestClient(app) as client:
        for path in ('/api/v1/profit', '/api/v1/alerts', '/api/v1/truth',
                     '/api/v1/invalidations', '/api/v1/performance'):
            r = client.get(path, follow_redirects=False)
            assert r.status_code == 401, path


def test_invalidations_and_performance_return_data(monkeypatch):
    from app.data_sources.data_volume import DataVolumeReader
    monkeypatch.setattr(DataVolumeReader, 'invalidation_records', lambda self: {'records': []})
    monkeypatch.setattr(DataVolumeReader, 'signal_performance', lambda self: {'trades': 0})
    with TestClient(app) as client:
        tok = _login(client)
        h = {'Authorization': f'Bearer {tok}'}
        assert client.get('/api/v1/invalidations', headers=h).json() == {'records': []}
        assert client.get('/api/v1/performance', headers=h).json() == {'trades': 0}


def test_profit_reuses_row_builder(monkeypatch):
    import app.routes.profit as p

    async def fake_rows(*a, **k):
        return ([], None)

    monkeypatch.setattr(p, '_build_rows', fake_rows)
    with TestClient(app) as client:
        tok = _login(client)
        r = client.get('/api/v1/profit', headers={'Authorization': f'Bearer {tok}'})
        assert r.status_code == 200
        body = r.json()
        assert body['count'] == 0 and 'summary' in body and 'breakdown' in body


# ---- analysis-bundle (the mediator surface) -----------------------------


def test_analysis_bundle_requires_token():
    with TestClient(app) as client:
        r = client.get('/api/v1/analysis-bundle', follow_redirects=False)
        assert r.status_code == 401


def test_analysis_bundle_composes_all_sections(monkeypatch):
    """With a token the bundle returns every section. Profit is stubbed to the
    empty row builder; every other source runs against the (empty) test data
    volume and must still populate its key rather than 500 the request."""
    import app.routes.profit as p

    async def fake_rows(*a, **k):
        return ([], None)

    monkeypatch.setattr(p, '_build_rows', fake_rows)
    with TestClient(app) as client:
        tok = _login(client)
        r = client.get('/api/v1/analysis-bundle', headers={'Authorization': f'Bearer {tok}'})
        assert r.status_code == 200, r.text
        body = r.json()
        for key in ('generated_at', 'params', 'note', 'strategy_lab', 'profit',
                    'performance', 'invalidations', 'tunables', 'truth', 'alerts'):
            assert key in body, key
        assert body['profit']['count'] == 0
        assert set(body['truth'].keys()) == {'snapshot', 'window'}
        assert body['params']['window'] == 'all'


def test_analysis_bundle_section_isolation(monkeypatch):
    """A source that raises degrades to an {"error": …} marker for its section
    only — the rest of the bundle is unaffected and the call is still 200."""
    import app.routes.profit as p
    from app.data_sources.engine_api import EngineApiClient

    async def fake_rows(*a, **k):
        return ([], None)

    async def boom(self, *a, **k):
        raise RuntimeError("tunables source down")

    monkeypatch.setattr(p, '_build_rows', fake_rows)
    monkeypatch.setattr(EngineApiClient, 'tunables_state', boom)
    with TestClient(app) as client:
        tok = _login(client)
        r = client.get('/api/v1/analysis-bundle', headers={'Authorization': f'Bearer {tok}'})
        assert r.status_code == 200, r.text
        body = r.json()
        assert isinstance(body['tunables'], dict)
        assert 'tunables source down' in body['tunables'].get('error', '')
        # a sibling section is unaffected
        assert body['profit']['count'] == 0


def test_analysis_bundle_row_cap_is_bounded(monkeypatch):
    """The profit row list is capped so a large data volume can't blow up the
    payload, even when the caller asks for more."""
    import app.routes.profit as p

    async def many_rows(*a, **k):
        return ([{"i": i} for i in range(5000)], None)

    monkeypatch.setattr(p, '_build_rows', many_rows)
    # Isolate the row-cap logic from the summary reducers (which expect the
    # full row shape the real builder produces, not our minimal stubs).
    monkeypatch.setattr(p, '_summary', lambda rows: {})
    monkeypatch.setattr(p, '_aggregates', lambda rows: {})
    monkeypatch.setattr(p, '_strategy_summary', lambda rows, fee: {})
    monkeypatch.setattr(p, '_breakdown_scoreband', lambda rows, fee: [])
    monkeypatch.setattr(p, '_breakdown_path', lambda rows, fee: [])
    monkeypatch.setattr(p, '_breakdown_regime', lambda rows, fee: [])
    with TestClient(app) as client:
        tok = _login(client)
        r = client.get(
            '/api/v1/analysis-bundle?limit=999999',
            headers={'Authorization': f'Bearer {tok}'},
        )
        assert r.status_code == 200
        body = r.json()
        assert body['profit']['count'] == 5000  # true total preserved
        assert len(body['profit']['rows']) == 1000  # but rows capped at 1000
        assert body['params']['row_cap'] == 1000
