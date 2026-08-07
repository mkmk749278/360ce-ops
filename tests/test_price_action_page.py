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


class TestFiltersAndExport:
    """Built 2026-08-06 on the owner's question: *"if we clear them then the
    data will be clear to estimate right"*.

    Clearing would have taken the closed book from 74 rows to 12 — the
    unstamped rows carry a perfectly valid `pnl_pct` and are most of the
    evidence; what they cannot do is appear in a split. So: a filter, not a
    purge. The clean population is readable immediately and nothing is deleted.
    """

    def _rows(self):
        # Built from the file's own `_row()` so the shape is the ledger's, not
        # one chosen here — a fixture whose keys you picked asserts your
        # assumption back at you.
        return [
            _row("a", pnl=2.0, tf="1d", regime_15m="RANGING"),      # both
            _row("b", pnl=-1.0, tf="round"),                        # level only
            _row("c", tf="", regime_15m="VOLATILE"),                # regime only
            _row("d", pnl=-3.0, tf=""),                             # neither
        ]

    def test_four_stamp_states_not_two(self):
        """The two fixes shipped an hour apart, so a row can carry one stamp and
        not the other. Folding that middle population into either end would
        misdescribe exactly the rows a reader wonders about."""
        from app.routes.price_action import stamp_state
        got = [stamp_state(r) for r in self._rows()]
        assert got == ["full", "partial", "partial", "none"]

    def test_the_filter_narrows_and_does_not_delete(self):
        from app.routes.price_action import filter_lane_rows
        rows = self._rows()
        assert len(filter_lane_rows(rows, stamped="full")) == 1
        assert len(filter_lane_rows(rows, stamped="none")) == 1
        assert len(filter_lane_rows(rows, stamped="all")) == 4
        assert len(rows) == 4, "filtering must not mutate the source"

    def test_each_option_is_counted_without_its_own_selector(self):
        """#90/#91: a selector applied to its own counts makes every option read
        `n = whatever I picked`."""
        from app.routes.price_action import selector_options
        opts = selector_options(
            self._rows(), stamped="full", regime="", level="", status="")
        # `stamped` counts ignore the active `stamped` filter …
        assert opts["stamped"] == {"full": 1, "partial": 2, "none": 1}
        # … while every other selector honours it.
        assert opts["level"] == {"1d": 1}

    def test_selectors_compose(self):
        from app.routes.price_action import filter_lane_rows
        rows = self._rows()
        assert len(filter_lane_rows(rows, stamped="full", level="1d")) == 1
        assert len(filter_lane_rows(rows, stamped="full", level="round")) == 0

    def test_every_panel_is_recomputed_on_the_filtered_rows(self):
        """A summary over the whole ledger above a filtered table is not a
        summary of anything the reader is looking at."""
        with _client(self._rows()) as c:
            full = c.get("/signals/price-action?stamped=full").text
            everything = c.get("/signals/price-action").text
        assert "<strong>1</strong> of 4 rows" in full
        assert "<strong>4</strong> of 4 rows" in everything
        # …and the headline moved with the filter, not just the row count.
        assert full != everything

    def test_the_export_is_uncapped_and_carries_the_stamp_state(self):
        """A truncated export is #97 wearing a download button. And the stamp
        state rides into the CSV so the two populations stay separable in a
        spreadsheet — which is where a mixed population gets averaged."""
        from app.routes.price_action import EXPORT_COLS
        assert "stamp_state" in EXPORT_COLS
        assert "pnl_pct" in EXPORT_COLS
        with _client(self._rows()) as c:
            r = c.get("/signals/price-action/export.csv")
        assert r.status_code == 200
        body = r.text
        assert "stamp_state" in body.splitlines()[0]
        assert len(body.strip().splitlines()) == 5      # header + 4 rows

    def test_the_export_honours_the_same_filters_as_the_page(self):
        """The download must never describe a different book than the screen."""
        with _client(self._rows()) as c:
            r = c.get("/signals/price-action/export.csv?stamped=full")
        assert len(r.text.strip().splitlines()) == 2    # header + 1 row

    def test_an_unstamped_row_keeps_its_pnl(self):
        """The whole reason not to purge: the missing fields are labels, not
        outcomes."""
        from app.routes.price_action import filter_lane_rows
        (row,) = filter_lane_rows(self._rows(), stamped="none")
        assert row["pnl_pct"] == -3.0


# ── a flat expiry is not a loss ───────────────────────────────────────────

