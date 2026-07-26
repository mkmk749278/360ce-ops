"""Tests for the SAR shadow-ledger Clear button (2026-07-26).

Why the button exists: a measurement window is only worth reading if it is
honest, and on 2026-07-26 every resolved row in this ledger had been replayed
against the wrong candle — 172 rows sitting on screen reading −4.4R while
describing nothing. The owner needs to throw a window away without waiting on
an engine deploy.

Because it is destructive and irreversible it follows the control doctrine, and
these tests pin each part of that: explicit confirm, POST→redirect→GET so a
refresh cannot re-fire it, audited whether or not it succeeded, and the engine
(which owns the buffer and the file) doing the actual clearing.
"""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.engine_api import EngineApiClient  # noqa: E402
from app.main import app  # noqa: E402
from app.routes import sar_exit as sar_route  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


def _capture_audit(monkeypatch) -> list:
    calls: list = []
    monkeypatch.setattr(
        sar_route.audit, "record", lambda *a, **k: calls.append(k) or None
    )
    return calls


def test_clear_requires_auth():
    with TestClient(app) as client:
        r = client.post("/signals/sar/clear", data={"confirm": "CLEAR"},
                        follow_redirects=False)
        assert r.status_code == 302
        assert r.headers["location"] == "/login"


def test_clear_without_confirm_does_not_call_the_engine(monkeypatch):
    called = []

    async def fake_clear(self):
        called.append(True)
        return {"cleared_records": 9}

    monkeypatch.setattr(EngineApiClient, "clear_sar_ledger", fake_clear)
    audit_calls = _capture_audit(monkeypatch)

    with TestClient(app) as client:
        _login(client)
        r = client.post("/signals/sar/clear", data={}, follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/signals/sar"
    assert called == []          # engine untouched
    assert audit_calls == []     # nothing happened, nothing to audit


def test_clear_calls_engine_and_audits(monkeypatch):
    async def fake_clear(self):
        return {"cleared_records": 172, "queued": False}

    monkeypatch.setattr(EngineApiClient, "clear_sar_ledger", fake_clear)
    audit_calls = _capture_audit(monkeypatch)

    with TestClient(app) as client:
        _login(client)
        r = client.post("/signals/sar/clear", data={"confirm": "CLEAR"},
                        follow_redirects=False)
        # POST→redirect→GET: a refresh lands on the GET, never re-firing.
        assert r.status_code == 303
        assert r.headers["location"] == "/signals/sar"

    assert len(audit_calls) == 1
    assert audit_calls[0]["action"] == "sar_ledger_clear"
    assert audit_calls[0]["ok"] is True


def test_queued_clear_says_so_rather_than_claiming_a_count(monkeypatch):
    """Isolated mode returns queued=True — the count is not knowable yet."""
    async def fake_clear(self):
        return {"queued": True, "cleared_records": 0}

    monkeypatch.setattr(EngineApiClient, "clear_sar_ledger", fake_clear)
    _capture_audit(monkeypatch)

    with TestClient(app) as client:
        _login(client)
        client.post("/signals/sar/clear", data={"confirm": "CLEAR"},
                    follow_redirects=False)
        r = client.get("/signals/sar")
        assert "queued" in r.text.lower()
        assert "0 records" not in r.text


def test_engine_failure_is_audited_and_surfaced(monkeypatch):
    async def fake_clear(self):
        return {"error": "engine unreachable"}

    monkeypatch.setattr(EngineApiClient, "clear_sar_ledger", fake_clear)
    audit_calls = _capture_audit(monkeypatch)

    with TestClient(app) as client:
        _login(client)
        client.post("/signals/sar/clear", data={"confirm": "CLEAR"},
                    follow_redirects=False)
        r = client.get("/signals/sar")
        assert "failed" in r.text.lower()

    # A failed control action still belongs in the audit trail.
    assert len(audit_calls) == 1
    assert audit_calls[0]["ok"] is False


def test_page_offers_the_clear_control(monkeypatch):
    with TestClient(app) as client:
        _login(client)
        r = client.get("/signals/sar")
        assert r.status_code == 200
        assert "/signals/sar/clear" in r.text
        # Confirmation is part of the contract, not decoration.
        assert 'name="confirm"' in r.text


# ---- counter-SAR alignment surface --------------------------------------
#
# The engine records `sar_aligned_at_entry` and can split the rollup on it, but
# a measurement nothing renders is not observable — "measured but nowhere to
# look" is an unfinished change. These pin that the split actually reaches the
# page and that the two populations stay apart.


def _row(aligned, r):
    return {"sar_aligned": aligned, "r_multiple": r, "hold_min": 15.0 if r else None}


def test_split_keeps_opposed_out_of_the_aligned_average():
    from app.routes.sar_exit import summarize_alignment

    out = summarize_alignment([
        _row(True, 1.5), _row(True, 0.5),
        _row(False, -0.25), _row(False, -0.25),
    ])
    assert out["aligned"]["n"] == 2
    assert out["aligned"]["avg_r"] == 1.0
    assert out["opposed"]["n"] == 2
    assert out["opposed"]["avg_r"] == -0.25
    assert out["opposed_share"] == 0.5
    # Pooled this would read +0.375 — a blend that moves with the agreement
    # mix rather than with the exit, which is the whole reason for the split.


def test_rows_without_a_verdict_are_excluded_not_bucketed():
    from app.routes.sar_exit import summarize_alignment

    out = summarize_alignment([_row(None, 9.0), _row(True, 1.0)])
    assert out["unknown"] == 1
    assert out["known"] == 1
    assert out["aligned"]["avg_r"] == 1.0


def test_running_trades_do_not_drag_an_average_toward_zero():
    from app.routes.sar_exit import summarize_alignment

    out = summarize_alignment([_row(True, 2.0), _row(True, None)])
    assert out["aligned"]["n"] == 2
    assert out["aligned"]["closed"] == 1
    assert out["aligned"]["avg_r"] == 2.0


def test_alignment_filter_selects_one_population():
    from app.routes.sar_exit import filter_sar_signals

    rows = [
        dict(_row(True, 1.0), status="CLOSED_TRAIL", strategy="MTP", provenance="emitted"),
        dict(_row(False, -0.3), status="CLOSED_TRAIL", strategy="MTP", provenance="emitted"),
    ]
    assert len(filter_sar_signals(rows, alignment="opposed")) == 1
    assert filter_sar_signals(rows, alignment="opposed")[0]["sar_aligned"] is False
    assert len(filter_sar_signals(rows, alignment="")) == 2


def test_page_renders_the_split(monkeypatch):
    with TestClient(app) as client:
        _login(client)
        r = client.get("/signals/sar")
        assert r.status_code == 200
        assert "SAR agreement at entry" in r.text
        assert "Opposed share" in r.text or "No classified trades yet" in r.text
