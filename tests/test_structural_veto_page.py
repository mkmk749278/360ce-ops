"""`/signals/structural-veto` — Phase 4's ops surface.

The engine change is dark, so this page is where the answer is read. A dark
change with nowhere to look is an unfinished change.
"""
from __future__ import annotations

import os
from contextlib import contextmanager

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.data_sources.structural_veto import build_report  # noqa: E402


def _ledger(rows=None, **over):
    """The engine's real payload shape (src/structural_veto.VetoLedger.flush)."""
    base = {
        "schema": 1,
        "written_at": 1_700_000_000.0,
        "counters": {"evaluated": 10, "stamped": 10, "would_reject": 2},
        "retention": {
            "name": "structural_veto", "n_pending": 8, "n_delivered": 2,
            "max_pending": 4000, "max_delivered": 2000,
            "evicted_pending": 0, "evicted_delivered": 0,
            "promoted": 2, "promote_misses": 0, "duplicate_skips": 0,
        },
        "rows": rows if rows is not None else [],
    }
    base.update(over)
    return base


def _row(sid, **kw):
    base = {
        "signal_id": sid, "symbol": "BTCUSDT", "side": "LONG",
        "setup_class": "MOVER_TREND_PULLBACK", "stamped_at": 1.0,
        "entry": 100.0, "tp1": 104.0, "atr": 2.0,
        "opposing_dist_atr": 2.0, "opposing_dist_pct": 4.0,
        "opposing_inside_tp1": False, "opposing_score": 5.0,
        "opposing_age_s": 600.0, "value_area_pos": "inside",
        "refusals": [], "veto_mode": "measure", "veto_would_reject": False,
    }
    base.update(kw)
    return base


def _perf(*pairs):
    return {"records": [{"signal_id": s, "pnl_pct": p} for s, p in pairs]}


# ── the arithmetic ────────────────────────────────────────────────────────

def test_only_joined_rows_are_measured():
    """A stamped row joins only once it has DELIVERED and closed."""
    r = build_report(
        _ledger([_row("a"), _row("b"), _row("c")]), _perf(("a", 1.0), ("b", -2.0)),
    )
    assert r.n_rows == 3
    assert r.n_joined == 2


def test_the_baseline_is_the_delivered_book_average():
    r = build_report(_ledger([_row("a"), _row("b")]), _perf(("a", 2.0), ("b", -1.0)))
    assert r.baseline.avg_pnl == 0.5
    assert r.baseline.win_rate == 50.0


def test_three_buckets_never_two():
    """Folding rows whose feature never computed into `keep` is how a candidate
    rule takes credit for rows it never filtered."""
    rows = [
        _row("a", opposing_inside_tp1=True),
        _row("b", opposing_inside_tp1=False),
        _row("c", opposing_inside_tp1=None),
    ]
    r = build_report(_ledger(rows), _perf(("a", -3.0), ("b", 1.0), ("c", 5.0)))
    s = next(x for x in r.splits if x.name == "target_behind_level")
    assert s.drop.n == 1 and s.keep.n == 1
    assert s.unknown == 1
    assert s.n_scored == 2          # unknown is NOT scored


def test_delta_is_measured_against_the_rows_that_split_scored():
    """Not against the whole ledger — a summary computed on a different
    population than the row beside it is not a summary of anything."""
    rows = [
        _row("a", opposing_inside_tp1=True),
        _row("b", opposing_inside_tp1=False),
        _row("c", opposing_inside_tp1=None),
    ]
    # 'c' has a huge pnl but is unknown, so it must not move the baseline.
    r = build_report(_ledger(rows), _perf(("a", -2.0), ("b", 2.0), ("c", 99.0)))
    s = next(x for x in r.splits if x.name == "target_behind_level")
    assert s.delta == 2.0           # keep(+2) - baseline over scored rows (0)


def test_the_enforceable_rule_is_present_and_labelled():
    r = build_report(_ledger([_row("a")]), _perf(("a", 1.0)))
    names = [s.name for s in r.splits]
    assert "target_behind_level" in names
    s = next(x for x in r.splits if x.name == "target_behind_level")
    assert "no threshold" in s.detail


