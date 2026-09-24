"""Path routing — where each (path, side) goes, in both directions, on one table.

Owner, 2026-09-24: *"there is no clear diversion screen, Dark to live and live
to dark, make it clear and I do it later."* Two mechanisms move a path's
signals across the dark/live line, and until now they lived in different
places in different shapes:

* **live → dark** is *path retirement* (engine ``src/path_retirement.py``): a
  (path, side) on the ``retired_paths`` list is diverted to the dark feed
  instead of the queue. It was editable only as a free-text runtime tunable,
  and the Promotions page's link to it pointed at a POST-only route.
* **dark → live** is *promotion* (engine ``src/dark_promotion.py``): a per-path
  rule that lets a named slice of the dark feed through.

They are the same decision pointing opposite ways, and they interact: a
retired row is diverted by being marked dark with the gate
``retired:<PATH>:<SIDE>``, and it then passes the same promotion ``decide`` as
every other dark row. A rule whose gate allow-list is *Any* therefore puts
the retired rows straight back on the live feed — the retirement is armed on
one page and silently undone on another. That interaction is the reason this
is one table rather than two cards: a row here says, for one (path, side),
what the live feed does, what the dark → live rule does, and whether the two
disagree.

Ops computes no statistic here. Delivered-book evidence is the engine's path
scorecard (read through the diagnostic catalog); dark-feed evidence is the
same reducer the dark-feed and Promotions pages use; the retirement list is
the engine's own parsed snapshot. What this module does is *compose* them and
*edit the list* — and the edit is a read-modify-write against a snapshot taken
at the moment of the write, never against the page the operator loaded.
"""
from __future__ import annotations

from typing import Any, Optional
from collections.abc import Iterable

from app.data_sources import dark_promotion as dp

SIDES = ("LONG", "SHORT")
ANY_SIDE = "*"

#: Live-feed states for one (path, side). Four, never two: "diverted" and
#: "diverted, but the master switch is off" have opposite effects, and
#: "unknown" is not "live" — an engine that could not report its list says
#: nothing about what is reaching subscribers.
LIVE = "live"
DIVERTED = "diverted"
DIVERTED_INERT = "diverted_inert"
UNKNOWN = "unknown"

LIVE_COPY = {
    LIVE: "Reaches subscribers.",
    DIVERTED: (
        "Diverted to the dark feed — subscribers do not receive it; it keeps "
        "being measured."
    ),
    DIVERTED_INERT: (
        "On the retired list, but the retirement master switch is OFF — it "
        "still reaches subscribers."
    ),
    UNKNOWN: (
        "The engine did not report its retirement list, so this page cannot "
        "say whether this reaches subscribers."
    ),
}


def _norm(value: Any) -> str:
    return str(value or "").strip().upper()


def _side(value: Any) -> str:
    side = _norm(value)
    if side.startswith("DIRECTION."):
        side = side.split(".", 1)[1]
    return side


# --------------------------------------------------------------------------- #
# The retired list
# --------------------------------------------------------------------------- #


def retirement_readable(retirement: Any) -> bool:
    """True when the engine published a usable retirement snapshot."""
    return isinstance(retirement, dict) and not retirement.get("error") and (
        isinstance(retirement.get("retired"), list)
    )


def pairs_of(entries: Any) -> list[tuple[str, str]]:
    """``[{"setup_class", "side"}, …]`` → ``[(SETUP, SIDE), …]``, order kept."""
    out: list[tuple[str, str]] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        setup = _norm(entry.get("setup_class"))
        side = _side(entry.get("side")) or ANY_SIDE
        if setup and (setup, side) not in out:
            out.append((setup, side))
    return out


def is_listed(pairs: Iterable[tuple[str, str]], setup: str, side: str) -> bool:
    setup, side = _norm(setup), _side(side)
    return any(p == setup and s in (ANY_SIDE, side) for p, s in pairs)


