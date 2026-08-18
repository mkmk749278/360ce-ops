"""The System pages, and the misclassification they were built to end.

2026-08-18. The owner was getting this HIGH page, over and over, each one
followed by a recovery::

    redis_unreachable — Could not read snapshot:tickers idletime from the
    engine redis container … Probe said: no_output · rc=0 · exited 0 and
    printed nothing.

…while separately being unable to tell whether the engine was alive, because
the dashboard's engine-backed pages had stopped answering.

Both are one event and the alert named the wrong container. These tests pin
that, and they pin the three properties the pages exist to hold:

* a probe that could not run never renders as passing;
* a summary never disagrees with the table under it;
* a missing key points at the engine, a dead PING points at redis, and neither
  is ever reported as the other.
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from app.agent.detectors import RedisProbe, RedisStalenessDetector  # noqa: E402
from app.data_sources import system_health as sh  # noqa: E402


# ---------------------------------------------------------------------------
# The detector fix
# ---------------------------------------------------------------------------

class TestNoOutputIsNotUnreachable:
    """`rc=0` is positive evidence redis ANSWERED. It cannot mean unreachable."""

    def test_empty_reply_with_a_clean_exit_points_at_the_engine(self):
        results = RedisStalenessDetector().check(
            RedisProbe(ok=False, cause="no_output", returncode=0,
                       detail="exited 0 and printed nothing", attempts=2)
        )
        assert len(results) == 1
        result = results[0]
        # The rename IS the fix. Verify by reverting: against the pre-2026-08-18
        # tree this reads `redis_unreachable` and this assertion fails.
        assert result.fingerprint == "snapshot_key_missing"
        assert result.severity == "HIGH", (
            "still HIGH — the engine has stopped publishing and every "
            "engine-backed page is going empty. What changed is which "
            "container the owner is sent to."
        )
        assert result.raw["points_at"] == "360scalp-v2-engine"
        assert "snapshot:tickers" in result.description

    def test_it_says_redis_is_up_rather_than_leaving_it_to_be_inferred(self):
        """The old alert's whole cost was sending the owner to the wrong box."""
        result = RedisStalenessDetector().check(
            RedisProbe(ok=False, cause="no_output", returncode=0)
        )[0]
        assert "Redis is UP" in result.description
        assert "SnapshotWriter" in result.description

    @pytest.mark.parametrize("probe", [
        RedisProbe(ok=False, cause="exec_error", returncode=1,
                   detail="Error response from daemon: container is not running"),
        RedisProbe(ok=False, cause="timeout", detail="no response within 10s"),
        RedisProbe(ok=False, cause="exception", detail="FileNotFoundError: docker"),
        RedisProbe(ok=False, cause="not_run", detail="the cycle did not reach the probe"),
    ])
    def test_every_other_failure_still_pages_as_unreachable(self, probe):
        """Narrowed, not removed. A stopped container must still page HIGH —
        this is the 2026-07-27 property (a probe failure never reads as
        all-clear) and the fix must not cost it."""
        results = RedisStalenessDetector().check(probe)
        assert len(results) == 1
        assert results[0].fingerprint == "redis_unreachable"
        assert results[0].severity == "HIGH"

    def test_a_nonzero_exit_with_empty_output_is_still_unreachable(self):
        """The exit code is what carries the argument, not the empty output.

        `docker exec` against a stopped container can print nothing on stdout
        too — what separates the two cases is rc, so a `no_output` that did
        NOT exit cleanly must not be re-labelled."""
        results = RedisStalenessDetector().check(
            RedisProbe(ok=False, cause="no_output", returncode=1)
        )
        assert results[0].fingerprint == "redis_unreachable"

    def test_a_healthy_probe_is_still_silent(self):
        assert RedisStalenessDetector().check(RedisProbe(ok=True, output="3")) == []


# ---------------------------------------------------------------------------
# The chain
# ---------------------------------------------------------------------------

def _containers(**over):
    row = {
        "name": "360scalp-v2-engine", "present": True, "verdict": "running",
        "status_text": "Up 3 hours", "health": "healthy", "restart_count": 0,
        "health_failing_streak": 0, "last_health": None,
    }
    rows = [dict(row, name=n) for n in
            ("360scalp-v2-engine", "360scalp-v2-redis", "360scalp-v2-api")]
    payload = {"blind": False, "blind_reason": "", "rows": rows}
    payload.update(over)
    return payload


