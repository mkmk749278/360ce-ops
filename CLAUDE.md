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

## PnL % leads; R is the bridge, never the headline

Owner, 2026-08-02: *"that R is purely confusing — 3% SL still only 1R and 0.3 SL
0.3R. only show PnL, calculate only that."*

**He is right, and the engine settles it.** `signal_dispatch` sizes every
position at a **fixed notional** — `raw_qty = notional / entry_price` — so the
stop distance appears **nowhere** in the sizing formula. R divides each outcome
by its own stop, which equalises trades only when size is scaled inversely to
that stop. It is not. So a trade losing 0.80% and one losing 6.14% both read
exactly `−1.00R` while costing $4.00 and $30.70 of the same $500.

That is not presentation, it changes conclusions:

- **It misranks paths.** On the 2026-08-02 window `MEAN_REVERT` reads
  worst-in-book by R (−0.481R) while losing **$2.02** per trade;
  `MA_CROSS_TREND_SHIFT` reads mid-table (−0.293R) while losing **$11.05** —
  five times more money from the path R called healthier, because its median stop
  is 5.15% against MEAN_REVERT's 1.09%.
- **It flipped a sign.** The same day's SAR arms read **+0.035R** and
  **−0.041%** — R said the mechanism made money, the money said it lost — because
  the winners sat on tighter stops (3.25%) than the losers (3.46%), so dividing
  by the stop inflated the wins and shrank the losses.

So every measurement page leads with **PnL %**, computes its aggregates and win
rates from it, and ranks by it. R stays visible, muted, one column, because the
Strategy Lab and the edge matrix are keyed in R and a reader moving between them
needs the bridge — but it is never what a verdict is read from here.

Corollary: **a percentage needs no denominator, so it cannot shrink its own
population.** R silently drops rows with no entry-risk stamp; PnL keeps them.
Where the two counts differ, say so on screen.

**The open question this raises is the owner's, not ours:** either keep showing
money (done), or size positions by risk so that R becomes meaningful and the book
is equalised. The second is a money-path change and needs sign-off — do not
re-derive it as a display fix.

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

