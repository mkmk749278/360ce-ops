"""2026-07-10 ops upgrade: raw-file browser downloads + the audit board.

The raw-download route's whole safety story is ``DataVolumeReader.
resolve_safe`` — the traversal/symlink cases are pinned here at unit level
AND through the route.
"""
from __future__ import annotations

import os
import time

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.data_sources.data_volume import DataVolumeReader  # noqa: E402
from app.main import app  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


def _reader(tmp_path) -> DataVolumeReader:
    # Settings is a frozen dataclass — swap the volume dir via replace().
    from dataclasses import replace

    settings = replace(load_settings(), engine_data_dir=str(tmp_path))
    return DataVolumeReader(settings)


class TestListFiles:
    def test_lists_nested_newest_first(self, tmp_path):
        old = tmp_path / "signal_history.json"
        old.write_text("[]")
        os.utime(old, (time.time() - 5000, time.time() - 5000))
        sub = tmp_path / "paper_books"
        sub.mkdir()
        fresh = sub / "paper_pnl_user_1.json"
        fresh.write_text("{}")

        files = _reader(tmp_path).list_files()
        assert [f["rel_path"] for f in files] == [
            "paper_books/paper_pnl_user_1.json",
            "signal_history.json",
        ]
        assert files[0]["size_bytes"] == 2

    def test_depth_bounded_and_hidden_skipped(self, tmp_path):
        (tmp_path / ".hidden.json").write_text("{}")
        deep = tmp_path / "a" / "b"
        deep.mkdir(parents=True)
        (deep / "too_deep.json").write_text("{}")
        assert _reader(tmp_path).list_files() == []

    def test_missing_volume_is_empty_not_error(self, tmp_path):
        assert _reader(tmp_path / "nope").list_files() == []


class TestResolveSafe:
    def test_plain_file_resolves(self, tmp_path):
        (tmp_path / "x.json").write_text("{}")
        assert _reader(tmp_path).resolve_safe("x.json") is not None

    def test_dotdot_traversal_rejected(self, tmp_path):
        (tmp_path / "x.json").write_text("{}")
        r = _reader(tmp_path)
        assert r.resolve_safe("../etc/passwd") is None
        assert r.resolve_safe("a/../../etc/passwd") is None

    def test_symlink_out_of_volume_rejected(self, tmp_path):
        outside = tmp_path.parent / "outside.txt"
        outside.write_text("secret")
        (tmp_path / "vol").mkdir()
        (tmp_path / "vol" / "link.txt").symlink_to(outside)
        assert _reader(tmp_path / "vol").resolve_safe("link.txt") is None

    def test_directory_rejected(self, tmp_path):
        (tmp_path / "sub").mkdir()
        assert _reader(tmp_path).resolve_safe("sub") is None


class TestRawDownloadRoute:
    def test_download_roundtrip(self, tmp_path, monkeypatch):
        target = tmp_path / "watchdog_audit.jsonl"
        target.write_text('{"event":"page"}\n')
        monkeypatch.setattr(
            DataVolumeReader,
            "resolve_safe",
            lambda self, rel: target if rel == "watchdog_audit.jsonl" else None,
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/data/raw/watchdog_audit.jsonl")
            assert r.status_code == 200
            assert r.content == b'{"event":"page"}\n'
            assert "attachment" in r.headers["content-disposition"]

            assert client.get("/data/raw/../../etc/passwd").status_code == 404

    def test_requires_auth(self):
        with TestClient(app) as client:
            r = client.get("/data/raw/anything.json", follow_redirects=False)
            assert r.status_code == 302

    def test_files_table_rendered(self, monkeypatch):
        monkeypatch.setattr(
            DataVolumeReader,
            "list_files",
            lambda self: [
                {"rel_path": "pricing_freshness.json", "size_bytes": 512, "mtime": time.time() - 40}
            ],
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/data")
            assert r.status_code == 200
            assert "pricing_freshness.json" in r.text
            assert "/data/raw/pricing_freshness.json" in r.text
            assert "40s ago" in r.text


class TestAuditBoard:
    def test_requires_auth(self):
        with TestClient(app) as client:
            r = client.get("/audit", follow_redirects=False)
            assert r.status_code == 302

    def test_board_renders_all_findings(self):
        with TestClient(app) as client:
            _login(client)
            r = client.get("/audit")
            assert r.status_code == 200
            for fid in ("F-01", "F-07", "F-20"):
                assert fid in r.text
            # Colour badges present
            assert "badge st-done" in r.text
            assert "badge st-open" in r.text
            assert "badge sev-critical" in r.text

    def test_needs_attention_sorts_first(self):
        with TestClient(app) as client:
            _login(client)
            body = client.get("/audit").text
            # F-01 (open/critical) must appear before F-02 (done/critical).
            assert body.index("F-01") < body.index("F-02")

    def test_status_filter(self):
        with TestClient(app) as client:
            _login(client)
            body = client.get("/audit?status=done").text
            assert "F-02" in body  # done
            # F-01 appears only in the summary links, not as a table row.
            assert "F-01" not in body
