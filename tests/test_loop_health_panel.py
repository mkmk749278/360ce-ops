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

import re

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


# ---------------------------------------------------------------------------
# Slow is not wedged (engine 2026-08-19: the progress heartbeat)
# ---------------------------------------------------------------------------
#
# The heartbeat file used to be written only at the END of a scan cycle, so
# "cycle wall-time" and "heartbeat age" were one number and this card could not
# tell a loop that had stopped from a loop that was merely slow. It called both
# `hanging` and told the owner the container was "being killed right now" —
# true all day on 2026-08-19, and about to become false for the slow case. The
# engine now beats when a SYMBOL finishes; only a stale beat is a restart.


def _beating(**scan):
    """A payload from an engine that reports the progress beat."""
    scan.setdefault("progress_heartbeat_enabled", True)
    scan.setdefault("heartbeat_progress_writes", 40)
    return _payload(**scan)


def test_a_long_cycle_with_a_live_beat_is_slow_not_wedged():
    out = reduce_loop_health(_beating(in_flight_sec=200.0, heartbeat_age_sec=3.0))
    assert out["state"] == "slow"
    assert "no restart is coming" in out["note"]


def test_a_stale_beat_is_wedged_whatever_the_cycle_says():
    """The heartbeat is the only quantity that decides a restart."""
    out = reduce_loop_health(_beating(in_flight_sec=200.0, heartbeat_age_sec=300.0))
    assert out["state"] == "hanging"
    assert "wedged rather than merely slow" in out["note"]


def test_an_engine_without_the_progress_beat_still_pools_the_two():
    """Three states, never two: absent is not off.

    On an engine that predates the beat the two ages really ARE one number, so
    grading `in_flight` alone as slow would tell the owner no restart is coming
    while autoheal restarts the container. The old behaviour is kept for exactly
    the engines the old behaviour is true of.
    """
    out = reduce_loop_health(_payload(in_flight_sec=200.0, heartbeat_age_sec=3.0))
    assert out["state"] == "hanging"
    assert out["progress_heartbeat"] is None


def test_the_progress_beat_being_switched_off_is_its_own_state():
    out = reduce_loop_health(
        _payload(progress_heartbeat_enabled=False, in_flight_sec=200.0,
                 heartbeat_age_sec=3.0),
    )
    assert out["progress_heartbeat"] is False
    assert out["state"] == "hanging", "switched off, so the two ages are one again"


def test_the_past_deadline_note_stops_promising_a_restart_that_will_not_come():
    """An alarming caption over a healthy subsystem is worse than a blank.

    With the progress beat, a completed cycle past the deadline no longer
    restarts anything — so the copy that said it does would send the owner to
    debug a loop that is working. `/invalidations` (2026-08-07) is the standing
    example of this costing an hour in the wrong direction.
    """
    beating = reduce_loop_health(_beating(over_kill=2))
    assert beating["state"] == "past_deadline"
    assert "no longer" in beating["note"]
    assert "the dashboard and the app feed go empty" not in beating["note"]

    old = reduce_loop_health(_payload(over_kill=2))
    assert "the dashboard and the app feed go empty" in old["note"]


def _render_liveness(monkeypatch, payload):
    """Drive the REAL route and the REAL template with a chosen engine payload.

    Rendering the template standalone needs a fabricated `request` (the nav
    calls `may_use` on it), and a fabricated one is a fixture agreeing with
    whatever I assumed — the failure this repo has paid for twice. Stubbing the
    engine probe instead leaves every other hop real.
    """
    from fastapi.testclient import TestClient

    import app.routes.system as system_routes
    from app.main import app
    from tests.test_panel_readability import _login

    async def _fake_probe(_request):
        return payload

    monkeypatch.setattr(system_routes, "_loop_health_probe", _fake_probe)
    with TestClient(app) as client:
        _login(client)
        return client.get("/system/liveness").text

