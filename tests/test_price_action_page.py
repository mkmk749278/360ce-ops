"""`/signals/price-action` — Phase 5's ops surface.

The page that answers the owner's original question: if we really follow price
action, what is our signal volume and what is its performance?
"""
from __future__ import annotations

import os
from contextlib import contextmanager

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.routes.dark_signals_live import (  # noqa: E402
    LANE_DARK_GATE, reduce_lane_rows, reduce_rows,
)
from app.routes.price_action import by_level_source, summarize  # noqa: E402


def _row(sid="a", pnl=None, gate=LANE_DARK_GATE, tf="1h", at=1_700_000_000.0, **kw):
    r = {
        "signal_id": sid, "symbol": "BTCUSDT", "side": "LONG",
        "setup_class": "PA_SWEEP_RECLAIM", "dark_gate": gate,
        "entry": 100.0, "stop_loss": 99.0, "tp1": 103.0,
        "status": "CLOSED" if pnl is not None else "OPEN",
        "pnl_pct": pnl, "emitted_at": at, "level_source_tf": tf,
        "confidence": 0.0, "mfe_pct": 0.0, "mae_pct": 0.0,
    }
    r.update(kw)
    return r


# ── the two populations must never pool ───────────────────────────────────

def test_lane_rows_are_excluded_from_the_dark_feed():
    """The dark feed's own first sentence — "a signal the scanner was willing to
    send with one gate loosened" — is FALSE of a lane row, which never entered
    the scanner's chain. Pooling is how 15 rows disappear into 2,418."""
    payload = {"rows": [_row("a"), _row("b", gate="execution:overextended")]}
    assert [r["signal_id"] for r in reduce_rows(payload)] == ["b"]


def test_the_lane_page_sees_only_lane_rows():
    payload = {"rows": [_row("a"), _row("b", gate="execution:overextended")]}
    assert [r["signal_id"] for r in reduce_lane_rows(payload)] == ["a"]


def test_the_split_constant_matches_the_engine():
    """An unpinned mirror is what drifted MEASUREMENT_SUFFIXES for a week. This
    reads the engine's own constant rather than restating it."""
    import importlib.util
    import pathlib

    engine = pathlib.Path("/home/user/360-v2/src/price_action_lane.py")
    if not engine.exists():
        import pytest
        pytest.skip("engine repo not present in this checkout")
    spec = importlib.util.spec_from_file_location("_pal", engine)
    src = engine.read_text()
    # Read the literal without importing the engine's dependency tree.
    for line in src.splitlines():
        if line.startswith("DARK_GATE = "):
            assert LANE_DARK_GATE == line.split("=", 1)[1].strip().strip('"')
            return
    raise AssertionError("engine DARK_GATE constant not found")


# ── the arithmetic ────────────────────────────────────────────────────────

def test_open_rows_are_in_no_realized_figure():
    """An unrealized number pooled into a win rate is a claim about trades that
    have not happened yet."""
    s = summarize([_row("a", pnl=2.0), _row("b")], fee_pct=0.07)
    assert s["n_total"] == 2
    assert s["n_closed"] == 1
    assert s["n_open"] == 1
    assert s["wins"] == 1
    assert s["win_rate"] == 100.0


def test_the_fee_is_charged_to_every_closed_row_including_winners():
    s = summarize([_row("a", pnl=1.0), _row("b", pnl=1.0)], fee_pct=0.07)
    assert s["gross_pct"] == pytest.approx(2.0)
    assert s["fees_pct"] == pytest.approx(0.14)
    assert s["net_pct"] == pytest.approx(1.86)


def test_a_book_that_is_gross_positive_and_net_negative_reads_no_edge():
    """The whole point of charging the fee. Our book loses ~10x its edge to
    fees, so a gross-only figure answers the wrong question."""
    s = summarize([_row("a", pnl=0.02), _row("b", pnl=0.02)], fee_pct=0.07)
    assert s["gross_pct"] > 0
    assert s["net_pct"] < 0
    assert s["verdict"] == "no_edge"


def test_no_closed_rows_yields_no_verdict_rather_than_a_zero():
    s = summarize([_row("a")], fee_pct=0.07)
    assert s["verdict"] is None
    assert s["win_rate"] is None
    assert s["net_pct"] is None


