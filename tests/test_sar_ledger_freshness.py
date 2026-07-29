"""A frozen SAR ledger must say so, instead of rendering as a live signal book.

Regression cover for the 2026-07-29 defect. The engine's resolver stopped
producing verdicts, but the price feed kept running — so `/signals/sar` showed
401 rows badged RUNNING beside *current* marks, and 395 of them described trades
that had already closed.

Checked against real Binance candles at the time: DEXEUSDT LONG hit its stop
**1 minute** after entry and sat here for 19.2h reading −14%; EULUSDT SHORT 4
minutes after entry, 12.2h; 104 running rows were already past a −1R stop and 37
past −5R. The ledger's newest stamp and newest resolution were both 11.6h old
while `current_price` was accurate to 0.18%. Nothing on the page looked wrong.

Two contracts here, and the second is the subtle one:

* the page states the pipeline's freshness *before* any of its numbers, because
  every number is an average over whatever the pipeline last managed to write;
* staleness is judged on **the ledger's own progress**, not wall-clock age. Hold
  times legitimately run from 15 minutes to the full 48h window, so any fixed
  age threshold either flags healthy long holds or misses a fresh freeze.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test")

from app.routes.sar_exit import (  # noqa: E402
    SAR_RUNNING,
    mark_stale_rows,
    reduce_ledger_freshness,
    reduce_sar_signals,
    summarize_rows,
)

CTX = "NY/ACCUMULATION/NORMAL/BTC_NEUTRAL"
HOUR = 3600.0


def _arm(suffix: str, *, ts: float, cls: str | None, symbol: str, entry: float,
         hold_min: float | None) -> dict:
    return {
        "gate_name": (
            "sar_exit_shadow:base" if suffix == "@SARBASE" else "sar_exit_shadow:trail"
        ),
        "setup_class": f"MOVER_TREND_PULLBACK{suffix}",
        "symbol": symbol, "side": "SHORT", "channel": "scalp",
        "entry": entry, "stop_loss": entry * 1.03, "tp1": entry * 0.97,
        "sl_distance": entry * 0.03,
        "context_key": CTX, "suppress_timestamp": ts,
        "classification": cls, "provenance": "enqueued",
        "exit_model": "trailing" if suffix == "@SAREXIT" else "static",
        "trail_exit_price": (entry * 0.99) if cls else None,
        "trail_exit_reason": "trail" if cls else None,
        "trail_hold_min": hold_min,
    }


def _pair(ts: float, *, symbol: str, resolved: bool, hold_min: float = 90.0):
    """A stamped pair in the ledger's real shape — both arms, one candidate."""
    cls = "WOULD_WIN" if resolved else None
    return [
        _arm("@SARBASE", ts=ts, cls=cls, symbol=symbol, entry=1.0,
             hold_min=hold_min if resolved else None),
        _arm("@SAREXIT", ts=ts + 0.001, cls=cls, symbol=symbol, entry=1.0,
             hold_min=hold_min if resolved else None),
    ]


def _rows(ledger):
    """Drive the real reducer — never hand-write its row shape."""
    rows = reduce_sar_signals(ledger)
    mark_stale_rows(rows)
    return rows


class TestStaleRowsAreJudgedOnLedgerProgress:
    def test_a_running_row_stamped_before_the_last_verdict_is_stale(self):
        """The resolver has demonstrably worked past this row's era and still
        left it unresolved, so "not yet" is no longer a credible reading."""
        now = 1_000_000.0
        ledger = (
            _pair(now - 6 * HOUR, symbol="STUCKUSDT", resolved=False)
            + _pair(now - 3 * HOUR, symbol="DONEUSDT", resolved=True, hold_min=30.0)
        )
        rows = _rows(ledger)
        stuck = [r for r in rows if r["symbol"] == "STUCKUSDT"]

        assert stuck, "the reducer must still emit the unresolved row"
        assert all(r["stale"] for r in stuck)

    def test_a_running_row_stamped_after_the_last_verdict_is_not_stale(self):
        """For this one "not yet" is honest — nothing has resolved past it."""
        now = 1_000_000.0
        ledger = (
            _pair(now - 6 * HOUR, symbol="DONEUSDT", resolved=True, hold_min=30.0)
            + _pair(now - 1 * HOUR, symbol="YOUNGUSDT", resolved=False)
        )
        rows = _rows(ledger)
        young = [r for r in rows if r["symbol"] == "YOUNGUSDT"]

        assert young
        assert not any(r["stale"] for r in young)

    def test_nothing_is_stale_when_the_ledger_has_never_resolved_anything(self):
        """A brand-new ledger has produced no verdict to be "passed over" by.
        Flagging these would report a freeze on an arm that just started."""
        now = 1_000_000.0
        rows = _rows(_pair(now - 2 * HOUR, symbol="NEWUSDT", resolved=False))
        assert not any(r["stale"] for r in rows)

    def test_a_resolved_row_is_never_stale(self):
        now = 1_000_000.0
        ledger = (
            _pair(now - 6 * HOUR, symbol="OLDUSDT", resolved=True, hold_min=15.0)
            + _pair(now - 2 * HOUR, symbol="NEWUSDT", resolved=True, hold_min=15.0)
        )
        rows = _rows(ledger)
        assert not any(r["stale"] for r in rows)


