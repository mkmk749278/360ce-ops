"""Tests for the shared CSV export helper (session 23)."""
from __future__ import annotations

import asyncio
import csv
import io

from app.reports import csv_response


def _body_text(resp) -> str:
    """Drain the StreamingResponse's async body iterator into a string."""
    async def _drain() -> str:
        parts = []
        async for chunk in resp.body_iterator:
            parts.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(parts)

    return asyncio.run(_drain())


def test_csv_response_headers_and_content():
    resp = csv_response(
        "performance_symbol_all",
        ["symbol", "n", "avg_pnl_pct"],
        [["BTCUSDT", 12, "0.1234"], ["ETHUSDT", 3, "-0.0500"]],
    )
    assert resp.media_type == "text/csv"
    cd = resp.headers["content-disposition"]
    assert cd.startswith('attachment; filename="performance_symbol_all_')
    assert cd.endswith('.csv"')
    assert resp.headers["cache-control"] == "no-store"


def test_csv_response_serializes_rows():
    resp = csv_response("x", ["a", "b"], [["one", None], [1, 2]])
    parsed = list(csv.reader(io.StringIO(_body_text(resp))))
    assert parsed[0] == ["a", "b"]
    assert parsed[1] == ["one", ""]  # None → empty string
    assert parsed[2] == ["1", "2"]
