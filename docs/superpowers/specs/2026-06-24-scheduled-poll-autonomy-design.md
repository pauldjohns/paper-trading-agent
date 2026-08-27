# Scheduled-poll autonomy for the LIVE-02 paper book — design

_Date: 2026-06-24 · Status: REV 2 (post three-reviewer pass + account-lockdown investigation) · Scope: full autonomy (headless ARM + POLL) at the leave-app-open tier, safe-by-construction via account lockdown; plus a map (not build) of the always-on-host path._

## Why we're building this (the deliverable that justifies the effort)

The underlying strategy's honest expectation is neg-to-breakeven after costs — so this is **not** built for alpha. The deliverable is the **autonomy harness itself**: a hands-off, safe, auditable forward-paper runner that is the operational groundwork for any later real-money phase, plus the honest forward record it accrues. Building full autonomy now is justified only under that framing; if the goal were merely "more paper days," clicking the supervised loop would be cheaper. (Reviewer note folded in: name the deliverable so the build's effort/risk is proportionate.)

## Context

- LIVE-02 paper book built; first supervised session ran 2026-06-24 (4 entries, closed $2000.55). Today's loop is **session-bound**: an interactive Claude session re-invokes itself via `ScheduleWakeup` every ~15 min and makes every broker MCP call. It dies on app-close / sleep / ~95h token expiry and must be babysat.
- **Autonomy findings (2026-06-24):** (a) a SCHEDULED, non-interactive task CAN reach the broker MCP once its tools are pre-approved (proven for `get_accounts`; the earlier "can't reach" was an unapproved-permission stall, not a missing connector). (b) Scheduled tasks fire **only while the Mac is awake + app open**; they catch up on next launch. (c) One-shot scheduled tasks are flaky ("could not start"); **recurring** tasks fire reliably (with multi-minute dispatch jitter). (d) **The loop's reads work against the non-order-capable account `987654321`** (`get_equity_tradability` confirmed; all other reads are account-less market data).
- The driver `scripts/run_paper_book.py` is **pure/stateless** over `book.json` (modes `arm`/`poll`/`status`); `arm` idempotent per day; `poll` appends one equity row; the driver makes **no MCP calls**.
- Helpers on-branch: `compute_signal_date.py`, `validate_raw.py` (central guarded ingest + transposition/look-ahead validation), `heartbeat_check.py` (MCP-free liveness watchdog).

## Goal

With the Mac awake and the app open, the daily ARM and the 15-min polls run fully **headless** (no human or interactive-Claude babysitting), paper-only, broker read-only, with real orders **structurally impossible**. Plus: **map (not build)** the always-on-host path.

## Non-goals

- 24/7 operation through sleep / app-closed (mapped, not built).
- Real-money orders (structurally prevented; see Safety).
- Strategy/param changes (frozen).
- Gap catch-up/replay (accepted; surfaced in the EOD audit + heartbeat).

## Safety — real orders are structurally impossible (primary), with defense-in-depth (secondary)

Threat model correction (reviewer-driven): in the autonomous model the **headless agent**, not the driver, makes every broker call — so package/driver controls alone do **not** stop a confused or prompt-injected agent from calling an order tool. The catastrophic path (a real order) is therefore closed at the **account layer**:

**PRIMARY — account lockdown (safe by construction):**
1. The loop uses account **`987654321`** (`agentic_allowed=false`) for its only account-scoped read (`get_equity_tradability`); all other reads are account-less market data. A `place_equity_order` against a non-agentic account is **broker-rejected** ("this agent cannot act on it").
2. **Recommended operator action:** also set `agentic_allowed=false` on `123456789` (the agentic account) via Robinhood's agentic enrollment. Reads survive (they work on the non-agentic account today), and then **no agent-actionable account exists** → an order is impossible regardless of mis-click, tool-rename, inherited approval, or prompt-injection. This is the single highest-leverage control; the autonomous loop should not be armed until it is done OR the residual is explicitly accepted.