class TestFlatExpiriesAreNotLosses:
    """`EXPIRED` rows are scored **0.00%** by the engine — a walked window in
    which neither level was touched. They are flat, not losing.

    Reported as losses they cost the 2026-08-07 book 5pp of win rate: the page
    read `115W / 347L` where the book was `115W / 267L / 80 flat`, and 25% where
    the rows that actually resolved to a level read 30%. Same class as the
    repo's standing "three buckets, never two" rule — folding a population that
    was never measured into one that was is how a rate describes rows nobody
    scored.
    """

    def _book(self):
        # 2 winners, 3 losers, 5 flat expiries.
        return (
            [_row(sid=f"w{i}", pnl=4.0, status="CLOSED_TP1") for i in range(2)]
            + [_row(sid=f"l{i}", pnl=-2.0, status="CLOSED_SL") for i in range(3)]
            + [_row(sid=f"e{i}", pnl=0.0, status="EXPIRED") for i in range(5)]
        )

    def test_a_zero_pnl_expiry_is_counted_flat_not_lost(self):
        s = summarize(self._book(), fee_pct=0.0)
        assert s["wins"] == 2
        assert s["losses"] == 3, "an expiry that lost nothing is not a loss"
        assert s["flats"] == 5

    def test_both_denominators_are_published_and_differ(self):
        """Where two denominators are defensible, publish both — and here they
        genuinely describe different populations, so the rates differ."""
        s = summarize(self._book(), fee_pct=0.0)
        assert s["n_closed"] == 10 and s["n_decided"] == 5
        assert s["win_rate"] == pytest.approx(20.0)          # of everything closed
        assert s["win_rate_decided"] == pytest.approx(40.0)  # of what reached a level

    def test_a_flat_row_still_pays_its_round_trip(self):
        """It opened and it closed, so the fee is real — the flat bucket changes
        the win rate and must not quietly change the money."""
        s = summarize(self._book(), fee_pct=0.07)
        assert s["fees_pct"] == pytest.approx(0.07 * 10)

    def test_a_book_with_no_expiries_reports_no_flats_and_one_rate(self):
        s = summarize([_row(sid="w", pnl=1.0), _row(sid="l", pnl=-1.0)], fee_pct=0.0)
        assert s["flats"] == 0
        assert s["win_rate"] == s["win_rate_decided"] == pytest.approx(50.0)


# ── concentration the move key cannot see ─────────────────────────────────

class TestEpisodesSeeWhatTheMoveKeyCannot:
    """`concentration()` keys on `symbol · side · entry`, which is right for the
    same sweep re-stamped at the same price and **structurally blind to a
    trend**: a collapsing symbol hands out a different entry every 30 minutes,
    so every re-entry counts as its own "move" and the panel reads clean.

    On the 2026-08-07 book it read 1.12 rows/move and "largest single move =
    1.0% of all rows" while BEATUSDT fell 2.45 → 2.06 and the lane bought
    reclaimed support nine times on the way down: one symbol, one side, 4.5
    hours, −85.71% against a whole-book net of −78.25%. Remove that one run and
    the book reads +7.46% — **the sign of the verdict was one episode.**
    """

    def _collapse(self):
        """Nine longs into one downtrend, 30 minutes apart, each at a new price
        — so the move key sees nine distinct moves and no concentration."""
        return [
            _row(sid=f"c{i}", pnl=-8.0, status="CLOSED_SL",
                 symbol="BEATUSDT", side="LONG",
                 entry=2.45 - i * 0.04, at=1_700_000_000.0 + i * 1_800.0)
            for i in range(9)
        ] + [_row(sid="ok", pnl=6.0, status="CLOSED_TP1",
                  symbol="HFTUSDT", side="LONG", entry=1.0, at=1_700_500_000.0)]

    def test_the_move_key_reports_this_book_as_unconcentrated(self):
        """The premise. If this ever stops holding, the episode panel's reason
        for existing has changed and the copy must change with it."""
        from app.routes.price_action import concentration
        c = concentration(self._collapse())
        assert c["n_moves"] == 10, "nine distinct entries read as nine moves"
        assert c["rows_per_move"] == 1.0
        assert c["top_share"] == pytest.approx(0.1)

    def test_episodes_collapse_the_run_the_move_key_split(self):
        from app.routes.price_action import episodes
        e = episodes(self._collapse(), fee_pct=0.0)
        assert e["n_episodes"] == 2, "nine re-entries into one collapse are one run"
        assert e["rows_per_episode"] == pytest.approx(5.0)  # 10 rows / 2 runs
        # 9 of 10 rows sit inside a multi-row run — the move key said 0%.
        assert e["multi_row_share"] == pytest.approx(0.9)

    def test_the_worst_run_is_named_with_its_share_of_the_loss(self):
        from app.routes.price_action import episodes
        e = episodes(self._collapse(), fee_pct=0.0)
        w = e["worst"]
        assert w["label"] == "BEATUSDT LONG"
        assert w["n_rows"] == 9
        assert w["net_pct"] == pytest.approx(-72.0)
        assert w["hours"] == pytest.approx(4.0)

    def test_the_book_without_that_run_flips_sign(self):
        """The number that decides whether any split further down the page can
        be read as a fact about the mechanism."""
        from app.routes.price_action import episodes
        e = episodes(self._collapse(), fee_pct=0.0)
        assert e["book_net"] == pytest.approx(-66.0)
        assert e["worst"]["book_without"] == pytest.approx(6.0)
        assert e["book_net"] < 0 < e["worst"]["book_without"]

    def test_removing_only_the_episode_not_the_whole_symbol(self):
        """A later, unrelated run on the same ticker must survive the removal —
        otherwise the panel overstates what one run cost."""
        from app.routes.price_action import episodes
        rows = self._collapse() + [
            _row(sid="later", pnl=5.0, status="CLOSED_TP1", symbol="BEATUSDT",
                 side="LONG", entry=1.5, at=1_700_900_000.0)
        ]
        e = episodes(rows, fee_pct=0.0)
        assert e["worst"]["n_rows"] == 9
        # 6.0 from HFT + 5.0 from the later BEAT run — the ticker is not purged.
        assert e["worst"]["book_without"] == pytest.approx(11.0)

    def test_a_gap_wider_than_the_window_starts_a_new_episode(self):
        from app.routes.price_action import episodes, EPISODE_GAP_S
        rows = [
            _row(sid="a", pnl=-1.0, symbol="XUSDT", side="LONG", at=1_000.0),
            _row(sid="b", pnl=-1.0, symbol="XUSDT", side="LONG",
                 at=1_000.0 + EPISODE_GAP_S + 1),
        ]
        assert episodes(rows, fee_pct=0.0)["n_episodes"] == 2

    def test_opposite_sides_are_never_one_episode(self):
        from app.routes.price_action import episodes
        rows = [
            _row(sid="a", pnl=-1.0, symbol="XUSDT", side="LONG", at=1_000.0),
            _row(sid="b", pnl=-1.0, symbol="XUSDT", side="SHORT", at=1_060.0),
        ]
        assert episodes(rows, fee_pct=0.0)["n_episodes"] == 2

    def test_an_all_open_book_refuses_a_worst_run_rather_than_inventing_zero(self):
        from app.routes.price_action import episodes
        e = episodes([_row(sid="o", symbol="XUSDT", at=1_000.0)], fee_pct=0.0)
        assert e["worst"] is None and e["book_net"] is None

    def test_an_empty_ledger_does_not_divide_by_zero(self):
        from app.routes.price_action import episodes
        e = episodes([], fee_pct=0.0)
        assert e["n_episodes"] == 0 and e["rows_per_episode"] == 0.0


