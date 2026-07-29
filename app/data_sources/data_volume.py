"""Read-only JSON loaders for the engine's ``data/`` directory.

The engine VPS mounts ``/opt/engine/data`` into this container at
``/engine-data``. We never write back."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.config import Settings

#: Version of the SAR shadow ledger this dashboard reads. Mirrors the engine's
#: ``sar_exit_shadow._DEFAULT_PATH``.
#:
#: The engine restarts this ledger on a **new path** whenever a defect makes the
#: existing rows evidence of a bug rather than of an exit method — v1→v2 for the
#: wrong-candle replay (#800), v2→v3 for the wrong trail fill (#822). The old
#: file is deliberately left on disk for forensics and never read again.
#:
#: Ops missed the v3 bump. #822 landed 2026-07-29 00:12 IST; ops kept reading
#: v2 for the next nine hours, so `/signals/sar` rendered a complete, confident
#: page built entirely from the population #822 had just ruled untrustworthy —
#: and the owner's "Clear SAR ledger" button appeared broken, because it
#: correctly cleared v3 while the page re-read an orphan nothing writes.
#:
#: **An orphaned file is worse than a missing one.** A missing path surfaces as
#: an error the page shows; an orphan renders as data. So the bump is not enough
#: on its own — :meth:`DataVolumeReader.sar_ledger_provenance` watches for a
#: *higher* version appearing on the volume and makes the next drift loud.
SAR_LEDGER_VERSION = 3
SAR_LEDGER_FILE = f"sar_exit_candidates_v{SAR_LEDGER_VERSION}.json"

_SAR_LEDGER_RE = re.compile(r"^sar_exit_candidates_v(\d+)\.json$")


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

        **The path is versioned and the engine bumps it** — see
        :data:`SAR_LEDGER_FILE`. v1→v2 (#800) for a walker that replayed the
        wrong candle; v2→v3 (#822) for a trail fill taken at the reversal bar's
        open instead of the stop actually breached. Each bump abandons the old
        file on disk, so reading a stale one keeps a discredited population on
        screen while looking perfectly healthy.

        Pair every read with :meth:`sar_ledger_provenance` and render what it
        says: which file this is, when it was last written, and whether the
        engine has already moved past it."""
        return self._load(SAR_LEDGER_FILE)

    def sar_ledger_provenance(self) -> dict[str, Any]:
        """Which SAR ledger file the page is reading, and whether it is current.

        Answers three questions the page must not leave to assumption:

        * **Which file** — named on screen, because "the ledger" is ambiguous
          once versions exist.
        * **When was it last written** — an mtime hours old on a ledger that is
          supposed to be stamping is the "stale" symptom stated as a fact
          rather than inferred from unchanging numbers.
        * **Has the engine moved on** — a *higher* version present on the volume
          means this dashboard is reading an orphan, which is exactly the
          failure that went unnoticed for nine hours. Detected by looking at
          what is actually on disk rather than by mirroring the engine's
          constant a second time: the fix for a drifting mirror is not another
          mirror, it is a check that the two ends still agree.

        Never raises — a provenance read that fails must not take the page with
        it, so an unreadable directory reports what it can and says the rest is
        unknown.
        """
        info: dict[str, Any] = {
            "file": SAR_LEDGER_FILE,
            "version": SAR_LEDGER_VERSION,
            "exists": False,
            "modified_at": None,
            "newer_version": None,
            "newer_file": None,
        }
        try:
            path = self._dir / SAR_LEDGER_FILE
            if path.exists():
                info["exists"] = True
                info["modified_at"] = datetime.fromtimestamp(
                    path.stat().st_mtime, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
            newest = SAR_LEDGER_VERSION
            for entry in self._dir.iterdir():
                m = _SAR_LEDGER_RE.match(entry.name)
                if m and int(m.group(1)) > newest:
                    newest = int(m.group(1))
            if newest > SAR_LEDGER_VERSION:
                info["newer_version"] = newest
                info["newer_file"] = f"sar_exit_candidates_v{newest}.json"
        except OSError:
            pass
        return info

    def emission_controller(self) -> Any:
        """Layer G's state, ledger and routability measurement (engine:
        ``src/emission_controller_store.py``).

        Keys: ``state`` (per-strategy overrides + verdict history),
        ``ledger`` (durable audit of *applied* adjustments), ``pending``
        (this cycle's would-be candidates), ``active_overrides``, and
        ``routability`` — the live measurement of how much of the controller's
        promotion budget goes to strategy keys the emission policy cannot read.

        Every row carries a ``routable`` flag **stamped by the engine**. Ops
        renders it and never re-derives it from a mirrored suffix list: that
        mirror is the drift class which silently inflated the Strategy Lab
        rollup for a week."""
        return self._load("emission_controller_store.json")

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
