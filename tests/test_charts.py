"""Inline-SVG charts (app/charts.py). Each builder draws only over data that
supports it, counts what it could not place, and invents no number."""
from __future__ import annotations

import os

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app import charts  # noqa: E402


def _row(ts, net):
    return {"closed_at_ts": ts, "net_usd": net}


def test_equity_curve_accumulates_in_close_order_and_finds_the_drawdown():
    rows = [_row(3, -4.0), _row(1, 5.0), _row(2, 2.0), _row(4, 1.0)]
    eq = charts.equity_curve(rows)
    assert eq["svg"] is not None
    assert eq["final"] == 4.0          # 5 + 2 - 4 + 1
    assert eq["max_dd"] == -4.0        # peak 7 → 3
    assert eq["n"] == 4 and eq["skipped"] == 0


def test_equity_curve_counts_rows_it_cannot_place():
    rows = [_row(1, 1.0), _row(2, 1.0), _row(3, 1.0), _row(None, 9.0), _row(4, None)]
    eq = charts.equity_curve(rows)
    assert eq["skipped"] == 2 and eq["final"] == 3.0


def test_too_few_points_draw_no_curve_and_say_why():
    eq = charts.equity_curve([_row(1, 1.0), _row(2, 1.0)])
    assert eq["svg"] is None and eq["why"]


def test_band_bars_skip_a_missing_value_instead_of_drawing_zero():
    b = charts.band_bars([{"key": "a", "avg_pnl": 0.5, "n": 3},
                          {"key": "b", "avg_pnl": -0.25, "n": 4},
                          {"key": "c", "avg_pnl": None, "n": 1}], "avg_pnl")
    assert [r["key"] for r in b["rows"]] == ["a", "b"]
    assert b["skipped"] == 1
    assert b["rows"][0]["width"] == 50.0 and b["rows"][1]["width"] == 25.0
    assert b["rows"][1]["pos"] is False


def test_funnel_names_the_gap_between_reasons_and_dropped_total():
    f = charts.delivery_funnel(100, 20, 80, [{"reason": "cooldown", "n": 50},
                                            {"reason": "corr_lock", "n": 20}])
    assert f["ok"] and f["delivered_pct"] == 20.0 and f["dropped_pct"] == 80.0
    assert f["unattributed"] == 10 and f["over_attributed"] == 0
    assert sum(r["n"] for r in f["reasons"]) == 70     # not stretched to 80


def test_funnel_without_a_dequeued_count_does_not_draw():
    assert charts.delivery_funnel(0, 0, 0, [])["ok"] is False
    assert charts.delivery_funnel(None, 1, 1, [])["ok"] is False


def test_pipeline_draws_unknown_hollow_and_escapes_labels():
    svg = str(charts.pipeline([
        {"key": "a", "label": "<engine>", "state": "ok"},
        {"key": "b", "label": "redis", "state": "mystery"},
    ]))
    assert "node-ok" in svg and "node-unknown" in svg
    assert "<engine>" not in svg and "&lt;engine&gt;" in svg
    assert charts.pipeline([{"key": "a", "state": "ok"}]) is None
