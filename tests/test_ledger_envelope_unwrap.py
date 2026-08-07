"""Every loader for a file the engine's `SuppressedCandidateStore` writes must
unwrap the schema-2 envelope.

The store persists `{"schema": 2, "records": [...], "evicted_by_gate": {...},
"stamped_total": N}` to **every** file it owns. On 2026-08-06 `/signals/sar` was
found reading one of them as a bare list; `_unwrap_records` was written and
applied to **that loader only**. `suppressed_candidates.json` is written by the
same store, gained the same envelope in the same commit, and kept reading raw —
so `reduce_gate_metrics` iterated a dict, got its keys, and the Strategy Lab
rendered *"No suppressed candidates stamped yet"* over five days of stamps.

Nothing crashed and no screen was empty: a full-looking page describing nothing.
This guard is derived from the source rather than written as a list, because a
list is what failed the first time.
"""
from __future__ import annotations

import ast
import os
import re

os.environ.setdefault("OPS_SESSION_SECRET", "test")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

import app.data_sources.data_volume as dv  # noqa: E402

#: Files written by the engine's `SuppressedCandidateStore`. The ENGINE owns
#: this list (`suppression_audit._DEFAULT_PATH`, `sar_exit_shadow._DEFAULT_PATH`,
#: `geometry_ab.get_geometry_store`) — ops cannot import it, so it is mirrored
#: here and the mirror is the point of failure this test exists to bound. A file
#: added engine-side and read here without unwrapping fails below.
STORE_WRITTEN = {
    "suppressed_candidates.json",
    "sar_exit_candidates_v3.json",
    "geometry_ab_candidates.json",
}


def _source() -> str:
    return open(dv.__file__, encoding="utf-8").read()


def test_every_store_written_loader_unwraps_the_envelope():
    """Parse the real module: any `self._load(<store file>)` must sit inside an
    `_unwrap_records(...)` call. Pin the call site, not the helper's existence —
    defining `_unwrap_records` is not calling it."""
    tree = ast.parse(_source())
    offenders = []

    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "_load"):
                continue
            # Which file does this _load read?
            names: list[str] = []
            for a in node.args:
                if isinstance(a, ast.Constant) and isinstance(a.value, str):
                    names.append(a.value)
                elif isinstance(a, ast.Name):
                    # A module-level constant, e.g. SAR_LEDGER_FILE.
                    for m in re.finditer(
                        rf'^{a.id}[^=]*=\s*"([^"]+)"', _source(), re.M
                    ):
                        names.append(m.group(1))
            if not any(os.path.basename(n) in STORE_WRITTEN for n in names):
                continue
            # It reads a store-written file — is the _load wrapped?
            wrapped = any(
                isinstance(outer, ast.Call)
                and isinstance(outer.func, ast.Name)
                and outer.func.id == "_unwrap_records"
                and any(node is arg for arg in ast.walk(outer))
                for outer in ast.walk(fn)
            )
            if not wrapped:
                offenders.append(f"{fn.name} reads {names} without _unwrap_records")

    assert not offenders, (
        "a loader reads a file the engine wraps in a schema-2 envelope and does "
        "not unwrap it — it will iterate a dict and render as an empty "
        "feature:\n  " + "\n  ".join(offenders)
    )


def test_the_helper_takes_both_shapes_and_passes_errors_through():
    """Both are valid on disk: a file written before the bump is still the file
    until the store next flushes."""
    envelope = {"schema": 2, "records": [{"gate_name": "router:x"}], "stamped_total": 9}
    bare = [{"gate_name": "router:x"}]
    err = {"error": "missing: /engine-data/suppressed_candidates.json"}

    assert dv._unwrap_records(envelope) == bare
    assert dv._unwrap_records(bare) == bare
    # "could not read" and "shape I do not know" have different fixes.
    assert dv._unwrap_records(err) == err


def test_the_suppression_loader_returns_a_list_from_a_real_envelope():
    """Drive the accessor, not the helper — the defect was at the call site."""
    class _DV(dv.DataVolumeReader):
        def __init__(self):
            pass

        def _load(self, name):
            return {
                "schema": 2,
                "records": [{"gate_name": "router:same_direction_throttle",
                             "setup_class": "MOVER_TREND_PULLBACK"}],
                "evicted_by_gate": {},
                "stamped_total": 5880,
            }

    out = _DV().suppressed_candidates()
    assert isinstance(out, list), "a dict here renders as 'nothing stamped yet'"
    assert out[0]["gate_name"] == "router:same_direction_throttle"
