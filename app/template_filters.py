"""Display formatting for numbers that arrive as raw floats.

Every figure on this dashboard comes out of a JSON artifact or the engine's REST
surface as a Python float, and a bare ``{{ value }}`` renders its full repr. On
2026-08-07 the live pages carried, among others:

* ``/signals`` — a PnL column reading ``-0.47041707080504297`` (38 such cells),
* ``/positions`` — a candle feed age of ``54.348713397979736`` **seconds**, and
  TP levels at twelve decimal places on an instrument quoted to seven,
* ``/signals/price-action`` — swept-level prices like ``501.6468015414258`` and
  ``0.044309999999999995``, the second being pure binary-float noise.

None of these is *wrong*, which is why they survived: a reader skims past
seventeen digits and reads the first three. But a page that renders float repr is
a page that has not decided what its numbers mean, and the noise is the tell —
``0.044309999999999995`` is a level nobody computed and nobody can act on.

Two filters, because prices and percentages are not the same problem:

* **``price``** keeps the instrument's own precision. This book spans
  ``64328.80`` and ``0.02062`` in the same table, so a fixed decimal count is
  wrong at one end or the other, and ``%g`` is worse — it flips to scientific
  notation exactly on the sub-cent movers that dominate the delivered feed.
  Eight decimal places is Binance's own bound for USD-M quote precision, so
  rounding there can lose nothing the exchange could have expressed; trailing
  zeros are then stripped so a 5-decimal instrument still reads as one.
* **``pct``** is a percentage: two decimals, always signed by the caller's
  colour class. A percentage carries no per-symbol precision, so a fixed count
  is the honest one.

Both render ``—`` for ``None``. That is not decoration and it is not a zero: an
em-dash is the repo's standing marker for *the engine did not report this*,
and rendering ``0.00`` for a missing value is how a blank becomes a finding.
"""
from __future__ import annotations

from typing import Any

#: Binance USD-M futures quote precision bound. Rounding here cannot discard a
#: price the exchange was able to express, which is what makes it safe to apply
#: to a column holding several instruments at once.
PRICE_DECIMALS = 8

EMDASH = "—"


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return out


def price(value: Any) -> str:
    """A price or level, at the instrument's own precision.

    ``0.019803918136`` → ``0.01980392``; ``0.044309999999999995`` → ``0.04431``;
    ``64328.8`` → ``64328.8``. Never scientific notation, which is what ``%g``
    would produce for the sub-cent movers this book is full of.
    """
    out = _as_float(value)
    if out is None:
        return EMDASH
    text = f"{out:.{PRICE_DECIMALS}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def pct(value: Any, places: int = 2) -> str:
    """A percentage, to a fixed number of places. No ``%`` sign — the column
    header owns the unit, and doubling it up is how a ratio gets read as a
    percentage of a percentage."""
    out = _as_float(value)
    if out is None:
        return EMDASH
    return f"{out:.{places}f}"


def secs(value: Any) -> str:
    """An age in seconds, to one decimal. A feed age is a freshness signal, and
    fifteen digits of it is fourteen digits of noise."""
    out = _as_float(value)
    if out is None:
        return EMDASH
    return f"{out:.1f}"


def duration(value: Any) -> str:
    """An age or uptime in seconds, in units a human reads at a glance.

    ``secs`` is right for a feed age, where the interesting range is a few
    seconds and one decimal matters. A container uptime lives in the range of
    hours to weeks, where ``1382.0541844088584`` is fourteen digits of noise
    and ``23m`` is the answer. Different question, different filter — rendering
    an uptime through ``secs`` is how a page stops being readable.
    """
    out = _as_float(value)
    if out is None:
        return EMDASH
    out = abs(out)
    if out < 60:
        return f"{out:.0f}s"
    if out < 3600:
        return f"{out / 60:.0f}m"
    if out < 86400:
        hours, minutes = divmod(int(out // 60), 60)
        return f"{hours}h {minutes:02d}m" if minutes else f"{hours}h"
    days = int(out // 86400)
    hours = int((out % 86400) // 3600)
    return f"{days}d {hours}h" if hours else f"{days}d"


def size(value: Any) -> str:
    """Bytes, in binary units. ``None`` renders the em-dash like every other
    filter here — a disk we could not stat is not a disk with zero bytes."""
    out = _as_float(value)
    if out is None:
        return EMDASH
    step = 1024.0
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(out) < step or unit == "TiB":
            return f"{out:.0f} {unit}" if unit == "B" else f"{out:.1f} {unit}"
        out /= step
    return f"{out:.1f} TiB"


def register(env) -> None:
    """Attach the filters to a Jinja environment."""
    env.filters["price"] = price
    env.filters["pct"] = pct
    env.filters["secs"] = secs
    env.filters["duration"] = duration
    env.filters["size"] = size