def _redis(**over):
    payload = {
        "reachable": True, "ping": "ok in 40ms", "ping_cause": "",
        "census_cause": "", "census_ran": True, "key_rows": [], "missing": [],
        "snapshot_key_count": 11,
    }
    payload.update(over)
    return payload


def _api(**over):
    payload = {"error": None, "latency_ms": 60,
               "pulse": {"uptime_seconds": 7200, "scanning_pairs": 75, "signals_today": 4}}
    payload.update(over)
    return payload


def _agent(**over):
    payload = {"present": True, "error": None, "age_sec": 30, "cycle_ok": True}
    payload.update(over)
    return payload


def _by_key(chain):
    return {s["key"]: s for s in chain}


class TestChain:
    def test_a_healthy_box_reports_every_link_up(self):
        chain = sh.build_chain(_containers(), _redis(), _api(), _agent())
        assert [s["state"] for s in chain] == ["ok"] * len(sh.CHAIN_STEPS)

    def test_every_declared_step_renders_whether_or_not_it_failed(self):
        """A row that appears only when it trips teaches the reader that its
        absence means 'fine', when it equally means the check stopped running."""
        chain = sh.build_chain(_containers(), _redis(), _api(), _agent())
        assert [s["key"] for s in chain] == [k for k, _, _, _ in sh.CHAIN_STEPS]
        assert all(s["label"] and s["owner"] for s in chain)

    def test_a_missing_key_blames_the_engine_not_redis(self):
        chain = _by_key(sh.build_chain(
            _containers(),
            _redis(missing=[{"key": "snapshot:tickers", "ops_needs": "Pairs"}]),
            _api(), _agent(),
        ))
        assert chain["redis_container"]["state"] == "ok"
        assert chain["snapshot_keys"]["state"] == "broken"
        assert "SnapshotWriter" in chain["snapshot_keys"]["evidence"]

    def test_key_absence_is_ungraded_when_redis_did_not_answer(self):
        """Do not grade keys we could not have read. An empty census behind a
        dead PING says nothing about the writer, and calling it a writer fault
        would be the original defect with the arrow reversed."""
        chain = _by_key(sh.build_chain(
            _containers(),
            _redis(reachable=False, ping_cause="exec_error",
                   missing=[{"key": "snapshot:tickers", "ops_needs": "Pairs"}]),
            _api(), _agent(),
        ))
        assert chain["redis_container"]["state"] == "broken"
        assert chain["snapshot_keys"]["state"] == "unknown"

    def test_docker_blindness_is_unknown_never_broken(self):
        """Reporting every container absent would be a lie about which thing
        broke — the `docker_ps_unavailable` rule, at the chain."""
        chain = _by_key(sh.build_chain(
            _containers(blind=True, blind_reason="timeout", rows=[]),
            _redis(), _api(), _agent(),
        ))
        for key in ("engine_container", "api_container"):
            assert chain[key]["state"] == "unknown", key

    def test_redis_is_graded_on_its_ping_not_on_what_docker_thinks(self):
        """PING is strictly better evidence than a container status: a
        container can read `Up` with a wedged server inside it. So redis stays
        graded even when docker cannot be asked at all — and the first cut
        computed this step twice and threw the first answer away."""
        chain = _by_key(sh.build_chain(
            _containers(blind=True, blind_reason="timeout", rows=[]),
            _redis(), _api(), _agent(),
        ))
        assert chain["redis_container"]["state"] == "ok"

    def test_a_dead_docker_socket_cannot_convict_redis(self):
        """Every probe here is a `docker exec`. When the DAEMON is unreachable
        the PING fails for a reason that has nothing to do with redis, and
        calling that "redis is down" would be this page committing, one layer
        up, the exact misclassification it exists to end.

        Found by rendering the page rather than by a test: the first cut read
        "Redis reachable — broken" on a box with no docker socket at all.
        """
        chain = _by_key(sh.build_chain(
            _containers(blind=True, blind_reason="exec_error · rc=1 · dial unix", rows=[]),
            _redis(reachable=False, ping_cause="exec_error", census_ran=False,
                   missing=[{"key": "snapshot:tickers", "ops_needs": "Pairs"}]),
            _api(), _agent(),
        ))
        assert chain["redis_container"]["state"] == "unknown"
        assert chain["snapshot_keys"]["state"] == "unknown"
        assert "docker daemon" in chain["redis_container"]["detail"]

    def test_a_failed_ping_carries_dockers_view_as_evidence(self):
        """A stopped container and a host too busy to start a process produce
        the same failed PING and have nothing else in common."""
        containers = _containers()
        containers["rows"][1]["status_text"] = "Exited (137) 4 minutes ago"
        chain = _by_key(sh.build_chain(
            containers, _redis(reachable=False, ping_cause="exec_error"),
            _api(), _agent(),
        ))
        assert chain["redis_container"]["state"] == "broken"
        assert "Exited (137)" in chain["redis_container"]["evidence"]

    def test_a_container_with_no_healthcheck_is_unknown_not_passing(self):
        rows = _containers()["rows"]
        rows[0]["health"] = None
        chain = _by_key(sh.build_chain({"blind": False, "blind_reason": "", "rows": rows},
                                       _redis(), _api(), _agent()))
        assert chain["engine_healthcheck"]["state"] == "unknown"

    def test_starting_is_a_caveat_not_a_failure(self):
        """The engine re-seeds 75 pairs on every boot; unhealthy during the
        480s grace is normal and paging on it trains the owner to ignore it."""
        rows = _containers()["rows"]
        rows[0]["health"] = "starting"
        chain = _by_key(sh.build_chain({"blind": False, "blind_reason": "", "rows": rows},
                                       _redis(), _api(), _agent()))
        assert chain["engine_healthcheck"]["state"] == "degraded"

    def test_the_restart_count_reaches_the_chain(self):
        """The number that separates 'quiet' from 'in an autoheal loop'."""
        rows = _containers()["rows"]
        rows[0]["restart_count"] = 41
        chain = _by_key(sh.build_chain({"blind": False, "blind_reason": "", "rows": rows},
                                       _redis(), _api(), _agent()))
        assert "41" in chain["engine_container"]["detail"]

    def test_a_young_engine_says_so_beside_its_signal_count(self):
        """A young process and a quiet market are identical in a signal count."""
        chain = _by_key(sh.build_chain(
            _containers(), _redis(),
            _api(pulse={"uptime_seconds": 300, "scanning_pairs": 75, "signals_today": 0}),
            _agent(),
        ))
        assert "young process" in chain["engine_working"]["evidence"]