def test_the_cell_count_is_reported():
    """"Best of N" is not a fact about the winner until N is on screen."""
    r = build_report(_ledger([_row("a")]), _perf(("a", 1.0)))
    assert r.cells_drawn == len(r.splits) >= 6


# ── refusals ──────────────────────────────────────────────────────────────

def test_an_empty_book_and_clear_air_are_counted_separately():
    rows = [_row("a", refusals=["no_levels"]), _row("b", refusals=["no_opposing"])]
    r = build_report(_ledger(rows), _perf(("a", 1.0), ("b", 1.0)))
    assert r.refusal_counts["no_levels"] == 1
    assert r.refusal_counts["no_opposing"] == 1


def test_the_mode_is_read_off_the_rows():
    """Never mirrored from a copy of the engine's flag registry —
    MEASUREMENT_SUFFIXES drifted for a week."""
    assert build_report(_ledger([_row("a")]), _perf()).mode == "measure"
    assert build_report(
        _ledger([_row("a", veto_mode="enforce")]), _perf(),
    ).mode == "enforce"


def test_an_unreadable_ledger_is_named_not_rendered_as_empty():
    assert "unreadable" in build_report(None, _perf()).error
    assert build_report({"error": "boom"}, _perf()).error == "boom"


def test_the_setup_filter_applies_before_every_count():
    rows = [_row("a"), _row("b", setup_class="TREND_PULLBACK_EMA")]
    r = build_report(_ledger(rows), _perf(("a", 1.0), ("b", 9.0)),
                     setup_class="MOVER_TREND_PULLBACK")
    assert r.n_rows == 1
    assert r.baseline.avg_pnl == 1.0        # the other path did not leak in
    # ...but the selector still lists every path.
    assert dict(r.setups)["TREND_PULLBACK_EMA"] == 1


# ── the page ──────────────────────────────────────────────────────────────

@contextmanager
def _client(ledger, perf):
    from fastapi.testclient import TestClient
    from app.main import app

    class _DV:
        """Overrides only the two accessors this page reads.

        Delegates everything else to the real data_volume — replacing the whole
        object breaks unrelated routes that the base template touches, and a
        fixture that has to stub the world is a fixture testing the wrong seam.
        """

        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def structural_veto(self):
            return ledger

        def signal_performance(self):
            return perf

    # Enter the client as a context manager so `lifespan` runs and sets
    # `app.state.templates`; the stub data_volume goes on AFTER, because
    # lifespan installs the real one.
    with TestClient(app) as c:
        prev = app.state.data_volume
        app.state.data_volume = _DV(prev)
        c.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
        try:
            yield c
        finally:
            app.state.data_volume = prev


def test_the_page_renders_and_says_nothing_is_applied():
    with _client(_ledger([_row("a")]), _perf(("a", 1.0))) as c:
        body = c.get("/signals/structural-veto").text
    assert "Structural veto" in body
    assert "MEASURING ONLY" in body
    assert "Nothing on this page is applied" in body


def test_the_page_reads_enforcing_off_the_rows():
    with _client(_ledger([_row("a", veto_mode="enforce")]), _perf(("a", 1.0))) as c:
        body = c.get("/signals/structural-veto").text
    assert "ENFORCING" in body
    assert "MEASURING ONLY" not in body


def test_the_route_is_registered_before_signal_detail():
    """`/signals/{signal_id}` matches any literal under /signals/. A page
    included after it 404s while its route object sits in app.routes looking
    perfectly registered — the route list is not the authority."""
    src = open("app/main.py").read()
    i = src.index("app.include_router(structural_veto.router)")
    j = src.index("app.include_router(signal_detail.router)")
    assert i < j, "structural_veto must be registered before signal_detail"


def test_the_live_request_does_not_404():
    with _client(_ledger([_row("a")]), _perf(("a", 1.0))) as c:
        assert c.get("/signals/structural-veto").status_code == 200
