"""Wrapper for ``docker exec engine python /app/scripts/diag_*.py``.

The ops container has docker.sock mounted and a CLI binary copied in — we
spawn ``docker exec`` against the engine container and capture stdout/stderr.
Accepting only a fixed allow-list of script names and stripping shell
metacharacters from args keeps this from becoming a generic RCE on the host.
Long-term, this should be replaced by an engine-side ``/internal/diag/*``
endpoint and the docker.sock mount removed — tracked in README §Security."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

from app.config import Settings


@dataclass
class DiagResult:
    stdout: str
    stderr: str
    returncode: int


_UNSAFE_CHARS = set(";&|`$<>(){}[]\n\r\"'\\")


class DiagRunner:
    _ALLOWED_SCRIPTS = {"diag_geometry_vs_reality"}

    def __init__(self, settings: Settings) -> None:
        self._container = settings.engine_container_name
        self._timeout = settings.diag_timeout_sec

    @classmethod
    def _safe_arg(cls, value: str) -> bool:
        return not any(ch in _UNSAFE_CHARS for ch in value)

    async def run(self, script: str, args: list[str]) -> DiagResult:
        if script not in self._ALLOWED_SCRIPTS:
            return DiagResult(stdout="", stderr=f"script not allowed: {script}", returncode=2)
        safe_args: list[str] = []
        for a in args:
            if a == "":
                continue
            if not self._safe_arg(a):
                return DiagResult(stdout="", stderr=f"unsafe arg: {a!r}", returncode=2)
            safe_args.append(a)
        cmd = [
            "docker", "exec", self._container,
            "python", f"/app/scripts/{script}.py",
            *safe_args,
        ]
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self._timeout)
            return DiagResult(
                stdout=stdout.decode("utf-8", errors="replace"),
                stderr=stderr.decode("utf-8", errors="replace"),
                returncode=proc.returncode if proc.returncode is not None else 0,
            )
        except asyncio.TimeoutError:
            return DiagResult(stdout="", stderr=f"timeout after {self._timeout}s", returncode=124)
        except FileNotFoundError:
            return DiagResult(stdout="", stderr="docker binary not found in ops container", returncode=127)
