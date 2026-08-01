# CLAUDE.md — 360 CE Ops

Guidance for Claude Code sessions working in this repository.

## Companion repo

This dashboard is a consumer of — and the control plane for — `mkmk749278/360-v2` (the engine). Before working here, read in order:

1. **`ARCHITECTURE.md` in 360-v2** — the whole system on one map (all four repos, the
   three planes, state map, deployment). §1 says what this repo owns and must never do;
   §4.6 is this repo's own subsystem entry. Read it first — the rest lands faster after.
2. `OWNER_BRIEF.md` in 360-v2 — operating contract, role boundaries, business rules
3. `ACTIVE_CONTEXT.md` in 360-v2 — current engine state, open queue
4. `docs/360CE_OPS_PLAN.md` in 360-v2 — design document for this app

`ARCHITECTURE.md` is **not mirrored here** — one canonical copy, by the same rule this
repo already lives by: the fix for a drifting mirror is not a second mirror.

## Your role here

Same as in 360-v2: CTE with full technical ownership. Read briefs every session. Update `ACTIVE_CONTEXT.md` (in 360-v2) at session end if anything here changed materially.

For every change, ask: **"how does this make signals more profitable for paid subscribers?"** If the answer is engineering polish without a measurable effect on subscriber-visible quality, defer.

## Scope of this repo

Diagnostic dashboard **and the engine control plane** (promoted 2026-06-20). Diagnostic-first, but no longer read-only: the owner's manual control of the engine (auto-mode flips, kill switch, and — over time — manual close / per-user settings) lives here. Monitoring of signals/positions/PnL stays primarily in the Lumin app; **ops owns control**.

> **Correction 2026-07-25: Telegram is NOT banned in India — it works.** This doc
> previously gave "Telegram is banned in-region" as the *reason* ops owns control.
> That premise was false. The decision stands on its own — ops is the auditable,
> owner-gated, PRG-confirmed control surface and that is where control belongs —
> but it is a standing owner decision, not a consequence of Telegram being
> unreachable. Do not re-derive product or routing choices from the old premise.

Three deliverables live in this repo:

- **The web dashboard + control plane** (`app/`) — FastAPI + HTMX, deployed at `ops.luminapp.org`.
- **The 24/7 monitoring agent** (`app/agent/`) — a separate container (`python -m app.agent.runner`, 60s poll cycle) running the Tier-0 safety detectors (naked position, signing-service down, engine/Redis stale, …). Alert state is a Redis-backed dedup/escalation FSM (`alert:state:{fingerprint}`, in-memory fallback); it pages via FCM push (Telegram notifier retained in code) and pings a healthchecks.io heartbeat after each clean cycle. The web app's `/alerts` page reads the same Redis state.
- **The native ops mobile app** (`mobile/`) — owner-only Flutter Android app over the ops `/api/v1` JSON surface (see `mobile/README.md` and `docs/OPS_MOBILE_APP_PLAN.md`). Its CI (`.github/workflows/mobile-apk.yml`) generates the Android scaffolding with `flutter create`, so `mobile/android/` is not committed.

**This repo is also where "dark" becomes visible.** Money-path work in the engine
ships dark — invisible to users, *live to the owner* (see `CLAUDE.md § Project
Phase` in 360-v2). The measurement runs from day one; ops is where the owner
actually reads it. So a dark engine change is not finished until its ops surface
exists: a panel, a table, or a truth-report section readable the same day.
"Measured but nowhere to look" is an unfinished change, and building that surface
is this repo's job.

Control doctrine (non-negotiable for every write surface):
- **Owner-gated end to end.** Writes call the engine's owner-gated endpoints; the dashboard's static Bearer token is owner-tier on the engine. Everything stays behind the auth gate (password + TOTP when enrolled).
- **Audited.** Every control action is appended to the audit log (`app/audit.py` → `OPS_AUDIT_LOG`). Audit writes are best-effort and never block the action — being able to hit the kill switch matters more than logging it.
- **PRG + confirm.** Control routes use POST→redirect→GET so a refresh can't re-fire an action; destructive actions (engage kill switch, switch to LIVE) require an explicit confirm.
- **The engine is the source of truth.** Ops never holds control state locally; it reads it back from the engine after every write.

## Re-check the claim before you test it (mirrors 360-v2's)