def spec_for(pairs: Iterable[tuple[str, str]]) -> str:
    """The string the engine's ``retired_paths`` tunable parses.

    Empty is a real value ("retire nothing") on the engine side, so an empty
    list writes ``""`` rather than being skipped.
    """
    return ", ".join(f"{p}:{s}" for p, s in pairs)


def with_diverted(
    pairs: list[tuple[str, str]], setup: str, side: str
) -> list[tuple[str, str]]:
    """The list with (setup, side) diverted. Unchanged when it already is."""
    setup, side = _norm(setup), _side(side)
    if is_listed(pairs, setup, side):
        return list(pairs)
    return list(pairs) + [(setup, side)]


def with_restored(
    pairs: list[tuple[str, str]], setup: str, side: str
) -> list[tuple[str, str]]:
    """The list with (setup, side) back on the live feed.

    A whole-path entry (``SETUP:*``) is split rather than dropped: restoring
    the LONG side of a path retired in both directions must leave the SHORT
    side retired, or one click would quietly restore two things.
    """
    setup, side = _norm(setup), _side(side)
    out: list[tuple[str, str]] = []
    for p, s in pairs:
        if p != setup:
            out.append((p, s))
        elif s == side:
            continue
        elif s == ANY_SIDE:
            for other in SIDES:
                if other != side and (p, other) not in out:
                    out.append((p, other))
        else:
            out.append((p, s))
    return out


def default_diff(retirement: Any) -> dict:
    """What adopting the signed-off default would change, pair by pair.

    Expressed per (path, side) so the page can say "this diverts X and
    restores Y" before the owner clicks — and so the route can require the
    confirm only when something would start reaching subscribers again.
    """
    if not retirement_readable(retirement):
        return {"readable": False, "to_divert": [], "to_restore": [], "same": False}
    now = pairs_of(retirement.get("retired"))
    default = pairs_of(retirement.get("default"))

    def expand(pairs: list[tuple[str, str]]) -> set[tuple[str, str]]:
        out: set[tuple[str, str]] = set()
        for p, s in pairs:
            for side in (SIDES if s == ANY_SIDE else (s,)):
                out.add((p, side))
        return out

    a, b = expand(now), expand(default)
    return {
        "readable": True,
        "to_divert": sorted(b - a),
        "to_restore": sorted(a - b),
        "same": a == b,
        "default_spec": spec_for(default),
    }


# --------------------------------------------------------------------------- #
# The dark → live side
# --------------------------------------------------------------------------- #


def promotion_for_side(snapshot: Any, setup: str, side: str) -> dict:
    """What the path's promotion rule does for THIS side.

    ``covers`` is tri-state: ``True`` (the rule promotes this side when its
    other conditions match), ``False`` (it cannot), ``None`` (a trend
    condition decides per row — it abstains on a regime that names no trend).
    """
    rule = dp.rule_for(snapshot, setup)
    state, text = dp.rule_state(snapshot, rule)
    if rule is None or state != "live":
        return {"state": state, "text": text, "covers": False, "rule": rule}
    direction = str(rule.get("direction") or "any").strip().lower()
    if direction == "any":
        covers: bool | None = True
    elif direction in ("long", "short"):
        covers = direction.upper() == _side(side)
    else:
        covers = None
    return {"state": state, "text": text, "covers": covers, "rule": rule}


def _rule_reaches_retired(rule: dict | None) -> bool:
    """Can this rule's gate condition match a row diverted by retirement?

    Retirement marks its rows with the gate ``retired:<PATH>:<SIDE>``. A rule
    listing *Any* matches it; so does a rule that names a ``retired:`` gate.
    """
    if not isinstance(rule, dict):
        return False
    gates = [str(g) for g in (rule.get("gates") or [])]
    return any(g == dp.ANY_TOKEN or g.lower().startswith("retired:") for g in gates)


# --------------------------------------------------------------------------- #
# The table
# --------------------------------------------------------------------------- #