class TestLedgerFreshness:
    def test_a_live_ledger_reads_live(self):
        now = 1_000_000.0
        ledger = (
            _pair(now - 20 * 60, symbol="AUSDT", resolved=True, hold_min=5.0)
            + _pair(now - 10 * 60, symbol="BUSDT", resolved=False)
        )
        f = reduce_ledger_freshness(_rows(ledger), now)
        assert f["state"] == "live"

    def test_stamping_that_stopped_is_reported(self):
        """The engine can dispatch signals while this arm stamps nothing — on
        2026-07-29 it dispatched 12 during the silence."""
        now = 1_000_000.0
        ledger = _pair(now - 11.6 * HOUR, symbol="AUSDT", resolved=True, hold_min=30.0)
        f = reduce_ledger_freshness(_rows(ledger), now)

        assert f["state"] == "stalled"
        assert "stamped" in f["detail"].lower()
        assert f["stamp_age_sec"] == pytest.approx(11.6 * HOUR, rel=1e-6)

    def test_resolution_that_stopped_is_reported_even_while_stamping_continues(self):
        """The failure mode that hid for 11.6 hours: rows keep arriving, so the
        page looks busy, while no record has been given a verdict for hours."""
        now = 1_000_000.0
        ledger = (
            _pair(now - 12 * HOUR, symbol="OLDUSDT", resolved=True, hold_min=15.0)
            + _pair(now - 60, symbol="FRESHUSDT", resolved=False)
        )
        f = reduce_ledger_freshness(_rows(ledger), now)

        assert f["state"] == "stalled"
        assert "resolved" in f["detail"].lower()
        assert f["running"] >= 1

    def test_a_quiet_market_cannot_trip_the_stall_banner(self):
        """A non-empty backlog is the precondition: with nothing awaiting a
        verdict, no resolutions is the correct amount of resolutions."""
        now = 1_000_000.0
        ledger = _pair(now - 60, symbol="AUSDT", resolved=True, hold_min=15.0)
        f = reduce_ledger_freshness(_rows(ledger), now)
        assert f["state"] != "stalled"

    def test_an_empty_ledger_is_not_a_stall(self):
        f = reduce_ledger_freshness([], 1_000_000.0)
        assert f["state"] == "empty"

    def test_the_2026_07_29_shape_is_reported_as_stalled(self):
        """The owner's export, reduced to its essentials: a burst of stamping
        that stops, resolutions that stop with it, and a large running
        population left behind."""
        now = 1_000_000.0
        ledger = []
        # 10 candidates stamped across a working window, 3 of them resolved.
        for i in range(10):
            ledger += _pair(
                now - (12 + i * 0.5) * HOUR,
                symbol=f"S{i}USDT",
                resolved=i < 3,
                hold_min=30.0,
            )
        rows = _rows(ledger)
        f = reduce_ledger_freshness(rows, now)

        assert f["state"] == "stalled"
        assert f["stale_running"] > 0
        assert f["stale_running"] <= f["running"]
        # And the rows the resolver passed over are the ones marked.
        running_stale = [
            r for r in rows if r["status"] == SAR_RUNNING and r["stale"]
        ]
        assert running_stale


