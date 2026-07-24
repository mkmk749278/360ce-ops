"""Exit-method backtest runner + route.

Covers the safety envelope (unsafe pairs dropped, args validated, single-slot),
the docker-exec success/error paths (mocked — no docker), the **cross-process
state** fix (a second runner instance sees the first's job — the reason a POST
then redirect-GET on different workers used to read back idle), and the ops route
(trigger, poll partial, downloads). Mirrors the diag-runner test discipline.
"""
from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.data_sources.exit_backtest import (  # noqa: E402
    ExitBacktestParams,
    ExitBacktestRunner,
)
from app.main import app  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


def _runner() -> ExitBacktestRunner:
    # Fresh state dir per runner so tests don't bleed state into each other.
    settings = load_settings()
    import tempfile
    object.__setattr__(settings, "exit_backtest_state_dir", tempfile.mkdtemp())
    r = ExitBacktestRunner(settings)
    return r


def _params() -> ExitBacktestParams:
    return ExitBacktestParams.clamped(
        months=1, pairs="BTCUSDT", entry_tf="15m", exit_tf="15m", period=10,
        mult=3, sar_step=0.02, sar_max=0.2, fee_pct=0.07, funding_bps=1,
        lookahead=20, max_forward_bars=192)


# --------------------------------------------------------------------------- #
# Params — validation / clamping / arg building (pure)
# --------------------------------------------------------------------------- #
def test_params_drop_unsafe_pairs_and_uppercase():
    p = ExitBacktestParams.clamped(
        months=6, pairs="btcusdt, eth-usdt , SOL;rm, ok123",
        entry_tf="5m", exit_tf="15m", period=10, mult=3.0, sar_step=0.02,
        sar_max=0.2, fee_pct=0.07, funding_bps=1.0, lookahead=20,
        max_forward_bars=192)
    # eth-usdt (hyphen) and SOL;rm (semicolon) are rejected; the rest survive.
    assert p.pairs == "BTCUSDT,OK123"


def test_params_clamp_out_of_range():
    p = ExitBacktestParams.clamped(
        months=999, pairs="", entry_tf="bogus", exit_tf="15m", period=1,
        mult=999, sar_step=0.0, sar_max=0.0, fee_pct=9, funding_bps=999,
        lookahead=1, max_forward_bars=-5)
    assert p.months == 24.0
    assert p.entry_tf == "5m"           # bogus TF -> default
    assert p.period == 2
    assert p.sar_step == 0.001
    assert p.sar_max >= p.sar_step
    assert p.max_forward_bars == 0


def test_params_to_args_includes_pairs_only_when_set():
    args = _params().to_args()
    assert "--months" in args and "1" in args
    assert args[args.index("--pairs") + 1] == "BTCUSDT"

    noargs = ExitBacktestParams.clamped(
        months=1, pairs="", entry_tf="15m", exit_tf="15m", period=10, mult=3,
        sar_step=0.02, sar_max=0.2, fee_pct=0.07, funding_bps=1, lookahead=20,
        max_forward_bars=192).to_args()
    assert "--pairs" not in noargs


# --------------------------------------------------------------------------- #
# Runner — single slot, disabled guard, exec success/error (mocked)
# --------------------------------------------------------------------------- #
def test_disabled_runner_refuses():
    r = _runner()
    r._enabled = False
    ok, msg = r.start(_params())
    assert ok is False and "disabled" in msg


def test_runner_rejects_concurrent_run():
    r = _runner()
    # A running job is on disk (as if another worker started it).
    r._write_raw({"status": "running", "started_at": 9e18})
    ok, msg = r.start(_params())
    assert ok is False and "already running" in msg


async def test_run_success_reads_artifacts():
    r = _runner()
    # 1st exec = the script (rc 0), then two cats (signals.csv, summary.md).
    r._exec = AsyncMock(side_effect=[
        (0, "ran ok\n", ""),
        (0, "timestamp,symbol,pnl_engine\na,BTCUSDT,1.2\nb,ETHUSDT,-0.3\n", ""),
        (0, "# Exit-method bake-off\n\nPF stuff\n", ""),
    ])
    await r._run(_params())
    st = r.snapshot()
    assert st.status == "done"
    assert st.n_signals == 2                 # 2 data rows (header excluded)
    assert st.has_csv and "BTCUSDT" in (r.read_csv() or "")
    assert "bake-off" in st.summary_md


async def test_run_nonzero_rc_is_error():
    r = _runner()
    r._exec = AsyncMock(return_value=(1, "", "boom: no numpy"))
    await r._run(_params())
    st = r.snapshot()
    assert st.status == "error"
    assert "boom" in st.error


async def test_run_rejects_unsafe_out_root():
    r = _runner()
    r._out_root = "/app/scripts/out; rm -rf /"   # metacharacters
    r._exec = AsyncMock()
    await r._run(_params())
    assert r.snapshot().status == "error"
    r._exec.assert_not_awaited()                 # never reached docker exec


def test_start_refuses_when_state_dir_not_writable():
    r = _runner()
    # Point the state dir at a path *under a regular file* so makedirs fails —
    # this is the silent "nothing happens" mode we now surface loudly.
    import tempfile
    fd, blocker = tempfile.mkstemp()
    os.close(fd)
    r._dir = os.path.join(blocker, "sub")
    r._state_path = os.path.join(r._dir, "state.json")
    ok, msg = r.start(_params())
    assert ok is False and "not" in msg.lower() and "writable" in msg.lower()
    assert r.diagnostics()["writable"] is False