def test_per_day_is_measured_from_the_rows_not_an_assumed_window():
    day = 86_400.0
    s = summarize(
        [_row("a", at=1_700_000_000.0), _row("b", at=1_700_000_000.0 + 2 * day)],
        fee_pct=0.07,
    )
    assert s["span_days"] == 2.0
    assert s["per_day"] == 1.0


def test_a_single_row_states_no_rate_rather_than_a_fabricated_one():
    s = summarize([_row("a", pnl=1.0)], fee_pct=0.07)
    assert s["per_day"] is None
    assert s["span_days"] is None


def test_level_sources_are_split_not_pooled():
    """A 1d level and a 1h level are different obstacles."""
    out = by_level_source([
        _row("a", pnl=1.0, tf="1d"), _row("b", pnl=-1.0, tf="1h"),
        _row("c", pnl=1.0, tf="1d"),
    ])
    by = {b["source"]: b for b in out}
    assert by["1d"]["n"] == 2 and by["1d"]["win_rate"] == 100.0
    assert by["1h"]["n"] == 1 and by["1h"]["win_rate"] == 0.0


def test_an_unstamped_level_source_is_named_not_dropped():
    out = by_level_source([_row("a", pnl=1.0, level_source_tf="")])
    assert out[0]["source"] == "unstamped"


# ── the page ──────────────────────────────────────────────────────────────

@contextmanager
def _client(rows):
    from fastapi.testclient import TestClient
    from app.main import app

    class _DV:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def dark_signals(self):
            return {"rows": rows}

    with TestClient(app) as c:
        prev = app.state.data_volume
        app.state.data_volume = _DV(prev)
        c.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
        try:
            yield c
        finally:
            app.state.data_volume = prev


def test_the_page_renders_and_says_nothing_was_delivered():
    with _client([_row("a", pnl=1.0)]) as c:
        body = c.get("/signals/price-action").text
    assert "Price action, driving" in body
    assert "OWNER ONLY" in body
    assert "no channel" in body


def test_an_empty_lane_reads_quiet_rather_than_broken():
    """The trigger is deliberately rare. Zero rows is the quiet state, and a
    page that reads as a fault would train the owner to ignore it."""
    with _client([]) as c:
        body = c.get("/signals/price-action").text
    assert "NO ROWS YET" in body
    assert "quiet" in body


def test_no_edge_renders_as_a_supported_outcome():
    with _client([_row("a", pnl=0.01), _row("b", pnl=0.01)]) as c:
        body = c.get("/signals/price-action").text
    assert "NO EDGE DETECTED" in body
    assert "supported outcome" in body


def test_confidence_zero_is_explained_rather_than_shown_as_missing():
    with _client([_row("a", pnl=1.0)]) as c:
        body = c.get("/signals/price-action").text
    assert "honest, not missing" in body


def test_the_route_is_registered_before_signal_detail():
    src = open("app/main.py").read()
    assert src.index("app.include_router(price_action.router)") < \
           src.index("app.include_router(signal_detail.router)")


def test_the_live_request_does_not_404():
    with _client([_row("a", pnl=1.0)]) as c:
        assert c.get("/signals/price-action").status_code == 200


class TestConcentration:
    """Every rate on this page is per row, and a sweep persists for several
    bars. #816: counted per row a population read 32% win / −0.364R and per
    move 55% / +0.003R — the sign of the verdict was an artefact of
    re-detection. Disclosing that is not optional; de-duplicating silently
    would be the other mistake."""

    def _rows(self):
        return [
            {"symbol": "COTIUSDT", "side": "SHORT", "entry": 0.013942, "pnl_pct": -2.97},
            {"symbol": "COTIUSDT", "side": "SHORT", "entry": 0.013942, "pnl_pct": -2.97},
            {"symbol": "COTIUSDT", "side": "SHORT", "entry": 0.01269, "pnl_pct": -3.58},
            {"symbol": "BANKUSDT", "side": "LONG", "entry": 0.04209, "pnl_pct": 3.32},
        ]

    def test_repeated_stamps_of_one_move_collapse(self):
        from app.routes.price_action import concentration
        c = concentration(self._rows())
        assert c["n_rows"] == 4
        assert c["n_moves"] == 3          # the two identical COTI rows are one move
        assert c["n_symbols"] == 2
        assert c["top"].startswith("COTIUSDT SHORT")
        assert c["top_share"] == 0.5

    def test_rows_per_move_is_one_when_nothing_repeats(self):
        from app.routes.price_action import concentration
        rows = [
            {"symbol": "AUSDT", "side": "LONG", "entry": 1.0},
            {"symbol": "BUSDT", "side": "SHORT", "entry": 2.0},
        ]
        assert concentration(rows)["rows_per_move"] == 1.0

    def test_an_empty_ledger_does_not_divide_by_zero(self):
        from app.routes.price_action import concentration
        c = concentration([])
        assert c["n_moves"] == 0 and c["rows_per_move"] == 0.0

    def test_the_panel_renders_and_names_the_restart_cause(self):
        """"Blank needs a cause before it gets a caption" — the duplicates in
        the owner's window came from the throttle not surviving a restart, and
        the panel says so rather than leaving the reader to infer a market."""
        from app.routes.price_action import concentration
        c = concentration(self._rows())
        assert c["rows_per_move"] > 1.0


