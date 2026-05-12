"""Environment-driven configuration for 360 CE Ops."""
from __future__ import annotations

import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None, *, required: bool = False) -> str:
    value = os.getenv(name, default)
    if value is None and required:
        raise RuntimeError(f"Missing required env var: {name}")
    return value or ""


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    session_secret: str
    auth_token: str
    engine_api_base: str
    engine_data_dir: str
    monitor_logs_base_url: str
    monitor_logs_ttl_sec: int
    engine_container_name: str
    diag_timeout_sec: int
    port: int
    log_level: str


def load_settings() -> Settings:
    return Settings(
        session_secret=_env("OPS_SESSION_SECRET", required=True),
        auth_token=_env("OPS_AUTH_TOKEN", required=True),
        engine_api_base=_env("ENGINE_API_BASE", "https://api.luminapp.org"),
        engine_data_dir=_env("ENGINE_DATA_DIR", "/engine-data"),
        monitor_logs_base_url=_env(
            "MONITOR_LOGS_BASE_URL",
            "https://raw.githubusercontent.com/mkmk749278/360-v2/monitor-logs",
        ),
        monitor_logs_ttl_sec=_env_int("MONITOR_LOGS_TTL_SEC", 60),
        engine_container_name=_env("ENGINE_CONTAINER_NAME", "engine"),
        diag_timeout_sec=_env_int("DIAG_TIMEOUT_SEC", 30),
        port=_env_int("OPS_PORT", 8000),
        log_level=_env("LOG_LEVEL", "INFO"),
    )