**SECONDARY — defense-in-depth (belt-and-suspenders; each independently insufficient but cheap):**
3. Package contains zero order-surface tokens — `test_no_place_invariant.py`, **widened** to scan `scripts/` and the task-prompt files (today it globs only `src/autotrader_live/`, missing `scripts/run_paper_monitor_live.py` which contains `review_equity_order`), and the forbidden set **extended** to include `review_option_order`.
4. The driver makes no MCP calls (can't place an order even if asked).
5. Task prompts are strictly read-only and name all six forbidden tools.
6. Per-task tool approval: approve only the read tools; never approve any order tool (a stray order call then stalls). Scope (per-tool vs broader) is **unconfirmed** → must be verified adversarially in dry-run (attempt an order call in a throwaway task with only reads approved; confirm it stalls).
7. The latent `LiveBroker`/`OrderIntent` order-construction code in `broker.py` is fenced into a module not importable by the autonomous loop (or deleted until a funded phase needs it).
8. **Runtime audit:** each ARM/POLL emits a one-line record of which broker tools it called; an alert fires if the set ever exceeds the approved reads (catches an attempted-but-stalled order).
9. **Kill-switch:** a documented one-gesture stop (disable the recurring tasks + revoke broker approval), rehearsed in dry-run.

Paper-vs-real labeling: every artifact/prompt states "live-money account, simulator-only, orders forbidden"; the loop's recorded account is the non-order-capable `987654321` so the label matches reality.

## Architecture — three scheduled tasks over the pure driver

Each task is a recurring (never one-shot) headless scheduled-task agent. Crons are expressed in **local-hour** form (DST-safe: MT and ET share US DST dates and RTH is invariant in local wall-clock), never as named offsets.

### Task 1 — ARM (recurring, idempotent fire-once/day)
- **Cron:** local hour ≈ 07:40 MT (≈09:40 ET) weekdays, expressed as a local-hour cron. After the open so the broker confirms a live session.
- **Steps:**
  1. **Fresh start:** clear/relocate any stale `raw/` files from a prior day (prevents reuse of yesterday's dumps).
  2. **Gate:** `get_equity_quotes(["SPY"])` `state=="active"` AND `session_clock.is_regular_session(now_ET)`; else exit.
  3. **Idempotency:** `status` → if `last_arm_date == today` → exit.
  4. **signal_date:** fetch SPY history `end_time=<today>T00:00:00Z` → `hist_SPY.json` → `compute_signal_date.py`.
  5. **Universe fetch:** `run_scan` → top-30; tradability (against `987654321`) / fundamentals / quotes (batched) + earnings; per-symbol historicals (`end_time` cutoff). Write verbatim.
  6. **Central ingest (guarded), now with completeness:** `validate_raw.py <signal_date>` — extended to assert **full coverage** (every top-30 symbol present in tradability + fundamentals + quotes, or a loud fetch-failure abort) and **freshness** (every consumed file's mtime is today), in addition to the existing hist symbol/bars/last-date/guard/close-cross-check/duplicate checks. ARM proceeds only after an `arm_complete` sentinel is written (all fetches landed). Any failure → **abort ARM, no book mutation, push "ARM ABORTED: <reason>".**
  7. **Arm:** `run_paper_book.py arm <signal_date> <today>`.
  8. **Notify:** "ARMED N: [syms]" or the abort.

### Task 2 — POLL (recurring, every ~15 min)
- **Cron:** local-hour window covering RTH **starting at/after the ARM time** (so the first poll never precedes the arm; closes the open-breakout-blind window). Slightly wide; the agent re-gates.
- **Steps:**
  1. **Gate / final-poll state machine:** if `is_regular_session` → normal poll. If not in session but `last_poll_ts` date == today and no `eod_done` marker → this is the **final poll** (do it, push EOD, set `eod_done`). Otherwise (closed all day / already did EOD) → exit. Cron window is set so at least one fire lands 15:55–16:00 ET despite jitter (don't miss the close-bell breakout — the AMAT-15:59 case).
  2. **Self-heal:** if RTH and `last_arm_date != today` → trigger the ARM path (don't just skip; otherwise a failed ARM = a dead trading day).
  3. Load held+armed from `book.json`; fetch their quotes → `quotes.json` (≤20/batch).
  4. `run_paper_book.py poll <ts>` — derive `poll_day` from **ET** (not a UTC slice) for the `entries_taken` comparison.
  5. **Notify on events only** (fills/stop-outs); else silent.

### Task 3 — HEARTBEAT (existing, re-enabled + corrected)
- `heartbeat_check.py` must key on **"armed/active today"**: compare the last `equity_curve` row's **date** (not just file mtime) to today AND check `book.last_arm_date == today` via `status`. Emit a distinct **"NOT ARMED today"** alert (vs a generic STALE) and stop false-`STALE`-ing on un-armed mornings (the durable append-only `equity_curve.jsonl` always exists after day 1). Re-enabled when arming the autonomous mode; paused on intentional stop.
- **Broker-auth failure is the real death signal at BOTH ARM and POLL:** on any MCP auth/unreachable error, push "re-auth needed" and stop. The old ~95h wall-clock token guard is **dropped** (it anchored to an unobservable value and would either false-lock-out or fail-silent; a dead token → stale curve → heartbeat already covers it). Keep ~90h only as an optional soft morning warning, never a hard gate.

## Data flow

Unchanged except the actor: `raw/` ← headless agent MCP dumps (verbatim) → `validate_raw.py` (central, guarded, now coverage+freshness) → canonical files → `run_paper_book.py arm/poll` → `book.json` (atomic) + `equity_curve.jsonl`/`fills.jsonl`. The EOD task also copies `book.json` **off-machine** (the record otherwise lives only on the laptop).

## Error / death handling

- Bad/stale/partial data at ARM → `validate_raw.py` fails → abort, no mutation, push.
- Missing/bad quote at POLL → driver skips that name's stop-check + marks at cost (`n_marked_at_cost`); resting stop stands; never force-sell on bad data.
- Poll hang/crash, dead token, or un-armed day → heartbeat / NOT-ARMED / auth-failure alerts (distinct messages).
- Mac-sleep mid-RTH → missed slots; the EOD audit diffs expected RTH 15-min slots vs actual `equity_curve` rows and reports the gap count (a sleep gap is visible same-day). Honest caveat: a virtual stop only executes on a poll, so a poll gap defers a stop-check (recorded, not hidden).

## Notifications policy

Event-only: ARM summary/abort, fills, stop-outs, errors (auth/abort), heartbeat death/stale/NOT-ARMED, one EOD summary (incl. the slot-gap count). No per-poll "nothing happened" pings.

## Always-on-host map (design only)

Three axes kept distinct (reviewer fix): dollar cost, integration effort, auth-preservation.
- **Tier 0 (this design):** leave-app-open on the operator's working Mac.
- **Tier 1 — lowest integration effort + auth-preserving (recommended 24/7 path):** a dedicated always-on Mac (`caffeinate`/no-sleep, auto-login, app auto-launch) reuses the validated MCP path verbatim with **zero re-validation and no token-relay**. NOT "cheapest" — it is real standing hardware + power cost; its virtue is reuse + auth-preservation.
- **Tier 2 — token-relay (not recommended):** forward the broker session to a remote runner. Security-sensitive, unknown feasibility against the agentic MCP. One cautionary line; not to be scoped without explicit need.
- **Tier 3 — cheapest in dollars / most portable:** official Robinhood REST API with keys on a small VPS. Different integration + full re-validation of the data/exec layer; loses MCP conveniences.

## Mode coexistence (decided, not open)

The supervised `ScheduleWakeup` RUNBOOK loop is **kept as a maintained, tested fallback** until the scheduled path banks ≥5 clean autonomous days (the scheduled platform is known-flaky; the fallback already exists at ~zero cost).

## Testing

- **Account lockdown:** confirm the full loop runs on `987654321`; confirm (operator) that disabling agentic on `123456789` leaves reads working and order calls rejected.
- **Approval scope (gating):** in a scheduled Run-now, exercise + approve each of the ~7 read tools; adversarially confirm an order call with only reads approved **stalls**. Build does not proceed until this passes.
- **ARM equivalence:** Run-now → armed `book.json` matches the manual path for the same `signal_date`; partial-fetch injection (delete one batch file) → validate aborts.
- **POLL correctness:** appends a correct row; self-heal triggers ARM when un-armed; final-poll state machine fires once near close and not again off-hours.
- **Idempotency:** second ARM same day = no-op; poll-before-arm = `entries_taken=False`.
- **Heartbeat:** planted-stale (RTH) → push delivered (closes the device-delivery validation); un-armed morning → "NOT ARMED", not false STALE.
- **No-order invariant:** widened `test_no_place_invariant` green over `src/` + `scripts/` + prompts; includes `review_option_order`.

## Open questions / risks

- Per-task approval scope (per-tool vs broader) — confirm in dry-run before arming.
- Full read surface in an **auto-fired** (not Run-now) scheduled task — still to confirm (only `get_accounts` proven auto; readsurface probe never auto-fired due to app-inactivity).
- Whether `123456789`'s `agentic_allowed` is operator-flippable, and whether reads survive a full agentic de-enrollment (reads survive on the *separate* non-agentic account today; full de-enrollment untested).
- Heavy ARM (≈30 historicals) reliability inside one headless agent — confirm in dry-run; may need batching/self-parallelization with the central-validate as the backstop.