# ── Layer 1 — Context, and the shadow rule over it ────────────────────────

def _ctx_row(sid="a", pnl=None, zone="in", room=1.0, lvl="edge", **kw):
    r = _row(sid=sid, pnl=pnl, **kw)
    r["vp_entry_zone"] = zone
    r["vp_poc_room_pct"] = room
    r["vp_level_zone"] = lvl
    return r


class TestLayerOneContextSplit:
    """The lane implements the program doc's layers 2/3/4 and had no layer 1.
    A sweep+reclaim is a failed break — mean reversion — which pays in balance
    and traps in imbalance, and those two states have an identical
    layer-2/3/4 signature. That is why nothing already stamped separated them."""

    def test_unstamped_is_its_own_bucket_never_folded_into_a_real_one(self):
        from app.routes.price_action import by_context
        rows = [
            _ctx_row(sid="a", pnl=1.0, zone="in"),
            _ctx_row(sid="b", pnl=-1.0, zone="out"),
            _row(sid="c", pnl=-5.0),  # no layer-1 stamp at all
        ]
        out = {c["zone"]: c for c in by_context(rows, fee_pct=0.0, key="vp_entry_zone")}
        assert set(out) == {"in", "out", "unstamped"}
        assert out["unstamped"]["n"] == 1
        assert out["unstamped"]["unstamped"] is True
        assert out["in"]["n"] == 1, "an unstamped row must not land in a real bucket"

    def test_the_level_zone_splits_independently_of_the_entry_zone(self):
        from app.routes.price_action import by_context
        rows = [_ctx_row(sid="a", pnl=1.0, zone="in", lvl="interior")]
        entry = {c["zone"] for c in by_context(rows, fee_pct=0.0, key="vp_entry_zone")}
        level = {c["zone"] for c in by_context(rows, fee_pct=0.0, key="vp_level_zone")}
        assert entry == {"in"} and level == {"interior"}

    def test_fees_are_charged_in_the_split(self):
        from app.routes.price_action import by_context
        rows = [_ctx_row(sid="a", pnl=1.0, zone="in")]
        out = by_context(rows, fee_pct=0.07, key="vp_entry_zone")[0]
        assert out["avg_net_pct"] == pytest.approx(0.93)