def _cell_index(cells: Any) -> dict[tuple[str, str], dict]:
    out: dict[tuple[str, str], dict] = {}
    for cell in cells or []:
        if isinstance(cell, dict):
            out[(_norm(cell.get("setup_class")), _side(cell.get("side")))] = cell
    return out


def build_rows(
    retirement: Any,
    snapshot: Any,
    cells: Any,
    dark_rows: list[dict],
) -> list[dict]:
    """One row per (path, side) that has evidence or configuration.

    A side appears when anything names it: a delivered-book cell, a dark row,
    the retired list, or the signed-off default. A side nothing names is
    omitted rather than rendered as an empty row — a one-sided path
    (``BREAKDOWN_SHORT``) would otherwise show a LONG row that can never
    exist.
    """
    readable = retirement_readable(retirement)
    enabled = bool((retirement or {}).get("enabled")) if readable else False
    retired = pairs_of((retirement or {}).get("retired")) if readable else []
    default = pairs_of((retirement or {}).get("default")) if readable else []
    cell_at = _cell_index(cells)

    dark_by: dict[tuple[str, str], list[dict]] = {}
    for row in dark_rows or []:
        key = (_norm(row.get("setup_class")), _side(row.get("side")))
        if key[0] and key[1] in SIDES:
            dark_by.setdefault(key, []).append(row)

    keys: set[tuple[str, str]] = set(cell_at) | set(dark_by)
    for p, s in retired + default:
        for side in (SIDES if s == ANY_SIDE else (s,)):
            keys.add((p, side))
    keys = {k for k in keys if k[0] and k[1] in SIDES}

    rows = []
    for setup, side in sorted(keys):
        if not readable:
            live = UNKNOWN
        elif is_listed(retired, setup, side):
            live = DIVERTED if enabled else DIVERTED_INERT
        else:
            live = LIVE
        promo = promotion_for_side(snapshot, setup, side)
        conflict = None
        if live == DIVERTED and promo["covers"] is not False and _rule_reaches_retired(
            promo["rule"]
        ):
            conflict = (
                "The dark → live rule for this path matches the gate retirement "
                "stamps (Any, or a retired: gate), so it puts these diverted "
                "rows back on the live feed. Retirement is being undone. Narrow "
                "the rule's gates, or restore this side instead."
            )
        cell = cell_at.get((setup, side))
        rows.append({
            "setup_class": setup,
            "side": side,
            "live": live,
            "live_copy": LIVE_COPY[live],
            "in_default": is_listed(default, setup, side) if readable else None,
            "delivered": cell,
            "dark": dp.summarize(dark_by.get((setup, side), [])),
            "promotion": promo,
            "conflict": conflict,
            "can_divert": readable and live in (LIVE,),
            "can_restore": readable and live in (DIVERTED, DIVERTED_INERT),
        })
    return rows


def headline(rows: list[dict], retirement: Any, snapshot: Any) -> dict:
    """The counts the page leads with, so the reader knows the state before
    reading a single row."""
    return {
        "diverted": sum(1 for r in rows if r["live"] == DIVERTED),
        "diverted_inert": sum(1 for r in rows if r["live"] == DIVERTED_INERT),
        "live": sum(1 for r in rows if r["live"] == LIVE),
        "promoting": sum(1 for r in rows if r["promotion"]["state"] == "live"
                         and r["promotion"]["covers"] is not False),
        "conflicts": sum(1 for r in rows if r["conflict"]),
        "retirement_enabled": (
            bool(retirement.get("enabled")) if retirement_readable(retirement) else None
        ),
        "promotion_master": (
            bool(snapshot.get("master_enabled")) if isinstance(snapshot, dict)
            and "master_enabled" in snapshot else None
        ),
        "dark_lane": (
            bool(snapshot.get("dark_lane_enabled")) if isinstance(snapshot, dict)
            and "dark_lane_enabled" in snapshot else None
        ),
    }
