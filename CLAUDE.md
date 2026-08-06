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

## The read-only door (`/guest`) — a second tier that can never write

Added 2026-08-06 (`docs/READ_ONLY_ACCESS.md`) for the owner's question: *"give
access to you to browse our ops page to load data … except control Panel, from
control I generate temporary code … and I can disable that access too"*. The
owner mints a short-lived code on `/control`; the holder exchanges it at
`/guest` for a read-only session; the owner revokes it from the same card.

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
| Exit-method what-ifs on the dark feed — reads the engine's held-to-stop arm, prices `exit_sim`'s catalog | `app/data_sources/dark_exit_sim.py` |
| Structural SL/TP1 snap stamps — `structural_snap_v1.json` (`/signals/structural-snap`) | `app/data_sources/structural_snap.py` |

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