**Wait ~4 minutes before checking CI here.** That is what this repo's `lint +
tests` job takes; the engine is ~8 min and `lumin-app` ~16 min. Polling a check
run that cannot have finished yet burns API calls and turns one wait into six —
sleep the known duration first, *then* read the conclusion. These are expected
durations, not deadlines: a job still running at the mark gets another wait. If
the number drifts materially, update it here rather than re-learning it every
session.

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
- **A truncated measurement is not a smaller version of the whole one** (2026-08-03,
  engine #869). The owner asked this page for *"max PnL before hitting SL"* and *"same
  exit strategies like Held to stop"*, and neither was answerable — not for want of a
  column, but because the engine's dark walk **stops at the first TP1-or-SL touch**. So
  `mfe_pct` on a row that closed at TP1 is bounded by the TP1 distance *by
  construction*: it says how far the trade ran before its own exit and is structurally
  silent on how far it was going to run. Rendering it under the words "max profit" would
  have been a number that is always about right and never means what it says. Everything
  after that touch was never walked, which is the same reason no held-to-stop or
  laddered exit could be priced. The engine now walks a **second arm** with TP1 removed
  (`dark_emission._walk_hold`) and ops prices the Profit tab's catalog off its stamps
  (`app/data_sources/dark_exit_sim.py`). The two peaks render in separate columns and
  the page says why — pooling a truncated series with a complete one and averaging is
  how a page reports the market when it is describing its own walk.
- **A second arm needs its own sweep, or it freezes when the first one finishes.** The
  held arm exits at the stop, which is normally *later* than the row's own TP1 — so a
  resolve loop keyed on `status == OPEN` stops advancing exactly the arm built to
  outlive that exit. Engine-side the population is now "owed a verdict on **either**
  arm", the freshness stamps grade whichever arm is still walking, and a row is done
  only when both are. This is #835's shape (a measurement riding another object's
  lifetime), avoided rather than paid for again — the tell was that "the row is closed"
  and "there is nothing left to measure" had quietly become the same sentence.
- **The strategy catalog is one catalog.** `dark_exit_sim.build_catalog` mirrors
  `exit_sim.build_catalog`'s keys, labels and order, and a test asserts it — a reader
  moving between `/profit` and `/signals/dark-live` must not have to check which "TP1
  full" is which. Where the two genuinely differ, the *page* says so rather than the
  numbers diverging silently: the BE arm is the Profit tab's approximation, ported
  deliberately, with its optimistic direction named on screen.
- **Charge the fee to the baseline too.** Every exit method is compared against the
  row's own SL/TP1 outcome; charging the round trip to the methods and not to the
  baseline manufactures an edge out of the fee. A test asserts that two identical exits
  show zero edge.
- **Refuse a leg you cannot price, and refuse the whole row with it.** A ladder leg at a
  TP level the engine never stamped cannot fall through to the stop — that books a loss
  the method may never have taken, and the shortfall then reads as the method performing
  badly rather than as missing data. `level_not_stamped` is its own skip reason, on
  screen, counted apart from "the arm is still running" and "written before the arm
  shipped", because the reader's next move differs for each.
- **A concentration key is not portable between lanes, and porting one blind would
  have printed the exact reassurance it exists to prevent** (2026-08-07). The
  price-action page groups a run by **time** — 90 minutes, three times the engine's
  per-symbol emit throttle — because that lane re-enters a trending symbol every 30
  minutes and one 4.5h burst carried its whole sign. Copying that key here finds
  almost nothing: a dark candidate is diverted at the `signal_queue.put` site
  **before** `SignalRouter._process`, so no per-symbol cooldown applies to it at all,
  and its repeats are spread across hours instead of bunched. Median gap between
  consecutive stamps on one symbol·side is **~12 hours**; only ~15% fall inside the
  window. So the run key reads **1.10 rows/group, worst run 3 rows and 8% of the
  loss** — *concentration is not a problem here* — over a book where the worst **ten
  campaigns** (symbol·side across the whole window; 59 rows, **13%**) take the
  selection from **−119.85% to +4.40%**. **82% of rows sit in a multi-row campaign
  against 17% in a multi-row run.** Both render, neither is "the" number, and nothing
  de-duplicates. Two things to keep: a window that derives from a throttle on one
  lane is a **chosen** grouping on a lane with no throttle, and the page says which it
  is; and re-detection here understates the loss (one row per campaign reads −0.342%
  against −0.199% per row) — the opposite direction from #816, so "de-duplicate to be
  safe" is not a safe default either.
- **A coverage count says how many rows are missing and cannot say which way they
  lean — grade the subset on the column every row has** (2026-08-07). The exit-method
  table said *"measured over 217 rows … 245 excluded"*, which reads as a sampling
  caveat and is a **directional** one: the priced rows averaged **−0.3215%** against
  **−0.1706%** for the retired ones, and all the still-running rows were winners. The
  held arm's reach also varies by path — **54%** of `MOVER_AVWAP_SCALP` against
  **24%** of `FAILED_AUCTION_RECLAIM` — so a pooled average is weighted quite
  differently from the per-path table below it. The row's own SL/TP1 `pnl_pct` is
  recorded on **every** row whichever bucket it lands in, so the always-present column
  is what grades the subset the second arm defines. Corollary that decides how to read
  the table: a lean shared by both sides cancels out of **"vs the row's own exit"**
  (same rows, same baseline) and does not cancel out of the absolute avg/total — so
  the edge column survives and the level does not. Nothing is reweighted: a
  rotated-out mover is not missing at random, and de-biasing needs a model of why the
  candles stopped.
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

**2026-08-02: this page is no longer only a what-if.** The owner asked for the
lane to be live, and engine `src/entry_quality.py` now runs a real gate in the
scanner's post-scoring chain. So the page carries **two** kinds of thing and must
never let them read as one:

- the **Live entry-quality rules** panel — rules that actually run and can cost a
  candidate its emission;
- the splits below it — what-ifs computed here and applied nowhere.

The old blanket *"Nothing on this page is applied"* copy is gone, and a route
test asserts it does not come back. Rules for the panel:

- **Measure it on the stamps, not on the join.** A candidate the gate suppressed
  never delivered, so it has no closed-signal record and cannot appear in
  `join_outcomes` — a panel built on the joined book would silently exclude
  exactly the population the gate acted on and render a live gate that had done
  nothing.
- **A suppression has no outcome here, and never will.** That column is a count.
  The forward measurement lives in the suppression audit (Strategy Lab → gate
  audit), because an enforcing gate starves its own evidence and stamping is the
  only thing that keeps its verdict arriving.
- **"Would have removed" colours inverted.** It is the performance of rows the
  rule would have *dropped*, so negative is the rule looking right — and it is
  published beside the delivered book's own average, because a removal figure
  with no denominator means nothing.
- **Unknown is its own bucket, and an enforcing rule that abstains is badged.**
  Fail-open is deliberate engine-side; the cost is that an inert rule reads
  exactly like a working one on every count except this column.
- **The mode is read off the rows the gate decided**, never mirrored from a copy
  of the engine's rule registry. `MEASUREMENT_SUFFIXES` drifted for a week; the
  fix for a drifting mirror is not a second mirror.
- **The panel filters with the table.** A gate summary over the whole ledger above
  a table showing one path is not a summary of anything the reader is looking at
  (#90, again).

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

## `/signals/structural-snap` — two arms, and only one of them is knowable

Added 2026-08-04 (engine `src/structural_snap.py`) for the owner's question
*"what actually price action is, are we using it?"*. The audit answer: the
engine reads structure into its **score** (SMC sweeps/MSS carry 25 of 100
points) and almost never into its **triggers** — ~82% of the enqueued book is
MA/indicator-triggered, `MOVER_TREND_PULLBACK` alone being 59% — and **not at
all** into its targets, which are fixed R-multiples off the stop distance.

A repair for exactly that had existed in `structural_levels.py` since it was
written, and `build_channel_signal` called it. It never ran: the call sat behind
`candle_highs is not None` and no caller in the engine has ever passed that
argument, while the branch's comment read *"shared by EVERY evaluator that
passes candle arrays"*. Dead twice over — every evaluator overwrites
`sig.stop_loss` / `sig.tp1` on the line after that helper returns.

**The design constraint this page exists to respect: the two arms are not
equally knowable.**

- **TP1 arm — fully decidable.** The snap moves TP1 *nearer only*, and
  `max_favorable_excursion_pct` records how far the trade ran before its close.
  So a nearer target was reached iff `MFE >= its distance`, with no ordering
  ambiguity and no refused bucket beyond rows that never joined an outcome.
- **SL arm — partly decidable, and the residue is biased.** A *wider* stop on a
  loser asks whether price would have come back, and the walk ended at the stop
  (`undecidable_truncated`). A *tighter* stop on a winner asks whether the
  drawdown preceded the target, and MFE/MAE carry no ordering between them
  (`undecidable_ordering`). The two are **never pooled**, because they remove
  opposite ends of the distribution — dropping them silently leaves a
  loss-selected sample on one side and a win-selected one on the other, and a
  loss-selected sample is worse than no sample because it looks like an answer.

**There is deliberately no combined figure**, and a test asserts the key does
not exist. One number over both arms would move with the SL arm's refusal rate
rather than with the mechanism. Same rule as `/signals/sar-live`'s two fills and
two denominators, arriving from a third direction.

Other rules the page carries:

- **The decidable fraction sits beside every delta**, not in a footnote.
- **The baseline is measured on the rows the arm scored**, never over the whole
  ledger (#90, again).
- **`unchanged` is counted apart from every refusal.** A row where no level fell
  inside the search band is a rule that changed nothing, and a rule that changes
  nothing has not been tested however good its delta looks.
- **MFE/MAE are tick-sampled, not intrabar** — `trade_monitor` updates them on
  mark-price ticks, so a touch between ticks is not recorded. Every "the level
  was reached" verdict is therefore conservative: this lane can under-count
  rescues and can never invent one. The bias points *against* the snap, which is
  the safe direction for an adoption decision, and the page says so rather than
  presenting the count as exact.
- **Level provenance is on screen, because the two generators differ in
  standing.** A swing is a price the market traded and rejected; a round number
  is injected by us on an **absolute** grid — 0.2% wide at $50,000 and 20% wide
  at $0.05, where it cannot fall inside any stop band. An all-`swing` column at
  sub-cent prices is the grid being inert, not round numbers being unhelpful,
  and `round_step_pct` is what distinguishes the two.
- **Refusals are named, never pooled into "no data".** `tf_unknown` means a new
  evaluator has no declared trigger timeframe and is being refused rather than
  snapped against the wrong timeframe's structure; `short_series` is the buffer;
  `no_candles` is the store. Different fixes.
- **The mode is read off the rows** (`apply_mode`), never mirrored from a copy of
  the engine's flag registry.

**The same page carries a second, wider defect from the same audit.**
`Scanner._get_primary_timeframe` was `return "5m"` for **every** channel since it
was written — a constant wearing a lookup's docstring — and **six** money-path
consumers read it: continuation-sweep evidence (the 25-pt SMC dimension), the
VWAP extension gate, the OI + funding gate, cross-timeframe volume divergence,
the chart-pattern confidence bonus (the 10-pt Patterns dimension), and the volume
inputs to the composite score. Every one was therefore computed on 5m bars for
setups that do not trade 5m — MVRTP (~59% of the book) is 15m, as are MVAVW /
MEAN_REVERT / RANGE_FADE; MA_CROSS is 1h and WHALE is 1m.

The census panel carries two rules the page must not lose:

- **The denominator is signals, not resolutions.** Six consumers call the
  engine's resolver per candidate, so its own counters run ~6× the signal count.
  A book fraction taken from them would be inflated sixfold and look entirely
  plausible. The panel reads `score_tf_mismatch`, stamped once per row.
- **It says how much of the book is affected, never how much better it would
  be.** Five of the six consumers run *before* the stamp, so they decide whether
  a candidate is in the ledger at all — anything measured from these rows is
  survivorship-biased by construction, and pricing the correction properly needs
  a shadow gate chain. That limitation is on screen, not in a footnote.

**A `str`-typed runtime tunable needed plumbing in two places, and both failures
were silent.** The per-path allow-list renders through `control.html`, which had
one numeric branch for everything non-bool — untypeable. And `/control/tunables`
skips empty form values (right for an untouched number field), so `""` could
never be sent: an allow-list that could be added to from ops and never cleared,
which is the one state a money-path switch must not be in. `_str_keys` is the
companion to `_bool_keys` and fixes the second half.

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

Rules the page carries, all ports rather than inventions:

- **Money leads and nothing here divides by a stop (2026-08-03).** This page led with
  R for its first six days, and **R described 6% of it**: 421 of 448 trades in the
  owner's 30d window carried no `sl_distance_pct_at_entry`, so `−5.17R total /
  −0.192R avg / 26% win (7W/20L)` were computed on 27 rows while the `avg_pnl_pct`
  beside them covered all 448 — and the page never said the two described different
  populations. The general rule was already written above (*"PnL % leads; R is the
  bridge"*, 2026-08-02) and this page was simply the last one not converted; the
  owner caught it. Here R is not muted but **gone**, because the two surfaces that
  need the bridge — the Strategy Lab and the edge matrix — are elsewhere, and a
  denominator 94% of the rows cannot supply is not a bridge, it is a hole. The page
  says on screen why it has no R; a route test asserts no R *figure* renders.
- **A portfolio percentage is fine once the size is an INPUT.** The old rule forbade
  one because "it needs an assumed position size, and `MAX_SAME_DIRECTION_GLOBAL=3`
  means two users get different fills". That objection is about an **assumed** size.
  The owner now types the amount (default 100 USDT **notional** — the same thing the
  engine's `raw_qty = notional / entry_price` means by it, so no leverage is
  invented), and the page states the assumption beside the number instead of hiding
  one: every delivered signal taken, fixed size, no compounding. The fill-count
  caveat survives as copy, because it is still true.
- **Charge the fee, and say what it is.** The owner's 30d window is **−$3.21 gross**
  at $100/signal and **−$34.57** once a 0.07% round trip is charged — the cost of
  trading is ~10× the edge, so a gross-only figure answers the wrong question, and no
  surface in this repo had ever shown it. The rate is an input (default 0.07 = Binance
  USD-M maker 0.02% in + taker 0.05% out); gross and fees render beside net so the
  split is readable; `Best`/`Worst` stay gross so the fee is not subtracted twice in
  the reader's head, and the table says which columns are which. This is the repo's
  own *"say whether an R is gross or net"* rule, finally applied to money.
- **"Can I hold it" is a concurrency question, not a PnL question.** The balance
  input reports the **required** balance — peak concurrent positions × amount, from a
  sweep over each trade's entry and close — rather than simulating which signals a
  smaller balance would have skipped. Skip order is a modelling choice that can
  flatter or hurt the number, and so is compounding; both were declined explicitly.
  A row with no entry stamp is **named**, never assumed simultaneous (which overstates
  the requirement) or sequential (which understates it).
- **Refuse, don't clamp — and name the reason.** A row with no readable `pnl_pct` is
  counted in the trade count, excluded from every money figure, and the shortfall
  stated. The geometry-stamp split survives *only as an engine-health line*, labelled
  "affects no number on this page": `no_geometry` (stamped by today's engine and still
  unusable — a producer fault that does **not** age out) is this repo's only detector
  for that fault, and dropping it with R would have lost it silently. It stays counted
  apart from `awaiting_engine_stamp` (closed before the field existed, shrinks on its
  own) because pooling them reports a live fault that is not happening.
- **Bucket by CLOSE time.** A day's PnL is the PnL realised that day; bucketing by
  entry credits Monday with a trade that closed Thursday.
- **A window boundary is not a bucket boundary, and a part-period must never render
  as a whole one (2026-08-03).** `resolve_range` returned `now - N days` — a
  *moment* — while `bucket_rows` groups by **calendar day**, so the oldest bucket of
  every rolling preset held only the tail of that day and looked exactly like a
  complete one. On the owner's own export `window=1d` showed `2026-08-02` as
  **+12.29% over 3 trades, 3W/0L**; the day was **−0.40% over 11, 4W/7L**. The window
  had removed 8 trades, 7 of them losers — **the sign flipped**, and the owner read
  the row as "yesterday was good". Presets now snap to midnight UTC, so a day is the
  whole day whatever window contains it, and the bucket count is unchanged.
  Snapping days is **not** snapping weeks or months (a 7d window does not begin on a
  Monday), so `bucket_completeness` ships anyway and is the guard against a future
  `resolve_range` regression. Its two states are never pooled — `window_cut` (the data
  exists; **widen the window**) against `in_progress` (the period has not finished;
  **wait**) — because the reader's next move differs, and both stamps ride into the
  CSV, since a spreadsheet is exactly where a part-period gets averaged in. The
  explanatory note renders whether or not any row is flagged.
- **Say which day the date means.** Every figure here is UTC — the engine is UTC end
  to end and ops ports its clock rather than inventing one, so an ops day and an
  engine day are the same day. But a UTC day ends at **05:30 IST**, so a trade closing
  19:48 UTC is 01:18 the next morning locally and reads as "yesterday" on screen. Every
  bucket label carries `UTC` and the footer names the local rollover
  (`OPS_LOCAL_TZ_HINT`, display only — it re-buckets nothing). A date with no zone is
  the same class of omission as a percentage with no denominator.
- **The row cap is a render bound.** The per-trade table caps at 500 *after* every
  filter and after sorting, and says when it bit; the per-trade CSV is uncapped,
  because a truncated export is #97 wearing a download button.
- **This page and the Lumin app's "YOUR PAPER P&L" are different books and must never
  be reconciled.** Traced 2026-08-03 after the owner compared them: the app card reads
  a **per-user paper book** (`execution/paper_book_registry.py`) that starts empty at
  enrollment, applies per-user symbol/path/regime prefs and a per-user RiskManager on
  top of router delivery, sizes at `min(equity × 2%, $100)` — ~$20 on a fresh book,
  **compounding** — and books each TP-ladder partial on its own day. This page is every
  delivered closed signal, pooled, at a fixed notional the owner types, one blended
  `pnl_pct` per row. Different population, different sizing, different exit accounting.
  Do not "fix" a disagreement between them.

`entry_regime` arrives from engine #817 and **cannot be backfilled** — the regime at
entry is knowable only at entry — so records closed before that deploy render as their
own `UNPLACED` bucket rather than being folded into a real regime.


## `/diagnostics/data-intake` — the price-action lane card

Added 2026-08-06, one PR **after** the census it renders, which is the whole
lesson. Engine #889 shipped `_price_action_lane_report()` and a PR body saying
*"`/api/data-intake` now carries the refusal mix"*. Both true. This page rendered
nothing for it, so the owner — who had just asked why the lane produced no
signals — opened the page the answer was supposed to be on and found no such
card.

**A field one repo writes and no repo reads is #817 with the arrow reversed, and
it is harder to catch**: the producing side's test passed, because it asserted
its own function's return shape. That is a mock asserting your assumption back at
you one repo short of the reader. It is now pinned on the producing side by
driving the real assembler.

Rules the card carries:

- **The block is nested under `derived`, and the first cut read it off the top
  level.** The ops fixture agreed, because a fixture chooses a location and then
  agrees with you about it — every test green over a card that would have
  rendered `NOT REPORTED` against the real engine. `zone_distance_atr` with the
  path wrong instead of the field.
- **The classification is COPY, not a mirror of the engine's reason list.** The
  table iterates **the engine's payload** and looks each reason up in
  `LANE_REFUSAL_COPY`; a reason ops has never heard of renders under its raw name
  badged `unclassified`, never dropped and never silently bucketed. Iterating
  ops' own keys would be silent by construction on the next reason —
  `MEASUREMENT_SUFFIXES` wearing a third hat.
- **Four classes, because they have four different next moves.** `fault`
  (`no_levels` / `short_series` / `bad_geometry` — the lane is blind, ours to
  fix) · `coverage` (`no_footprint` — Phase 2b does not reach the symbol) ·
  `market` (`no_sweep` / `delta_opposed` / `no_opposing_target` /
  `rr_below_floor` — not on offer, or offered and failed its own test) ·
  `throttle` (`cooldown`). Pooling any two into "no signals" is how a page
  reports a fault that is not happening.
- **`cooldown` is not a refusal and must never sit in `market`.** It means a
  setup *was* found and deliberately not stamped, so a non-zero count is
  **positive evidence the lane fires**. Bucketed with `no_sweep` it reads as a
  quiet market when it was us throttling — #816 arriving from the display side.
- **Emission is not the health signal, and the card says so.** The trigger is
  rare by design: zero rows is a quiet market. A card that reads three stamped
  rows as a fault teaches the owner to ignore it.
- **The denominator is evaluations, never rows.** Dividing refusals by emissions
  puts a rare-by-design numerator under a rare-by-design denominator.
- **A missing block reads as an old engine, not a quiet lane.** "Blank needs a
  cause before it gets a caption" — the two states have different fixes and the
  conflation is the defect this card repairs.

## `/signals/price-action` — the lane's own page, and why it is not a purge

Phase 5's surface (engine `src/price_action_lane.py`). Every row is a signal the
engine generated **from structure alone** and diverted before the queue.

**Two pages were both called "Price action" and only one was reachable**
(2026-08-06). The label sat on `/signals/structural-snap` — the SL/TP1 geometry
repair, a different mechanism — while this page had no nav entry at all and set
`active: "signals"`, the *Feed* tab's key, so the Feed pill lit up on a page that
was not the feed. `tests/test_nav.py` now derives the requirement from the route
decorators and parses `base.html`'s NAV literal: a new literal page under
`/signals/` fails CI until it is linked, no two labels or active keys may
collide, and every destination is driven as a real request. **A hand-maintained
nav is a floor** — the `is_tradfi_perp` rule wearing a fifth hat.

### Filter, do not purge

The owner asked whether clearing the rows written before a stamp existed would
make the data clearer to estimate from. **It would have taken the closed book
from 74 rows to 12.** Those rows lack `entry_regime` and `level_source_tf` and
carry a perfectly valid `pnl_pct` — they cannot appear in a *split* and they are
most of the *evidence*. Precision comes from n, so a purge makes the estimate
smaller, not clearer.

And the splits were **already** clean: `unstamped` is its own bucket and is never
folded into a real one, so it cannot contaminate a cell. The only place those
rows pool is the headline, where pooling is correct because PnL is valid on every
row. Same call `/track-record` already makes with `UNPLACED`.

So the page filters instead:

- **Four stamp states, not two.** The level provenance and layer 1 shipped about
  an hour apart, so rows between them carry one stamp and not the other.
  `full` / `partial` / `none` — that middle population is real and is exactly the
  set a reader wonders about.
- **Each selector's counts are measured with every filter applied EXCEPT its
  own** (#90/#91). A selector applied to its own counts makes every option read
  *"n = whatever I picked"*.
- **Every panel recomputes on the filtered rows**, and the table cap applies
  **after** filtering (#97) and says on screen when it bit.
- **The export is uncapped and shares one loader with the page**, so the download
  can never describe a different book than the screen — a truncated export is #97
  wearing a download button. `stamp_state` rides on every row, because a
  spreadsheet is precisely where two populations get averaged into one.

### Layer 1 — the split the lane exists to be judged on

§1 of the program doc defines this lane's trigger **relative to the prevailing
trend** (a break with it is a BOS, against it a CHoCH). The lane reads location,
trigger and confirmation and has **no context layer**, so it takes both
identically. Both timeframes render side by side and are never pooled: the
scanner classifies on 5m and the lane triggers on 15m, and a 15m downtrend with a
5m bounce is exactly the setup this lane keeps buying.

The regime label set is **not** enumerated here — the engine's detector owns it,
and a list kept in ops is silent by construction on the next label.

### Read n first, and say how many cells

`FAILED_AUCTION_RECLAIM` (+0.846R on three rows, CI [−1.00, +2.00], promotion
requested within the day) is the standing example of what reading a thin cell
costs. **Nothing on this page asserts a fixed book size** — two sentences here
did, and both had outlived their data within a day (*"the whole closed book is
under 50 rows"* printed above 462; *"expected to be almost all unstamped today"*
above a book 16% unstamped). Every count on the page derives from the rows.

### Layer 1 — Context, the layer the lane did not have (2026-08-07)

Added with engine #897, for the owner's question *"why don't we make them
meaningful signals first"*. The answer was not a better filter over the stamped
columns: **§1 of the program doc defines price action as a four-layer read and
the lane had three.** Location (LevelBook), Trigger (sweep + reclaim) and
Confirmation (footprint delta) shipped; **Context never did**, while the engine's
`volume_profile.py` had computed POC and the value area all along.

A sweep + reclaim is a **failed break**, so it is mean reversion: it pays in
**balance** and traps in **imbalance**, and **those two states have an identical
layer-2/3/4 signature**. That is why every column on this page read like noise —
the discriminating variable was not in the data at all.

Rules the two panels carry, each one already in this file arriving from a new
direction:

- **`unstamped` is its own bucket** and the stamps are **not backfillable** — the
  value area at entry is knowable only at entry. The table is ~100% unstamped the
  day it ships and the copy says to expect that, because a blank needs a cause
  before it gets a caption.
- **The shadow rule says which half of its cutoff is fitted.** "Inside the value
  area" is the value area's own 70%-of-volume definition and predates the lane;
  *which* layer-1 conditions to combine was chosen while looking at this book. So
  its first number is a hypothesis this window **generated**, not one it tested.
  The owner asked for it knowing that — the panel says it anyway, because the
  next reader was not in that conversation, and a route test asserts the word
  stays on screen.
- **Three buckets, never two**, with the **abstain fraction printed first**;
  folding rows whose layer 1 never computed into `keep` is how a rule takes
  credit for rows it never filtered.
- **The baseline is the whole book and does not move** with the rule's coverage
  (a test pins it); the *decided* population is published beside it so a rule
  that abstained on most of the book cannot read as one tested on it.
- **`vp_poc_room_pct` is signed toward the trade**, so positive always means
  "POC is ahead of *this* trade". Raw distance flips for longs — PUMPUSDT's first
  live row reads `+0.631%` where raw would read negative.

### Concentration: the move key cannot see a trending symbol

`concentration()` keys on `symbol · side · entry`, which is right for one sweep
re-stamped at one price and **structurally blind to a moving symbol** — a trend
hands out a new entry every time. On 2026-08-07 it read `1.12 rows/move` and
*"largest single move = 1.0% of all rows"*, i.e. *concentration is not a problem
here*, over a book where **BEATUSDT whipsawed 24%, the lane bought reclaimed
support ten times, none won, and the worst nine formed one 4.5h run worth −85.71%
against a whole-book net of −78.25%**. Removing that one run read +7.46%: *the
sign of the verdict was one episode.*

The episode panel sits **beside** the move panel rather than replacing it — they
answer different questions — and **nothing de-duplicates**, because that
judgement is the reader's and counting silently is what the panel exists to stop.
#816 (*a throttle on rate is not a throttle on evidence*) arriving at the display
side.

### A flat expiry is not a loss

The engine scores `EXPIRED` at **0.00%** — a walked window in which neither level
was touched. `losses = n_closed - wins` swept every zero into the loss count, so
the page read `115W / 347L` where the book was `115W / 267L / 80 flat`, and 25%
where the rows that actually resolved to a level read 30%. Both denominators are
published and neither is called *the* win rate; a flat row still pays its round
trip, so it stays in the money figures. "Three buckets, never two", arriving at
the win-rate line.

## The read-only door (`/guest`) — a second tier that can never write

Added 2026-08-06 (`docs/READ_ONLY_ACCESS.md`) for the owner's question: *"give
access to you to browse our ops page to load data … except control Panel, from
control I generate temporary code … and I can disable that access too"*. The
owner mints a short-lived code on **Control → Access** (`/control/access`); the
holder exchanges it at `/guest` for a read-only session; the owner revokes it
from the same page. Measured against the live nav, a holder sees 5 of the 6
groups (Control absent entirely) and 5 of 6 Diagnostics sub-tabs (Diag runner
withheld).

**The panel is its own sub-tab, not a card on `/control`.** Every other control
on that page writes to the *engine*; this writes to ops' own access store, and a
revoke sitting between the kill switch and auto-mode reads as an engine action.
It carries its own flash key for the same reason — two writers on one flash key
means an action can render its result on a page the operator did not come from.
The panel is itself owner-only and tested as such: a tier that could see the
grant list could mint itself another.

Rules, each of which is a rule already in this file arriving at the auth layer:

- **The scope table is TOTAL, because a deny-list is silent on the next page.**
  `guest_scope` classifies every registered route as guest-readable or
  owner-only, `tests/test_guest_access.py` derives that requirement from
  `app.routes`, and an unclassified route is denied at runtime. A deny-list
  would have handed tomorrow's ops page to every live code the day it shipped —
  `is_tradfi_perp`'s name list and `MEASUREMENT_SUFFIXES` wearing a fourth hat.
  When CI says "unclassified route(s)", classify it; do not delete the
  assertion.
- **"GET is safe" is false here, and the counter-example is load-bearing.**
  `/exit-backtest/run-now` is a GET link that starts a `docker exec` job on the
  production engine — deliberately, because a proxy was eating the form POST. A
  method-only gate hands a read-only guest a job trigger. The method check is
  rule 1 *and* the route table is rule 2; neither is sufficient.
- **Revocation is re-read per request, never trusted from the session.** The
  cookie carries the grant *id*; the grant is looked up on every request. A
  login-time check would make "I can disable that access too" true only once the
  cookie expired, which is not what the sentence means. Same shape as the
  engine-is-the-source-of-truth rule in the control doctrine: ops holds no
  authorisation state locally, it re-reads it.
- **The nav is filtered from the set the gate enforces**, injected as a Jinja
  global — not from a second list of guest-visible pages. A nav that mirrored
  the gate would drift, and the drift is invisible until somebody clicks a link
  that 403s. The fix for a drifting mirror is not a second mirror.
- **A refusal states its cause**, on the 403 page and in the audit row, and
  every owner-only entry carries a written reason (a test asserts none is
  blank). "403" with no reason is "blank needs a cause before it gets a caption"
  at the auth layer — the reader cannot tell *you may not* from *this is
  broken*.
- **The guest-side lockout must never reach the owner.** Ten failed codes closes
  `/guest` for fifteen minutes; `/login` is a different route and unaffected. A
  throttle that can lock the owner out of his own kill switch is a worse failure
  than the one it prevents.
- **Scope is fixed at read; there is no scope parameter.** A tier that can
  *sometimes* write is one whose blast radius has to be re-derived at every call
  site. Read-only is the only second tier that needs no such argument, and the
  control doctrine above (owner-gated, audited, PRG-confirmed) is unchanged by
  this because nothing here can write.

## The 2026-08-06 panel surf — what reading every page found

The owner asked for a pass over all the panels. Fetching all 26 and looking at
what they *rendered* — not at what their code intended — turned up four defects,
three of which were invisible to the suite by construction, because **a
paragraph, a caption and a page size are none of them assertions.**

- **Two dead pages, for four days.** `/signals/sar` and `/sar-exit` both read
  UNAVAILABLE — *unexpected ledger shape* — beside an mtime updating every few
  minutes. The engine writes this ledger through
  `suppression_audit.SuppressedCandidateStore`, which it **shares** with
  `suppressed_candidates.json`; when that store gained a schema-2 envelope on
  2026-08-02 to carry the suppression audit's eviction counts, *this* file
  changed shape with it. **A schema bump made for one consumer silently changed
  the file of another** — #817's class at the level of the container rather than
  the field, and the tell was in the writer, whose loader carries a comment
  explaining that a bare list is the pre-schema shape. The producing side knew
  there were two shapes; no reader here was told. `_unwrap_records` takes both,
  and a loader error still passes through untouched, because *could not read*
  and *shape I do not know* have different fixes.
- **A caption that named a benign cause for a state with another one.** Over
  that UNAVAILABLE badge sat the words *"An empty ledger here means off, not
  broken."* The ledger was neither empty nor off. This is "blank needs a cause
  before it gets a caption" broken from the caption's side, and it is worse than
  a bare blank, because it sends the reader to a switch instead of a parser. The
  caption now follows the state.
- **An alert page describing a delivery path it did not have.** `/alerts` said
  *"Telegram is unavailable — check it regularly; there is no push."* Both
  halves false: this file has carried the *Telegram is not banned* correction
  since 2026-07-25, and FCM push shipped in Phase 4. Together they told the owner
  that this page was the only way he would ever learn about a naked position —
  **the one direction an alert surface must never be wrong in.** The page no
  longer asserts a path; it reads whether push is armed (service account **and**
  a registered device) and names which half is missing. Note the fix's own trap,
  caught mid-edit: replacing it with *"the agent pushes these"* would have been
  the same defect with the opposite sign, since push is disabled-safe and a
  missing service account makes every send a silent no-op.
- **A 3.9 MB table, 62% of it saying nothing.** The Strategy×Context matrix
  rendered all **9,261** cells, of which **5,766 read INSUFFICIENT_DATA** — a
  cell under `EDGE_MIN_SAMPLES` carries no verdict by construction. Nothing was
  wrong with any number; the page was simply unreadable, which for a surface
  whose whole job is to be read is the same as being broken. The default is now
  the cells that carry a verdict, capped at 400 as a **render bound** applied
  after the split and the sort (#97's rule, unapplied on this repo's biggest
  page), with the counts and the cap on screen, `?show=all` to restore them, and
  the CSV uncapped. Two things it deliberately does not do: **the sort is by
  evidence, not by edge** — sorting by edge puts the best-looking cell of
  thousands on the top line, and "best of N" is not a fact about the winner
  until N is on screen — and **every other panel still reduces over the whole
  matrix**, since a rollup measured on the capped rows would quietly become a
  rollup of "the 400 cells with the most evidence".

The general lesson, and it is why the surf was worth doing: **every one of these
pages had passing tests.** A test proves the code does what you wrote; it says
nothing about whether the page reads correctly, whether its sentences are still
true, or whether anyone can get through it. Read the rendered output
periodically — the defects it finds are not the ones CI is shaped to catch.

## The 2026-08-07 panel surf — the second pass, and what a derived check found

A read-only guest session ran the same exercise a day later: fetch all 26 pages
plus their exports and look at what they *rendered*. Nine defects, and the shape
is the one this file keeps naming — **a seam**. None crashed, none left an empty
screen, every one had passing tests.

The two that changed a number the owner reads:

- **The Strategy Lab threw away the matrix's denominator.** Each edge cell is a
  `deque(maxlen=50)` engine-side, so `n = 50` may stand for fifty outcomes or
  five thousand — and **1,731 of 3,531 verdict-carrying cells sat at that cap**.
  The engine has counted and persisted the evictions since 2026-08-04 under
  `__evicted__`, in a docstring quoting *this file's* rule about bounded buffers.
  `reduce_edge_matrix` dropped the key on its own `"|" not in str(key)` guard:
  **a field one repo writes and no repo reads**, #817 with the arrow reversed and
  the same shape as the price-action lane card the day before. Cells now carry
  `evicted` / `seen` / `sampled`, tri-state — `None` means *the engine did not
  say*, never *nothing was evicted*, because a bool there reads as a clean
  population in the flattering direction. Read alongside it: only **156 of 3,531**
  verdict cells contain a single delivered row, and 360 read a 100% win rate.
- **Layer G understated its own headline by 4.3×.** `wasted_pct` divided
  `unroutable` by *every* promotion while the numerator is only knowable for a
  row the engine stamped — so the panel built to expose #806/#807 read **19%**
  (10 of 52) where the measured share was **83%** (10 of 12). The right
  denominator was being computed one line below, under a comment calling it
  "honest", and never used. **Mirror the engine's denominators** includes the
  ones you compute yourself.

Three captions that named a cause the page could not observe — the 2026-08-06
class, recurring three times in one day:

- **`/invalidations`, and the fix for it was wrong in the opposite direction —
  which is the more useful lesson.** The page called an empty ledger *"the quiet
  case, not a fault"* while `invalidation_records.json` sat at **2 bytes, 22 days
  unwritten** over a book of 1,043 closed signals. The fix graded it on the
  artifact's **mtime** and badged it `WRITER STALE`: *"`invalidation_audit` has
  stopped recording, and every kill classification since that write is lost."*

  **The owner corrected it within the hour: invalidation and pre-TP are PER-USER
  settings, not engine-wide** (OWNER_BRIEF B17, `user_invalidation_settings`).
  With no user opted in, no kill fires, no row is written, and a 22-day-old empty
  file is exactly correct. That is the `/alerts` trap from 2026-08-06 arriving
  with **the sign flipped** — and the alarming version is the worse one, because
  a benign wrong caption makes a reader ignore a page while an alarming wrong
  caption sends the owner to debug a subsystem that is working.

  Two things to take from it beyond the fix:

  - **"Corroborating evidence" that shares a source is one fact read twice.**
    The WRITER STALE copy cited `/raw-edge`'s 0% invalidation share as an
    independent second signal. It is not independent: that bucket is derived
    from the same terminal `outcome_label`, so both numbers restate *no signal
    ever reached INVALIDATED* — which is precisely what the per-user
    explanation predicts. Before calling two figures corroboration, check
    whether they are computed from the same field.
  - **Grade on whether the artifact was OWED anything, not on its clock.** Ops
    cannot read per-user preferences, so it must not claim the feature is off —
    but it *can* see the population that would be harmed (#815): the
    closed-signal record stamps `INVALIDATED` on exactly the signals that write
    here. Zero of those → empty is right at **any** age. One or more with an
    empty ledger → a writer fault at **any** age. Unreadable → `owed_unknown`,
    graded as neither. The mtime is now context on screen, never the verdict.
    Note the expiry path (`main.py` `cleanup_expired`) also writes here but only
    as a race fallback, so 291 EXPIRED closes imply nothing is owed and are
    deliberately not counted.

  The same fix turned up a **second** bug in that function: the `missing` branch
  matched neither of its own producer's words (`_load` says `"missing: <path>"`),
  so a file the engine had never written rendered under UNREADABLE, *"a fault on
  our side"* — the wrong state and the wrong next move.
- **`/dark-signals` hardcoded a root cause for every future Binance ban.** The
  banner is correctly conditional on `ban_seconds`, and then asserted *"Root
  cause is engine-side (a dead key hammering listenKey)"* as fixed copy from the
  2026-07-24 diagnosis, while nothing on the page observes a cause at all. The
  breaker now records what Binance actually said and the page quotes it; where
  it knows nothing it claims nothing, and it says plainly that the ban is on the
  *box*, which any process sharing the IP can have earned.
- **`/sar-exit`'s empty-ledger sentence printed beside 800 stamped rows.** The
  2026-08-06 fix gave `unavailable` its own caption and left `dark`, `measuring`
  and `live` sharing one `{% else %}` — the caption following *one* state is not
  the caption following the state. The same panel was badged **LIVE — "pairs are
  stamping and resolving"** on a `classified > 0` test while **0 pairs** had ever
  completed out of 5,522 stamps, every rollup row reading `n live = 0` / `ΔR = —`.
  `one_armed` is now its own state and publishes the per-arm resolution split,
  because "324 resolved" and "324 resolved, all of them one arm" support
  opposite readings of the same page.

And three that were simply unreadable or unreachable:

- **`/truth` rendered no timestamp at all**, while serving a TTL-cached snapshot
  of `monitor-logs`. Its `cohort_edge_gate` row read `streak 85` beside a live
  pulse reading `streak 156` for the same probe, with nothing on either surface
  saying they were on different clocks. Graded now on the **engine's**
  `generated_at` — a surface may not grade its own freshness on a clock it
  supplies — with the lookback window named, since a figure here can disagree
  with a live panel for two independent reasons.
- **The guest tier was shown controls it could only 403 on.** The nav has been
  filtered from `GUEST_READ_ROUTES` since the tier shipped; **in-page controls
  were not**, and the filtering stopped exactly one layer short of what matters.
  `/exit-backtest` rendered a POST run form and the `run-now` job trigger to a
  read-only session, with the copy *"Button not responding? Use the plain link"*
  between them, coaching the reader into the refusal. The gate held, so this was
  never a security defect — it is the nav's own rule unapplied one level down,
  and **a control that 403s is indistinguishable from a broken page.**
  `guest_scope.may_use` reads the same table the gate enforces; there is no
  second list.

  **The derived check is what earned its keep.** Rather than hiding the two
  controls that had been noticed, `tests/test_guest_access.py` renders **every**
  guest-readable page, collects every form action, `hx-post` and `href` in it,
  and drives each one — and it immediately found two more nobody had seen: a
  destructive `POST /signals/sar/clear` ledger-wipe card rendered in full to a
  guest (confirm checkbox and danger button included), and a `/control` link on
  `/signals/entry-features`. Writing a list of controls to hide would have been
  silent on both, and on the next one.
- **Raw float repr on three pages** — a PnL column at `-0.47041707080504297`, a
  feed age at `54.348713397979736` seconds, TP levels at twelve decimals on an
  instrument quoted to seven, swept levels at `0.044309999999999995`. None is
  *wrong*, which is why they survived; the noise is the tell, since that last
  value is a level nobody computed. `app/template_filters.py` splits it in two,
  because prices and percentages are different problems: `price` keeps the
  instrument's own precision (this book spans `64328.80` and `0.02062` in one
  table, and `%g` would flip to scientific notation exactly on the sub-cent
  movers that dominate the feed), `pct` is fixed. Both render `—` for missing,
  never `0.00`.

**`test_templates_compile.py` was itself a hand-built mirror**, and its docstring
was the tell: *"mirrors the default `Jinja2Templates` environment the app builds
in `app/main.py` … so a template that compiles here compiles in the app"*. That
parenthesis stopped being true the moment `main.py` registered its first global,
and a mirror can only ever diverge toward **passing over a template the app
cannot render**. It imports the real environment now, and a second test pins the
wiring so a future refactor cannot quietly rebuild a local one.

**One finding was investigated and deliberately not fixed.** `/profit` rendered
in 27.6s during the surf — 20× every other page. Re-measured: **5.5s cold, 1.2s
warm, 0.55s on a narrower window**; the outlier coincided with the live Binance
ban, ~500 rows replaying at `FREE_RUN_CONCURRENCY=5` against 10s timeouts before
the circuit tripped. The path is already bounded — semaphore, per-signal cache,
terminal results cached permanently, degraded rows on a short TTL, and the
breaker short-circuits without a network call. Any change here would mean
choosing a half-open-probe threshold with no evidence. **A finding and a fix are
separate deliverables**, and "the number looked bad once" is not evidence about
what happens when you change it.

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
| Trailing-exit arms, four lanes — `sar_live_arms_v1` · `dark_sar_arms_v1` · `atr_trail_arms_v1` · `dark_atr_trail_arms_v1` (`/signals/sar-live`, `/signals/atr-live`) | `app/data_sources/data_volume.py` (`trail_arms`) |
| Dark emission lane — `dark_signals_live_v1.json` (`/signals/dark-live`) | `app/data_sources/data_volume.py` |
| Live marks for open rows (whole futures book, one request, TTL-cached) | `binance_klines.BinanceKlinesClient.fetch_all_prices` |
| Live-arm freshness test fixture — real engine output, regenerate with 360-v2 `scripts/gen_ops_sar_live_fixture.py` | `tests/fixtures_sar_live_freshness.json` |
| "Held to stop" free-run replay (Profit tab) | `app/data_sources/free_run.py` |
| Scaled-exit what-if simulator (Profit tab) | `app/data_sources/exit_sim.py` |
| Exit-method what-ifs on the dark feed — reads the engine's held-to-stop arm, prices `exit_sim`'s catalog | `app/data_sources/dark_exit_sim.py` |
| Structural SL/TP1 snap stamps — `structural_snap_v1.json` (`/signals/structural-snap`) | `app/data_sources/structural_snap.py` |

## Conventions

- All runtime config via `app/config.py` env-overridable settings.
- FastAPI async everywhere — no blocking HTTP in routes.
- HTMX partials are routes prefixed `/_partial/...` returning HTML fragments.
- Templates extend `base.html`; `login.html` is the one exception (standalone).
- Owner-only auth — password gate via `OPS_AUTH_TOKEN` + optional TOTP second factor via `OPS_TOTP_SECRET` (enroll with `python scripts/generate_totp_secret.py`; audit F-08).
- Numbers reach templates as raw floats; render them through `app/template_filters.py`
  (`price` / `pct` / `secs`), never as a bare `{{ value }}`. `price` keeps the
  instrument's own precision because this book spans `64328.80` and `0.02062` in one
  table; `pct` is fixed-place because a percentage has no tick size. Both render `—`
  for missing — an em-dash is "the engine did not report this", and `0.00` there is
  how a blank becomes a finding.
- In-page controls are filtered for a read-only guest with `may_use(request, path,
  method)`, off the same `guest_scope` table the gate enforces — never a second list.
  `tests/test_guest_access.py` derives the requirement by rendering every
  guest-readable page and driving every form action, `hx-post` and link in it.
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

## `/signals/sar-live` — the second arm (2026-08-09)

Two panels added for the owner's questions: *"add max profit hit before hitting
SL like live feed"* and *"add strategies to check what if you move SL after
moving 3% etc"* (engine `sar_live_shadow._step_hold`, `src/sar_exit_strategies.py`).

**The MFE column on this page is not "max profit", and never was.** The engine's
SAR loop stops advancing `mfe_pct` at the arm's own exit, so on a row that
flipped out early it records how far the trade ran *before SAR closed it* — a
figure that is always about right and never means what a reader assumes. This is
#869's defect arriving on a second page: the shape is fine, the definition is
not, and it is worse than a blank because a blank prompts a question. The engine
now walks a second arm to the **original** stop; the two peaks render in separate
columns and are never blended.

Rules the panels carry:

- **The horizon bucket is a FLOOR, not an answer**, and is never pooled with the
  rows that reached their stop. Pooled, a growing horizon bucket moves the
  headline without one trade behaving differently.
- **Read "Armed" before any PnL column.** A rule whose trigger the trade never
  reached is an ordinary trade on the original stop and scores exactly the
  baseline — so a rule armed on 5% of the book has not been tested whatever its
  average says. A test pins the never-armed-equals-baseline property.
- **The edge column is PAIRED** — measured only on rows where the rule and the
  baseline both priced, never two populations differenced. And the fee is
  charged to the baseline too, or the cost of trading becomes an edge.
- **The engine's `open` set stopped meaning "running SAR arms".** It now holds
  rows whose SAR arm exited hours ago while the held arm walks on, so
  `reduce_arms` splits on the row's own `status`, not on which list the engine
  filed it under. Reading `open` as live would put a resolved fill in the Running
  table under a live mark and a "Dist. to stop" column — a finished trade
  rendered as an open one, on the page whose whole identity is that difference.
- **`pre_arm` rows are their own bucket.** A row written before the arm shipped
  carries no `hold_status`; it is owed nothing and ages out on its own, and
  counting it as unresolved reports a fault that is not happening.
- **Coverage is graded, not merely counted** — on `pnl_level_pct`, which every
  row carries whichever bucket it lands in (the 2026-08-07 dark-feed rule).

**The label seam, caught by rendering rather than by a test.** The per-arm state
carries a rule *key*, not a label, so the first cut rendered `be_3` / `lock1_3` /
`trail2_3` at the reader: right numbers, unreadable headings, sixteen passing
tests. The catalog now ships **in the ledger** (`strategy_catalog`, written once
per file) and ops looks each key up — one writer, one reader, and a rule the
manifest does not describe renders badged rather than renamed. `MEASUREMENT_SUFFIXES`
wearing a sixth hat. **Render the page once before calling a panel done.**

And the cross-repo tests **drive the real engine module** (`_engine_rows()` steps
actual arms through `sar_live_shadow`) rather than a fixture, because a fixture
chooses a shape and then agrees with you about it — `zone_distance_atr` and the
price-action lane card both cost a session to that.

## `/signals/atr-live` and the lane selector — four populations, never pooled

Added 2026-08-09 (engine `src/trail_mechanisms.py`, `src/atr_trail_live.py`) for
the owner: *"exactly implement same for ATR-trail (Chandelier), and also
implement ATR-trail (Chandelier) and SAR on the dark feed too, then we can see
which actually makes a good setup, then we decide the exit mechanism. Live feed
is mostly MVRTP only; in the dark feed we at least have some other paths."*

**"Exactly the same" is an argument for one handler and one template.** The
engine runs one arm engine with a mechanism parameter, so `/signals/sar-live`
and `/signals/atr-live` are the same page with a different level function behind
it. Two route modules would be two places for the next panel to be added to,
which is how one surface silently stops showing what the other does — and both
pages carry six sessions' worth of hard-won columns that must not fork.

Both pages take `?lane=delivered|dark`, so there are **four populations and four
files**, and the split is not cosmetic:

| | Parabolic SAR | ATR-trail (Chandelier) |
|---|---|---|
| Delivered signals | `sar_live_arms_v1.json` | `atr_trail_arms_v1.json` |
| Dark feed | `dark_sar_arms_v1.json` | `dark_atr_trail_arms_v1.json` |

Rules the pages carry:

- **The delivered lane is the only evidence allowed to justify changing what
  subscribers receive**, and the page badges which one you are on before any
  number. A dark row reached nobody; pooling would inflate that evidence
  silently, because a reader who has not heard of the second population cannot
  filter it out. `TRAIL_ARM_FILES` is the single lookup — a page never assembles
  a filename of its own, and an unknown mechanism reads **nothing** rather than
  falling back to SAR's file under its own heading.
- **The dark lanes are on a different clock.** They ride the maintenance loop's
  ~5-minute resolve cycle, not the monitor loop's 60s heartbeat, so
  `LANE_STALE_SEC` is per lane. One bound for both would print FROZEN over a
  perfectly healthy dark lane — the caption naming a cause the page cannot
  observe, which this repo has paid for three times.
- **The mechanism block comes out of the LEDGER.** Label, parameters and whether
  the mechanism carries a direction are written by the engine into every file
  (`reduce_mechanism`), exactly as `strategy_catalog` and `spec` are.
  `MECHANISM_FALLBACK` exists only so a pre-manifest ledger renders, and the page
  **badges FALLBACK LABELS on screen** when it is used — a silent fallback is a
  mirror nobody knows is a mirror, which is how `MEASUREMENT_SUFFIXES` drifted
  for a week. A mechanism ops has never heard of keeps the engine's own label
  rather than borrowing another's short name.
- **A chandelier has no direction of its own**, so `sar_up` is `None` on those
  rows and renders as an em-dash. Not `False`: "does not answer that" and "says
  down" are different facts.
- **Read the risk columns before the PnL on the ATR page.** Unlike SAR — whose
  direction genuinely opposes the trade about a fifth of the time — the trail is
  nearly always onside at entry, so it cancels the evaluator's stop from bar one
  and replaces it with one that is frequently **wider**. That is what
  `r_level_risk` is for, and it is why a chandelier verdict must never be read in
  R alone. PnL % still leads.
- **The export stamps `mechanism` and `lane` on every row.** A spreadsheet is
  exactly where two populations get averaged into one, and a download that cannot
  say which mechanism or which delivery it describes is the artifact the whole
  file split exists to prevent.
- **The label is one string across surfaces.** `dark_signals.METHOD_LABELS["atr"]`
  and the ATR page's label are asserted equal, and
  `tests/test_atr_trail_contract.py` drives ops' **real** bake-off simulator and
  asserts it fills at the engine's chandelier level (agreement to 1e-9 on a
  shared vector). Two surfaces under one label computing two different levels
  already cost a session on 2026-07-31, and the agreement on the easy majority is
  what made it invisible.

## Copying a one-shot secret is part of the hand-off, not a nicety

`/control/access` displays a minted read-only code **exactly once** — only its
SHA-256 hash is stored, so a partial hand-selection costs a whole grant. The card
now carries copy buttons for the code, the `/guest` URL, and both together.

Two things it does deliberately:

- **The button copies the value the SERVER rendered**, and the test asserts the
  button's payload against that value rather than against the button existing. A
  copy control wired to the wrong string is the failure being guarded.
- **`navigator.clipboard` is undefined over plain HTTP and in some embedded
  browsers**, so the failure path *selects* the text and says so. A button that
  appears to work and does nothing is worse than no button when the thing it
  copies cannot be shown again — the same class as a control that 403s being
  indistinguishable from a broken page.

The URL is derived from the request rather than hardcoded, because the page is
reachable on the deployed host and on a local dev server and a copy button that
hands over the wrong host is worse than none.

## A control must show the state it is about to change (2026-08-10)

The owner set his own account's exit mechanism to SAR from `/control/users`, got
the flash confirming it, and the card still read **default (SL/TP FSM —
unchanged)** on every reload. Nothing was broken: the write landed (the trail
governor page showed both his open positions carrying `mechanism: sar`), and the
select was simply three hardcoded `<option>`s with no `selected` — over a lookup
payload that had never carried the field. A write with no read-back, on the one
control in this repo that changes how a real position closes.

Rules, and the first is the control doctrine's own sentence arriving at a `<select>`:

- **The engine is the source of truth, so read it back and render it** — the
  engine's lookup now returns `exit_mechanism` + `governor_enabled`, and the card
  states the live state *above* the control rather than only inside it.
- **Three states, never two.** `LIVE` (mechanism set **and** master switch on) ·
  `SET, NOT RUNNING` (set, switch off — a real state, and the one where a page
  showing only the per-user half would call an inert setting live) · *not
  reported* (an engine that predates the field). The last is not `default`, and
  merging them is "blank needs a cause before it gets a caption" at a control.
- **Use `.get`, not attribute access, for a field an older engine may not send.**
  Jinja yields `Undefined` for a missing key, which is neither `none` nor a
  value — the first cut fell past both branches and printed the "set" wording
  with a blank mechanism name.
- **Assert the tag, not the word.** The pre-fix page contained `sar` too, in an
  option nobody had chosen; the guard checks `selected` inside the option tag and
  fails against the old template.

## `/signals/trail-governor` — `place_failed` needed the exchange's words

Same session. Once the governing timeframe was corrected the governor reached the
placement step and Binance refused **every** stop: `place_failed` climbing 2 per
sweep with `handovers` stuck at 0. The page could show the integer and nothing
else — and -2021 (the level is already through the mark), -1111 (rounding),
-4015 (duplicate id) and a disconnected key are that same integer with four
different next moves.

The copy made it worse rather than merely incomplete: it called `place_failed`
*"the safe failure: nothing was given up"*, which is true about protection and
silent about a mechanism that will never hand over. **A caption that is true
about the wrong axis reads as reassurance.** The engine now publishes the last
few rejections; the page renders them, says `—` where the rejection did not come
from Binance at all, and prints the ring's size against the unbounded count so
the newest few cannot read as the whole population.
