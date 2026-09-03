"""Async httpx client for engine ``/api/*`` endpoints."""
from __future__ import annotations

from typing import Any

import httpx

from app.config import Settings


def _named_failure(exc: Exception) -> str:
    """A cause, always — `str(exc)` on a timeout is the empty string.

    `httpx.ReadTimeout()` and several of its siblings carry no message, so the
    wrapper below returned `{"error": ""}`: a failure envelope that every
    reader then has to guess about, and one that fails a truthiness check
    (`if payload.get("error")`) exactly as if nothing had gone wrong. On
    2026-09-03 that took `/signals/ai-governor` past its unreachable branch and
    into *"the engine has no `read.ai_governor` catalog entry — an engine
    predating this page, so it is a deploy question"*, over an engine that had
    the entry and was answering. **A blank needs a cause before it gets a
    caption**, one layer below every page that renders one.
    """
    return str(exc) or f"{type(exc).__name__} (the client gave no message)"


class EngineApiClient:
    """Thin async client. Returns JSON or ``{"error": ...}`` on failure so
    templates can render either shape without crashing on transient outages."""

    def __init__(self, settings: Settings) -> None:
        self._base = settings.engine_api_base.rstrip("/")
        self._token = settings.auth_token
        self._client: httpx.AsyncClient | None = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base,
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=httpx.Timeout(10.0),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _get(self, path: str, **params: Any) -> Any:
        try:
            r = await self.client.get(path, params=params or None)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            detail: Any = None
            try:
                detail = exc.response.json().get("detail")
            except Exception:
                detail = exc.response.text
            return {
                "error": detail or _named_failure(exc),
                "status_code": exc.response.status_code,
                "endpoint": path,
            }
        except httpx.HTTPError as exc:
            return {"error": _named_failure(exc), "endpoint": path}

    async def _post(self, path: str, payload: dict[str, Any]) -> Any:
        """POST a JSON body to an engine control endpoint.

        Returns parsed JSON on success, or ``{"error": ..., "status_code":
        ...}`` on failure so the control route can surface a precise banner
        (409 same-mode, 503 not-initialised, 403 non-owner) instead of a
        generic crash.  The dashboard's static token is owner-tier on the
        engine, so owner-gated writes authorise.
        """
        try:
            r = await self.client.post(path, json=payload)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            detail: Any = None
            try:
                detail = exc.response.json().get("detail")
            except Exception:
                detail = exc.response.text
            return {
                "error": detail or _named_failure(exc),
                "status_code": exc.response.status_code,
                "endpoint": path,
            }
        except httpx.HTTPError as exc:
            return {"error": _named_failure(exc), "endpoint": path}

    async def _delete(self, path: str) -> Any:
        """DELETE an engine control resource.

        Same error contract as :meth:`_post` — a failure returns a dict with
        ``error``/``status_code`` rather than raising, so a control route can
        name what the engine refused instead of rendering a 500.
        """
        try:
            r = await self.client.delete(path)
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            detail: Any = None
            try:
                detail = exc.response.json().get("detail")
            except Exception:
                detail = exc.response.text
            return {
                "error": detail or _named_failure(exc),
                "status_code": exc.response.status_code,
                "endpoint": path,
            }
        except httpx.HTTPError as exc:
            return {"error": _named_failure(exc), "endpoint": path}

    async def health(self) -> Any:
        return await self._get("/api/health")

    async def pulse(self) -> Any:
        return await self._get("/api/pulse")

    async def pairs(self) -> Any:
        return await self._get("/api/pairs")

    async def auto_mode(self) -> Any:
        return await self._get("/api/auto-mode")

    async def signals(
        self,
        status: str | None = None,
        setup_class: str | None = None,
        limit: int | None = None,
    ) -> Any:
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        if setup_class:
            params["setup_class"] = setup_class
        if limit is not None:
            params["limit"] = limit
        return await self._get("/api/signals", **params)

    async def signal(self, signal_id: str) -> Any:
        return await self._get(f"/api/signals/{signal_id}")

    async def positions(self) -> Any:
        return await self._get("/api/positions")

    async def positions_diag(self) -> Any:
        """Operator-facing position-state X-ray.

        Owner-tier endpoint shipped in 360-v2 PR #385 that surfaces, per
        active signal, exactly the inputs ``TradeMonitor._evaluate_signal``
        reads — stored SL/TP, 1m candle wick the monitor is comparing
        against, candle-feed age, and ``sl_breach_distance_pct``.
        Distinguishes stale-feed vs monitor-evaluation-bug vs state-sync-gap
        failure modes when a position closes on Binance but stays ACTIVE in
        the engine.

        Auth: same Bearer token the dashboard already uses for ``/api/*``.
        Static-token bypass is treated as owner-tier by the engine, so the
        dashboard's ``OPS_AUTH_TOKEN``-equivalent token is sufficient.
        """
        return await self._get("/internal/diag/positions")

    async def trail_governor(self) -> Any:
        """The live trailing-exit governor — the mechanism placing real orders.

        Every other trail surface in this repo reads a ledger of what a stop
        *would* have been. This one reads the positions whose stop the engine
        is actually amending, bar by bar, on a real account.

        Read the refusal mix before the rows: a governor with nothing to
        govern is indistinguishable from one that is switched off, opted into
        by nobody, or refusing a stale series — and those have four different
        fixes.
        """
        return await self._get("/internal/diag/trail-governor")

    async def loop_health(self) -> Any:
        """Scan-cycle wall-time, snapshot-writer timing, edge-store flush state.

        The numbers that decide whether the engine container survives. The
        scanner touches its heartbeat once per cycle and ``healthcheck.py``
        kills on that file going stale, so **cycle wall-time is heartbeat
        age** — and it used to live only in a log line, which is why the
        restarts it caused had no explanation anywhere.

        Read it from here rather than timing anything on ops' clock, and grade
        it against the bounds the payload carries rather than a threshold
        invented in this repo.
        """
        return await self._get("/internal/diag/loop-health")

    async def host_resources(self) -> Any:
        """CPU/memory/disk headroom, and the config the engine is really using.

        The owner's opening question of the 2026-08-19 stability session —
        *"engine cpu 221% used is our vps not enough or what"* — had no answer
        on any surface, because CPU only means something against the **quota**
        and the quota was visible over SSH alone.

        Read from the engine, never measured here. Ops runs in a different
        container on a different cgroup, so a reading taken in this process
        would describe this process while looking exactly like a reading of the
        engine.
        """
        return await self._get("/internal/diag/host-resources")

    async def diag_catalog(self) -> Any:
        """What named diagnostics the engine offers. Data, not a mirror.

        Ops keeps no list of what exists — that is the drifting-mirror defect
        this repo has paid for under several names. An entry ops has never heard
        of still renders, under the engine's own label.
        """
        return await self._get("/internal/diag/catalog")

    async def diag_run(self, key: str, args: dict | None = None) -> Any:
        """Run ONE named catalog entry.

        The body carries a catalog KEY, never a command. Ops does not decide
        what a key may do and cannot widen it: `360-v2/src/diag_catalog.py` owns
        that, refuses unknown keys, and is asserted there (per entry, by AST) to
        reach no order, secret or kill switch.
        """
        return await self._post(
            "/internal/diag/catalog/run",
            {"key": key, "args": dict(args or {})},
        )

    async def data_intake(self) -> Any:
        """What the engine is actually reading from Binance.

        Owner-tier endpoint (360-v2 price-action program Phase 1). Assembled
        from state the engine already holds — no new vendor calls — and in
        isolated mode served from the snapshot the engine container publishes,
        because the API container's facade cannot see WS connections, the
        candle store or the rate limiter.
        """
        return await self._get("/internal/diag/data-intake")

    async def router_delivery(self) -> Any:
        """What the router did with what it dequeued — the last hop to a user.

        Enqueue is not delivery. `SignalRouter._process` rejects on twelve
        conditions and counts each keyed by reason **and** `reason:setup_class`.
        Until 2026-08-07 the only caller of `delivery_stats()` was a log line
        that printed the un-keyed half, so the per-setup breakdown — the one
        that says whether a high-volume path is eating the concurrency caps —
        existed and was readable by nobody.

        Owner-tier. In isolated mode the router lives in the engine container
        and its counters are in-process ints, so this is served from the
        snapshot that container publishes.
        """
        return await self._get("/internal/diag/router-delivery")

    async def activity(self, setup_class: str | None = None) -> Any:
        params: dict[str, str] = {}
        if setup_class:
            params["setup_class"] = setup_class
        return await self._get("/api/activity", **params)

    async def agents(self) -> Any:
        return await self._get("/api/agents")

    # ---- Control plane (writes — owner-tier) -----------------------------
    # The dashboard is the engine control plane now that Telegram is
    # unavailable in-region (2026-06-20).  These call the engine's
    # owner-gated write endpoints; the static Bearer token is owner-tier.

    async def set_auto_mode(self, mode: str) -> Any:
        """Flip the engine-wide auto-execution mode (off/paper/live/both).

        Engine returns 409 when the requested mode is already active —
        surfaced as an ``error`` so the route renders it as a no-op notice
        rather than a success."""
        return await self._post("/api/auto-mode", {"mode": mode})

    async def kill_switch_state(self) -> Any:
        """Current global kill-switch state ``{engaged, reason,
        initialised}``."""
        return await self._get("/api/kill-switch")

    async def set_kill_switch(self, engaged: bool, reason: str | None = None) -> Any:
        """Engage (halt all auto-trade) or disengage the global kill
        switch.  Owner-gated on the engine."""
        payload: dict[str, Any] = {"engaged": engaged}
        if reason:
            payload["reason"] = reason
        return await self._post("/api/kill-switch", payload)

    async def auto_trade_global_state(self) -> Any:
        """Global ``auto_trade_globally_enabled`` flag ``{enabled,
        initialised}`` — distinct from the kill switch; disabling halts new
        order placement engine-wide without touching existing positions."""
        return await self._get("/api/auto-trade-global")

    async def set_auto_trade_global(self, enabled: bool) -> Any:
        """Enable/disable global auto-trade. Owner-gated on the engine."""
        return await self._post("/api/auto-trade-global", {"enabled": enabled})

    async def signal_expiry_state(self) -> Any:
        """Time-based signal-expiry backstop ``{enabled, initialised}``. When
        disabled (default), signals run to TP/SL only — no max-hold force-close.
        The 2h auto-trade reconciler stale-close net is independent."""
        return await self._get("/api/signal-expiry")

    async def set_signal_expiry(self, enabled: bool) -> Any:
        """Enable/disable the signal-expiry backstop. Owner-gated on the engine."""
        return await self._post("/api/signal-expiry", {"enabled": enabled})

    async def billing_enabled_state(self) -> Any:
        """Google Play subscription paywall master switch ``{enabled,
        configured, initialised}``.  When disabled the engine's verify + RTDN
        endpoints 503; existing tiers are untouched (they expire naturally).
        ``configured`` False = no package / service account, so billing 503s
        regardless of ``enabled``."""
        return await self._get("/api/billing/play/enabled")

    async def set_billing_enabled(self, enabled: bool) -> Any:
        """Turn the Play billing paywall on/off engine-wide. Owner-gated on the
        engine."""
        return await self._post("/api/billing/play/enabled", {"enabled": enabled})

    async def tunables_state(self) -> Any:
        """Runtime-tunables snapshot ``{initialised, tunables: [...]}`` — the
        owner-controlled engine parameters (noise-floor stops, BE ratchet
        arm/park, cohort-edge gate). Each entry carries label/description/
        type/min/max/unit/category plus the current effective value."""
        return await self._get("/api/tunables")

    async def set_tunables(self, values: dict[str, Any]) -> Any:
        """Update one or more runtime tunables. Owner-gated on the engine;
        the engine validates types and ranges and persists to Firestore."""
        return await self._post("/api/tunables", {"values": values})

    async def dark_promotions(self) -> Any:
        """Promotion rules, the vocabulary they can be built from, and counters.

        ``vocabulary`` is derived by the engine from the dark ledger's own rows,
        not enumerated anywhere — so the form can only offer gates, regimes and
        sessions this engine has actually stamped. A rule keyed on a label the
        detector never emits would be enabled, plausible, and matching nothing
        forever, which on screen is indistinguishable from a rule waiting for
        its setup to appear.
        """
        return await self._get("/api/admin/dark-promotions")

    async def set_dark_promotion(self, rule: dict[str, Any]) -> Any:
        """Create or replace one path's promotion rule. Owner-gated.

        The engine returns what it **stored**, not what was sent — it
        normalises tokens, refuses an unknown direction and clamps the cap — so
        the caller renders the response rather than the form it just submitted.
        """
        return await self._post("/api/admin/dark-promotions", rule)

    async def delete_dark_promotion(self, setup_class: str) -> Any:
        """Remove one path's promotion rule entirely.

        Distinct from disabling it: a disabled rule keeps its conditions, so
        re-arming is one switch. Deleting throws the conditions away.
        """
        return await self._delete(f"/api/admin/dark-promotions/{setup_class}")

    async def reset_signals(self) -> Any:
        """Full-signal reset: clears active signals, history, stats, invalidation,
        and paper broker state for all users.  Owner-gated on the engine."""
        return await self._post("/api/admin/reset-signals", {})

    async def clear_sar_ledger(self) -> Any:
        """Purge the SAR exit shadow ledger. Owner-gated on the engine.

        Separate from :meth:`reset_signals` on purpose — that clears the live
        signal feed, this clears a measurement window. Idempotent."""
        return await self._post("/api/admin/sar-ledger/clear", {})

    async def close_signal(self, signal_id: str) -> Any:
        """Force-close ONE stuck OPEN signal via the engine's expiry-close path.
        Owner-gated on the engine; idempotent (a signal already gone is not an
        error)."""
        return await self._post(
            "/api/admin/close-signal", {"signal_id": signal_id}
        )

    async def referral_commissions(self, status: str | None = None) -> Any:
        """Referral commission ledger (Phase 2, 2026-07-21) — the rows the
        owner pays out manually.  Owner-gated on the engine."""
        params: dict[str, Any] = {}
        if status:
            params["status"] = status
        return await self._get("/api/referral/admin/commissions", **params)

    async def mark_referral_commissions_paid(self, commission_ids: list[int]) -> Any:
        """Flip settled commission rows accrued → paid after a manual
        payout.  Owner-gated on the engine."""
        return await self._post(
            "/api/referral/admin/commissions/mark-paid",
            {"commission_ids": commission_ids},
        )

    async def trial_funnel(self, limit: int = 200) -> Any:
        """Signup free-trial funnel (360-v2, 2026-07-25) — cohort, offers,
        claims, conversions, beside the two dark-first flag states.

        This is the surface that makes the trial's dark window readable:
        while ``SIGNUP_TRIAL_ENABLED`` is false the engine still stamps every
        eligible user, so the owner sees the real would-be cohort before
        deciding to switch grants on.  Owner-gated on the engine."""
        return await self._get("/api/trial/admin/funnel", limit=limit)

    async def user_lookup(self, phone: str) -> Any:
        """Look up a user's current tier/paid_until/display_name by phone —
        what the ops UI shows before the owner decides whether/what to
        grant.  Owner-gated on the engine; a 404 (unknown phone) surfaces
        as ``{"error": ..., "status_code": 404}`` via ``_get``'s plain
        ``httpx.HTTPError`` branch."""
        return await self._get("/api/admin/users/lookup", phone=phone)

    async def grant_tier(
        self,
        phone: str,
        tier: str,
        duration_days: int | None = None,
        reason: str | None = None,
    ) -> Any:
        """Manually grant (or revoke, ``tier="free"``) a subscription tier —
        tester/influencer comp, not a Play Billing purchase.  Owner-gated
        on the engine; every grant carries an expiry (engine defaults
        ``duration_days`` to 30 when omitted; ignored for ``tier="free"``)."""
        payload: dict[str, Any] = {"phone": phone, "tier": tier}
        if duration_days is not None:
            payload["duration_days"] = duration_days
        if reason:
            payload["reason"] = reason
        return await self._post("/api/admin/grant-tier", payload)

    async def set_exit_mechanism(
        self,
        phone: str,
        exit_mechanism: str,
        reason: str | None = None,
    ) -> Any:
        """Opt one account into (or out of) the live trail governor.

        A **money-path** write: anything but ``default`` means the engine
        cancels that user's evaluator SL and TP ladder at handover and manages
        the exit itself. It still does nothing unless the engine-wide
        ``trail_governor_enabled`` tunable is ON — the response carries
        ``governor_enabled`` so the caller can say which half is missing
        rather than reporting a bare success.
        """
        payload: dict[str, Any] = {
            "phone": phone,
            "exit_mechanism": exit_mechanism,
        }
        if reason:
            payload["reason"] = reason
        return await self._post("/api/admin/users/exit-mechanism", payload)
