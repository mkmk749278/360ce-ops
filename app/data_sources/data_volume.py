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

#: The **live** SAR mechanism's arm ledger (engine ``src/sar_live_shadow.py``,
#: ``SarLiveLedger.DEFAULT_PATH``). Versioned in the filename for the same
#: reason the replay ledger is: a schema bump gets a new file rather than
#: reinterpreting rows written under different rules.
SAR_LIVE_VERSION = 1
SAR_LIVE_FILE = f"sar_live_arms_v{SAR_LIVE_VERSION}.json"

#: Dark emission lane (engine ``src/dark_emission.py``). Signals from the paths
#: the gates normally silence, emitted for real and diverted before the queue.
DARK_EMISSION_VERSION = 1
DARK_EMISSION_FILE = f"dark_signals_live_v{DARK_EMISSION_VERSION}.json"

#: SAR exit arms opened on DARK rows — deliberately a different file from
#: ``sar_live_arms_v1.json``. That one is the population ``/signals/sar-live``
#: presents as the evidence for adopting SAR on the money path, and every arm in
#: it belongs to a signal a subscriber received. These belong to signals nobody
#: received. Reading them from one file would silently merge the two, and this
#: page could not un-merge them afterwards.
DARK_SAR_VERSION = 1
DARK_SAR_FILE = f"dark_sar_arms_v{DARK_SAR_VERSION}.json"

#: MVRTP entry-feature stamps (engine ``src/entry_features.py``). What CVD,
#: book depth, funding, the level book and pullback shape said at the moment
#: each MOVER_TREND_PULLBACK signal was created — inputs that path has never
#: read. Stamps only: this file carries no outcomes, because outcomes come from
#: ``signal_performance.json`` joined on ``signal_id``. Deliberately no second
#: forward-resolver anywhere in this lane.
ENTRY_FEATURES_VERSION = 1
ENTRY_FEATURES_FILE = f"entry_features_v{ENTRY_FEATURES_VERSION}.json"

#: Structural SL/TP1 snap stamps (engine ``src/structural_snap.py``). Where the
#: nearest swing high/low or round number sat relative to the stop and TP1 each
#: signal actually shipped with. Stamps only — outcomes join from
#: ``signal_performance.json`` on ``signal_id``, so this lane grows no resolver
#: of its own and inherits the closed-signal record's correctness.
STRUCTURAL_SNAP_VERSION = 1
STRUCTURAL_SNAP_FILE = f"structural_snap_v{STRUCTURAL_SNAP_VERSION}.json"
STRUCTURAL_VETO_VERSION = 1
STRUCTURAL_VETO_FILE = f"structural_veto_v{STRUCTURAL_VETO_VERSION}.json"

