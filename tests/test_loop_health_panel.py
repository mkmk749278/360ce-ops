"""`/system/liveness` must render the number the healthcheck kills on.

Scan-cycle wall-time was computed by the engine every cycle and read by
nothing — not the snapshot, not the truth report, not ops — while it is exactly
what `healthcheck.py` grades and what autoheal restarts the container over. On
2026-08-19 that produced cycles of 9.2s to 402.5s, a restart inside a
measurement window, and no surface anywhere that could say so.

These tests drive the real reducer and the real page, and they pin the three
states apart: an engine that does not report is NOT a healthy engine.
"""
from __future__ import annotations

from app.data_sources.system_health import reduce_loop_health


def _payload(**scan):
    base = {
        "cycles": 100, "last_sec": 12.0, "worst_sec": 14.0,
        "over_warn": 0, "over_kill": 0,
        "warn_sec": 60.0, "kill_sec": 120.0,
        "last_cycle_at": 1.0, "executor_workers": 2,
    }
    base.update(scan)
    return {"loop_health": {
        "scan_cycle": base,
        "indicator_cache": {"capped_hits": 5, "stale_avoided": 2,
                            "undatable": 0, "undatable_at_cap": 0,
                            "bucket_cap": 1000},
        "snapshot_writer": {"cycles": 113, "overruns": 63,
                            "last_cycle_sec": 1.8, "worst_cycle_sec": 134.5,
                            "ttl_sec": 900, "last_completed_at": 1.0,
                            "write_times": {}},
        "strategy_edge": {"dirty": False, "cells": 11261, "saves": 4},
    }}


def test_a_healthy_loop_reads_within_bounds():
    out = reduce_loop_health(_payload())
    assert out["reported"] is True
    assert out["state"] == "ok"
    assert out["scan"]["worst_sec"] == 14.0


def test_a_cycle_past_the_deadline_is_its_own_state():
    """This is a restart that has already been earned, not pressure."""
    out = reduce_loop_health(_payload(over_kill=2, over_warn=40, worst_sec=402.5))
    assert out["state"] == "past_deadline"
    assert "120s healthcheck deadline" in out["note"]
    assert "snapshot:*" in out["note"], (
        "the note must connect the restart to the empty app feed — that link is "
        "the one nobody had"
    )


def test_sustained_pressure_is_graded_before_anything_is_killed():
    """The leading edge. A probe that fires only on over_kill fires too late."""
    out = reduce_loop_health(_payload(over_warn=60, over_kill=0))
    assert out["state"] == "pressure"
    assert out["over_warn_pct"] == 60.0


def test_one_slow_cycle_in_a_long_book_is_weather():
    out = reduce_loop_health(_payload(over_warn=3, over_kill=0))
    assert out["state"] == "ok"


def test_an_engine_that_does_not_report_is_not_a_healthy_engine():
    """`not_reported` and `ok` are different claims and only one is earned."""
    out = reduce_loop_health({})
    assert out["reported"] is False
    assert out["state"] == "not_reported"
    assert out["scan"] is None
    assert "NOT a claim that the loop is fine" in out["note"]

    out = reduce_loop_health({"error": "connection refused"})
    assert out["state"] == "not_reported"


def test_a_young_process_is_not_a_stalled_one():
    out = reduce_loop_health(_payload(cycles=0))
    assert out["state"] == "not_reported"
    assert "young process" in out["note"]


def test_the_bounds_come_from_the_engine_and_are_flagged_when_they_do_not():
    """Ops inventing a threshold is what made /truth read STALE 23h a day."""
    out = reduce_loop_health(_payload())
    assert out["bounds_reported"] is True

    p = _payload()
    p["loop_health"]["scan_cycle"].pop("warn_sec")
    p["loop_health"]["scan_cycle"].pop("kill_sec")
    assert reduce_loop_health(p)["bounds_reported"] is False


def test_the_indicator_cache_block_is_read_where_the_engine_writes_it():
    """Top level, not nested under scan_cycle.

    A fixture that puts it where the reader assumed would agree with the reader
    and disagree with the engine — the failure that cost a session twice.
    """
    out = reduce_loop_health(_payload())
    assert out["cache"]["stale_avoided"] == 2

    nested = {"loop_health": {"scan_cycle": {
        "cycles": 5, "last_sec": 1.0, "worst_sec": 1.0, "over_warn": 0,
        "over_kill": 0, "warn_sec": 60.0, "kill_sec": 120.0,
        "indicator_cache": {"stale_avoided": 99},
    }}}
    assert reduce_loop_health(nested)["cache"] is None, (
        "reading it off scan_cycle would be a field ops reads and no repo writes"
    )


def test_the_liveness_page_renders_the_scan_cycle_card():
    """The last hop: a panel on a page nobody can reach is not a panel."""
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.test_panel_readability import _login

    with TestClient(app) as client:
        _login(client)
        body = client.get("/system/liveness").text
        assert "Scan cycle" in body
        assert "the number the healthcheck kills on" in body
        # The card renders whether or not anything is wrong — a check that
        # appears only when it trips teaches the reader its absence means fine.
        assert ("NOT REPORTED" in body or "WITHIN BOUNDS" in body
                or "UNDER PRESSURE" in body or "PAST THE DEADLINE" in body)


def test_boot_warmup_does_not_make_the_card_red():
    """The verdict this card was about to be wrong in.

    On the first deploy of this panel it read PAST THE DEADLINE for a healthy
    boot, because a cold start's first cycles legitimately run long (74.5s /
    131.2s / 72.8s measured, against a steady state of 8-47s) and the engine
    counted them into the same bucket. Red that can never be anything but red
    is a dead instrument, and this repo has already paid for one of those.
    """
    p = _payload(over_kill=0, over_warn=0)
    p["loop_health"]["scan_cycle"]["over_kill_boot"] = 1
    p["loop_health"]["scan_cycle"]["over_warn_boot"] = 3

    out = reduce_loop_health(p)
    assert out["state"] == "ok"
    assert out["boot_over_kill"] == 1
    assert out["boot_over_warn"] == 3
    assert "boot warm-up" in out["note"], (
        "counted apart is not the same as hidden — the card must still say it happened"
    )


def test_a_steady_state_breach_still_pages_when_boot_also_had_one():
    """Excluding boot from the verdict must not mask a real fault beside it."""
    p = _payload(over_kill=2, over_warn=9)
    p["loop_health"]["scan_cycle"]["over_kill_boot"] = 1
    out = reduce_loop_health(p)
    assert out["state"] == "past_deadline"
    assert out["boot_over_kill"] == 1


def test_an_engine_predating_the_boot_split_reads_not_reported():
    """`0` and "this build does not say" are different claims."""
    out = reduce_loop_health(_payload())
    assert out["boot_reported"] is False
    assert out["boot_over_kill"] == 0

    p = _payload()
    p["loop_health"]["scan_cycle"]["over_kill_boot"] = 0
    assert reduce_loop_health(p)["boot_reported"] is True
