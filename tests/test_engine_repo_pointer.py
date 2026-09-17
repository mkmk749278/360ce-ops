"""No test may hardcode one machine's path to the engine checkout.

Written 2026-09-17 after finding four sites that did. `test_dark_promotion_refusals.py`
(x2) and `test_price_action_page.py` carried
`pathlib.Path("/home/user/360-v2/...")`, and `test_sar_hold.py` used the same
string as its `$ENGINE_REPO` default. That path exists in exactly one container,
so on every other checkout those cross-repo contracts **skipped silently** —
including the pin on the engine's own `dark_promotion.decide` conjunction, which
is the only thing keeping ops' `rule_unmet` replay from drifting.

Two reasons this is a derived guard rather than a note in CLAUDE.md:

* The failure is a SKIP. A skip is green, so nothing in a passing suite could
  ever have said the contract was not being checked — the same shape as
  `MEASUREMENT_SUFFIXES` drifting for a week, and as the ledger that flushed
  without loading.
* A hand-kept list of offending files is the deny-list this repo has paid for
  under seven names. The requirement is derived from the tree: every test file,
  every quoted absolute path, checked on every run.

Comment lines are exempt, and deliberately so: the fix's own explanation names
the old path, and a guard that cannot tell code from the prose explaining it
forces the next author to delete the explanation to get green. Engine
`#1038` paid for that lesson the same night, on a sed-offender check that first
fired on its own docstring.
"""
from __future__ import annotations

import re
from pathlib import Path

TESTS = Path(__file__).resolve().parent

#: A quoted absolute filesystem path whose first segment is a user's home or a
#: sandbox root. Matched on the LITERAL, so `$ENGINE_REPO` and any path built
#: from `Path(__file__)` pass — the point is that the pointer must be a fact
#: about the checkout, not about one box.
_ABSOLUTE_LITERAL = re.compile(r"""["'](/home/|/Users/|/root/|/workspace/)""")


def _code_lines(path: Path):
    """Yield (lineno, text) for lines that are not whole-line comments."""
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("#"):
            continue
        yield n, line


def test_no_test_hardcodes_an_absolute_path_to_another_checkout():
    offenders = [
        f"{path.name}:{n}: {line.strip()}"
        for path in sorted(TESTS.glob("*.py"))
        if path.name != Path(__file__).name
        for n, line in _code_lines(path)
        if _ABSOLUTE_LITERAL.search(line)
    ]
    assert not offenders, (
        "a test points at one machine's absolute path — on any other checkout "
        "it will SKIP silently, which is green and says nothing. Use "
        "`os.getenv(\"ENGINE_REPO\") or Path(__file__).resolve().parents[2] / "
        "\"360-v2\"`:\n  " + "\n  ".join(offenders)
    )


def test_every_engine_pointer_resolves_relative_to_this_checkout():
    """The positive half: the pointer must be derived, not merely non-absolute.

    A file that names the engine repo has to reach it either through
    `$ENGINE_REPO` or through its own location. Asserting only the absence of
    `/home/...` would pass a file that hardcoded a RELATIVE guess just as
    happily.
    """
    unwired = []
    for path in sorted(TESTS.glob("*.py")):
        if path.name == Path(__file__).name:
            continue
        src = path.read_text(encoding="utf-8")
        code = "\n".join(line for _n, line in _code_lines(path))
        if '"360-v2"' not in code and "360-v2/" not in code:
            continue
        if "ENGINE_REPO" in code or "__file__" in code:
            continue
        unwired.append(path.name)
    assert not unwired, (
        "names the engine repo without deriving where it is: " + ", ".join(unwired)
    )