class TestAgentStep:
    def test_a_stale_heartbeat_says_nothing_is_paging(self):
        step = sh.agent_step(_agent(age_sec=sh.AGENT_STALE_SEC + 1))
        assert step["state"] == "broken"
        assert "Nothing is paging" in step["evidence"]

    def test_a_failed_cycle_is_not_a_missed_cycle(self):
        """The dead-man's switch is deliberately not pinged on a failed cycle,
        so on that channel a degraded agent and a dead one share a symptom.
        Here they must not."""
        assert sh.agent_step(_agent(cycle_ok=False))["state"] == "degraded"
        assert sh.agent_step(_agent(cycle_ok=True))["state"] == "ok"

    def test_no_heartbeat_is_unknown_not_broken(self):
        """An agent predating the heartbeat and a stopped one both leave no
        key. Ops cannot tell them apart, so it must not pick one."""
        step = sh.agent_step({"present": False, "error": None, "age_sec": None})
        assert step["state"] == "unknown"

    def test_an_unreadable_store_is_not_a_dead_agent(self):
        step = sh.agent_step({"error": "connection refused", "age_sec": None})
        assert step["state"] == "unknown"
        assert "connection refused" in step["evidence"]


# ---------------------------------------------------------------------------
# The roster, and the probe plumbing
# ---------------------------------------------------------------------------

