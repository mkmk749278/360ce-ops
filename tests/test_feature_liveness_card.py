"""Pulse-page feature-liveness card (2026-07-14 incident response).

The engine publishes ``feature_liveness.json`` every 5-min audit cycle —
per-feature output-vs-upstream verdicts, sustained-streak alerts, and
fail-open exception counters.  Ops renders it on Pulse so a silently-dead
feature is visible at a glance (red badge), not just in the pager path.
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("OPS_SESSION_SECRET", "test-secret")
os.environ.setdefault("OPS_AUTH_TOKEN", "test-token")

from fastapi.testclient import TestClient  # noqa: E402

from app.config import load_settings  # noqa: E402
from app.data_sources.data_volume import DataVolumeReader  # noqa: E402
from app.main import app  # noqa: E402


def _login(client: TestClient) -> None:
    client.post("/login", data={"password": "test-token"})


def _reader(tmp_path) -> DataVolumeReader:
    from dataclasses import replace

    settings = replace(load_settings(), engine_data_dir=str(tmp_path))
    return DataVolumeReader(settings)


_MANIFEST = {
    "generated_at": 1752480000.0,
    "boot_grace_active": False,
    "features": {
        "geometry_ab": {
            "status": "violating",
            "detail": "upstream +120 but output +0 (streak 7/6)",
            "streak": 7,
        },
        "btc_reference": {"status": "ok", "detail": "BTC ref 100.00", "streak": 0},
    },
    "alerts": [
        {
            "feature": "geometry_ab",
            "detail": "upstream +120 but output +0 (streak 7/6)",
            "streak": 7,
        }
    ],
    "fail_open": {
        "scanner.stamp_geometry_ab": {
            "count": 90,
            "last_error": "ValueError: truth value of an array",
            "last_ts": 1752480000.0,
        }
    },
}


class TestAccessor:
    def test_reads_manifest(self, tmp_path):
        (tmp_path / "feature_liveness.json").write_text(json.dumps(_MANIFEST))
        data = _reader(tmp_path).feature_liveness()
        assert data["features"]["geometry_ab"]["status"] == "violating"

    def test_missing_file_is_error_dict_not_crash(self, tmp_path):
        data = _reader(tmp_path).feature_liveness()
        assert "error" in data


class TestPulseCard:
    def test_renders_alert_and_badges(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            DataVolumeReader, "feature_liveness", lambda self: _MANIFEST
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/")
            assert r.status_code == 200
            html = r.text
            assert "Feature liveness" in html
            assert "geometry_ab" in html
            assert "upstream +120 but output +0" in html
            assert "st-open" in html          # violating → red badge
            assert "st-active" in html        # ok → green badge
            assert "scanner.stamp_geometry_ab" in html
            assert "90" in html

    def test_renders_quietly_when_manifest_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            DataVolumeReader,
            "feature_liveness",
            lambda self: {"error": "missing: /engine-data/feature_liveness.json"},
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/")
            assert r.status_code == 200
            assert "pre-rollout build" in r.text

    def test_all_ok_shows_green_summary(self, tmp_path, monkeypatch):
        healthy = {
            "generated_at": 1752480000.0,
            "boot_grace_active": False,
            "features": {
                "geometry_ab": {"status": "ok", "detail": "output +12 / upstream +40", "streak": 0},
            },
            "alerts": [],
            "fail_open": {},
        }
        monkeypatch.setattr(
            DataVolumeReader, "feature_liveness", lambda self: healthy
        )
        with TestClient(app) as client:
            _login(client)
            r = client.get("/")
            assert r.status_code == 200
            assert "producing data at their expected rates" in r.text
            assert "st-open" not in r.text
