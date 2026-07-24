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


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
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
    totp_secret: str
    engine_api_base: str
    engine_data_dir: str
    monitor_logs_base_url: str
    monitor_logs_ttl_sec: int
    engine_container_name: str
    diag_timeout_sec: int
    audit_log_path: str
    app_tokens_path: str
    device_tokens_path: str
    fcm_service_account: str
    agent_redis_url: str
    # Free-run ("held to SL") candle replay — Profit tab only.
    binance_futures_rest_base: str
    # Binance IP-ban circuit breaker for the kline client (Held-to-stop +
    # Dark Signals). Cooldown when Binance 418/403/429s the box.
    binance_ban_cooldown_sec: float
    binance_ban_max_sec: float
    free_run_enabled: bool
    free_run_max_lookback_min: int
    free_run_cache_ttl_sec: int
    free_run_concurrency: int
    # Dark-Signals trailing-exit bake-off (Performance tab). Observe-only shadow
    # measurement: replay real signals under a trailing-only (no-TP) exit.
    dark_signals_enabled: bool
    dark_tf_min: int
    dark_atr_period: int
    dark_atr_mult: float
    dark_sar_step: float
    dark_sar_max: float
    dark_funding_bps_per_8h: float
    dark_warmup_min: int
    dark_max_lookback_min: int
    dark_cache_ttl_sec: int
    dark_concurrency: int
    port: int
    log_level: str


def load_settings() -> Settings:
    return Settings(
        session_secret=_env("OPS_SESSION_SECRET", required=True),
        auth_token=_env("OPS_AUTH_TOKEN", required=True),
        # TOTP second factor (audit F-08). Base32 secret from
        # scripts/generate_totp_secret.py; empty = password-only (legacy).
        totp_secret=_env("OPS_TOTP_SECRET", ""),
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
        # Hashed native-app tokens (see app/app_tokens.py). Same writable
        # volume as the audit log so owner logins survive a container restart.
        app_tokens_path=_env("OPS_APP_TOKENS_PATH", "/data/ops_app_tokens.json"),
        # Registered FCM device tokens for push (Phase 4). Plaintext addressing
        # tokens (not secrets) on the writable volume, shared between the web app
        # (registers) and the monitoring agent (sends).
        device_tokens_path=_env("OPS_DEVICE_TOKENS_PATH", "/data/ops_device_tokens.json"),
        # Firebase service-account JSON (as a string) for FCM HTTP v1 send. When
        # empty, push is disabled and the agent falls back to Telegram only.
        fcm_service_account=_env("FIREBASE_SERVICE_ACCOUNT", ""),
        # Same Redis the monitoring agent writes alert state to — the web
        # dashboard reads it to render the alerts panel.
        agent_redis_url=_env("AGENT_REDIS_URL", "redis://360ce-ops-redis:6379/0"),
        # Profit tab "held to SL" replay. We trade USDT-M futures, so the
        # candles come from Binance Futures (fapi), matching the engine's
        # own kline source. Public market data — no key, read-only.
        binance_futures_rest_base=_env("BINANCE_FUTURES_REST_BASE", "https://fapi.binance.com"),
        # Ban-circuit cooldown: fallback 120s when Binance gives no explicit
        # "banned until" (e.g. a 429), clamped to at most 30m even when the
        # parsed ban is longer (we re-probe rather than trust an unbounded ban).
        binance_ban_cooldown_sec=_env_float("BINANCE_BAN_COOLDOWN_SEC", 120.0),
        binance_ban_max_sec=_env_float("BINANCE_BAN_MAX_SEC", 1800.0),
        free_run_enabled=_env_bool("FREE_RUN_ENABLED", True),
        # Cap the replay window so a signal whose stop is never touched can't
        # drive an unbounded candle pull. 7 days of 1m candles ≈ 7 requests.
        free_run_max_lookback_min=_env_int("FREE_RUN_MAX_LOOKBACK_MIN", 10080),
        # Active rows are re-replayed at most this often; terminal (SL-hit)
        # rows are immutable and cached for the process lifetime.
        free_run_cache_ttl_sec=_env_int("FREE_RUN_CACHE_TTL_SEC", 30),
        free_run_concurrency=_env_int("FREE_RUN_CONCURRENCY", 5),
        # Dark-Signals trailing-exit bake-off. Reuses the same Binance-futures
        # candle source as the held-to-stop replay. Defaults: 5m exit-decision
        # bars, ATR(10)×3 (the SuperTrend(10,3) the owner tested), SAR 0.02/0.2,
        # and a conservative 1bp/8h modelled funding cost (real-money holds pay
        # funding, and a no-TP trail holds far longer than a scalp).
        dark_signals_enabled=_env_bool("DARK_SIGNALS_ENABLED", True),
        dark_tf_min=_env_int("DARK_TF_MIN", 5),
        dark_atr_period=_env_int("DARK_ATR_PERIOD", 10),
        dark_atr_mult=_env_float("DARK_ATR_MULT", 3.0),
        dark_sar_step=_env_float("DARK_SAR_STEP", 0.02),
        dark_sar_max=_env_float("DARK_SAR_MAX", 0.2),
        dark_funding_bps_per_8h=_env_float("DARK_FUNDING_BPS_PER_8H", 1.0),
        # Warm-up window fetched before dispatch to seed ATR/SuperTrend. Fixed
        # (not derived from period/tf) so a swept period/tf still seeds from the
        # same cached candles. 1200m covers period≤60 on 15m bars in one page.
        dark_warmup_min=_env_int("DARK_WARMUP_MIN", 1200),
        dark_max_lookback_min=_env_int("DARK_MAX_LOOKBACK_MIN", 10080),
        dark_cache_ttl_sec=_env_int("DARK_CACHE_TTL_SEC", 30),
        dark_concurrency=_env_int("DARK_CONCURRENCY", 5),
        port=_env_int("OPS_PORT", 8000),
        log_level=_env("LOG_LEVEL", "INFO"),
    )
