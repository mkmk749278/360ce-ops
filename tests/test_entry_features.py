"""The entry-feature page must not let a candidate rule flatter itself.

Owner, 2026-08-01: *"we need to know the difference as of now vs later"*. This
page answers that by putting the book as it shipped beside the book one rule
would have produced — on the same rows, immediately, rather than waiting for two
time periods to accumulate.

Everything pinned here is a way that comparison could quietly lie:

* rows the rule could not judge being folded into the side that flatters it,
* the "now" baseline drifting between rows so the comparison isn't like-for-like,
* R computed off the exit stop rather than the entry risk (#848, one page over),
* a rule that keeps everything reading as a free win.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
# conftest imports first and sets this to "test-token"; setdefault here keeps a
# standalone run of this module working without diverging from it.
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.routes.entry_features import (  # noqa: E402
    FEATURES,
    join_outcomes,
    split_by_feature,
)


def _stamp(sid, **kw):
    row = {"signal_id": sid, "setup_class": "MOVER_TREND_PULLBACK", "symbol": "BTCUSDT"}
    row.update(kw)
    return row


def _rec(sid, pnl, sl_pct=3.0, **kw):
    rec = {"signal_id": sid, "pnl_pct": pnl, "symbol": "BTCUSDT"}
    if sl_pct is not None:
        rec["sl_distance_pct_at_entry"] = sl_pct
    rec.update(kw)
    return rec


class TestJoin:
    def test_it_divides_by_the_entry_risk_not_the_exit_stop(self):
        """#848, one page over. A BE-shifted trade stopped out for -0.1%
        against a designed 3% risk is a -0.03R scratch, not a -1.00R loss."""
        joined, cov = join_outcomes([_stamp("s1")], [_rec("s1", -0.1, 3.0)])
        assert joined[0]["r"] == pytest.approx(-0.1 / 3.0)
        assert cov["scored"] == 1

    def test_a_record_with_no_entry_risk_gets_no_r_but_keeps_its_pnl(self):
        joined, cov = join_outcomes([_stamp("s1")], [_rec("s1", -3.0, None)])
        assert joined[0]["r"] is None
        assert joined[0]["pnl_pct"] == pytest.approx(-3.0)
        assert cov["scored"] == 0 and cov["joined"] == 1

    def test_a_stamp_with_no_record_is_counted_not_dropped(self):
        """A signal the router dropped, or one still open. Silently discarding
        it would report a book that is not the book."""
        joined, cov = join_outcomes(
            [_stamp("s1"), _stamp("never-delivered")], [_rec("s1", 1.0)]
        )
        assert len(joined) == 1
        assert cov["stamped_not_closed"] == 1 and cov["stamps"] == 2

    def test_junk_payloads_do_not_raise(self):
        assert join_outcomes(None, None) == ([], {
            "stamps": 0, "joined": 0, "stamped_not_closed": 0, "scored": 0})
        assert join_outcomes("nonsense", {"not": "a list"})[0] == []


class TestNowVsLater:
    def _book(self):
        stamps = [
            _stamp("a", pullback_vol_ratio=2.0),
            _stamp("b", pullback_vol_ratio=0.3),
            _stamp("c", pullback_vol_ratio=1.5),
        ]
        recs = [_rec("a", 3.0), _rec("b", -3.0), _rec("c", 6.0)]
        return join_outcomes(stamps, recs)[0]

    def test_now_is_the_whole_book_keep_is_the_subset(self):
        out = split_by_feature(self._book(), "pullback_vol_ratio", 0.8)
        assert out["now"]["n"] == 3
        assert out["keep"]["n"] == 2 and out["drop"]["n"] == 1
        assert out["now"]["avg_r"] == pytest.approx(2.0 / 3.0)
        assert out["keep"]["avg_r"] == pytest.approx(1.5)
        assert out["delta_avg_r"] == pytest.approx(1.5 - 2.0 / 3.0)

    def test_now_does_not_move_when_the_threshold_does(self):
        """The baseline is the book as it shipped. If it drifted with the
        threshold, the comparison would not be like-for-like and every Δ on the
        page would be measured against a different thing."""
        book = self._book()
        a = split_by_feature(book, "pullback_vol_ratio", 0.1)
        b = split_by_feature(book, "pullback_vol_ratio", 1.9)
        assert a["now"] == b["now"]

    def test_unjudgeable_rows_are_their_own_bucket(self):
        """The failure this prevents: binning unknowns with `keep` lets a rule
        take credit for rows it never filtered."""
        stamps = [_stamp("a", pullback_vol_ratio=2.0), _stamp("b"), _stamp("c")]
        recs = [_rec("a", 3.0), _rec("b", -3.0), _rec("c", 9.0)]
        out = split_by_feature(join_outcomes(stamps, recs)[0], "pullback_vol_ratio", 0.8)
        assert out["unknown"]["n"] == 2
        assert out["keep"]["n"] == 1
        assert out["keep"]["n"] + out["drop"]["n"] + out["unknown"]["n"] == out["now"]["n"]

    def test_direction_is_inverted_for_features_where_high_is_the_problem(self):
        """`extension_pct` keeps rows BELOW the threshold — a rule that kept the
        most extended entries would be backwards, and the arrow on screen is how
        the reader knows which way it runs."""
        assert FEATURES["extension_pct"]["keep_above"] is False
        stamps = [_stamp("a", extension_pct=2.0), _stamp("b", extension_pct=40.0)]
        recs = [_rec("a", 3.0), _rec("b", -3.0)]
        out = split_by_feature(join_outcomes(stamps, recs)[0], "extension_pct", 15.0)
        assert out["direction"] == "≤"
        assert out["keep"]["n"] == 1 and out["drop"]["n"] == 1

    def test_a_rule_that_keeps_everything_is_visible_as_such(self):
        out = split_by_feature(self._book(), "pullback_vol_ratio", 0.01)
        assert out["kept_fraction"] == pytest.approx(1.0)
        assert out["delta_avg_r"] == pytest.approx(0.0)

    def test_pnl_is_published_beside_r_on_every_split(self):
        """The R-scored population is not a random sample of the book, so a
        page that showed only R would describe a subset and not say so."""
        stamps = [_stamp("a", pullback_vol_ratio=2.0), _stamp("b", pullback_vol_ratio=2.0)]
        recs = [_rec("a", 3.0), _rec("b", -5.0, None)]   # b has no denominator
        out = split_by_feature(join_outcomes(stamps, recs)[0], "pullback_vol_ratio", 0.8)
        assert out["keep"]["n"] == 2 and out["keep"]["scored"] == 1
        assert out["keep"]["avg_r"] == pytest.approx(1.0)
        assert out["keep"]["avg_pnl_pct"] == pytest.approx(-1.0)

    def test_an_empty_book_yields_none_never_zero(self):
        out = split_by_feature([], "pullback_vol_ratio", 0.8)
        assert out["now"]["avg_r"] is None
        assert out["delta_avg_r"] is None
        assert out["kept_fraction"] is None


class TestOpsMirrorsTheEngine:
    def test_every_rendered_feature_declares_its_direction_and_question(self):
        """A threshold with no stated direction is unreadable, and a split with
        no stated question invites the reader to invent one."""
        for name, spec in FEATURES.items():
            assert isinstance(spec.get("keep_above"), bool), name
            assert spec.get("asks"), name
            assert spec.get("label"), name
            assert spec.get("default") is not None, name

    def test_the_keep_above_set_matches_the_engines(self):
        """Ops ports the engine's math; it does not invent it. These two
        drifting would put the arrow on screen at odds with the split beneath
        it — the mirror problem this repo has already paid for once."""
        engine_keep_above = {"pullback_vol_ratio", "level_dist_r", "cvd_slope"}
        ops_keep_above = {k for k, v in FEATURES.items() if v["keep_above"]}
        assert engine_keep_above <= ops_keep_above, (
            "engine _KEEP_ABOVE has a feature ops filters the other way"
        )


class TestRoute:
    def _client(self, monkeypatch, stamps, records):
        from fastapi.testclient import TestClient

        from app.main import app

        class _DV:
            def entry_features(self):
                return {"schema": 1, "written_at": 1.0, "rows": stamps}

            def signal_performance(self):
                return records

        app.state.data_volume = _DV()
        return TestClient(app)

    def _login(self, client):
        client.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})

    def test_the_page_renders_and_says_nothing_is_applied(self, monkeypatch):
        client = self._client(
            monkeypatch,
            [_stamp("a", pullback_vol_ratio=2.0)],
            [_rec("a", 3.0)],
        )
        with client:
            self._login(client)
            r = client.get("/signals/entry-features")
            assert r.status_code == 200
            assert "Nothing on this page is applied" in r.text

    def test_an_empty_lane_says_quiet_not_broken(self, monkeypatch):
        """The dark page reported a fault that was not happening on exactly this
        shape. Empty must read as quiet, with the check to distinguish them."""
        client = self._client(monkeypatch, [], [])
        with client:
            self._login(client)
            r = client.get("/signals/entry-features")
            assert r.status_code == 200
            assert "quiet" in r.text.lower()

    def test_a_broken_data_volume_degrades_rather_than_500s(self, monkeypatch):
        from fastapi.testclient import TestClient

        from app.main import app

        class _Boom:
            def entry_features(self):
                raise RuntimeError("volume unmounted")

            def signal_performance(self):
                return []

        app.state.data_volume = _Boom()
        client = TestClient(app)
        with client:
            self._login(client)
            assert client.get("/signals/entry-features").status_code == 200


class TestRouteOrdering:
    """``signal_detail`` registers ``/signals/{signal_id}``, which matches any
    ``/signals/<literal>`` path. Every literal page under ``/signals/`` must be
    included BEFORE it or the catch-all swallows the request into a 404 — this
    page was registered after it on the first cut and 404'd while its own route
    object sat in ``app.routes`` looking perfectly registered.
    """

    def test_the_page_is_reachable_and_not_shadowed_by_the_catch_all(self):
        from fastapi.testclient import TestClient

        from app.main import app

        class _DV:
            def entry_features(self):
                return {"schema": 1, "written_at": 1.0, "rows": []}

            def signal_performance(self):
                return []

        app.state.data_volume = _DV()
        with TestClient(app) as client:
            client.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
            assert client.get("/signals/entry-features").status_code == 200

    def test_it_is_included_before_the_catch_all(self):
        """Asserted on the router order itself, so the reason survives even if
        the request test above is later changed."""
        import app.main as main

        src = (main.__file__ or "")
        text = open(src, encoding="utf-8").read()
        i_page = text.index("app.include_router(entry_features.router)")
        i_catch = text.index("app.include_router(signal_detail.router)")
        assert i_page < i_catch, (
            "entry_features must be registered before signal_detail, whose "
            "/signals/{signal_id} route matches any /signals/<literal>"
        )
