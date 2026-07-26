"""Read-only JSON loaders for the engine's ``data/`` directory.

The engine VPS mounts ``/opt/engine/data`` into this container at
``/engine-data``. We never write back."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.config import Settings


class DataVolumeReader:
    def __init__(self, settings: Settings) -> None:
        self._dir = Path(settings.engine_data_dir)

    def _load(self, name: str) -> Any:
        path = self._dir / name
        if not path.exists():
            return {"error": f"missing: {path}"}
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            return {"error": f"parse: {exc}"}
        except OSError as exc:
            return {"error": f"read: {exc}"}

    def signal_performance(self) -> Any:
        return self._load("signal_performance.json")

    def invalidation_records(self) -> Any:
        return self._load("invalidation_records.json")

    def signal_history(self) -> Any:
        return self._load("signal_history.json")

    # -- Strategy Lab artifacts (autonomous-portfolio measurement layer) --

    def market_context(self) -> Any:
        return self._load("market_context.json")

    def suppressed_candidates(self) -> Any:
        return self._load("suppressed_candidates.json")

    def strategy_edge(self) -> Any:
        return self._load("strategy_edge_store.json")

    def strategy_allocations(self) -> Any:
        return self._load("strategy_allocations.json")

    def sar_exit_candidates(self) -> Any:
        """The SAR exit A/B pair ledger (engine: ``src/sar_exit_shadow.py``).

        A flat list of stamped records, two per candidate: ``X@SARBASE`` (the
        live evaluator geometry) and ``X@SAREXIT`` (the same entry exited by a
        trailing 15m Parabolic SAR). Its own ledger, separate from
        ``suppressed_candidates.json``, so A/B volume can never evict gate
        records. Empty/missing until the owner enables the dark flag.

        **v2 path (2026-07-26).** The v1 file was resolved by a walker that
        located the entry bar by counting elapsed time, so every row replayed a
        different candle than the trade — exit price came out a pure function
        of (symbol, side). Those rows are evidence of a bug, not of an exit
        method, so the engine restarted the ledger on a new path and ops must
        follow it; reading v1 would keep the fabricated numbers on screen."""
        return self._load("sar_exit_candidates_v2.json")

    def feature_liveness(self) -> Any:
        """The engine's feature-liveness manifest (2026-07-14 incident
        response): per-feature output-vs-upstream verdicts, sustained-streak
        alerts, and fail-open exception counters, republished every 5-min
        audit cycle."""
        return self._load("feature_liveness.json")

    # ------------------------------------------------------------------
    # Raw-file surface (2026-07-10 ops upgrade): the curated exports above
    # re-serialize parsed JSON; these expose the volume's actual files so
    # the operator can pull ANY artifact (paper books, watchdog audit,
    # cohort store…) without SSH. Still read-only by construction.
    # ------------------------------------------------------------------

    _LIST_MAX_DEPTH = 2  # data/ + one subdir level (paper_books/, …)

    def list_files(self) -> list[dict[str, Any]]:
        """Every regular file on the volume (bounded depth), newest first.

        Errors (unmounted volume in dev) come back as an empty list — the
        page renders a hint instead of crashing.
        """
        out: list[dict[str, Any]] = []
        try:
            base = self._dir.resolve()
            if not base.is_dir():
                return out
            stack: list[tuple[Path, int]] = [(base, 0)]
            while stack:
                current, depth = stack.pop()
                for child in sorted(current.iterdir()):
                    if child.name.startswith("."):
                        continue
                    if child.is_dir():
                        if depth + 1 < self._LIST_MAX_DEPTH:
                            stack.append((child, depth + 1))
                        continue
                    try:
                        st = child.stat()
                    except OSError:
                        continue
                    out.append(
                        {
                            "rel_path": str(child.relative_to(base)),
                            "size_bytes": st.st_size,
                            "mtime": st.st_mtime,
                        }
                    )
        except OSError:
            return []
        out.sort(key=lambda f: f["mtime"], reverse=True)
        return out

    def resolve_safe(self, rel_path: str) -> Path | None:
        """Resolve ``rel_path`` strictly inside the volume, or None.

        The one guard that makes a raw-download route safe: symlinks and
        ``..`` are resolved BEFORE the containment check, so nothing outside
        the read-only mount is ever served.
        """
        try:
            base = self._dir.resolve()
            candidate = (base / rel_path).resolve()
            if not candidate.is_relative_to(base):
                return None
            if not candidate.is_file():
                return None
            return candidate
        except OSError:
            return None