class TestRoster:
    def test_every_declared_container_carries_a_written_impact(self):
        """`container_down` with no consequence beside it does not tell the
        owner whether to get out of bed."""
        for entry in sh.ROSTER:
            assert entry.impact.strip(), entry.name
            assert entry.role.strip(), entry.name

    def test_the_roster_matches_both_compose_files(self):
        """Derived, not trusted. A container renamed in compose and not here
        would render `absent` forever — a permanent red on the page whose whole
        job is that red meaning something."""
        import pathlib
        import re

        repo = pathlib.Path(__file__).resolve().parents[1]
        declared = {e.name for e in sh.ROSTER}
        found: set[str] = set()
        for compose in (repo / "docker-compose.yml",
                        repo.parent / "360-v2" / "docker-compose.yml"):
            if not compose.exists():
                continue          # the engine repo is not always checked out beside this one
            found |= set(re.findall(r"container_name:\s*(\S+)", compose.read_text()))
        if not found:
            pytest.skip("no compose file available to derive from")
        assert found <= declared, f"running but not on the roster: {sorted(found - declared)}"


class TestProbePlumbing:
    async def test_a_clean_exit_with_no_output_is_its_own_cause(self):
        """The whole defect in one assertion: a command may exit 0 and print
        nothing perfectly legitimately, so that is not an error — but it is not
        `ok` either, and the caller decides what it means."""
        import sys

        result = await sh._run((sys.executable, "-c", "pass"))
        assert result.ok is False
        assert result.cause == "no_output"
        assert result.returncode == 0

    async def test_a_missing_binary_reports_its_own_words(self):
        result = await sh._run(("definitely-not-a-real-binary-360ce",))
        assert result.cause == "exception"
        assert "docker binary not found" in result.detail

    async def test_a_timeout_kills_the_child_rather_than_leaking_it(self):
        """`asyncio.wait_for` cancels the coroutine, not the process — a probe
        that timed out because the host was busy would leave a child running to
        make it busier. Paid for once already in the agent (2026-08-16); this
        module must not re-buy it."""
        import sys

        result = await sh._run(
            (sys.executable, "-c", "import time; time.sleep(30)"), timeout=0.5,
        )
        assert result.cause == "timeout"


class TestHost:
    def test_a_disk_it_cannot_stat_is_named_not_zeroed(self):
        host = sh.collect_host("/definitely/not/a/path/360ce")
        engine = next(d for d in host["disks"] if d["path"] == "/definitely/not/a/path/360ce")
        assert engine["error"]
        assert "used" not in engine, "a disk we could not stat is not a disk with zero bytes"


# ---------------------------------------------------------------------------
# The pages themselves — rendered, not just returned
# ---------------------------------------------------------------------------

@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as c:
        c.post("/login", data={"password": "test-token"}, follow_redirects=False)
        yield c


class TestPagesRender:
    """A test proves the code does what you wrote; it says nothing about
    whether the page reads correctly. Three of the defects in this change were
    found by rendering it and reading the words, so these pin the words."""

    @pytest.mark.parametrize("path", ["/system", "/system/liveness", "/system/redis"])
    def test_it_answers(self, client, path):
        assert client.get(path).status_code == 200

    def test_a_dead_docker_socket_never_prints_no_answer_for_redis(self, client):
        """The defect this test exists for was on the page, not in the model.

        `build_chain` was already blind-aware; the Redis template asked
        `redis.reachable` directly and printed "NO ANSWER — this one IS the
        container" on a box with no docker socket at all. Every probe on that
        page is a `docker exec`, so a dead daemon convicts nothing.

        This test env has no docker socket, which is exactly the state.
        """
        body = client.get("/system/redis").text
        assert "NO ANSWER" not in body
        assert "NOT MEASURED" in body

    def test_the_verdict_agrees_with_the_table_under_it(self, client):
        """A summary computed separately from the table beside it is the defect
        this repo has paid for under three different names. The headline is
        derived from the same chain the table renders — so a link counted
        broken in one must be broken in the other."""
        from app.data_sources import system_health as sh_mod

        body = client.get("/system/liveness").text
        for key, label, _owner, _why in sh_mod.CHAIN_STEPS:
            assert f'id="{key}"' in body, key
            assert label in body, label

    def test_the_guest_tier_can_read_all_three(self):
        """Stated as a test because it is a deliberate tier decision: this is
        the page you most want to be able to hand somebody at 3am, and it must
        not be the one behind the strictest gate. There is no write surface
        here for it to cost anything."""
        from app import guest_scope

        for path in ("/system", "/system/liveness", "/system/redis"):
            assert path in guest_scope.GUEST_READ_ROUTES, path


