"""CSV report serialization — shared by the export routes.

The dashboard's analytics pages (Performance / Invalidations / Signals) reduce
the engine's mounted JSON into operator tables.  This module turns those same
reduced rows into downloadable CSV so the owner can pull a snapshot into a
spreadsheet for ad-hoc slicing without standing up a notebook against the VPS
data volume.

Read-only by construction: it only formats data the routes already computed.
No engine state is touched."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from starlette.responses import StreamingResponse


def _timestamped(filename_stem: str) -> str:
    """``stem`` → ``stem_YYYYMMDD-HHMMSSZ.csv`` so downloaded snapshots are
    self-dating on the operator's disk (multiple pulls don't clobber)."""
    stamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%SZ")
    return f"{filename_stem}_{stamp}.csv"


def csv_response(
    filename_stem: str,
    header: Sequence[str],
    rows: Iterable[Sequence[Any]],
) -> StreamingResponse:
    """Build a ``text/csv`` attachment response from a header + row iterable.

    Values are written verbatim through ``csv.writer`` (which handles quoting /
    escaping), so callers pass already-formatted scalars — floats pre-rounded,
    ``None`` rendered as the empty string."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow(["" if c is None else c for c in row])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{_timestamped(filename_stem)}"',
            "Cache-Control": "no-store",
        },
    )
