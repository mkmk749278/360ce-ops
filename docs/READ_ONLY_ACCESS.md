# Temporary read-only access to ops

Ops is the engine's control plane, so its one existing door has always been
owner-tier: the session behind `OPS_AUTH_TOKEN` (+ TOTP) can engage the kill
switch, switch auto-execution to LIVE, and reset every signal. That is the right
shape for the owner and the wrong shape for anything else — there was no way to
let a collaborator, or an AI agent, *read* the measurement pages without handing
over the key that arms live trading.

This is that second door. The owner mints a short-lived code from **Control →
Access** (`/control/access`); the holder exchanges it for a **read-only**
session; the owner revokes it from the same page.

---

## For the owner

Everything lives on **Control → Access** (`/control/access`) — its own sub-tab,
not a card on the engine page. It is separated deliberately: every other control
writes to the *engine*, this writes to ops' own access store, and a revoke
sitting between the kill switch and auto-mode reads like an engine action. The
page also carries its own flash (`_access_flash`), because two writers on one
flash key means an action can render its result on a page the operator did not
come from.

**Mint.** *Generate a code* → label it, pick a duration (1h / 6h / 24h / 7d,
default 6h). The code is displayed **once**, on the render that follows. It is
not recoverable afterwards — the store keeps only its SHA-256 hash — so if it
scrolls past, revoke it and mint another. Hand over the code plus
`https://ops.luminapp.org/guest`.

**Watch.** *Issued codes* lists every grant: how many requests it has made, when
it was last used, and how many were **refused**. A non-zero refusal count is not
necessarily an attack — a stale bookmark or a link in a page body does it — but
it is the number worth reading. *Access log* below it carries the mints, logins,
revocations and every refusal with its path and stated reason, filtered to this
subsystem rather than repeating the engine's control history.

**Revoke.** *Revoke* on one grant, or *Revoke all*. It takes effect on the
guest's **next request**: the session holds only the grant id and the grant is
re-read on every request, so there is no window in which a revoked code keeps
working. A grant also dies on its own at its expiry, with no cleanup job in the
path — a code nobody remembers to revoke is not a code that keeps working.

---

## For the holder

```bash
# One-time: exchange the code for a session cookie.
curl -s -c ops.jar -X POST "https://ops.luminapp.org/guest?json=1" \
     -d "code=XXXXX-XXXXX-XXXXX-XXXXX"
# → {"ok":true,"label":"claude-agent","expires_in_sec":86376,"scope":"read-only"}

# Then read anything in scope, with the cookie.
curl -s -b ops.jar https://ops.luminapp.org/track-record/trades.csv
curl -s -b ops.jar https://ops.luminapp.org/signals/dark-live/export.csv
```

In a browser, go to `/guest` and type the code. Case, dashes and spaces do not
matter, and `I`/`L`/`O`/`U` are folded to what you meant — the code is read off a
screen and typed.

The code is exchanged **once**, for a cookie. There is deliberately no
per-request code header: a credential replayed on every request is a credential
written into every log line and shell history it passes through, whereas the
cookie carries only the grant id.

**Readable:** the feed and every signal, all the measurement lanes (SAR live,
dark feed, entry features, structural snap/veto, price action), positions, pairs,
the whole Performance tab, the Strategy Lab and Layer G, the truth report,
alerts, data-intake, the audit board, and **every CSV/JSON export** on those
pages.

**Not readable, and it will say why:** the control panel and its Access sub-tab
(a holder must not see the live grants, let alone mint one), the diag runner,
subscriber tables (users / referrals / trials), the raw data volume, the
`/api/v1` token surface — and **every** write, on every route.

Measured against the live nav, a holder sees **5 of 6 groups** — Overview,
Signals, Performance, Autonomy, Diagnostics, with Control absent entirely — and
inside Diagnostics **5 of 6** sub-tabs, Diag runner being the one withheld. Every
other sub-tab in those groups is reachable.

---

## How it is enforced

Three rules, in `app/guest_scope.py`, applied in this order:

1. **Method** — a guest may issue `GET` and `HEAD`. Nothing else, ever. This is
   structural: it covers every write route in the app today and every one added
   tomorrow, with nobody remembering to update anything.
2. **Route classification** — the matched route's *path template* must be
   classified `guest`. Unclassified is denied.
3. There is no third rule and no override.

**Why rule 2 exists, given rule 1.** "GET is safe" is false in this app, and it
is false in the place you would least expect: `/exit-backtest/run-now` is a `GET`
link that starts a `docker exec` backtest against the production engine —
deliberately, because a proxy was eating the form POST. A method-only gate would
have handed a read-only guest a job trigger. So the safe set is enumerated, not
inferred.

**Why a table and not a deny-list.** This repo has paid for the deny-list shape
repeatedly — `is_tradfi_perp`'s name list, `MEASUREMENT_SUFFIXES`, the
hand-written key carry in `_build_scan_context` — and the lesson each time was
that a list of what to *exclude* is silent by construction on the next member.
Here the next member is a new ops page, and a deny-list would hand it to every
live guest code the day it ships. So the table is **total**:
`tests/test_guest_access.py` derives its requirement from `app.routes` and fails
when a route appears that nobody has classified, and the runtime default for an
unclassified route is **deny**. A new page is invisible to guests until someone
says otherwise, and CI says so out loud.

**The nav is filtered from the same set the gate enforces** — not from a second
list of "pages a guest sees", which would drift, and whose drift only shows up
when somebody clicks a link that 403s. A test walks every link a guest is
actually shown and asserts the gate accepts all of them.

### If CI fails with "unclassified route(s)"

You added a page. Decide: does a read-only reader need it?

- Yes → add its path template to `GUEST_READ_ROUTES` in `app/guest_scope.py`.
- No → add it to `OWNER_ONLY` **with a reason**. The reason is rendered on the
  403 page and written to the audit log; a refusal with no stated cause is the
  same defect as a blank panel with no caption.

Do not delete the assertion.

---

## What is *not* claimed

- **This is not a second owner tier.** Scope is fixed at read for every grant and
  there is no scope parameter. A tier that can *sometimes* write is one whose
  blast radius has to be re-derived at every call site; read-only is the only
  tier that needs no such argument.
- **A read-only reader still sees real trading data** — every signal, every
  position, every PnL figure the owner sees. It withholds the control plane and
  subscriber PII, not the book. Mint accordingly.
- **The lockout protects the guest door only.** Ten failed codes in five minutes
  closes `/guest` for fifteen; `/login` is a different route and is deliberately
  unaffected, because a guest-side lockout must never be able to lock the owner
  out of his own kill switch.