The engine's `CLAUDE.md` carries the full rule; it applies here with extra force,
because **this repo's output is sentences as much as numbers.** A page states what
its figures mean, and a wrong sentence over correct figures is still a wrong page
— "copy is part of the measurement", one level up.

On 2026-08-01 `/signals/entry-features` shipped a paragraph telling the owner that
`TREND_PULLBACK_EMA`'s TP1 sat inside its stop and implying the fix was to raise
it. Both numbers in that paragraph were right and the implication was wrong;
testing it took one query against data already exported. Before writing a
sentence that recommends an action, falsify the action, not just the number.

## Change-management protocol (mirrors 360-v2's)

Every change ships via PR. Fresh topic branch off `main`. Design-summary in the PR body before code review. Never push to `main` directly — auto-deploy on `main` push ships in ~60s and bypasses review.

## What the Strategy Lab is (and what it must not do)

`/strategy-lab` is the owner-facing surface of the engine's **Autonomous Portfolio**
(Layers A–G — see `OWNER_BRIEF.md § 3.11` in 360-v2). It renders the
Strategy×Context edge matrix, the per-gate KEEP/TUNE/DROP audit, the allocator's
would-do panel, and the counterfactual measurement arms.

Two rules, both learned the hard way:

- **Measurement arms are not strategies.** `@FIXED`/`@ATR`/`@TUNED`/`@DSV2`/`@GOV`/
  `@SARBASE`/`@SAREXIT` are stamped from the *same candidates* as the real rows, so
  counting them in the per-strategy rollup double-counts the candidate. The list
  lives in `strategy_lab.MEASUREMENT_SUFFIXES` and mirrors the engine's
  `geometry_ab._VARIANT_SUFFIXES` — **keep them in sync.** They drifted once
  (@TUNED/@DSV2/@GOV shipped engine-side and ops never learned about them) and
  quietly inflated the rollup for a week.
- **Ops ports the engine's math, it does not invent it.** Reducers here are ports of
  engine functions; the thresholds are mirrored and displayed in the UI footer for
  honesty. If a number here disagrees with the truth report, ops is wrong. Mirror
  the engine's **denominators** too: a rate divided by "all bucketed rows" where the
  engine divides by "resolved rows" agrees only until the two populations differ.
