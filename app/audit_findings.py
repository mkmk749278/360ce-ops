"""Implementation status of the 2026-07-10 institutional audit findings.

Single source for the ``/audit`` page. Statuses are maintained by hand at
each session end (same discipline as ACTIVE_CONTEXT.md in 360-v2) — this is
a *statement of record*, not live telemetry, so every entry carries the
where/why in ``note``.

Status values: ``done`` · ``partial`` · ``open`` · ``owner`` (waits on an
owner/legal action, not code).
"""
from __future__ import annotations

from typing import TypedDict


class Finding(TypedDict):
    id: str
    severity: str  # critical | high | medium | low
    title: str
    status: str  # done | partial | open | owner
    note: str


# Ordered as in docs/INSTITUTIONAL_AUDIT_2026_07_10.md (360-v2 repo).
FINDINGS: list[Finding] = [
    {
        "id": "F-01",
        "severity": "critical",
        "title": "Net trading edge unproven (book negative over long windows)",
        "status": "open",
        "note": "Needs the 60–90-day frozen-config proof window (≥500 closed "
        "signals). STATISTICAL_CHANGE_POLICY.md adopted (S47) — the clock "
        "starts when live changes stop.",
    },
    {
        "id": "F-02",
        "severity": "critical",
        "title": "No backups / restore drill / DR runbook",
        "status": "done",
        "note": "S47: backup_data.sh + restore_data.sh + nightly vps-backup.yml "
        "(off-site artifact) + DR_RUNBOOK.md. Owner: BACKUP_PASSPHRASE secret + "
        "first restore drill still pending.",
    },
    {
        "id": "F-03",
        "severity": "critical",
        "title": "No legal entity, no counsel review",
        "status": "owner",
        "note": "Not code. Incorporate + retain counsel (ToS / B16 framing / "
        "KYC opinion).",
    },
    {
        "id": "F-04",
        "severity": "critical",
        "title": "Bus factor 1 — sole operator holds all credentials",
        "status": "partial",
        "note": "S47: SAFE_HALT_RUNBOOK.md + CONTINUITY_PACK_TEMPLATE.md shipped. "
        "Owner: fill the vault + grant emergency access to one trusted person.",
    },
    {
        "id": "F-05",
        "severity": "high",
        "title": "Auto entries MARKET-at-dispatch vs measured limit-zone book",
        "status": "open",
        "note": "FSM LIMIT-at-zone + TTL design approved; money-path owner-"
        "sign-off item, ships dark-first as its own change.",
    },
    {
        "id": "F-06",
        "severity": "high",
        "title": "No portfolio-level directional-exposure cap",
        "status": "open",
        "note": "Money-path sign-off item; needs its own spec (max same-"
        "direction positions / beta-weighted cap).",
    },
    {
        "id": "F-07",
        "severity": "high",
        "title": "Stale-price class blinded SL/TP on open positions (3 recurrences)",
        "status": "done",
        "note": "S46 fallbacks (freshness stamps, mark-feed divert, mover "
        "re-seed) + S48 paged invariant: pricing_freshness.json publisher, "
        "watchdog blind-position pager with escalating engine restart, hourly "
        "INVARIANT_WARN.",
    },
    {
        "id": "F-08",
        "severity": "high",
        "title": "Ops dashboard single-password gate on kill switch + docker.sock",
        "status": "done",
        "note": "S47: TOTP second factor on both login paths (#60). Owner: "
        "enroll via generate_totp_secret.py + set OPS_TOTP_SECRET. docker.sock "
        "diag path → engine-side endpoint remains a future hardening.",
    },
    {
        "id": "F-09",
        "severity": "high",
        "title": "Paper book froze silently ~24h — no measurement liveness alert",
        "status": "done",
        "note": "S47: paper-silence invariant in monitor_heartbeat.py pages via "
        "vps-liveness. S48: watchdog re-checks at minutes cadence. Root cause "
        "of the original freeze: diag_paper_health.py on the VPS still pending.",
    },
    {
        "id": "F-10",
        "severity": "medium",
        "title": "Hand-rolled JWT; shared secret signs owner tier; static bypass",
        "status": "open",
        "note": "JWT crypto verified sound (constant-time, alg-pinned, exp-"
        "checked) in S47; owner-key split / static-bypass removal needs design.",
    },
    {
        "id": "F-11",
        "severity": "medium",
        "title": "Signing container root + 0666 socket",
        "status": "done",
        "note": "S47: socket 0666 → 0660 + appgroup ownership; dev/test "
        "fallback loud-warns. Container still root for the volume bind "
        "(AppArmor) — accepted, documented.",
    },
    {
        "id": "F-12",
        "severity": "medium",
        "title": "No APK obfuscation; Assist paywall client-side",
        "status": "done",
        "note": "S47: --obfuscate --split-debug-info on APK + AAB builds, "
        "symbol maps archived (#115). Smoke-test first obfuscated release "
        "before promoting.",
    },
    {
        "id": "F-13",
        "severity": "medium",
        "title": "Legacy self-update-from-GitHub APK path in the Play app",
        "status": "owner",
        "note": "Removal is a distribution decision (pre-Play installs may "
        "still use it).",
    },
    {
        "id": "F-14",
        "severity": "medium",
        "title": "No app-layer API rate limiting beyond OTP",
        "status": "done",
        "note": "S47: per-client sliding-window limiter (240/min default, "
        "health paths exempt, 429 + Retry-After), env-tunable.",
    },
    {
        "id": "F-15",
        "severity": "medium",
        "title": "Money-path state (cohort gate, streaks) in flat JSON files",
        "status": "open",
        "note": "Needs design: migrate to SQLite/Firestore with schema + "
        "checksums.",
    },
    {
        "id": "F-16",
        "severity": "medium",
        "title": "Small-n live changes; no Sharpe/PF/maxDD anywhere",
        "status": "partial",
        "note": "STATISTICAL_CHANGE_POLICY.md (n≥200 / 21d) adopted in S47. "
        "Automated stats report (Sharpe, PF, maxDD, expectancy CI) in the "
        "truth report: not built yet.",
    },
    {
        "id": "F-17",
        "severity": "medium",
        "title": "Assist tier bypasses server blast-radius machinery",
        "status": "open",
        "note": "Document honestly in-app + client-side caps mirroring server "
        "defaults.",
    },
    {
        "id": "F-18",
        "severity": "low",
        "title": "No crash reporting SDK in the app",
        "status": "open",
        "note": "Crashlytics/Sentry with PII scrubbing before user growth.",
    },
    {
        "id": "F-19",
        "severity": "low",
        "title": "No dependency scanning, no pentest",
        "status": "open",
        "note": "Dependabot/pip-audit in CI + one external pentest before "
        "scaling paid users.",
    },
    {
        "id": "F-20",
        "severity": "low",
        "title": "Monitoring/alerting/CI all coupled to GitHub",
        "status": "done",
        "note": "S48: Telegram paging (workflows + watchdog) + healthchecks.io "
        "dead-man pings (watchdog loop + host cron) — alerting no longer "
        "GitHub-only. Owner: create the two healthchecks + set env/secrets.",
    },
]

# Session-48 additions beyond the audit table — shown in their own section
# so the page reflects the full autonomous-ops upgrade.
EXTRAS: list[Finding] = [
    {
        "id": "S48-A",
        "severity": "high",
        "title": "Autonomous self-healing stack (autoheal + watchdog + host layer)",
        "status": "done",
        "note": "Deep healthchecks, autoheal sidecar, watchdog supervisor "
        "(budgeted engine restarts, kill-switch escalation, disk auto-prune), "
        "deploy/host/setup_host.sh. See docs/AUTONOMOUS_OPS.md in 360-v2.",
    },
    {
        "id": "S48-B",
        "severity": "high",
        "title": "Phone-level paging via dedicated Telegram alert bot",
        "status": "done",
        "note": "ALERT_TELEGRAM_BOT_TOKEN/_CHAT_ID (falls back to engine bot). "
        "Owner: create bot + set repo secrets and .env, then drill it.",
    },
]

STATUS_ORDER = {"open": 0, "partial": 1, "owner": 2, "done": 3}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def summary(findings: list[Finding]) -> dict[str, int]:
    out = {"done": 0, "partial": 0, "open": 0, "owner": 0}
    for f in findings:
        out[f["status"]] = out.get(f["status"], 0) + 1
    return out
