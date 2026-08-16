"""The redis idletime probe: what it leaves behind, and what it reports.

Three defects fixed on 2026-08-16, all in the path behind the
``redis_unreachable`` HIGH page the owner had been getting once or twice a
night and which cleared on the next cycle every time:

1. ``asyncio.wait_for(proc.communicate(), …)`` cancels the *coroutine*, not
   the process.  A probe that timed out because the host was busy left a
   ``docker exec`` running to make it busier — once a minute, forever.
2. The alert named two causes and distinguished neither, while ``rc`` and
   stderr went to a container log.
3. A single deadline miss paged HIGH.  The retry lives inside one cycle, so
   a genuinely stopped container still pages on the cycle it stops.

These drive the real subprocess machinery where the property is about the
process, and the real ``_redis_idletime`` where the property is about the
retry.  A spy records what the collaborator returned; nothing here invents a
collaborator's shape.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from app.agent import runner
from app.agent.detectors import RedisProbe


class TestExecCapture:
    async def test_completed_run_reports_no_cause(self):
        rc, stdout, stderr, cause = await runner._exec_capture(
            (sys.executable, "-c", "print('12')")
        )
        assert (rc, stdout.strip(), cause) == (0, "12", "")

    async def test_nonzero_exit_is_a_result_not_a_probe_failure(self):
        """A stopped container exits non-zero. That is an answer, not a fault
        of the probe — the caller decides what it means, so ``cause`` is empty
        and the exit code survives."""
        rc, _stdout, stderr, cause = await runner._exec_capture(
            (sys.executable, "-c", "import sys; sys.stderr.write('nope'); sys.exit(3)")
        )
        assert cause == ""
        assert rc == 3
        assert "nope" in stderr

    async def test_missing_binary_reports_exception_with_its_own_words(self):
        rc, _stdout, stderr, cause = await runner._exec_capture(
            ("definitely-not-a-real-binary-360ce",)
        )
        assert cause == "exception"
        assert rc is None
        assert "FileNotFoundError" in stderr

    async def test_timeout_kills_the_child_instead_of_leaking_it(self, monkeypatch):
        """The defect this file exists for.

        The spy hands back the **real** ``asyncio.subprocess.Process`` so the
        assertion is on real state: after ``_exec_capture`` returns, the child
        must be reaped (``returncode is not None``). Against the pre-fix code
        it is ``None`` — still running, once a minute, forever.
        """
        spawned: list[asyncio.subprocess.Process] = []
        real = asyncio.create_subprocess_exec

        async def spy(*args, **kwargs):
            proc = await real(*args, **kwargs)
            spawned.append(proc)
            return proc

        monkeypatch.setattr(asyncio, "create_subprocess_exec", spy)

        rc, _stdout, stderr, cause = await runner._exec_capture(
            (sys.executable, "-c", "import time; time.sleep(30)"),
            timeout=0.5,
        )

        assert cause == "timeout"
        assert rc is None
        assert "no response within" in stderr
        assert len(spawned) == 1
        assert spawned[0].returncode is not None, "timed-out probe was left running"


class TestRedisIdletimeRetry:
    async def test_success_first_try(self, monkeypatch):
        async def fake(argv, timeout=None):
            return 0, "7\n", "", ""

        monkeypatch.setattr(runner, "_exec_capture", fake)
        probe = await runner._redis_idletime()
        assert probe == RedisProbe(ok=True, output="7", returncode=0, attempts=1)

    async def test_a_single_deadline_miss_does_not_page(self, monkeypatch):
        """One 10s timeout on a busy 1-vCPU box is not evidence redis is gone.

        Two Telegram messages per flake — the HIGH and its recovery a minute
        later — on the channel that also has to carry a naked position.
        """
        calls: list[tuple] = []

        async def fake(argv, timeout=None):
            calls.append(argv)
            if len(calls) == 1:
                return None, "", "no response within 10s", "timeout"
            return 0, "9\n", "", ""

        monkeypatch.setattr(runner, "_exec_capture", fake)
        probe = await runner._redis_idletime()
        assert probe.ok is True
        assert probe.output == "9"
        assert probe.attempts == 2
        assert len(calls) == 2

    async def test_persistent_failure_still_pages_in_the_same_cycle(self, monkeypatch):
        """The retry must not become a delay.

        A stopped container fails immediately and fails the same way twice, so
        detection latency is unchanged: the probe still returns ``ok=False``
        within this cycle and the detector still raises HIGH.
        """
        async def fake(argv, timeout=None):
            return 1, "", "Error response from daemon: container is not running", ""

        monkeypatch.setattr(runner, "_exec_capture", fake)
        probe = await runner._redis_idletime()
        assert probe.ok is False
        assert probe.cause == "exec_error"
        assert probe.returncode == 1
        assert "not running" in probe.detail
        assert probe.attempts == runner.REDIS_PROBE_ATTEMPTS

    async def test_clean_exit_with_no_output_is_a_failure_not_an_idle_time(
        self, monkeypatch
    ):
        async def fake(argv, timeout=None):
            return 0, "  \n", "", ""

        monkeypatch.setattr(runner, "_exec_capture", fake)
        probe = await runner._redis_idletime()
        assert probe.ok is False
        assert probe.cause == "no_output"

    async def test_summary_is_safe_to_put_in_an_alert(self, monkeypatch):
        async def fake(argv, timeout=None):
            return None, "", "no response within 10s", "timeout"

        monkeypatch.setattr(runner, "_exec_capture", fake)
        probe = await runner._redis_idletime()
        summary = probe.summary()
        assert "timeout" in summary
        assert "no response within 10s" in summary


class TestDockerPsStillReportsBlindnessAsEmpty:
    async def test_probe_failure_returns_empty_so_the_detector_can_say_blind(
        self, monkeypatch
    ):
        """``CoreContainerDetector`` reads ``{}`` as *we cannot see the stack*
        and raises ``docker_ps_unavailable`` rather than reporting every
        container missing. Refactoring the helper must not change that."""
        async def fake(argv, timeout=None):
            return None, "", "no response within 10s", "timeout"

        monkeypatch.setattr(runner, "_exec_capture", fake)
        assert await runner._docker_ps_statuses() == {}

    async def test_parses_names_and_statuses(self, monkeypatch):
        async def fake(argv, timeout=None):
            return 0, "360scalp-v2-engine\tUp 6 hours\n360scalp-v2-redis\tUp 6 hours\n", "", ""

        monkeypatch.setattr(runner, "_exec_capture", fake)
        assert await runner._docker_ps_statuses() == {
            "360scalp-v2-engine": "Up 6 hours",
            "360scalp-v2-redis": "Up 6 hours",
        }


@pytest.mark.parametrize("attempts", [1, 2, 3])
async def test_attempts_is_configurable_and_never_zero(monkeypatch, attempts):
    calls: list[tuple] = []

    async def fake(argv, timeout=None):
        calls.append(argv)
        return 1, "", "boom", ""

    monkeypatch.setattr(runner, "_exec_capture", fake)
    probe = await runner._redis_idletime(attempts=attempts)
    assert len(calls) == attempts
    assert probe.attempts == attempts
