"""Exit-method backtest runner — triggers the engine's large-sample bake-off.

The engine ships ``scripts/exit_method_backtest.py`` (360-v2): it generates ~6
months of historical entries from the price-action strategies over a fixed
universe and replays four exits (engine-baseline / ATR / SuperTrend / Parabolic
SAR) on each, reporting per-regime PF, drop-top-N outlier robustness, and median
vs mean — the checks that decide whether SAR-15m's edge is real or luck.

That run is **minutes** of compute, so this ops-side wrapper is a **background
job**, not a blocking request: ``start`` launches a ``docker exec`` against the
engine container as an asyncio task, the page polls a status partial, and when it
finishes the produced ``signals.csv`` + ``summary.md`` are read back and offered
as downloads.

**State lives on the shared ``ops-data`` volume, not in process memory** — mirrors
``device_registry`` ("read fresh, mutate under a lock"). This is what makes the
POST→redirect→GET cycle work under *any* server topology: if ops runs more than
one worker/process, the request that starts the job and the request that polls it
are different processes, so in-memory state would read back idle ("nothing
happens after clicking Run"). Persisting the single job's state + artifacts to a
file every process reads fixes that, and also survives the ~60s redeploy.

Same safety envelope as the diag runner (``diag_runner.py``): the script name is
fixed, every arg is validated against a metacharacter allow-list, and the pair
list is restricted to ``[A-Z0-9]`` tokens — so this can't become a generic RCE on
the host via the docker.sock mount. Read-only on the engine side (the script only
reads public Binance klines and never touches engine state).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from typing import Any, Optional

from app.config import Settings

logger = logging.getLogger("ops.exit_backtest")

# Same metacharacter denylist the diag runner uses (defence in depth — every arg
# is also numeric or a validated symbol before it reaches here).
_UNSAFE_CHARS = set(";&|`$<>(){}[]\n\r\"'\\ ")

# The engine-side script, by absolute container path (mirrors diag_runner's
# ``/app/scripts/...`` convention — independent of the container's cwd).
_SCRIPT = "/app/scripts/exit_method_backtest.py"
_TF_CHOICES = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h")
# A "running" state older than the timeout plus this slack means the worker that
# owned the job died mid-run (redeploy/crash) — report it rather than spin forever.
_STALE_SLACK_SEC = 180


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

    def to_dict(self) -> dict[str, Any]:
        return {
            "months": self.months, "pairs": self.pairs, "entry_tf": self.entry_tf,
            "exit_tf": self.exit_tf, "period": self.period, "mult": self.mult,
            "sar_step": self.sar_step, "sar_max": self.sar_max,
            "fee_pct": self.fee_pct, "funding_bps": self.funding_bps,
            "lookahead": self.lookahead, "max_forward_bars": self.max_forward_bars,
        }


@dataclass
class JobState:
    """Snapshot of the single in-flight-or-last backtest job (read from disk)."""

    status: str = "idle"        # idle | running | done | error
    params: Optional[dict] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    returncode: Optional[int] = None
    error: str = ""
    stderr_tail: str = ""
    n_signals: Optional[int] = None
    summary_md: str = ""
    has_csv: bool = False

    @property
    def running(self) -> bool:
        return self.status == "running"

    def elapsed_sec(self) -> Optional[int]:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else time.time()
        return max(0, int(end - self.started_at))


class ExitBacktestRunner:
    """Owner-only, single-slot background runner for the engine backtest script.

    State + artifacts persist under ``state_dir`` on the shared ops-data volume,
    so every worker/process reads the same job status and downloads.
    """

    def __init__(self, settings: Settings) -> None:
        self._enabled = settings.exit_backtest_enabled
        self._container = settings.engine_container_name
        self._timeout = settings.exit_backtest_timeout_sec
        self._out_root = settings.exit_backtest_out_root.rstrip("/")
        self._default_pairs = settings.exit_backtest_default_pairs
        self._dir = settings.exit_backtest_state_dir.rstrip("/")
        self._state_path = os.path.join(self._dir, "state.json")
        self._csv_path = os.path.join(self._dir, "signals.csv")
        self._md_path = os.path.join(self._dir, "summary.md")
        self._lock = threading.Lock()
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

    def _writable(self) -> bool:
        """True iff we can actually create + write the state dir (the silent
        failure mode: /data not mounted writable → state never persists → the
        page shows idle after Run)."""
        try:
            os.makedirs(self._dir, exist_ok=True)
            probe = os.path.join(self._dir, ".wtest")
            with open(probe, "w", encoding="utf-8") as fh:
                fh.write("ok")
            os.unlink(probe)
            return True
        except OSError:
            return False

    def diagnostics(self) -> dict[str, Any]:
        """Surfaced on the page so a broken deploy is self-evident."""
        return {
            "engine_container": self._container,
            "state_dir": self._dir,
            "writable": self._writable(),
            "script": _SCRIPT,
            "timeout_sec": self._timeout,
        }

    # --- persistence ------------------------------------------------------- #
    def _read_raw(self) -> dict[str, Any]:
        try:
            with open(self._state_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return {}

    def _write_raw(self, data: dict[str, Any]) -> None:
        try:
            os.makedirs(self._dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, separators=(",", ":"))
                os.replace(tmp, self._state_path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as exc:
            logger.error("exit-backtest state write failed: %s", exc)

    def _write_artifact(self, path: str, text: str) -> None:
        try:
            os.makedirs(self._dir, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=self._dir, suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(text)
                os.replace(tmp, path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        except OSError as exc:
            logger.error("exit-backtest artifact write failed: %s", exc)

    def _read_text(self, path: str) -> str:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    # --- public reads ------------------------------------------------------ #
    def snapshot(self) -> JobState:
        """Fresh job state from disk — visible to any worker/process."""
        raw = self._read_raw()
        st = JobState(
            status=str(raw.get("status") or "idle"),
            params=raw.get("params") if isinstance(raw.get("params"), dict) else None,
            started_at=raw.get("started_at"),
            finished_at=raw.get("finished_at"),
            returncode=raw.get("returncode"),
            error=str(raw.get("error") or ""),
            stderr_tail=str(raw.get("stderr_tail") or ""),
            n_signals=raw.get("n_signals"),
        )
        # A run whose owning process died mid-flight would sit "running" forever;
        # surface it as an error past a generous deadline instead.
        if st.status == "running" and st.started_at is not None:
            if time.time() - st.started_at > self._timeout + _STALE_SLACK_SEC:
                st.status = "error"
                st.error = ("run did not finish — the worker may have restarted "
                            "mid-run. Check engine logs and retry.")
                st.finished_at = st.finished_at or time.time()
        if st.status == "done":
            st.has_csv = os.path.exists(self._csv_path)
            st.summary_md = self._read_text(self._md_path)
        return st

    def read_csv(self) -> Optional[str]:
        text = self._read_text(self._csv_path)
        return text or None

    def read_summary(self) -> Optional[str]:
        text = self._read_text(self._md_path)
        return text or None

    # --- run --------------------------------------------------------------- #
    def start(self, params: ExitBacktestParams) -> tuple[bool, str]:
        """Kick off a run. Refuses if disabled or one is already in flight."""
        if not self._enabled:
            return False, "exit backtest is disabled"
        with self._lock:
            if self.snapshot().running:
                return False, "a backtest is already running"
            if not self._writable():
                return False, (f"cannot persist job state — {self._dir} is not "
                               "writable (ops-data volume). Fix the mount and retry.")
            # Clear stale artifacts and stamp the running state atomically so a
            # concurrent poll (any process) immediately sees "running".
            for p in (self._csv_path, self._md_path):
                try:
                    os.unlink(p)
                except OSError:
                    pass
            self._write_raw({
                "status": "running",
                "params": params.to_dict(),
                "started_at": time.time(),
            })
            # Confirm it actually landed — never return "started" on a silent
            # write failure (that was the invisible-idle bug class).
            if self._read_raw().get("status") != "running":
                return False, "failed to persist job state (write returned no data)"
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

    @staticmethod
    def _has_unsafe(values: list[str]) -> Optional[str]:
        for a in values:
            if any(ch in _UNSAFE_CHARS for ch in a):
                return f"unsafe arg: {a!r}"
        return None

    async def _run(self, params: ExitBacktestParams) -> None:
        out_dir = f"{self._out_root}/ops_{int(time.time())}"
        args = params.to_args() + ["--out-dir", out_dir]
        unsafe = self._has_unsafe(args + [out_dir])
        if unsafe is not None:
            self._finish_error(params, unsafe)
            return
        cmd = ["docker", "exec", self._container, "python", _SCRIPT, *args]
        try:
            rc, stdout, stderr = await self._exec(cmd, self._timeout)
        except asyncio.TimeoutError:
            self._finish_error(params, f"timeout after {self._timeout}s")
            return
        except FileNotFoundError:
            self._finish_error(params, "docker binary not found in ops container")
            return
        except Exception as exc:  # noqa: BLE001 — surface, never hang on "running"
            self._finish_error(params, f"run failed: {exc}")
            return

        if rc != 0:
            self._finish_error(params, stderr.strip() or f"script exited rc={rc}",
                               returncode=rc, stderr=stderr)
            return

        csv_text = await self._cat(f"{out_dir}/signals.csv")
        summary_md = await self._cat(f"{out_dir}/summary.md")
        if csv_text:
            self._write_artifact(self._csv_path, csv_text)
        if summary_md:
            self._write_artifact(self._md_path, summary_md)
        n_signals = max(0, csv_text.strip().count("\n")) if csv_text else None
        started = self.snapshot().started_at
        self._write_raw({
            "status": "done",
            "params": params.to_dict(),
            "started_at": started,
            "finished_at": time.time(),
            "returncode": 0,
            "n_signals": n_signals,
        })

    async def _cat(self, path: str) -> Optional[str]:
        if any(ch in _UNSAFE_CHARS for ch in path):
            return None
        try:
            rc, out, _ = await self._exec(
                ["docker", "exec", self._container, "cat", path], self._timeout,
            )
        except (asyncio.TimeoutError, FileNotFoundError, Exception):  # noqa: BLE001
            return None
        return out if rc == 0 else None

    def _finish_error(self, params: ExitBacktestParams, message: str, *,
                      returncode: Optional[int] = None, stderr: str = "") -> None:
        started = self.snapshot().started_at
        self._write_raw({
            "status": "error",
            "params": params.to_dict(),
            "started_at": started,
            "finished_at": time.time(),
            "returncode": returncode,
            "error": message,
            "stderr_tail": stderr[-2000:] if stderr else "",
        })