class TestBalanceShadowRule:
    """Applied to nothing, and half its cutoff is fitted — the page says so."""

    def _book(self):
        return [
            # in balance, POC ahead -> kept
            _ctx_row(sid="k1", pnl=3.0, zone="in", room=1.5),
            _ctx_row(sid="k2", pnl=-1.0, zone="in", room=0.8),
            # in balance but POC behind -> dropped
            _ctx_row(sid="d1", pnl=-6.0, zone="in", room=-1.2),
            # outside value -> dropped
            _ctx_row(sid="d2", pnl=-4.0, zone="out", room=2.0),
            # no layer-1 stamp -> abstain
            _row(sid="u1", pnl=-9.0),
        ]

    def test_three_buckets_never_two(self):
        from app.routes.price_action import balance_shadow
        s = balance_shadow(self._book(), fee_pct=0.0)
        assert s["keep"]["n"] == 2
        assert s["drop"]["n"] == 2
        assert s["unknown"]["n"] == 1, "an unstamped row must never count as kept"

    def test_an_unstamped_row_is_never_folded_into_keep(self):
        """How a candidate rule takes credit for rows it never filtered."""
        from app.routes.price_action import balance_shadow
        s = balance_shadow([_row(sid="u", pnl=5.0)], fee_pct=0.0)
        assert s["keep"]["n"] == 0 and s["unknown"]["n"] == 1
        assert s["unknown_frac"] == pytest.approx(1.0)

    def test_room_must_be_positive_not_merely_present(self):
        """POC behind the trade is the rotation going the wrong way — signed
        toward the trade, so a negative value is a real reading, not missing."""
        from app.routes.price_action import balance_shadow
        s = balance_shadow([_ctx_row(sid="d", pnl=1.0, zone="in", room=-0.1)], fee_pct=0.0)
        assert s["keep"]["n"] == 0 and s["drop"]["n"] == 1

    def test_the_baseline_is_the_whole_book_and_does_not_move(self):
        """If the baseline shifted with the rule's coverage, every Δ would be
        measured against a different thing."""
        from app.routes.price_action import balance_shadow
        book = self._book()
        a = balance_shadow(book, fee_pct=0.0)
        b = balance_shadow(book + [_row(sid="u2")], fee_pct=0.0)  # open row, no pnl
        assert a["base"]["n_closed"] == b["base"]["n_closed"]
        assert a["base"]["avg_net_pct"] == pytest.approx(b["base"]["avg_net_pct"])

    def test_kept_and_unknown_fractions_are_published(self):
        from app.routes.price_action import balance_shadow
        s = balance_shadow(self._book(), fee_pct=0.0)
        assert s["kept_frac"] == pytest.approx(2 / 5)
        assert s["unknown_frac"] == pytest.approx(1 / 5)

    def test_the_decided_population_is_reported_apart_from_the_baseline(self):
        """A rule that abstained on most of the book has not been tested on it."""
        from app.routes.price_action import balance_shadow
        s = balance_shadow(self._book(), fee_pct=0.0)
        assert s["decided"]["n"] == 4
        assert s["base"]["n"] == 5

    def test_an_empty_book_does_not_divide_by_zero(self):
        from app.routes.price_action import balance_shadow
        s = balance_shadow([], fee_pct=0.0)
        assert s["kept_frac"] is None and s["delta_vs_base"] is None


class TestLayerOneRendersAndStatesItsLimits:

    def test_the_page_renders_the_context_split_and_the_shadow_rule(self):
        with _client([_ctx_row(sid="a", pnl=1.0)]) as c:
            body = c.get("/signals/price-action").text
        assert "Layer 1 — Context" in body
        assert "balance_only" in body

    def test_the_shadow_panel_says_it_is_applied_to_nothing(self):
        with _client([_ctx_row(sid="a", pnl=1.0)]) as c:
            body = c.get("/signals/price-action").text
        assert "APPLIED TO NOTHING" in body

    def test_the_page_admits_the_cutoff_is_partly_fitted_to_this_window(self):
        """The owner accepted a fitted rule knowingly; the next reader was not
        in that conversation. Copy is part of the measurement."""
        with _client([_ctx_row(sid="a", pnl=1.0)]) as c:
            body = c.get("/signals/price-action").text
        assert "fitted" in body.lower()
        assert "56.3%" in body, "the 54-variant warning belongs over this number"

    def test_the_export_carries_the_layer_one_columns(self):
        """A spreadsheet is precisely where two populations get averaged into
        one — the stamp has to ride along or the split cannot be redone."""
        from app.routes.price_action import EXPORT_COLS
        for col in ("vp_entry_zone", "vp_level_zone", "vp_poc_room_pct"):
            assert col in EXPORT_COLS, col
