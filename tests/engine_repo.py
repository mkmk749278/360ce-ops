"""Where the engine checkout is, for the cross-repo contract tests — one writer.

Twenty-odd tests drive the ENGINE's real code (an assembler, a parser, a
constant) rather than a fixture, because a fixture chooses a location and then
agrees with you about it. Each used to locate the engine itself, in three
idioms, and only four honoured ``$ENGINE_REPO`` — so a CI job cloning the
engine anywhere but the sibling path would have found some contracts and
silently skipped the rest.

``ENGINE_REPO`` is ``$ENGINE_REPO`` when set, else the sibling checkout
(``../360-v2``). ``ABSENT`` is the one skip reason, and it names both places the
engine can come from, because the reason is the instrument: a skip is green, and
a reader who believes CI covered a skipped contract is the state this repo sat
in until 2026-09-26 (see CLAUDE.md, "The cross-repo contract tests").

The ``contracts`` workflow (.github/workflows/contracts.yml) clones the engine
and fails if any test here skips. ``lint + tests`` never checks it out.
"""
from __future__ import annotations

import os
from pathlib import Path

ENGINE_REPO: Path = Path(
    os.getenv("ENGINE_REPO") or Path(__file__).resolve().parents[2] / "360-v2"
).resolve()

ABSENT = (
    "no engine repo beside ops — set $ENGINE_REPO or run the `contracts` "
    "workflow; `lint + tests` never checks it out"
)