def test_diagnostics_and_status_render_on_page():
    with TestClient(app) as client:
        _login(client)
        r = client.get("/exit-backtest")
        assert r.status_code == 200
        assert "state dir" in r.text and "engine container" in r.text
        assert "IDLE" in r.text                      # prominent status badge


def test_flash_message_renders():
    with TestClient(app) as client:
        _login(client)
        r = client.get("/exit-backtest?flash=a+backtest+is+already+running&ok=0")
        assert r.status_code == 200
        assert "already running" in r.text


async def test_state_is_shared_across_runner_instances():
    """The bug fix: a POST that starts a job on one worker must be visible to a
    poll served by a *different* worker. Two runner instances over the same state
    dir stand in for two workers."""
    settings = load_settings()
    import tempfile
    shared_dir = tempfile.mkdtemp()
    object.__setattr__(settings, "exit_backtest_state_dir", shared_dir)

    worker_a = ExitBacktestRunner(settings)
    worker_b = ExitBacktestRunner(settings)

    started = asyncio.Event()
    release = asyncio.Event()

    async def blocking_exec(cmd, timeout):
        started.set()
        await release.wait()          # keep the job "running"
        return (0, "", "")

    worker_a._exec = blocking_exec
    ok, _ = worker_a.start(_params())
    assert ok is True
    await started.wait()
    # Worker B never called start(), yet sees A's running job.
    assert worker_b.snapshot().running is True
    release.set()
    await worker_a._task


# --------------------------------------------------------------------------- #
# Route — page, trigger (PRG), status partial, downloads
# --------------------------------------------------------------------------- #
def test_page_renders_form():
    with TestClient(app) as client:
        _login(client)
        r = client.get("/exit-backtest")
        assert r.status_code == 200
        assert "/exit-backtest/run" in r.text
        assert "Exit-method backtest" in r.text
        assert r.headers.get("cache-control") == "no-store"


def test_run_triggers_and_redirects():
    with TestClient(app) as client:
        _login(client)
        fake = MagicMock()
        fake.start.return_value = (True, "started")
        fake.snapshot.return_value = _idle_running()
        fake.default_pairs = ""
        fake.enabled = True
        app.state.exit_backtest = fake
        r = client.post("/exit-backtest/run",
                        data={"months": 6, "entry_tf": "5m", "exit_tf": "15m"},
                        follow_redirects=False)
        assert r.status_code == 303
        fake.start.assert_called_once()


def test_htmx_run_returns_status_inline():
    """HTMX submit returns the status partial from the same request — instant
    inline confirmation, no redirect dependency."""
    with TestClient(app) as client:
        _login(client)
        fake = MagicMock()
        fake.start.return_value = (True, "started")
        fake.snapshot.return_value = _idle_running()
        app.state.exit_backtest = fake
        r = client.post("/exit-backtest/run",
                        data={"months": 6, "entry_tf": "5m", "exit_tf": "15m"},
                        headers={"HX-Request": "true"})
        assert r.status_code == 200
        assert "RUNNING" in r.text          # status swapped in from the POST itself
        assert "started" in r.text          # flash confirmation
        fake.start.assert_called_once()


def test_run_now_get_fallback_starts_and_redirects():
    """The plain-link fallback (GET) must start the job and redirect with flash."""
    with TestClient(app) as client:
        _login(client)
        fake = MagicMock()
        fake.start.return_value = (True, "started")
        app.state.exit_backtest = fake
        r = client.get("/exit-backtest/run-now", follow_redirects=False)
        assert r.status_code == 303
        assert "flash=started" in r.headers["location"]
        fake.start.assert_called_once()


def test_status_partial_shows_done_with_downloads():
    with TestClient(app) as client:
        _login(client)
        fake = MagicMock()
        fake.snapshot.return_value = _done_state()
        app.state.exit_backtest = fake
        r = client.get("/_partial/exit-backtest/status")
        assert r.status_code == 200
        assert "/exit-backtest/download.csv" in r.text
        assert "summary" in r.text


def test_download_csv_when_present():
    with TestClient(app) as client:
        _login(client)
        fake = MagicMock()
        fake.read_csv.return_value = "h\n1\n"
        app.state.exit_backtest = fake
        r = client.get("/exit-backtest/download.csv")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        assert "attachment" in r.headers["content-disposition"]


def test_download_csv_redirects_when_absent():
    with TestClient(app) as client:
        _login(client)
        fake = MagicMock()
        fake.read_csv.return_value = None
        app.state.exit_backtest = fake
        r = client.get("/exit-backtest/download.csv", follow_redirects=False)
        assert r.status_code == 303


# --- tiny state builders for the route tests ------------------------------- #
def _idle_running():
    from app.data_sources.exit_backtest import JobState
    return JobState(status="running", started_at=0.0)


def _done_state():
    from app.data_sources.exit_backtest import JobState
    return JobState(status="done", summary_md="# summary\nPF 1.2\n",
                    n_signals=1, has_csv=True, started_at=0.0, finished_at=5.0)