class TestPartialInspect:
    """`docker inspect a b c` exits 1 when ANY name is missing and still prints
    the ones it found. Losing that stdout turns "one container is gone" — the
    exact state the roster exists to surface — into "we are blind to all nine".
    """

    async def test_a_failed_run_still_carries_what_it_printed(self):
        import sys

        result = await sh._run((
            sys.executable, "-c",
            "import sys; print('{\"Name\": \"/kept\"}'); sys.exit(1)",
        ))
        assert result.ok is False
        assert result.cause == "exec_error"
        assert "kept" in result.stdout, (
            "stdout must survive a non-zero exit — see _inspect_all"
        )

    async def test_a_missing_container_does_not_blind_the_whole_census(self, monkeypatch):
        """Drives the real `_inspect_all` against a fake `_run` shaped like
        docker's actual partial-failure output, rather than a stdout shape
        invented here."""
        async def fake_run(cmd, timeout=sh.PROBE_TIMEOUT_S):
            return sh.ProbeResult(
                ok=False, cause="exec_error", returncode=1,
                stdout='{"Name": "/360scalp-v2-engine", "State": {"Running": true}}\n',
                detail="Error: No such object: 360scalp-v2-watchdog",
            )

        monkeypatch.setattr(sh, "_run", fake_run)
        found, probe = await sh._inspect_all()
        assert "360scalp-v2-engine" in found
        assert probe.cause == "exec_error"


class TestTimestampParsing:
    """Docker stamps RFC3339 with nanoseconds, which `fromisoformat` refuses,
    and the page that must answer during an outage must have no input shape
    that can take it down."""

    @pytest.mark.parametrize("stamp", [
        "2026-08-18T06:35:12.123456789Z",
        "2026-08-18T06:35:12.123456789+00:00",
        "2026-08-18T06:35:12.123456789-05:00",
        "2026-08-18T06:35:12Z",
        "2026-08-18T06:35:12.123456789",      # no zone at all
    ])
    def test_every_shape_yields_an_age_rather_than_raising(self, stamp):
        when = sh._parse_iso(stamp)
        assert when is not None, stamp
        assert when.tzinfo is not None, "naive would TypeError against an aware now()"
        assert isinstance(sh._age_sec(when), float)

    @pytest.mark.parametrize("stamp", [None, "", "0001-01-01T00:00:00Z", "not a date"])
    def test_an_unusable_stamp_is_none_never_an_age_of_zero(self, stamp):
        """`0001-01-01` is Docker's own "never" for FinishedAt. An age computed
        from it would render as two thousand years, which is a number and
        therefore worse than a blank."""
        assert sh._parse_iso(stamp) is None
        assert sh._age_sec(None) is None


