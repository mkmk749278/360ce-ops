"""Exit-method backtest runner — triggers the engine's large-sample bake-off.

The engine ships ``scripts/exit_method_backtest.py`` (360-v2): it generates ~6
months of historical entries from the price-action strategies over a fixed
universe and replays four exits (engine-baseline / ATR / SuperTrend / Parabolic
SAR) on each, reporting per-regime PF, drop-top-N outlier robustness, and median
vs mean — the checks that decide whether SAR-15m's edge is real or luck.

That run is **minutes** of compute over the default universe, so this ops-side
wrapper is a **background job**, not a blocking request: ``start`` launches a
``docker exec`` against the engine container as an asyncio task, the page polls a
status partial, and when it finishes the produced ``signals.csv`` + ``summary.md``
are read back (``docker exec cat``) and offered as downloads.

Same safety envelope as the diag runner (``diag_runner.py``): the script name is
fixed, every arg is validated against a metacharacter allow-list, and the pair
list is restricted to ``[A-Z0-9]`` tokens — so this can't become a generic RCE on
the host via the docker.sock mount. Read-only on the engine side (the script only
reads public Binance klines and never touches engine state).
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Optional

from app.config import Settings

# Same metacharacter denylist the diag runner uses (defence in depth — every arg
# is also numeric or a validated symbol before it reaches here).
_UNSAFE_CHARS = set(";&|`$<>(){}[]\n\r\"'\\ ")

# The engine-side script, by absolute container path (mirrors diag_runner's
# ``/app/scripts/...`` convention — independent of the container's cwd).
_SCRIPT = "/app/scripts/exit_method_backtest.py"
_TF_CHOICES = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h")


def _safe_symbol(sym: str) -> Optional[str]:
    """Upper-case a symbol iff it is a pure ``[A-Z0-9]`` token, else drop it."""
    s = sym.strip().upper()
    if s and all(c.isalnum() for c in s) and s.isascii():
        return s
    return None


@dataclass(frozen=True)
class ExitBacktestParams:
    """Validated, clamped knobs for one backtest run → CLI args. Pure/testable."""

    months: float = 6.0
    pairs: str = ""            # comma-separated; "" → the script's fixed universe
    entry_tf: str = "5m"
    exit_tf: str = "15m"
    period: int = 10
    mult: float = 3.0
    sar_step: float = 0.02
    sar_max: float = 0.2
    fee_pct: float = 0.07
    funding_bps: float = 1.0
    lookahead: int = 20
    max_forward_bars: int = 192

    @classmethod
    def clamped(
        cls,
        *,
        months: float,
        pairs: str,
        entry_tf: str,
        exit_tf: str,
        period: int,
        mult: float,
        sar_step: float,
        sar_max: float,
        fee_pct: float,
        funding_bps: float,
        lookahead: int,
        max_forward_bars: int,
    ) -> "ExitBacktestParams":
        clean_pairs = ",".join(
            p for p in (_safe_symbol(x) for x in pairs.split(",")) if p
        )
        step = min(0.2, max(0.001, sar_step))
        return cls(
            months=min(24.0, max(0.1, months)),
            pairs=clean_pairs,
            entry_tf=entry_tf if entry_tf in _TF_CHOICES else "5m",
            exit_tf=exit_tf if exit_tf in _TF_CHOICES else "15m",
            period=min(100, max(2, period)),
            mult=min(20.0, max(0.1, mult)),
            sar_step=step,
            sar_max=min(1.0, max(step, sar_max)),
            fee_pct=min(2.0, max(0.0, fee_pct)),
            funding_bps=min(100.0, max(0.0, funding_bps)),
            lookahead=min(200, max(2, lookahead)),
            max_forward_bars=min(5000, max(0, max_forward_bars)),
        )

    def to_args(self) -> list[str]:
        args = [
            "--months", f"{self.months:g}",
            "--entry-tf", self.entry_tf,
            "--exit-tf", self.exit_tf,
            "--period", str(self.period),
            "--mult", f"{self.mult:g}",
            "--sar-step", f"{self.sar_step:g}",
            "--sar-max", f"{self.sar_max:g}",
            "--fee-pct", f"{self.fee_pct:g}",
            "--funding-bps", f"{self.funding_bps:g}",
            "--lookahead", str(self.lookahead),
            "--max-forward-bars", str(self.max_forward_bars),
        ]
        if self.pairs:
            args += ["--pairs", self.pairs]
        return args


@dataclass
class JobState:
    """Snapshot of the single in-flight-or-last backtest job."""

    status: str = "idle"        # idle | running | done | error
    params: Optional[ExitBacktestParams] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    returncode: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    error: str = ""
    summary_md: str = ""
    csv_text: str = ""
    n_signals: Optional[int] = None

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def has_csv(self) -> bool:
        return bool(self.csv_text)

    def elapsed_sec(self) -> Optional[int]:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0, int(end - self.started_at))


class ExitBacktestRunner:
    """Owner-only, single-slot background runner for the engine backtest script."""

    def __init__(self, settings: Settings) -> None:
        self._enabled = settings.exit_backtest_enabled
        self._container = settings.engine_container_name
        self._timeout = settings.exit_backtest_timeout_sec
        self._out_root = settings.exit_backtest_out_root.rstrip("/")
        self._default_pairs = settings.exit_backtest_default_pairs
        self._state = JobState()
        self._task: Optional[asyncio.Task] = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def default_pairs(self) -> str:
        return self._default_pairs

    @property
    def timeout_sec(self) -> int:
        return self._timeout

    def snapshot(self) -> JobState:
        return self._state

    def start(self, params: ExitBacktestParams) -> tuple[bool, str]:
        """Kick off a run. Refuses if disabled or one is already in flight."""
        if not self._enabled:
            return False, "exit backtest is disabled"
        if self._state.running:
            return False, "a backtest is already running"
        self._state = JobState(
            status="running", params=params, started_at=time.time(),
        )
        self._task = asyncio.create_task(self._run(params))
        return True, "started"

    async def _exec(self, cmd: list[str], timeout: float) -> tuple[int, str, str]:
        """Run a command, capturing stdout/stderr. Isolated for testability."""
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise
        rc = proc.returncode if proc.returncode is not None else 0
        return rc, out.decode("utf-8", errors="replace"), err.decode("utf-8", errors="replace")

    def _validate_args(self, args: list[str]) -> Optional[str]:
        for a in args:
            if any(ch in _UNSAFE_CHARS for ch in a):
                return f"unsafe arg: {a!r}"
        return None

    async def _run(self, params: ExitBacktestParams) -> None:
        out_dir = f"{self._out_root}/ops_{int(time.time())}"
        args = params.to_args() + ["--out-dir", out_dir]
        unsafe = self._validate_args(args + [out_dir])
        if unsafe is not None:
            self._finish_error(unsafe)
            return
        cmd = ["docker", "exec", self._container, "python", _SCRIPT, *args]
        try:
            rc, stdout, stderr = await self._exec(cmd, self._timeout)
        except asyncio.TimeoutError:
            self._finish_error(f"timeout after {self._timeout}s")
            return
        except FileNotFoundError:
            self._finish_error("docker binary not found in ops container")
            return

        self._state.returncode = rc
        self._state.stdout = stdout
        self._state.stderr = stderr
        if rc != 0:
            self._finish_error(stderr.strip() or f"script exited rc={rc}")
            return

        # Read the artifacts back out of the engine container for download.
        csv_text = await self._cat(f"{out_dir}/signals.csv")
        summary_md = await self._cat(f"{out_dir}/summary.md")
        self._state.csv_text = csv_text or ""
        self._state.summary_md = summary_md or ""
        if csv_text:
            # Header + rows → data row count.
            self._state.n_signals = max(0, csv_text.strip().count("\n"))
        self._state.status = "done"
        self._state.finished_at = time.time()

    async def _cat(self, path: str) -> Optional[str]:
        if any(ch in _UNSAFE_CHARS for ch in path):
            return None
        try:
            rc, out, _ = await self._exec(
                ["docker", "exec", self._container, "cat", path], self._timeout,
            )
        except (asyncio.TimeoutError, FileNotFoundError):
            return None
        return out if rc == 0 else None

    def _finish_error(self, message: str) -> None:
        self._state.status = "error"
        self._state.error = message
        self._state.finished_at = time.time()