class TestTheABIsDeclaredUnreadableWhenNoPairResolves:
    """`delta_r` needs BOTH arms resolved. On the 2026-07-29 export not one of
    507 rows carried it — including all 106 that had every other resolve-path
    field — so the one number this arm exists to produce had nothing behind it,
    and the page simply omitted the stat rather than saying so."""

    def test_summary_reports_zero_comparable_pairs_when_the_control_never_resolves(self):
        now = 1_000_000.0
        ts = now - 2 * HOUR
        # The trail arm resolves; its paired control does not — the exact shape
        # of the 2026-07-29 export, where the @SAREXIT side carried hold_min,
        # r_multiple, pnl_pct and mfe_pct while delta_r was blank on all 507.
        ledger = [
            _arm("@SARBASE", ts=ts, cls=None, symbol="AUSDT", entry=1.0,
                 hold_min=None),
            _arm("@SAREXIT", ts=ts + 0.001, cls="WOULD_WIN", symbol="AUSDT",
                 entry=1.0, hold_min=30.0),
        ]
        rows = _rows(ledger)
        s = summarize_rows(rows)

        assert s["closed"] > 0, "the trail arm did resolve"
        assert s["avg_delta_r"] is None, (
            "a delta against an unresolved control is not a comparison"
        )
        assert s["compared"] == 0

    def test_a_fully_resolved_pair_does_produce_a_comparison(self):
        """The counterpart, so the assertion above cannot pass over dead code:
        when both arms resolve, the delta is real and must appear."""
        now = 1_000_000.0
        rows = _rows(_pair(now - 2 * HOUR, symbol="AUSDT", resolved=True,
                           hold_min=30.0))
        s = summarize_rows(rows)

        assert s["compared"] > 0
        assert s["avg_delta_r"] is not None


# ---------------------------------------------------------------------------
# The banner has to actually render — a Jinja slip here shows up in production
# ---------------------------------------------------------------------------
from fastapi.testclient import TestClient  # noqa: E402

from app.data_sources.data_volume import DataVolumeReader  # noqa: E402
from app.main import app  # noqa: E402


def _login(client):
    client.post("/login", data={"password": "test-token"})


class TestTheStallBannerRenders:
    """The stalled/STALE branches are only reached when the ledger is frozen,
    so a healthy-ledger route test never touches them."""

    def _stub(self, monkeypatch, ledger):
        monkeypatch.setattr(
            DataVolumeReader, "sar_exit_candidates", lambda self: ledger
        )

    def _frozen_ledger(self):
        import time as _t
        now = _t.time()
        ledger = []
        for i in range(6):
            ledger += _pair(
                now - (12 + i * 0.5) * HOUR,
                symbol=f"S{i}USDT",
                resolved=i < 2,
                hold_min=30.0,
            )
        return ledger

    def test_a_frozen_ledger_says_so_on_the_page(self, monkeypatch):
        self._stub(monkeypatch, self._frozen_ledger())
        with TestClient(app) as client:
            _login(client)
            r = client.get("/signals/sar")
            assert r.status_code == 200
            assert "This ledger is not running." in r.text
            assert "STALE" in r.text
            assert "last resolution" in r.text

    def test_a_live_ledger_shows_no_stall_banner(self, monkeypatch):
        """The banner must not cry wolf on a working arm — it would be ignored
        by the time it mattered."""
        import time as _t
        now = _t.time()
        self._stub(monkeypatch, (
            _pair(now - 600, symbol="AUSDT", resolved=True, hold_min=5.0)
            + _pair(now - 300, symbol="BUSDT", resolved=False)
        ))
        with TestClient(app) as client:
            _login(client)
            r = client.get("/signals/sar")
            assert r.status_code == 200
            assert "This ledger is not running." not in r.text
            assert "This ledger is falling behind." not in r.text

    def test_an_unreadable_ab_says_zero_comparable_pairs(self, monkeypatch):
        import time as _t
        ts = _t.time() - 2 * HOUR
        self._stub(monkeypatch, [
            _arm("@SARBASE", ts=ts, cls=None, symbol="AUSDT", entry=1.0,
                 hold_min=None),
            _arm("@SAREXIT", ts=ts + 0.001, cls="WOULD_WIN", symbol="AUSDT",
                 entry=1.0, hold_min=30.0),
        ])
        with TestClient(app) as client:
            _login(client)
            r = client.get("/signals/sar")
            assert r.status_code == 200
            assert "comparable pairs" in r.text
            assert "The A/B is not readable." in r.text
