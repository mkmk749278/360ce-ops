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


def _row(aligned, r, **over):
    row = {
        "sar_aligned": aligned,
        "r_multiple": r,
        "hold_min": 15.0 if r else None,
        "status": "CLOSED_TRAIL" if r is not None else "RUNNING",
        "symbol": over.pop("symbol", "AAAUSDT"),
        "side": "LONG",
        "exit_price": over.pop("exit_price", r),
    }
    row.update(over)
    return row


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


# ---- what #90 got wrong, pinned ------------------------------------------


def _ledger_rec(symbol, aligned, *, provenance, resolved=True):
    """A ledger record in the ENGINE's shape, driven through the real reducer.

    Hand-writing the reduced row shape is how a wrong key goes green over dead
    code, so the population tests below build records the way the engine writes
    them and let ``reduce_sar_signals`` produce the rows under test.
    """
    rec = {
        "setup_class": "MTP@SAREXIT",
        "symbol": symbol,
        "side": "LONG",
        "entry": 100.0,
        "sl_distance": 1.0,
        "exit_model": "trailing",
        "provenance": provenance,
        "suppress_timestamp": 1_800_000_000.0,
    }
    if resolved:
        rec.update({
            "classification": "WOULD_WIN",
            "trail_exit_price": 101.0,
            "trail_exit_reason": "trail",
            "trail_hold_min": 15.0,
            # The engine writes this ONLY in the resolve path, so an unresolved
            # record genuinely carries no key — that asymmetry is the bug the
            # "no verdict" copy misread, and the fixture must reproduce it.
            "sar_aligned_at_entry": aligned,
        })
    return rec


def test_the_split_follows_the_source_filter():
    """#90 measured the panel on the whole ledger while the table was filtered.

    That is the #88 mistake again: only the delivered population can justify a
    live change, and a panel that ignores the selector pools it with candidates
    nobody ever saw.
    """
    from app.routes.sar_exit import (
        filter_sar_signals,
        reduce_sar_signals,
        summarize_alignment,
    )

    rows = reduce_sar_signals([
        _ledger_rec("AAAUSDT", True, provenance="emitted"),
        _ledger_rec("BBBUSDT", False, provenance="suppressed"),
        _ledger_rec("CCCUSDT", False, provenance="suppressed"),
    ])
    assert summarize_alignment(rows)["opposed_share"] == 2 / 3

    scoped = filter_sar_signals(rows, source="emitted")
    # Delivered rows only: one trade, agreed, so nothing was taken against the
    # indicator in the population that reached a subscriber.
    assert summarize_alignment(scoped)["opposed_share"] == 0.0


def test_page_panel_moves_with_the_source_filter(monkeypatch):
    """The route-level version — where #90 actually went wrong.

    ``summarize_alignment`` was always correct; the route handed it the
    unfiltered ledger. So the page could show 149 gate-suppressed rows under a
    split computed over all 267 and nothing said so.
    """
    from app.data_sources.data_volume import DataVolumeReader

    ledger = [
        _ledger_rec("AAAUSDT", True, provenance="emitted"),
        _ledger_rec("BBBUSDT", False, provenance="suppressed"),
        _ledger_rec("CCCUSDT", False, provenance="suppressed"),
        _ledger_rec("DDDUSDT", False, provenance="suppressed"),
    ]
    monkeypatch.setattr(DataVolumeReader, "sar_exit_candidates", lambda self: ledger)

    with TestClient(app) as client:
        _login(client)
        # Whole ledger: 3 of 4 resolved trades were taken against the indicator.
        assert "Opposed share: 75%" in client.get("/signals/sar").text
        # Delivered only: the one row a subscriber saw had SAR on its side.
        assert "Opposed share: 0%" in client.get("/signals/sar?source=emitted").text


def test_running_rows_are_pending_not_a_missing_verdict():
    """261 of 277 rows read "the walker refused to replay them" on 2026-07-27.

    Not one of them had been refused: every RUNNING row is blank by
    construction because the engine writes the flag in the resolve path. The
    two causes must stay separate or the page reports a data fault that is
    not happening.
    """
    from app.routes.sar_exit import reduce_sar_signals, summarize_alignment

    rows = reduce_sar_signals([
        _ledger_rec("AAAUSDT", True, provenance="emitted"),
        _ledger_rec("BBBUSDT", None, provenance="emitted", resolved=False),
        _ledger_rec("CCCUSDT", None, provenance="emitted", resolved=False),
    ])
    out = summarize_alignment(rows)
    assert out["pending"] == 2
    assert out["unresolved"] == 0
    assert out["unknown"] == 2


def test_a_finished_row_without_a_verdict_is_a_real_exclusion():
    from app.routes.sar_exit import summarize_alignment

    out = summarize_alignment([
        _row(True, 1.0),
        dict(_row(None, None), status="NO_DATA"),
    ])
    assert out["pending"] == 0
    assert out["unresolved"] == 1


def test_pending_is_selectable():
    from app.routes.sar_exit import filter_sar_signals

    rows = [_row(True, 1.0), _row(None, None), _row(None, None)]
    assert len(filter_sar_signals(rows, alignment="pending")) == 2


def test_overlapping_entries_into_one_move_are_disclosed():
    """Three BUSDT rows carried 3/8 of the agreed bucket and one rally.

    +2.23R / +2.12R / +2.72R, stamped 00:04 / 00:47 / 01:34, all exiting at
    0.1959 — the same move counted three times. The bucket must say so.
    """
    from app.routes.sar_exit import summarize_alignment

    out = summarize_alignment([
        _row(True, 2.23, symbol="BUSDT", exit_price=0.1959),
        _row(True, 2.12, symbol="BUSDT", exit_price=0.1959),
        _row(True, 2.72, symbol="BUSDT", exit_price=0.1959),
    ])
    assert out["aligned"]["closed"] == 3
    assert out["aligned"]["distinct_exits"] == 1


def test_share_survives_verdicts_without_resolutions():
    """Forward-compat with stamping agreement at entry.

    Once the flag lands at stamp time a bucket holds unresolved rows, and every
    rate has to keep dividing by the resolved count — the engine's denominator.
    A None share must not reach ``format()``.
    """
    from app.routes.sar_exit import summarize_alignment

    out = summarize_alignment([_row(True, None), _row(False, None)])
    assert out["known"] == 2
    assert out["known_closed"] == 0
    assert out["opposed_share"] is None
    assert out["aligned"]["avg_r"] is None