- **A panel must be measured on the population the page is showing.** A summary
  computed over the whole ledger while the table beside it is filtered is not a
  summary of anything the owner is looking at. `/signals/sar` shipped a split panel
  reading all 267 rows above a table showing 149 (#90, fixed #91) — pooling
  delivered, router-dropped and gate-killed candidates into the one number an
  adoption decision reads. Every count is measured with **every filter applied
  except its own**; a selector applied to its own counts makes each option describe
  only itself.
- **Truncate after filtering, never before.** `reduce_sar_signals` defaulted to
  `limit=300` and cut inside the reducer, ahead of `filter_sar_signals` — so every
  filter ran on the newest 300 pairs of a ~2,000-pair ledger, roughly 4 hours of it.
  That starves the rarest and most important population hardest: **"Delivered to
  users" silently meant "delivered, within the newest 300"** — 4 emitted rows against
  152 enqueued and 144 suppressed, and only the delivered ones can justify changing
  what users receive (#97, 2026-07-28). A row cap is a *render* bound: apply it in the
  route, after every filter, and say on screen when it bit.
- **Disclose concentration; don't silently average it.** Overlapping entries into
  one move resolve at the same exit price and are not independent evidence — three
  BUSDT rows stamped 00:04 / 00:47 / 01:34 all exited at 0.1959 and carried 3/8 of a
  bucket. Show the distinct-outcome count beside the trade count. De-duplicating is
  a judgement call; counting them silently is not.
- **Copy is part of the measurement.** A panel asserting "a near-deterministic loss"
  directly above a bucket reading 38% win rate is wrong on screen even when every
  number is right, and explanatory text that names a *cause* for missing rows must
  be true of those rows. Say whether an R is gross or net — #90 predicted a
  cost-inclusive figure and then measured a cost-free one.

## Replayed vs live — what `/signals/sar-live` shows that no other page can

Added 2026-07-30 (#106) alongside engine #832. Every measurement surface in this repo
before it was a **replay**: a candidate is stamped, and a resolver scores it later.
That answers *"would this have been profitable"* and says nothing about whether the
mechanism is **operable** — whether the level can be computed in time, parked, and
acted on before the outcome is known.

It also inherits the resolver's health, which is not a theoretical risk: on
2026-07-30 `/signals/sar` had 8 of 19 rows unresolved and **all four of the window's
winners were among them**, so the −0.682R on screen was a fact about a starved refresh
budget. A loss-selected sample is worse than no sample, because it looks like an answer.

`/signals/sar-live` reads arms the engine stepped forward inside its monitor loop, so
an open row carries the stop the mechanism *would have parked right now*. Three rules
specific to it:

- **The file's age leads the page, before any number on it.** A frozen arm file renders
  as a book of open trades beside accurate live prices — exactly how the replay ledger
  looked healthy on 2026-07-29 with an 11.6h-old newest resolution. **A working price
  feed is not evidence the measurement is running.**
- **…and the file's age is not the arm's age (#108).** The first cut graded liveness on
  the file's mtime alone, then fetched the live Binance price itself and printed it
  beside whatever stop the row happened to hold, under the words *"the stop the
  mechanism would have parked right now"*. On 2026-07-30 the file was **18 seconds old
  and correct** while two KORUUSDT arms in it had consumed **zero** bars in 2h19m — one
  with a parked stop the price had already crossed by 5.45%. The page reported "LIVE — 3
  arms running, stepped inside the monitor loop." **A surface cannot grade its own
  liveness on a clock it supplies**: the rule above, broken from the other side, by us.
  Freshness is per-arm — the engine stamps `last_advance_at`, `bars_behind` and
  `stalled`, this page leads every open row with them, and a negative distance-to-stop
  is badged `crossed` rather than printed as one more percentage, because it is a
  contradiction (the level was breached and the arm did not act), not a near miss.
- **Missing, empty and stale are three different states, and the engine's 60s heartbeat
  is what separates them.** File missing = the monitor loop is not running the arms;
  current and empty = running, nothing open (the quiet case, *not* a fault); stale =
  the loop stopped stepping. The first cut of the engine ledger wrote only on change,
  so an idle engine produced no file and this page reported a fault that was not
  happening — this repo's own *"blank needs a cause before it gets a caption"* rule,
  broken one repo over and caught by the owner minutes after deploy.
- **An arm can be born a replay, and the page that says "this is not a replay" owns
  checking it (#109, engine #836).** An arm anchors to the newest closed bar the store
  holds at creation, and nothing checked that the bar was *current*. For a promoted
  mover — REST re-seed only, no WS klines — ACHUSDT's 15m series was ~40h stale, so the
  arm read SAR-at-entry off a 40h-old bar and its first advance walked 39.5 hours of
  history in one pass. **Every freshness column #108 added read healthy on it**, because
  by the time it was written the arm genuinely was fresh — freshness answers "is this
  stop current", never "was this row earned". The engine now stamps `anchor_bars_behind`
  / `first_step_bars`; this page grades on them, runs a bars-vs-lifetime check on rows
  written before them, and **excludes a replayed arm from every R** — counted, named,
  never averaged in. Two corollaries: a **missing stamp is not a pass** (the rows
  without one are exactly the rows with the bug, so they get `unverified`, its own
  bucket), and the panel **renders whether or not anything failed** — a check that
  appears only when it trips teaches the reader that its absence means "fine" when it
  equally means the check stopped running.
- **Where two denominators are defensible, publish both (#109).** R divides by the SL
  distance at entry — but when SAR governs, that stop is *cancelled*, and SAR's own was
  wider than it on 14 of 27 handovers in the owner's window (mean 1.25×, max 2.81×). The
  same loss reads −1.90R against the designed risk and −0.71R against the risk actually
  parked. `r_level_risk` sits beside `r_level` and neither is called "the" R; the win
  rate is stated **once**, because a positive denominator cannot change a sign and
  printing it twice would imply two populations. Exactly the both-fills rule below, one
  denominator over.
- **Both fills are shown and neither is called the result.** `@level` is the parked stop
  touched intrabar; `@confirm` is the flip confirmed at the close and exited at market.
  Their difference is the cost of confirmation. There is deliberately no blended `avg_r`
  and a test asserts the key does not exist — collapsing them before that cost is known
  is choosing the answer, and the one you would choose is the flattering one.
  Timeframes (5m/15m) are reported separately for the same reason: pooled, the headline
  moves with the timeframe mix instead of the mechanism.

## The dark feed (`/signals/dark-live`) — a page that says nothing until a row closes

Added 2026-07-31 (#111, engine #839) for the owner's question: *"15 paths and only
`MOVER_TREND_PULLBACK` is our volume — what happened to the others?"* Each row is a
real signal the scanner was willing to send with one gate loosened, diverted before
the queue, so nothing here reached a channel, a push, the app feed or an order. That
sentence and *"a user would have seen this"* are different, and the page must never
merge them — the router's second layer is not applied, so the count over-reports a
feed size.

Two rules the page learned the day after it shipped:

- **A measurement page that shows nothing until a row resolves cannot answer the
  question it was built for.** The first cut rendered open rows as entry price and
  two dashes; a dark row resolves up to six hours later and never at all if its
  candles stop, so the owner's first read was a list of symbols (owner-caught
  2026-07-31, hours after deploy). Open rows now carry a live mark, the unrealized
  move, unrealized R against the *engine's* stamped SL distance, and room to each
  level — with the whole book marked in **one** request (`/fapi/v1/ticker/price`),
  never one per row, because a page whose cost scales with open trades is the hot-path
  shape the cost rules forbid. Unrealized numbers stay out of every realized column
  and out of the per-path table entirely.
- **MFE without MAE bounds nothing.** Every lane recorded how far a trade ran in
  our favour and none recorded how far it went against us first, so no question
  about stop distance could be answered — the optimistic reading of "tighten the
  stop" and the pessimistic one differed by more than the whole edge under
  discussion, because the gap is exactly *did the winners survive it*. Both
  halves now render side by side on `/signals/dark-live` and `/signals/sar-live`,
  and both are in the exports; a one-sided excursion column looks complete and
  silently answers nothing.
- **`INSUFFICIENT` has two causes and they must never be pooled** (2026-08-01).
  `no_walk` is a series that never arrived; `partial_window` is a row that *was*
  walked, where the walk did not cover its window. The second used to be called
  an EXPIRED and scored 0R — a claim about bars nobody looked at. ROBOUSDT
  expired on 309 bars of a 362-minute window and ARBUSDT on 329 of 365, so 89
  minutes of unexamined bars were reported as the setup doing nothing, and a
  touch inside them would have been booked as a zero. Different faults, different
  fixes: "blank needs a cause before it gets a caption", applied to a status.
- **…and a mark is only honest beside a row that can say whether it is still true.**
  This is #108 one page over, and it was avoided rather than paid for a third time:
  freshness is graded on the **engine's** stamps (`last_resolved_at`, `bars_behind`,
  `resolve_misses`), never on ops' own clock, and it leads the live columns. A row
  written before those stamps is `unverified` — its own bucket, because a missing
  stamp is not a pass. A level already crossed while the row still says OPEN is
  badged, not printed as one more negative percentage: the resolver walks bars in
  order and would have closed it, so those bars never arrived. And the open panel
  publishes **both** denominators — every marked row, and only the rows still being
  advanced — which agree while the lane is healthy and diverge exactly when it is not.

## `/signals/entry-features` — now vs later, on the same rows

Added 2026-08-01 (engine `src/entry_features.py`) for the owner's question:
*"taking entry is matter, how we are taking entry based on only EMA or what,
what if we add some more data to that"* — and *"we need to know the difference
as of now vs later"*. Extended the same day to every dark-feed path, on the
owner's follow-up: *"concentrate on entry, on which bases entry is confirming
especially on Trend pullback EMA and mover AVWAP"*.

**A feature set is not portable just because the code that computes it is.** The
first cut of that extension copied MVRTP's feature list onto every path, and the
owner caught it: that list was chosen for MVRTP's blindness — a three-SMA
pullback trigger that never looks at volume — so it measures nothing on paths
that fail elsewhere. What each path needs comes from reading *its* mechanism:

| Path | Confirms on | What it has no notion of |
|---|---|---|
| `MOVER_TREND_PULLBACK` | price vs SMA7/25/99 + one ATR | volume, and everything in `smc_data` |
| `TREND_PULLBACK_EMA` | 1H EMA21/50, then six **booleans** on 5m | the magnitude of any of them |
| `MOVER_AVWAP_SCALP` | anchored VWAP + slope + volume | where in the move it is |

TPE records *that* each threshold was crossed and never by how much, so its
features are the magnitudes behind its own gates (1H trend separation, where in
the 40–60 RSI band it fired, how far `prev_high` broke, how much of the impulse
leg the pullback gave back). MVAVW already gates on volume and slope; the anchor
is computed and then used only to produce a VWAP, so its age, the leg's size and
the number of prior returns to it are unconsulted — and `leg_move_pct` is exactly
what `execution:overextended` is about, the gate carried past on 21 of the 65
dark rows.

**The registry is not mirrored here.** Which features a path declares, in what
order, and which way a rule filters all arrive in the ledger's `spec` block,
written by the engine that decides them. `FALLBACK_SPEC` exists only so a
pre-`spec` ledger still renders, and the page **says on screen** when it is used
— a silent fallback is a mirror nobody knows is a mirror. This is the direct
lesson of `MEASUREMENT_SUFFIXES`, which drifted for a week: the fix for a
drifting mirror is not a second mirror, it is one writer and one reader.

**`tp1_r_multiple` is the row to read first**, and it is not a candidate filter
like the others — it is chosen by the evaluator, exact at stamp time, and it
bounds what every other row can achieve. `TREND_PULLBACK_EMA` ran a median
designed R:R of **0.79** in the 2026-08-01 dark window (TP1 nearer than the
stop), needing 54% to break even and posting 35% over 17 decided rows; TP1 is the
nearest 5m swing extreme, capped by ATR percentile, with nothing flooring it,
while `_enforce_tp_ladder_monotonicity` floors tp2 at 2.0R and tp3 at 4.0R.

**That is a description, not a lever, and this page said otherwise for half a
day.** Simulated on the 11:00 window (55 decided rows), flooring TP1 at 1.0R moves
the book from −0.081R to between −0.186R and −0.404R; at 1.5R, to between −0.245R
and −0.536R. It reproduces on the 08:26 window, so the direction is not one
export's artefact. The winners barely clear their current targets — TPE's hit at a
median 0.59R against a 0.89R peak — and only 27% of decided trades ever moved 1R
in our favour, with a median excursion of 0.53R. The low target is what harvests a
move that small. Read the column as *what the book can possibly earn*; it points
at entry quality and loss size, and away from the targets. Any change to it is
TP/SL shape and therefore owner-sign-off either way.

Rules specific to this page:

- **Three buckets, never two.** `keep` / `drop` / **`unknown`**. Folding rows
  whose feature never computed into `keep` is how a candidate rule takes credit
  for rows it never filtered. The unknown count is on screen for every split.
- **The baseline does not move.** "Now" is the whole joined book on every row of
  the table, so the only thing changing down the page is which subset a rule
  keeps. A test asserts `now` is identical across two different thresholds — if
  it drifted, every Δ would be measured against a different thing.
- **Read n and kept-fraction before Δ.** A rule keeping 95% of the book has not
  been tested whatever its Δ says. The window that prompted the work had 46 MVRTP
  signals and 19 tested cells, one of which cleared 95% *in the backwards
  direction* against a ~62% familywise chance of a spurious hit.
- **Both denominators, as everywhere else.** R divides by the engine's
  `sl_distance_pct_at_entry` (#848); gross % sits beside it because the R-scored
  subset is not a random sample of the book.
- **An em-dash is not a zero.** A missing order book is not balanced depth; a
  missing level book is not a clear path. The engine returns `None` with a reason
  and this page renders the dash.
- **Never pool timeframes silently.** TPE triggers on 5m and the mover paths on
  15m; a volume ratio over 5m bars and one over 15m bars are different
  measurements. Every split reports the timeframes it covered and badges itself
  `mixed` when there is more than one, and the unfiltered view says on screen
  that it is pooling paths at all.
- **A directional feature must be signed toward the trade.** `cvd_slope` and
  `book_imbalance` shipped raw in schema 1 and were split with a single "higher
  is better" rule, which scores every SHORT backwards — a falling CVD is the dip
  being sold, bad for a long and exactly what a short wants. The delivered book
  is ~50/50 by side, so the error did not show up as an empty column; it just
  made both features look like noise. They are `_aligned` from schema 2 on.
- **Say how many cells you drew.** "Best of N" is not a fact about the winner
  until N is on screen — the top row of a long table beats a coin flip by
  construction. The page prints the count and names
  `FAILED_AUCTION_RECLAIM` (+0.846R on three rows, CI [−1.00, +2.00], promotion
  requested within the day) as the standing example.

**Route ordering, paid for on the first cut:** `signal_detail` registers
`/signals/{signal_id}`, which matches any `/signals/<literal>`. This page was
included *after* it, so requests 404'd while its route object sat in `app.routes`
looking perfectly registered — the debugging cost was entirely in trusting the
route list over the request. Every literal page under `/signals/` must be
included **before** `signal_detail.router`; `tests/test_entry_features.py`
asserts the ordering in `app/main.py` as well as the live request.

## Recorded vs reconstructed — the line `/track-record` must not cross

Added 2026-07-28 (#98) for the owner's paper-trading problem: per-user paper books
start empty, so a new subscriber waits a week to a month before their own book says
anything, while the engine has recorded every closed signal all along.

`/track-record` is the **recorded** surface. Every row is a signal the router
confirmed, tracked forward in real time by `trade_monitor`, written at its terminal
transition. The Profit tab's `free_run` / `dark_signals` / `exit_backtest` are
**reconstructed** — they replay candles and rebuild an outcome after the fact.

**Never put a reconstructed number on `/track-record`, and never merge the two into
one figure.** The owner ruled out backfill explicitly. Counterfactuals are optimistic
(~0.38R measured, see the engine's `CLAUDE.md`), and a reconstructed result wearing a
track record's name is the single most dangerous artefact this repo could produce —
it is the number a subscription decision would rest on. There is a route test
asserting the page still says "recorded, not reconstructed"; copy is part of the
measurement.

Three rules the page carries, all ports rather than inventions:

- **R, not portfolio %.** A portfolio return needs an assumed position size, and the
  engine's `MAX_SAME_DIRECTION_GLOBAL=3` means two users on identical settings get
  different fills — so a percentage would not be a fact about anyone.
  `R = pnl_pct / sl_distance_pct_at_entry` is the same denominator the SAR arm and
  the edge matrix divide by, so a number here is comparable with one there.
- **Divide by the risk taken, not the stop on the record (2026-08-01).** This page
  divided by `stop_loss` for its first three days, and the engine *moves* a signal's
  stop in place as the trade runs — BE shift, TP1 park, trail — so that field is the
  stop as of the **exit**. A trade BE-shifted and then stopped out for −0.1% scored
  exactly −1.00R, indistinguishable from one that gave back its whole designed risk:
  9 of 28 SL_HITs in the owner's window, and the closed book read −0.088R against a
  true +0.160R. **The sign of the headline was an artifact of the denominator.** Ask
  of every ratio here whether its denominator is still the thing it was when the
  numerator started. Engine `sl_distance_pct_at_entry` (#817's class, fixed the same
  way) is the one to use.
- **Refuse, don't clamp — and name the reason.** A record without a usable entry
  risk has no R: counted in the trade count, excluded from every R figure, and the
  shortfall stated on screen. Scoring it 0R would drag the averages toward zero and
  make missing data read as mediocre performance. The shortfall is split on screen
  into `awaiting_engine_stamp` (closed before the field existed — unrecoverable,
  because the stop has already moved, and it shrinks on its own) and `no_geometry`
  (stamped by today's engine and still unusable — a producer fault that does *not*
  age out). Pooling them would report a live fault that is not happening, which is
  this repo's own "blank needs a cause before it gets a caption" rule.
- **Bucket by CLOSE time.** A day's PnL is the PnL realised that day; bucketing by
  entry credits Monday with a trade that closed Thursday.

`entry_regime` arrives from engine #817 and **cannot be backfilled** — the regime at
entry is knowable only at entry — so records closed before that deploy render as their
own `UNPLACED` bucket rather than being folded into a real regime.


## Data sources (one-line each)

| Source | Module |
|---|---|
| Live engine REST API (`/api/pulse`, `/api/signals`, …) | `app/data_sources/engine_api.py` |
| Engine `data/*.json` mounted at `/engine-data` (read-only) | `app/data_sources/data_volume.py` |
| `monitor-logs` branch artifacts (TTL cached) | `app/data_sources/monitor_logs.py` |
| `docker exec engine python /app/scripts/diag_*` | `app/data_sources/diag_runner.py` |
| Monitoring agent's active-alert Redis state | `app/data_sources/agent_alerts.py` |
| Binance Futures public 1m klines (no key, read-only) | `app/data_sources/binance_klines.py` |
| Closed-signal record — `signal_performance.json` (`/track-record`, `/performance`) | `app/data_sources/data_volume.py` |
| Live SAR mechanism arms — `sar_live_arms_v1.json` (`/signals/sar-live`) | `app/data_sources/data_volume.py` |
| Dark emission lane — `dark_signals_live_v1.json` (`/signals/dark-live`) | `app/data_sources/data_volume.py` |
| Live marks for open rows (whole futures book, one request, TTL-cached) | `binance_klines.BinanceKlinesClient.fetch_all_prices` |
| Live-arm freshness test fixture — real engine output, regenerate with 360-v2 `scripts/gen_ops_sar_live_fixture.py` | `tests/fixtures_sar_live_freshness.json` |
| "Held to stop" free-run replay (Profit tab) | `app/data_sources/free_run.py` |
| Scaled-exit what-if simulator (Profit tab) | `app/data_sources/exit_sim.py` |

## Conventions

- All runtime config via `app/config.py` env-overridable settings.
- FastAPI async everywhere — no blocking HTTP in routes.
- HTMX partials are routes prefixed `/_partial/...` returning HTML fragments.
- Templates extend `base.html`; `login.html` is the one exception (standalone).
- Owner-only auth — password gate via `OPS_AUTH_TOKEN` + optional TOTP second factor via `OPS_TOTP_SECRET` (enroll with `python scripts/generate_totp_secret.py`; audit F-08).
- Templates render unknown payload shapes via `tojson(indent=2)` rather than crashing on shape drift — the engine REST surface is the source of truth, this dashboard adapts to it. (The mobile app's screens follow the same rule: pull fields defensively, fall back to a raw-JSON card.)
- The `/api/v1` JSON surface (for the native app) authenticates with **ops-issued app-tokens** (`app/app_tokens.py`, stored hashed; `revoke-all` is the lost-phone switch). The engine's owner-tier Bearer must **never** ship inside the APK.
- Push alerts go through `app/fcm.py` (FCM HTTP v1 via `httpx` + `google-auth` — deliberately no `firebase-admin`) to tokens in `app/device_registry.py` (plain-file registry shared across the web and agent containers; read fresh, mutate under a lock). The whole push path is disabled-safe: no `FIREBASE_SERVICE_ACCOUNT`, no-op.
- Agent detectors are pure functions — all inputs arrive as parameters, no hidden I/O — so their tests pass plain dicts without live network or Docker.

## Hard limits

- No engine code is modified from this repo. (Engine *endpoints* are added in `mkmk749278/360-v2`; ops only *calls* them.)
- **Writes to engine state are allowed only through owner-gated engine endpoints, and only when audited** (see Control doctrine above). No direct mutation of engine data files, Redis, or SQLite from this repo — control flows through the engine's HTTP control surface, which owns the invariants (kill-switch write-through, FSM transitions, blast-radius caps). Never reach around the engine to flip state.
- No multi-user. No multi-tenant. No publicly-accessible endpoints (everything behind the auth gate). As a control plane this matters more, not less.
- The `docker.sock` mount is acceptable only because access is owner-only; never broaden user access without first replacing the diag runner with an engine-side endpoint.

## Commands

```bash
# Local dev
OPS_SESSION_SECRET=dev OPS_AUTH_TOKEN=dev uvicorn app.main:app --reload

# Tests
pytest -q

# Docker build + run (web app + monitoring-agent + redis)
docker compose up --build

# Monitoring agent locally
python -m app.agent.runner

# Native ops app (mobile/)
cd mobile && flutter pub get && flutter test
flutter run                                                  # against ops.luminapp.org
flutter run --dart-define=OPS_BASE_URL=http://10.0.2.2:8000  # against local ops
```