class TestRestartCohorts:
    """The first live read showed the number this page led with cannot answer
    the question it was pointed at.

    On the box: engine up 1h48m beside an api, signing and watchdog all up
    5h16m and a redis up three weeks — the engine had plainly restarted on its
    own — and its ``RestartCount`` read **0**. Docker counts restart-*policy*
    restarts there; autoheal issues a manual restart, which does not increment
    it, and a compose recreate starts the count over. Blind to both events
    worth catching.
    """

    @staticmethod
    def _rows(*pairs):
        return [{"name": n, "stack": "engine", "uptime_sec": u} for n, u in pairs]

    def test_a_container_that_restarted_alone_is_flagged(self):
        rows = self._rows(
            ("360scalp-v2-engine", 6480),      # 1h48m
            ("360scalp-v2-api", 18960),        # 5h16m
            ("360scalp-v2-signing", 18962),
            ("360scalp-v2-watchdog", 18965),
        )
        sh._mark_restart_cohorts(rows)
        by_name = {r["name"]: r for r in rows}
        assert by_name["360scalp-v2-engine"].get("restarted_alone") is True
        assert by_name["360scalp-v2-engine"]["deploy_cohort_size"] == 3
        for other in ("360scalp-v2-api", "360scalp-v2-signing", "360scalp-v2-watchdog"):
            assert not by_name[other].get("restarted_alone"), other

    def test_a_container_OLDER_than_the_deploy_is_not_a_restart(self):
        """The false positive the first cut shipped, caught by reading the live
        page an hour later.

        `360scalp-v2-redis` was up 21 days and got badged "went down by itself"
        purely because `autoheal` had been up 25 — it was alone in its bucket
        with an older stack-mate, which was the old rule. On a long-lived stack
        that flags everything except the single oldest container. Redis and
        autoheal are older than the deploy cohort because nothing changed under
        them: that is history, not an event.
        """
        rows = self._rows(
            ("360scalp-v2-engine", 8880),        # 2h28m — after the deploy
            ("360scalp-v2-api", 21300),          # 5h55m ┐
            ("360scalp-v2-signing", 21302),      #        ├ the deploy cohort
            ("360scalp-v2-watchdog", 21305),     # 5h55m ┘
            ("360scalp-v2-redis", 1897200),      # 21d 23h — predates it
            ("360scalp-v2-autoheal", 2160000),   # 25d — predates it
        )
        sh._mark_restart_cohorts(rows)
        by_name = {r["name"]: r for r in rows}
        assert by_name["360scalp-v2-engine"].get("restarted_alone") is True
        for untouched in ("360scalp-v2-redis", "360scalp-v2-autoheal",
                          "360scalp-v2-api", "360scalp-v2-signing",
                          "360scalp-v2-watchdog"):
            assert not by_name[untouched].get("restarted_alone"), untouched

    def test_no_identifiable_deploy_flags_nothing(self):
        """When no two containers share a bucket there is no deploy to date the
        stack against, so this refuses rather than guessing which one is odd."""
        rows = self._rows(("a", 100), ("b", 5000), ("c", 90000))
        sh._mark_restart_cohorts(rows)
        assert not any(r.get("restarted_alone") for r in rows)

    def test_an_ordinary_deploy_flags_nothing(self):
        """`compose up` brings a stack up over a few seconds. Second
        granularity would read that as six independent restarts, which is why
        the bucket is a minute."""
        rows = self._rows(
            ("360scalp-v2-engine", 300),
            ("360scalp-v2-api", 303),
            ("360scalp-v2-signing", 306),
            ("360scalp-v2-watchdog", 310),
        )
        sh._mark_restart_cohorts(rows)
        assert not any(r.get("restarted_alone") for r in rows)

    def test_the_oldest_container_is_never_flagged(self):
        """The longest-running container in a stack is the one thing that
        demonstrably did not restart."""
        rows = self._rows(("a", 100), ("b", 5000), ("c", 5002))
        sh._mark_restart_cohorts(rows)
        by_name = {r["name"]: r for r in rows}
        assert by_name["a"].get("restarted_alone") is True
        assert not by_name["b"].get("restarted_alone")
        assert not by_name["c"].get("restarted_alone")

    def test_stacks_are_not_compared_against_each_other(self):
        """The ops containers redeploy on a different schedule from the
        engine's. Pooling them would flag every ops deploy as an engine fault."""
        rows = [
            {"name": "360scalp-v2-engine", "stack": "engine", "uptime_sec": 18000},
            {"name": "360ce-ops", "stack": "ops", "uptime_sec": 40},
        ]
        sh._mark_restart_cohorts(rows)
        assert not any(r.get("restarted_alone") for r in rows)

    def test_a_container_with_no_uptime_is_skipped_not_flagged(self):
        rows = [{"name": "gone", "stack": "engine", "uptime_sec": None},
                {"name": "up", "stack": "engine", "uptime_sec": 9000}]
        sh._mark_restart_cohorts(rows)
        assert not any(r.get("restarted_alone") for r in rows)


class TestOneFilesystemShownThreeTimes:
    def test_paths_on_one_device_report_once(self):
        """The first live read rendered `35.0 GiB / 144.3 GiB` three times —
        three rows implying three independent headrooms over one volume, where
        the engine filling it takes the audit log and the dashboard with it.
        Matched on the device id, never on the figures happening to be equal."""
        host = sh.collect_host("/")
        assert host["distinct_filesystems"] >= 1
        primary = [d for d in host["disks"] if not d.get("error") and not d.get("shares_with")]
        shared = [d for d in host["disks"] if not d.get("error") and d.get("shares_with")]
        assert len(primary) == host["distinct_filesystems"]
        for d in shared:
            assert d["shares_with"] in {p["label"] for p in primary}