def test_the_slow_state_renders_and_keeps_its_stage_breakdown(monkeypatch):
    """A slow cycle asks the same question a wedged one does — where did it go.

    Keying the stage table on `hanging` alone would blank the breakdown for the
    exact state this change creates, and that is the population it is most
    useful for: a cycle running long right now that is going to finish.

    Renders through the app's REAL Jinja environment (globals, filters and all)
    rather than a locally-built one — `test_templates_compile.py` was itself a
    hand-built mirror until 2026-08-07, and a mirror can only ever diverge
    toward passing over a template the app cannot render.
    """
    payload = _beating(in_flight_sec=200.0, heartbeat_age_sec=3.0,
                       in_flight_stages={"indicators": 461.7, "smc": 12.0})
    out = reduce_loop_health(payload)
    assert out["state"] == "slow"
    assert [r["stage"] for r in out["in_flight_stages"]] == ["indicators", "smc"]

    html = _render_liveness(monkeypatch, payload)
    assert "CYCLE SLOW, LOOP ALIVE" in html
    assert "LOOP WEDGED NOW" not in html
    assert "indicators" in html, "the stage breakdown must survive the new state"


def test_the_beat_row_says_not_reported_rather_than_off(monkeypatch):
    """An engine predating the progress beat must not render as one with it
    switched off — different states, different next moves (deploy vs a flag)."""
    html = _render_liveness(monkeypatch, _payload())
    assert "Beats on progress" in html
    assert "not reported" in html


def test_the_wedged_note_does_not_name_a_cause_the_engine_cannot_report():
    """Two engines, two claims — a caption must follow the state it describes.

    With the progress beat a stale heartbeat means no unit of work finished. On
    an engine without it the same staleness only means no CYCLE completed, which
    a merely-slow cycle also produces — so asserting "no unit of work finished"
    there would name a cause the page cannot observe. That is the `/alerts` and
    `/invalidations` defect, and it is cheapest to avoid while writing the note.
    """
    beating = reduce_loop_health(_beating(in_flight_sec=400.0, heartbeat_age_sec=300.0))
    assert beating["state"] == "hanging"
    assert "no unit of work has finished" in beating["note"]

    old = reduce_loop_health(_payload(in_flight_sec=400.0, heartbeat_age_sec=300.0))
    assert old["state"] == "hanging"
    assert "no unit of work has finished" not in old["note"]
    assert "only at the END of a cycle" in old["note"]


def test_a_wedged_verdict_with_no_beat_reported_never_prints_none():
    """The old engine can reach `hanging` on `in_flight` alone, with no beat
    reported at all — quoting a missing value as a number is how a blank becomes
    a finding."""
    out = reduce_loop_health(_payload(in_flight_sec=400.0))
    assert out["state"] == "hanging"
    assert "heartbeat is None" not in out["note"], "a missing value quoted as one"
    assert "the heartbeat is not reported" in out["note"]


def test_every_state_the_reducer_can_emit_has_its_own_badge(monkeypatch):
    """Derived, because the hand-checked version cost a CI round.

    Adding `slow` beside `hanging` renamed two labels, and three assertions in
    `test_scan_stage_breakdown.py` were still pinning the old strings — the rot
    case this repo names: an assertion outliving its premise at the exact moment
    somebody is changing the premise, which is the one moment nobody re-reads
    it. The suite was still running locally when the push went out, and CI found
    them.

    So rather than a fourth hand-written label assertion: every state the
    reducer can emit must render a badge of its own on the real page. A state
    added without one falls through to WITHIN BOUNDS and reads healthy while it
    is not — and a label renamed on one side of that pair fails here rather than
    in whichever test happened to quote it.
    """
    cases = {
        "ok": _beating(),
        "pressure": _beating(cycles=20, over_warn=15, over_kill=0),
        "past_deadline": _beating(over_kill=2),
        "slow": _beating(in_flight_sec=200.0, heartbeat_age_sec=3.0),
        "hanging": _beating(in_flight_sec=400.0, heartbeat_age_sec=300.0),
    }
    badges = {}
    for state, payload in cases.items():
        assert reduce_loop_health(payload)["state"] == state, (
            f"fixture for {state!r} no longer produces it"
        )
        html = _render_liveness(monkeypatch, payload)
        # Scoped to the scan-cycle card: the page carries a global status banner
        # whose badge comes first in the document, and grading on that would
        # have compared five identical strings and called them a pass.
        card = html.split("the number the healthcheck kills on", 1)[-1]
        found = re.findall(r'<span class="badge badge-\w+">([^<]+)</span>', card)
        assert found, f"{state!r} rendered no badge at all"
        badges[state] = found[0].strip()

    assert len(set(badges.values())) == len(badges), (
        f"two states share a badge, so the page cannot tell them apart: {badges}"
    )
