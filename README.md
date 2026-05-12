# 360 CE Ops

Diagnostic dashboard for the 360 Crypto Eye engine — pulse, truth report, signals, invalidation audit, performance, on-demand diag scripts.

Design doc: [`mkmk749278/360-v2:docs/360CE_OPS_PLAN.md`](https://github.com/mkmk749278/360-v2/blob/main/docs/360CE_OPS_PLAN.md).

Operating context for the wider system: [`mkmk749278/360-v2:OWNER_BRIEF.md`](https://github.com/mkmk749278/360-v2/blob/main/OWNER_BRIEF.md) + [`ACTIVE_CONTEXT.md`](https://github.com/mkmk749278/360-v2/blob/main/ACTIVE_CONTEXT.md).

## Running locally

```bash
cp .env.example .env
# Fill in OPS_SESSION_SECRET and OPS_AUTH_TOKEN at minimum
docker compose up --build
```

Visit <http://localhost:8088>, sign in with `OPS_AUTH_TOKEN`.

For a development run without Docker:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
OPS_SESSION_SECRET=dev OPS_AUTH_TOKEN=dev uvicorn app.main:app --reload
```

## Env vars

| Var | Required | Default | Notes |
|---|---|---|---|
| `OPS_SESSION_SECRET` | yes | — | Used by starlette SessionMiddleware. Generate with `python -c "import secrets; print(secrets.token_urlsafe(32))"`. |
| `OPS_AUTH_TOKEN` | yes | — | Password for `/login`. Reuse the engine's `API_AUTH_TOKEN` so the same token is also valid as a Bearer for `/api/*`. |
| `ENGINE_API_BASE` | no | `https://api.luminapp.org` | Engine REST base URL. |
| `ENGINE_DATA_DIR` | no | `/engine-data` | Mount point for engine's `data/` (read-only). |
| `MONITOR_LOGS_BASE_URL` | no | `https://raw.githubusercontent.com/mkmk749278/360-v2/monitor-logs` | Truth report source. |
| `MONITOR_LOGS_TTL_SEC` | no | `60` | In-memory cache lifetime for monitor-logs fetches. |
| `ENGINE_CONTAINER_NAME` | no | `engine` | Container to `docker exec` for diag scripts. |
| `DIAG_TIMEOUT_SEC` | no | `30` | Hard cap on diag script runtime. |
| `OPS_PORT` | no | `8000` | Uvicorn bind port inside the container. |
| `LOG_LEVEL` | no | `INFO` | |

## Architecture

| Layer | Choice |
|---|---|
| Backend | Python 3.11 + FastAPI |
| Templates | Jinja2 (server-rendered) |
| Interactivity | HTMX (CDN, no JS build) |
| HTTP client | httpx (async) |
| Auth | starlette SessionMiddleware + HttpOnly cookie |

Data sources (one direction — read-only consumer):

1. Live engine REST: `https://api.luminapp.org/api/*`
2. Engine data files mounted read-only at `/engine-data`: `signal_performance.json`, `invalidation_records.json`, `signal_history.json`
3. `monitor-logs` branch artifacts via `raw.githubusercontent.com`, TTL-cached
4. `docker exec engine python /app/scripts/diag_*.py` for on-demand diagnostics

## Routes

| Path | Purpose |
|---|---|
| `/login`, `/logout` | Password gate (single owner) |
| `/` | Engine pulse + auto-mode + heartbeat (auto-refresh every 30s) |
| `/truth` | Runtime truth report viewer (markdown + JSON) |
| `/signals` | Signal table with status / setup-class filters |
| `/signals/{id}` | Per-signal drill-down: live + performance + invalidation + history |
| `/diag/geometry` | On-demand `diag_geometry_vs_reality.py` runner |
| `/invalidations` | PROTECTIVE / PREMATURE / NEUTRAL histograms by setup × kill-reason |
| `/performance` | Per-symbol / per-setup / per-regime rolling stats over 7d / 30d / all |
| `/healthz` | Unauthenticated liveness probe |

## Security notes

- Single-password gate; not multi-user. Owner-only.
- `docker.sock` is mounted into the ops container to enable the diag runner. This grants root-equivalent access to the host. Acceptable for an owner-only single-tenant ops box; **do not** broaden user access without first migrating diag execution to an engine-side `/internal/diag/*` endpoint and removing the socket mount.
- HTTPS termination is on the reverse proxy (Caddy / Nginx). The container listens on plain HTTP at `127.0.0.1:8088`.
- The session cookie is signed (HMAC) but not encrypted — only `{"authenticated": true}` ever lives in it.
- No writes are issued to the engine from this dashboard. Control surfaces stay in Telegram / Lumin app per the design plan.

## Deploy

Push to `main` → GitHub Actions builds the image, pushes to GHCR, and SSH-deploys the VPS service via `docker compose pull && docker compose up -d`.

Required GitHub Actions secrets:

- `VPS_HOST`, `VPS_USER`, `VPS_SSH_KEY` — SSH into the engine VPS
- `GHCR_READ_TOKEN` — VPS-side `docker login` to pull the private image

No engine code is modified by this repo. The `/opt/engine/data` host path is mounted read-only into the ops container.