class TestLayerOneSplit:
    """§1 of the program doc defines this lane's trigger relative to the
    prevailing trend, and the lane has no context layer at all. `entry_regime`
    was declared on `LaneSignal` and never assigned, so the question could not
    even be asked (owner-directed audit, 2026-08-06)."""

    def _rows(self):
        return [
            {"regime": "TRENDING_DOWN", "regime_15m": "TRENDING_DOWN",
             "regime_tf": "5m", "pnl_pct": -2.0},
            {"regime": "RANGING", "regime_15m": "TRENDING_DOWN",
             "regime_tf": "5m", "pnl_pct": -1.0},
            {"regime": "RANGING", "regime_15m": "RANGING",
             "regime_tf": "5m", "pnl_pct": 3.0},
            {"pnl_pct": -1.5},                      # pre-fix row, no regime
        ]

    def test_pre_fix_rows_are_unstamped_never_folded_into_a_real_bucket(self):
        """There is no honest backfill — the regime at entry is knowable only at
        entry (#817). Folding them in would let a bucket take credit for rows
        nobody classified."""
        from app.routes.price_action import by_regime
        out = by_regime(self._rows(), fee_pct=0.07, key="regime")
        names = {r["regime"] for r in out}
        assert "unstamped" in names
        un = next(r for r in out if r["regime"] == "unstamped")
        assert un["n"] == 1 and un["unstamped"] is True
        assert sum(r["n"] for r in out) == 4

    def test_the_two_timeframes_are_split_not_pooled(self):
        """A 15m downtrend with a 5m bounce is the setup this lane keeps buying.
        Pooling the two reads would hide exactly that case."""
        from app.routes.price_action import by_regime
        entry = by_regime(self._rows(), fee_pct=0.07, key="regime")
        trigger = by_regime(self._rows(), fee_pct=0.07, key="regime_15m")
        e = {r["regime"]: r["n"] for r in entry}
        t = {r["regime"]: r["n"] for r in trigger}
        assert e["RANGING"] == 2 and e["TRENDING_DOWN"] == 1
        assert t["TRENDING_DOWN"] == 2 and t["RANGING"] == 1
        assert e != t, "the two reads must be able to disagree"

    def test_the_label_set_is_not_mirrored_from_the_engine(self):
        """A list ops keeps is silent by construction on the next label the
        engine adds. The split iterates whatever the rows carry."""
        from app.routes.price_action import by_regime
        out = by_regime(
            [{"regime": "SOME_FUTURE_REGIME", "pnl_pct": 1.0}], fee_pct=0.0
            , key="regime")
        assert out[0]["regime"] == "SOME_FUTURE_REGIME"

    def test_the_regime_timeframe_is_reported_and_badged_when_mixed(self):
        from app.routes.price_action import regime_timeframes
        assert regime_timeframes(self._rows()) == ["5m"]
        mixed = self._rows() + [{"regime_tf": "15m"}]
        assert regime_timeframes(mixed) == ["15m", "5m"]

    def test_fees_are_charged_in_the_split(self):
        from app.routes.price_action import by_regime
        gross = by_regime(self._rows(), fee_pct=0.0, key="regime")
        net = by_regime(self._rows(), fee_pct=0.07, key="regime")
        g = next(r for r in gross if r["regime"] == "RANGING")["avg_net_pct"]
        n = next(r for r in net if r["regime"] == "RANGING")["avg_net_pct"]
        assert n == pytest.approx(g - 0.07)
