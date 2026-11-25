"""Price ladder — where a trade sits between its stop and its targets.

Used by /positions (to replace four separate price columns that pushed PnL %
off the right edge) and by the signal dossier. One axis, stop at 0 and the
furthest target at 100, the same for LONG and SHORT because the span is
signed.

**Refuse, don't clamp** (360-v2 CLAUDE.md, "a clamp is not a guard"): if the
levels are not ordered the way the side says they should be, there is no
honest axis to draw, so this returns ``None`` with a reason and the caller
shows the raw levels instead. Only the CURRENT price is clamped onto the bar,
and when it is, the mark says which edge it went past — a price beyond the
stop is a fact worth seeing, not a rounding detail.
"""
from __future__ import annotations

from typing import Any


def _f(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def ladder(
    direction: Any,
    stop: Any,
    entry: Any,
    current: Any,
    tp1: Any,
    tp2: Any = None,
    tp3: Any = None,
    stop_now: Any = None,
) -> dict[str, Any]:
    """Positions (0–100) of each level, or ``{"refused": reason}``."""
    side = str(direction or "").upper()
    sl, en = _f(stop), _f(entry)
    targets = [(name, _f(v)) for name, v in (("TP1", tp1), ("TP2", tp2), ("TP3", tp3))]
    targets = [(n, v) for n, v in targets if v is not None]
    if side not in ("LONG", "SHORT"):
        return {"refused": "side not reported"}
    if sl is None or en is None or not targets:
        return {"refused": "stop, entry or target not reported"}
    far = targets[-1][1]
    sign = 1 if side == "LONG" else -1
    chain = [en] + [v for _, v in targets]
    if any(sign * (b - a) <= 0 for a, b in zip(chain, chain[1:], strict=False)):
        return {"refused": f"targets are not ordered beyond entry for a {side}"}
    # A stop at or past entry is a normal live state — a break-even shift or a
    # trail moved it (first live read, 2026-11-25: FILUSDT SHORT, SL = entry).
    # The axis then starts at entry and the stop is drawn where it now rests.
    # Only a stop beyond the last target has no honest axis.
    moved = sign * (en - sl) <= 0
    if sign * (far - sl) <= 0:
        return {"refused": f"stop is beyond the last target for a {side}"}
    left = en if moved else sl
    span = far - left

    def pos(p: float) -> float:
        return round((p - left) / span * 100.0, 2)

    marks = [{"label": "SL (moved)" if moved else "SL", "price": sl, "pos": pos(sl),
              "kind": "stop-now" if moved else "stop"},
             {"label": "Entry", "price": en, "pos": pos(en), "kind": "entry"}]
    marks += [{"label": n, "price": v, "pos": pos(v), "kind": "target"} for n, v in targets]
    sn = _f(stop_now)
    if sn is not None and sn != sl:
        marks.append({"label": "Stop now", "price": sn, "pos": max(0.0, min(100.0, pos(sn))), "kind": "stop-now"})

    cur = _f(current)
    out: dict[str, Any] = {"refused": None, "marks": marks, "entry_pos": pos(en)}
    if cur is None:
        out["current"] = None
        return out
    raw = pos(cur)
    beyond = "past stop" if raw < 0 else "past last target" if raw > 100 else None
    shown = max(0.0, min(100.0, raw))
    favourable = sign * (cur - en) >= 0
    lo, hi = sorted((pos(en), shown))
    out["current"] = {
        "price": cur, "pos": shown, "beyond": beyond,
        "favourable": favourable, "fill_left": max(0.0, lo), "fill_width": max(0.0, hi - lo),
    }
    return out
