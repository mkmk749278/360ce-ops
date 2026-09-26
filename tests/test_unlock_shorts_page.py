"""`/signals/unlock-shorts` — the unlock-short dark lane's surface.

The fixture ``fixtures_unlock_shorts.json`` is the ENGINE'S OWN output: it is
printed by 360-v2 ``scripts/gen_ops_unlock_shorts_fixture.py``, which drives
the real ``src/unlock_shorts.step`` through a full lifecycle. A hand-written
fixture chooses a shape and then agrees with you about it; this one cannot.
Regenerate it whenever the engine's ledger shape changes.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import subprocess
import sys
from contextlib import contextmanager

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.data_sources import unlock_shorts as us  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
FIXTURE = json.loads((HERE / "fixtures_unlock_shorts.json").read_text())
NOW = FIXTURE["written_at"] + 60


def _fx(**over):
    d = copy.deepcopy(FIXTURE)
    d.update(over)
    return d


def _row(led, prefix):
    return next(r for r in led["rows"] if r["row_id"].startswith(prefix))


# ── lane state, graded on the engine's clock ─────────────────────────────

def test_the_engines_output_reads_live():
    r = us.build_report(_fx(), now=NOW)
    assert r.state == "live" and r.calendar_state == "ok"


def test_five_lane_states_never_pooled():
    assert us.build_report({"error": "missing: /engine-data/unlock_shorts_v1.json"}, now=NOW).state == "missing"
    assert us.build_report({"error": "parse: Expecting value"}, now=NOW).state == "unreadable"
    assert us.build_report(_fx(enabled=False), now=NOW).state == "off"
    assert us.build_report(_fx(), now=NOW + us.LANE_STALE_SEC + 60).state == "stale"
    assert us.build_report(["not", "a", "dict"], now=NOW).state == "unreadable"


def test_a_missing_file_names_its_cause_not_a_quiet_lane():
    r = us.build_report({"error": "missing: x"}, now=NOW)
    assert "predates the lane" in r.state_detail


def test_calendar_states():
    fx = _fx()
    fx["calendar"] = {"last_attempt_at": NOW - 10, "last_error": "HTTPError: 503"}
    assert us.build_report(fx, now=NOW).calendar_state == "failing"
    fx["calendar"] = {}
    assert us.build_report(fx, now=NOW).calendar_state == "never"
    fx["calendar"] = {"last_ok_at": NOW - 100, "last_error": "boom", "last_error_at": NOW - 10}
    assert us.build_report(fx, now=NOW).calendar_state == "retrying"
    fx["calendar"] = {"last_ok_at": NOW - us.CALENDAR_STALE_SEC - 10}
    assert us.build_report(fx, now=NOW).calendar_state == "stale"


# ── the results matrix ───────────────────────────────────────────────────

def test_results_are_the_engines_numbers_not_a_recomputation():
    led = _fx()
    r = us.build_report(led, now=NOW)
    aaa = _row(led, "AAAUSDT")
    sel = r.matrix["selected"]["net"]
    assert sel.n == 1 and sel.mean == pytest.approx(aaa["results"]["net_pct"])
    assert r.matrix["selected"]["stop20"].mean == pytest.approx(aaa["results"]["stop20"]["net_pct"])
    assert r.matrix["selected"]["stop20"].stops_hit == 1
    bbb = _row(led, "BBBUSDT")
    assert r.matrix["rejected"]["net"].mean == pytest.approx(bbb["results"]["net_pct"])


def test_four_buckets_never_two_and_all_is_the_whole_rule():
    led = _fx()
    r = us.build_report(led, now=NOW)
    assert r.bucket_n == {"all": 2, "selected": 1, "rejected": 1, "unknown": 0}
    # A row whose filter input was missing lands in `unknown`, never in a side.
    _row(led, "BBBUSDT")["selected"] = None
    r = us.build_report(led, now=NOW)
    assert r.bucket_n["unknown"] == 1 and r.bucket_n["rejected"] == 0


def test_no_interval_under_five_rows():
    r = us.build_report(_fx(), now=NOW)
    assert r.matrix["all"]["net"].ci is None


def test_an_interval_is_symbol_clustered_and_reproducible():
    vals = [1.0, 2.0, 3.0, -1.0, 0.5, 4.0]
    syms = ["A", "A", "B", "C", "D", "E"]
    assert us.cluster_ci(vals, syms) == us.cluster_ci(vals, syms)
    lo, hi = us.cluster_ci(vals, syms)
    assert lo < sum(vals) / len(vals) < hi


def test_there_is_no_blended_figure_across_variants():
    r = us.build_report(_fx(), now=NOW)
    assert set(r.matrix["all"]) == {v for v, _l, _d in us.VARIANTS}
    assert not any("blend" in k or "avg_all" in k for k in r.matrix["all"])


def test_an_unpriced_hedge_is_counted_not_zeroed():
    led = _fx()
    _row(led, "AAAUSDT")["results"]["btc_hedged_pct"] = None
    c = us.build_report(led, now=NOW).matrix["all"]["btc"]
    assert c.n == 1 and c.missing == 1


# ── refusals iterate the engine's payload ────────────────────────────────

def test_every_status_in_the_ledger_renders_and_an_unknown_one_is_badged():
    led = _fx()
    led["rows"].append({"row_id": "ZZZUSDT:2026-10-10", "status": "PAUSED"})
    r = us.build_report(led, now=NOW)
    statuses = {st: known for st, _n, _c, known in r.status_rows}
    assert {"CLOSED", "OPEN", "SCHEDULED", "MISSED", "LATE", "REFUSED", "CANCELLED"} <= set(statuses)
    assert statuses["PAUSED"] is False


def test_late_names_its_cause_and_an_unknown_reason_is_badged():
    led = _fx()
    r = us.build_report(led, now=NOW)
    reasons = {(st, why): known for st, why, _n, _c, known in r.reason_rows}
    assert reasons[("LATE", "calendar_late")] is True
    assert reasons[("REFUSED", "price_mismatch")] is True
    _row(led, "GGGUSDT")["reason"] = "something_new"
    r = us.build_report(led, now=NOW)
    assert (("REFUSED", "something_new"), False) in {((st, why), k) for st, why, _n, _c, k in r.reason_rows}


# ── open rows against a live mark ────────────────────────────────────────

def test_an_open_row_is_priced_against_the_mark_and_leads_with_freshness():
    led = _fx()
    r = us.build_report(led, now=NOW, prices={"CCCUSDT": 1.9})
    row = r.open_rows[0]
    assert row["symbol"] == "CCCUSDT" and "bars_behind" in row and "stalled" in row
    assert row["unrealized_gross_pct"] == pytest.approx((2.0 - 1.9) / 2.0 * 100)
    assert row["room"][20] == pytest.approx((2.4 - 1.9) / 1.9 * 100)


def test_a_crossed_level_and_a_hit_stop_are_different_facts():
    led = _fx()
    ccc = _row(led, "CCCUSDT")
    r = us.build_report(led, now=NOW, prices={"CCCUSDT": 2.5})
    assert r.open_rows[0]["room"][20] < 0  # crossed, the walk has not seen it yet
    ccc["stops"] = {"20": {"hit_ms": 1, "fill": 2.4, "level": 2.4}}
    r = us.build_report(led, now=NOW, prices={"CCCUSDT": 2.5})
    assert r.open_rows[0]["room"][20] == "hit"


def test_no_mark_blanks_the_column_only():
    r = us.build_report(_fx(), now=NOW, prices={})
    assert r.open_rows[0]["mark"] is None and r.open_rows[0]["unrealized_gross_pct"] is None
    assert r.marks_available is False


def test_the_first_verdict_is_the_earliest_exit_owed():
    led = _fx()
    r = us.build_report(led, now=NOW)
    owed = [r_["exit_due_ts"] for r_ in led["rows"] if r_["status"] in ("OPEN", "SCHEDULED")]
    assert r.first_verdict_due == min(owed)


# ── export ───────────────────────────────────────────────────────────────

def test_the_export_is_uncapped_and_stamps_the_bucket():
    led = _fx()
    text = us.export_csv(led)
    lines = text.strip().splitlines()
    assert len(lines) == 1 + len(led["rows"])
    header = lines[0].split(",")
    assert "bucket" in header and "stop20_net_pct" in header and "alt_hedged_pct" in header


# ── the page ─────────────────────────────────────────────────────────────

@contextmanager
def _client(ledger, prices=None):
    from fastapi.testclient import TestClient

    from app.main import app

    class _DV:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        def unlock_shorts(self):
            return ledger

    with TestClient(app) as c:
        prev_dv = app.state.data_volume
        prev_bk = app.state.binance_klines
        app.state.data_volume = _DV(prev_dv)

        class _Marks:
            async def fetch_all_prices(self, **_kw):
                return prices or {}

        app.state.binance_klines = _Marks()
        c.post("/login", data={"password": os.environ["OPS_AUTH_TOKEN"]})
        try:
            yield c
        finally:
            app.state.data_volume = prev_dv
            app.state.binance_klines = prev_bk


def test_the_page_renders_every_section_from_the_engines_output():
    led = _fx(written_at=__import__("time").time())
    with _client(led, prices={"CCCUSDT": 1.9}) as c:
        r = c.get("/signals/unlock-shorts")
    assert r.status_code == 200
    html = r.text
    assert "Unlock shorts" in html and "DARK" in html and "LIVE" in html
    for section in ("Results — closed rows", "Open — being walked", "Upcoming entries",
                    "Closed rows", "Every row, by status", "Lane internals"):
        assert section in html, section
    assert "AAAUSDT" in html and "CCCUSDT" in html and "DDDUSDT" in html
    assert "calendar_late" in html and "price_mismatch" in html


def test_the_page_says_what_dark_means_and_that_filters_are_not_applied():
    """Copy is part of the measurement: these sentences are pinned."""
    led = _fx(written_at=__import__("time").time())
    with _client(led) as c:
        html = c.get("/signals/unlock-shorts").text
    assert "Nothing here reaches a subscriber" in html
    assert "stamped, never applied" in html
    assert "never pooled" in html


def test_an_empty_results_table_says_when_the_first_verdict_is_due():
    led = _fx(written_at=__import__("time").time())
    led["rows"] = [r for r in led["rows"] if r["status"] != "CLOSED"]
    with _client(led) as c:
        html = c.get("/signals/unlock-shorts").text
    assert "No row has closed yet" in html and "first can close no earlier than" in html


def test_a_missing_file_renders_its_cause():
    with _client({"error": "missing: /engine-data/unlock_shorts_v1.json"}) as c:
        html = c.get("/signals/unlock-shorts").text
    assert "NOT WRITTEN" in html and "predates the lane" in html


def test_the_export_route_serves_every_row():
    led = _fx()
    with _client(led) as c:
        r = c.get("/signals/unlock-shorts/export.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert len(r.text.strip().splitlines()) == 1 + len(led["rows"])


def test_the_router_is_included_before_signal_detail():
    """/signals/{signal_id} swallows any literal registered after it."""
    src = (HERE.parent / "app" / "main.py").read_text()
    assert src.index("app.include_router(unlock_shorts.router)") < src.index("app.include_router(signal_detail.router)")


# ── local contract: regenerate from the engine and compare shapes ────────

ENGINE = pathlib.Path(os.environ.get("ENGINE_REPO") or (HERE.parent.parent / "360-v2")).resolve()


@pytest.mark.skipif(
    not (ENGINE / "scripts" / "gen_ops_unlock_shorts_fixture.py").exists(),
    reason="no engine repo beside ops — and CI never checks it out either",
)
def test_the_committed_fixture_matches_what_the_engine_writes_today():
    """Run the engine's generator and compare every row's KEYS with the fixture.

    Values drift legitimately (timestamps); a key the engine adds or drops is
    the contract changing, and the fixture must be regenerated with it.
    """
    proc = subprocess.run(
        [sys.executable, "scripts/gen_ops_unlock_shorts_fixture.py"],
        cwd=ENGINE, capture_output=True, text=True, timeout=120,
    )
    if proc.returncode != 0 and "ModuleNotFoundError" in proc.stderr:
        pytest.skip(f"engine deps not installed here: {proc.stderr.strip().splitlines()[-1]}")
    assert proc.returncode == 0, proc.stderr
    fresh = json.loads(proc.stdout)
    assert set(fresh) == set(FIXTURE)
    fx_rows = {r["row_id"]: set(r) for r in FIXTURE["rows"]}
    for r in fresh["rows"]:
        assert set(r) == fx_rows[r["row_id"]], r["row_id"]