_SAR_LIVE_RE = re.compile(r"^sar_live_arms_v(\d+)\.json$")


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

    def entry_features(self) -> Any:
        """MVRTP entry-time feature stamps (engine ``src/entry_features.py``).

        Shape::

            {"schema": 1, "written_at": <epoch>, "rows": [<stamp>, ...]}

        Each row is one MOVER_TREND_PULLBACK signal at creation, carrying the
        inputs the evaluator ignores. **No outcome fields** — those are joined
        from ``signal_performance()`` on ``signal_id``, so this lane inherits
        the closed-signal record's correctness (including the #848 entry-risk
        denominator) instead of growing a resolver of its own.
        """
        return self._load(ENTRY_FEATURES_FILE)

    def structural_snap(self) -> Any:
        """Structural SL/TP1 snap stamps (engine ``src/structural_snap.py``).

        Shape::

            {"schema": 1, "written_at": <epoch>, "max_rows": N,
             "evicted": N, "counters": {...}, "spec": {...},
             "rows": [<stamp>, ...]}

        One row per enqueued signal, carrying the arithmetic stop/TP1 the
        evaluator produced beside the levels a structural snap would have used,
        plus which generator supplied each level.

        ``evicted`` and ``max_rows`` ride with the data deliberately: the ring
        is capped, so every rate computed on it is a sample, and a reader in
        this process cannot otherwise see the cap. A verdict printed without
        its denominator reads as if it covered everything.

        **No outcome fields.** Those join from :meth:`signal_performance` on
        ``signal_id``.
        """
        return self._load(STRUCTURAL_SNAP_FILE)

    def structural_veto(self) -> Any:
        """Structural-veto stamps (engine ``src/structural_veto.py``, Phase 4).

        Shape::

            {"schema": 1, "written_at": <epoch>, "counters": {...},
             "retention": {...}, "rows": [<stamp>, ...]}

        One row per enqueued signal: how far the nearest **opposing** level sits
        from entry (ATR and percent), whether it falls between entry and TP1,
        that level's own score and age, and where price sits against the value
        area.

        **No outcome fields.** They join from :meth:`signal_performance` on
        ``signal_id``, so this lane inherits the closed-signal record's
        correctness instead of growing a resolver of its own.

        ``retention`` rides with the data because the ring is capped: it names
        `evicted_pending` (designed rotation) apart from `evicted_delivered`
        (the retention policy losing a confirmed row), and those are different
        events with different fixes.
        """
        return self._load(STRUCTURAL_VETO_FILE)

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

    # -- Live SAR exit mechanism (engine: src/sar_live_shadow.py) --------- #

    def sar_live_arms(self) -> Any:
        """The live SAR mechanism's arms — open and resolved.

        Engine writes ``data/sar_live_arms_v1.json`` from inside the monitor
        loop; the row keys are pinned on the producing side by
        ``tests/test_sar_live_contract.py`` there. Shape::

            {"schema": 1, "written_at": <epoch>,
             "open": [<arm>, ...], "resolved": [<arm>, ...]}

        Unlike ``sar_exit_candidates`` this is **not** a replay. Each arm was
        stepped forward bar by bar while the trade was open, so an ``open`` row
        carries the stop the mechanism would have had parked *right now* rather
        than a level reconstructed afterwards.
        """
        return self._load(SAR_LIVE_FILE)

    # -- Dark emission lane (engine: src/dark_emission.py) --------------- #

    def dark_signals(self) -> Any:
        """Owner-only signals from the paths the gates normally silence.

        These are **not** counterfactuals. Each row cleared the full scanner
        chain — scoring, MTF, min_confidence, the context floors,
        level_still_in_play, staleness — with only ``setup_compat`` or
        ``execution`` overridden, and was then diverted at the single
        ``signal_queue.put`` site so it could never reach the router, a
        channel, a push, the app feed or an order.

        They are also **not** what a user would have seen: the router's second
        layer (correlation lock, cooldowns, concurrency caps) is not applied,
        so the count over-reports a feed size. The page has to say that, or it
        repeats #816 — a pre-router population labelled as a delivered one.
        """
        return self._load(DARK_EMISSION_FILE)

    def dark_sar_arms(self) -> Any:
        """SAR exit arms opened on the dark rows — the second outcome per row.

        Same shape and same producer as ``sar_live_arms`` (engine
        ``src/sar_live_shadow.py``), stepped forward on the dark lane's own
        resolve cycle rather than the monitor loop, and written to its own file.

        Joined to a dark row by ``signal_id``: the dark row carries what its own
        SL/TP1 geometry produced, the arm carries what a SAR handover would have
        produced from the same entry. Neither is "the" outcome — that is the
        whole point of showing both.
        """
        return self._load(DARK_SAR_FILE)

    def dark_signals_provenance(self) -> dict[str, Any]:
        """Which dark-lane file, and when the engine last wrote it.

        The engine flushes on the 5-min resolve cycle, so this file is much
        slower-moving than the SAR live arms — a few minutes old is healthy
        here and would be FROZEN there. Missing entirely means the lane is off
        or the resolve loop is not running; current and empty means the lane is
        on and the loosened paths have produced nothing, which is a finding
        rather than a fault.
        """
        info: dict[str, Any] = {
            "file": DARK_EMISSION_FILE,
            "version": DARK_EMISSION_VERSION,
            "exists": False,
            "modified_at": None,
            "age_sec": None,
        }
        try:
            path = self._dir / DARK_EMISSION_FILE
            if path.exists():
                info["exists"] = True
                mtime = path.stat().st_mtime
                info["modified_at"] = datetime.fromtimestamp(
                    mtime, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
                info["age_sec"] = max(
                    0.0, datetime.now(timezone.utc).timestamp() - mtime
                )
        except OSError:
            pass
        return info

    def sar_live_provenance(self) -> dict[str, Any]:
        """Which live-arm file we are reading, and when the engine last wrote it.

        Same three questions as :meth:`sar_ledger_provenance`, for the same
        reason — ops read an orphaned SAR ledger for nine hours after #822 and
        every number on screen described a discarded population. A live panel
        makes that worse, not better: a frozen file still renders as a page full
        of open trades with plausible stops.

        ``age_sec`` is the one number the live tab must lead with, and it is
        only meaningful because the engine writes on a **60s heartbeat** rather
        than only when an arm changes. That is what separates the three states a
        reader needs: file missing = the monitor loop is not running the arms;
        current but empty = running with nothing open (the quiet case); stale =
        the loop stopped stepping. The engine's first cut wrote only on change,
        so an idle engine produced no file at all and this page reported a fault
        that was not happening (owner-caught 2026-07-30, minutes after deploy).

        So: a file older than a couple of minutes means the loop is not stepping
        the arms — never that the market is quiet.
        """
        info: dict[str, Any] = {
            "file": SAR_LIVE_FILE,
            "version": SAR_LIVE_VERSION,
            "exists": False,
            "modified_at": None,
            "age_sec": None,
            "newer_version": None,
            "newer_file": None,
        }
        try:
            path = self._dir / SAR_LIVE_FILE
            if path.exists():
                info["exists"] = True
                mtime = path.stat().st_mtime
                info["modified_at"] = datetime.fromtimestamp(
                    mtime, tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC")
                info["age_sec"] = max(0.0, datetime.now(timezone.utc).timestamp() - mtime)
            newest = SAR_LIVE_VERSION
            for entry in self._dir.iterdir():
                m = _SAR_LIVE_RE.match(entry.name)
                if m and int(m.group(1)) > newest:
                    newest = int(m.group(1))
            if newest > SAR_LIVE_VERSION:
                info["newer_version"] = newest
                info["newer_file"] = f"sar_live_arms_v{newest}.json"
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
