"""The restart that nothing on the box could see.

Measured live 2026-08-19: the engine restarted alone during a diagnostic
session — uptime went 1h30m to 8m — while docker's `RestartCount` read 0 and
`/alerts` was clear. Three instruments missed it at once, and each miss is a
test here:

* `RestartCount` counts only restart-POLICY restarts; autoheal issues a manual
  restart and a compose recreate starts the count over.
* `CoreContainerDetector` asks whether the container is running, and a restart
  that completes between two 60s polls never presents as anything else.
* The engine's own counters reset, so the quiet afterwards looks like a quiet
  market.

The one signal a restart always produces is uptime going backwards.
"""
from __future__ import annotations

from app.agent.detectors import EngineRestartDetector


def _det(**kw) -> EngineRestartDetector:
    return EngineRestartDetector(**kw)


def test_a_steady_engine_is_silent():
    d = _det()
    assert d.check({"uptime_seconds": 100.0}, now=0.0) == []
    assert d.check({"uptime_seconds": 160.0}, now=60.0) == []
    assert d.check({"uptime_seconds": 220.0}, now=120.0) == []


def test_the_first_reading_never_claims_a_restart():
    """Otherwise every agent deploy pages about the engine."""
    d = _det()
    assert d.check({"uptime_seconds": 5.0}, now=0.0) == []


def test_uptime_going_backwards_is_a_restart():
    d = _det()
    d.check({"uptime_seconds": 5400.0}, now=0.0)
    out = d.check({"uptime_seconds": 480.0}, now=60.0)
    assert len(out) == 1
    assert out[0].fingerprint == "engine_restarted"
    assert out[0].severity == "WARN"
    assert out[0].raw["uptime_before"] == 5400.0
    assert out[0].raw["uptime_now"] == 480.0


def test_one_restart_warns_and_two_page_and_they_are_never_pooled():
    """A bounce is autoheal working; a loop is the failure.

    Each restart re-seeds every pair over REST and rebuilds the indicator
    caches cold, so the cure pushes the next cycle further past the deadline
    that caused it. Grading them the same would either page on healthy
    self-healing or stay quiet through the loop.
    """
    d = _det(window_sec=3600, loop_threshold=2)
    d.check({"uptime_seconds": 5400.0}, now=0.0)

    first = d.check({"uptime_seconds": 300.0}, now=100.0)
    assert [r.severity for r in first] == ["WARN"]
    assert first[0].fingerprint == "engine_restarted"

    d.check({"uptime_seconds": 900.0}, now=700.0)          # climbing again
    second = d.check({"uptime_seconds": 120.0}, now=1000.0)
    assert [r.severity for r in second] == ["HIGH"]
    assert second[0].fingerprint == "engine_restart_loop"
    assert second[0].raw["restarts_in_window"] == 2


def test_restarts_age_out_of_the_window():
    """A restart last week is history, not an event."""
    d = _det(window_sec=3600, loop_threshold=2)
    d.check({"uptime_seconds": 5400.0}, now=0.0)
    d.check({"uptime_seconds": 60.0}, now=100.0)           # restart 1

    d.check({"uptime_seconds": 7200.0}, now=100_000.0)     # long healthy run
    out = d.check({"uptime_seconds": 60.0}, now=100_100.0)  # restart 2, much later
    assert out[0].fingerprint == "engine_restarted", (
        "the first restart is far outside the window and must not make this a loop"
    )


def test_small_backwards_jitter_is_not_a_restart():
    """Two pulses can disagree by a moment; a restart moves uptime by minutes."""
    d = _det()
    d.check({"uptime_seconds": 5000.0}, now=0.0)
    assert d.check({"uptime_seconds": 4998.0}, now=60.0) == []


def test_an_unreadable_pulse_claims_nothing_in_either_direction():
    """Unknown is not "no restart" — and it is not evidence of one either.

    An unreachable engine already has its own detector; a second alert for one
    event is how a page stops being read. What it must NOT do is compare across
    the gap afterwards and invent a restart.
    """
    d = _det()
    d.check({"uptime_seconds": 5400.0}, now=0.0)
    assert d.check({"error": "connection refused"}, now=60.0) == []
    assert d.check({}, now=120.0) == []
    assert d.check({"uptime_seconds": 30.0}, now=180.0) == [], (
        "the reading after a gap has no previous value to be lower than"
    )
    # ...and normal detection resumes once two good readings exist again.
    assert d.check({"uptime_seconds": 20.0}, now=240.0)[0].fingerprint == "engine_restarted"


def test_a_non_numeric_uptime_is_treated_as_unknown():
    d = _det()
    d.check({"uptime_seconds": 5400.0}, now=0.0)
    assert d.check({"uptime_seconds": "n/a"}, now=60.0) == []


def test_the_detector_is_built_once_so_it_can_hold_state():
    """A detector rebuilt each cycle has no previous uptime and never fires.

    That is the quiet way this check would ship dead, so the wiring is pinned
    rather than the class: `d9` must be constructed OUTSIDE the poll loop.
    """
    import ast
    import inspect

    from app.agent import runner

    tree = ast.parse(inspect.getsource(runner))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "run"
    )
    loops = [n for n in ast.walk(fn) if isinstance(n, ast.While)]
    assert loops, "the agent's poll loop has moved — re-point this guard"
    constructed_in_loop = any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "EngineRestartDetector"
        for loop in loops for n in ast.walk(loop)
    )
    assert not constructed_in_loop, (
        "EngineRestartDetector holds cross-cycle state; constructing it inside "
        "the poll loop makes it structurally unable to detect anything"
    )
    assert any(
        isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "EngineRestartDetector"
        for n in ast.walk(fn)
    ), "EngineRestartDetector is not wired into the agent at all"
