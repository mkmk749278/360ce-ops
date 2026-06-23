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


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


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
    audit_log_path: str
    agent_redis_url: str
    # Free-run ("held to SL") candle replay — Profit tab only.
    binance_futures_rest_base: str
    free_run_enabled: bool
    free_run_max_lookback_min: int
    free_run_cache_ttl_sec: int
    free_run_concurrency: int
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
        # Append-only control-action audit log.  Put it on a writable
        # volume in prod (not the read-only /engine-data mount) so it
        # survives container restarts.
        audit_log_path=_env("OPS_AUDIT_LOG", "/data/ops_audit.jsonl"),
        # Same Redis the monitoring agent writes alert state to — the web
        # dashboard reads it to render the alerts panel.
        agent_redis_url=_env("AGENT_REDIS_URL", "redis://360ce-ops-redis:6379/0"),
        # Profit tab "held to SL" replay. We trade USDT-M futures, so the
        # candles come from Binance Futures (fapi), matching the engine's
        # own kline source. Public market data — no key, read-only.
        binance_futures_rest_base=_env("BINANCE_FUTURES_REST_BASE", "https://fapi.binance.com"),
        free_run_enabled=_env_bool("FREE_RUN_ENABLED", True),
        # Cap the replay window so a signal whose stop is never touched can't
        # drive an unbounded candle pull. 7 days of 1m candles ≈ 7 requests.
        free_run_max_lookback_min=_env_int("FREE_RUN_MAX_LOOKBACK_MIN", 10080),
        # Active rows are re-replayed at most this often; terminal (SL-hit)
        # rows are immutable and cached for the process lifetime.
        free_run_cache_ttl_sec=_env_int("FREE_RUN_CACHE_TTL_SEC", 30),
        free_run_concurrency=_env_int("FREE_RUN_CONCURRENCY", 5),
        port=_env_int("OPS_PORT", 8000),
        log_level=_env("LOG_LEVEL", "INFO"),
    )
