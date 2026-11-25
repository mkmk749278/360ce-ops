"""Four small inline-SVG charts (outside audit, 2026-11-25: "not a single page
renders a chart"). No library, no script: server-rendered SVG that works with
JavaScript off, themes through CSS classes, and is testable as a string.

Rules each one keeps, from this repo's CLAUDE.md:

- **A chart is drawn only over data that supports it.** Too few points returns
  ``None`` and the page says why; a two-point line or a one-bar chart reads as a
  shape and is not one.
- **Excluded rows are counted, never silently dropped.** Every builder returns
  how many inputs it could not place, and the caption prints it.
- **No number is invented to make a shape close.** The router funnel draws the
  engine's own delivered/dropped totals; the reason breakdown is a separate bar,
  and when the reasons do not sum to the dropped total the gap is labelled
  rather than redistributed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from markupsafe import Markup, escape

_W = 760.0


def _f(v: Any) -> float | None:
    if isinstance(v, bool) or v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fmt_day(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 1. Track record: cumulative net USDT with drawdown under it
# ---------------------------------------------------------------------------

def equity_curve(rows: list[dict]) -> dict[str, Any]:
    """Cumulative ``net_usd`` by close time, and the drawdown from its peak.

    Rows with no close time or no priced ``net_usd`` cannot be placed on a time
    axis; they are counted in ``skipped`` and the caption says so.
    """
    pts: list[tuple[float, float]] = []
    skipped = 0
    for r in rows:
        ts, net = _f(r.get("closed_at_ts")), _f(r.get("net_usd"))
        if ts is None or net is None:
            skipped += 1
            continue
        pts.append((ts, net))
    pts.sort()
    out: dict[str, Any] = {"n": len(pts), "skipped": skipped, "svg": None}
    if len(pts) < 3:
        out["why"] = "fewer than three priced, dated trades in this selection"
        return out
    cum, peak, series, dd = 0.0, 0.0, [], []
    for ts, net in pts:
        cum += net
        peak = max(peak, cum)
        series.append((ts, cum))
        dd.append((ts, cum - peak))
    t0, t1 = series[0][0], series[-1][0]
    span_t = (t1 - t0) or 1.0
    hi = max(0.0, max(v for _, v in series))
    lo = min(0.0, min(v for _, v in series))
    span_v = (hi - lo) or 1.0
    max_dd = min(v for _, v in dd)
    H, DH, PAD = 150.0, 50.0, 4.0

    def x(t: float) -> float:
        return round((t - t0) / span_t * _W, 1)

    def y(v: float) -> float:
        return round(PAD + (hi - v) / span_v * (H - 2 * PAD), 1)

    def ydd(v: float) -> float:
        return round(H + 8 + (-v / (-max_dd or 1.0)) * DH, 1)

    line = " ".join(f"{x(t)},{y(v)}" for t, v in series)
    zero = y(0.0)
    ddpath = f"M0,{H + 8} " + " ".join(f"L{x(t)},{ydd(v)}" for t, v in dd) + f" L{_W},{H + 8} Z"
    final = series[-1][1]
    tone = "pos" if final > 0 else "neg" if final < 0 else ""
    svg = (
        f'<svg class="chart" viewBox="0 0 {_W:g} {H + DH + 12:g}" preserveAspectRatio="none" '
        f'role="img" aria-label="Cumulative net {final:+.2f} USDT over {len(pts)} trades; '
        f'max drawdown {max_dd:.2f} USDT">'
        f'<line class="ch-zero" x1="0" x2="{_W:g}" y1="{zero}" y2="{zero}"/>'
        f'<polyline class="ch-line {tone}" points="{line}"/>'
        f'<path class="ch-dd" d="{ddpath}"/>'
        f"</svg>"
    )
    out.update(
        svg=Markup(svg), final=final, max_dd=max_dd, hi=hi, lo=lo,
        start=_fmt_day(t0), end=_fmt_day(t1),
    )
    return out


# ---------------------------------------------------------------------------
# 2. Score bands: one bar per band, diverging around zero
# ---------------------------------------------------------------------------

def band_bars(rows: list[dict], value: str, *, second: str | None = None) -> dict[str, Any]:
    """Horizontal bars per band for ``value`` (and optionally a paired
    ``second`` drawn behind it — raw-edge's MFE behind realized).

    Bands with no readable value are counted in ``skipped``, not drawn at zero:
    a zero-length bar reads as "breakeven".
    """
    usable, skipped = [], 0
    for r in rows or []:
        v = _f(r.get(value))
        if v is None:
            skipped += 1
            continue
        usable.append((str(r.get("key")), v, _f(r.get(second)) if second else None, r.get("n")))
    out: dict[str, Any] = {"skipped": skipped, "rows": []}
    if len(usable) < 2:
        out["why"] = "fewer than two bands carry a value"
        return out
    vals = [abs(v) for _, v, s, _ in usable] + [abs(s) for *_, s, _ in usable if s is not None]
    scale = max(vals) or 1.0
    for key, v, s, n in usable:
        out["rows"].append({
            "key": key, "n": n, "value": v,
            "pos": v >= 0, "width": round(abs(v) / scale * 50.0, 2),
            "second": s,
            "second_pos": None if s is None else s >= 0,
            "second_width": None if s is None else round(abs(s) / scale * 50.0, 2),
        })
    return out


# ---------------------------------------------------------------------------
# 3. Router: dequeued → delivered | dropped, and what the drops were
# ---------------------------------------------------------------------------

def delivery_funnel(processed: Any, delivered: Any, dropped: Any, reasons: list[dict]) -> dict[str, Any]:
    p, d, x = _f(processed), _f(delivered), _f(dropped)
    out: dict[str, Any] = {"ok": False}
    if p is None or d is None or x is None or p <= 0:
        out["why"] = "the engine has not reported a dequeued count since its last start"
        return out
    segs = []
    attributed = 0.0
    for r in sorted(reasons or [], key=lambda r: -(_f(r.get("n")) or 0)):
        n = _f(r.get("n")) or 0.0
        if n <= 0:
            continue
        attributed += n
        segs.append({"label": str(r.get("reason")), "n": int(n), "pct": round(n / p * 100, 2)})
    top, rest = segs[:6], segs[6:]
    if rest:
        n = sum(s["n"] for s in rest)
        top.append({"label": f"{len(rest)} other gate(s)", "n": n, "pct": round(n / p * 100, 2), "other": True})
    gap = x - attributed
    out.update(
        ok=True, processed=int(p), delivered=int(d), dropped=int(x),
        delivered_pct=round(d / p * 100, 2), dropped_pct=round(x / p * 100, 2),
        reasons=top,
        # Said, not redistributed: a gap means the reason counters and the
        # dropped total disagree, which is a fact about the counters.
        unattributed=int(gap) if gap > 0 else 0,
        over_attributed=int(-gap) if gap < 0 else 0,
    )
    return out


# ---------------------------------------------------------------------------
# 4. Liveness: the chain as a row of nodes
# ---------------------------------------------------------------------------

_NODE_TONE = {"ok": "ok", "broken": "bad", "degraded": "warn"}


def pipeline(chain: list[dict]) -> Markup | None:
    """One node per hop, coloured by its state, linked to its table row.
    ``unknown`` is amber with a hollow centre — a hop nobody could check is not
    drawn as healthy, and is not drawn as broken either."""
    steps = [s for s in chain or [] if isinstance(s, dict)]
    if len(steps) < 2:
        return None
    n = len(steps)
    gap = _W / n
    parts = [f'<svg class="chart chart-pipe" viewBox="0 0 {_W:g} 74" role="img" '
             f'aria-label="{n}-hop liveness chain">']
    parts.append(f'<line class="ch-zero" x1="{gap / 2:.1f}" x2="{_W - gap / 2:.1f}" y1="20" y2="20"/>')
    for i, s in enumerate(steps):
        cx = gap / 2 + i * gap
        state = str(s.get("state") or "unknown")
        tone = _NODE_TONE.get(state, "unknown")
        label = escape(str(s.get("label") or s.get("key") or ""))
        key = escape(str(s.get("key") or ""))
        parts.append(
            f'<a href="#{key}"><title>{label}: {escape(state)}</title>'
            f'<circle class="node node-{tone}" cx="{cx:.1f}" cy="20" r="9"/>'
            f'<text class="node-label" x="{cx:.1f}" y="48" text-anchor="middle">{i + 1}</text></a>'
        )
    parts.append("</svg>")
    return Markup("".join(parts))
