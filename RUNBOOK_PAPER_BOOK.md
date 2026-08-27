# RUNBOOK — LIVE-02 intraday paper book (continuous, session-bound)

_How an interactive agent session drives the forward paper simulator against the live Robinhood MCP. **Paper only:** the broker is touched READ-ONLY (quotes/positions/scan); **no order-surface call is ever made — not even `review_equity_order`.** Every fill is virtual and recorded in `data/live/paper_book/`. The strategy is the existing single-name trend/trailing-stop, params fixed. Honest expectation: neg-to-breakeven after costs — this is a forward record + operational learning, NOT proven alpha. Every artifact that renders the equity curve must say so._

## Why this is agent-driven and SESSION-BOUND (not a daemon)
Python cannot invoke MCP tools here; the AGENT makes every MCP call. `ScheduleWakeup` re-invokes the agent on a timer, but **only while this interactive app/session stays open and the machine is awake** — it does NOT survive an app close, a laptop sleep, or the ~95h OAuth-token expiry, and unattended/scheduled runs in their own transcript cannot reach the broker MCP (LIVE-01 auth-spike: a scheduled probe FIRED but STALLED — connector absent, permission-prompt hang). So "continuous" means "continuous while this session is alive." A death (token/crash/hang/sleep) stops the loop; the external heartbeat (below) pings the operator; on restart the loop resumes from the last `book.json` checkpoint.

## Interpreter & data (read first — both bit us once)
- Run everything with the project venv: `.venv/bin/python` (Python 3.11). Bare `python` is 3.9 and produces ~100 spurious failures.
- The OHLCV cache `data/cache/*_day_split.parquet` is gitignored; a fresh `git worktree` has none. Copy the 15 parquet files from the main worktree's `data/cache/` before running tests.
- State dir: `data/live/paper_book/` (gitignored): `book.json` (atomic source of truth, holds the full fills list), `fills.jsonl` + `equity_curve.jsonl` (derived, self-healed from `book.json` on load), `raw/` (the agent's raw MCP dumps for central normalization).

## P0 GATE (must pass before any continuous run)
Before trusting the loop, prove in a foreground session: (1) `ScheduleWakeup` re-invokes the agent and the Robinhood MCP is still reachable after it fires (probe `get_accounts` → `123456789 agentic_allowed=true`); (2) a `get_equity_quotes(["SPY"])` round-trip returns a normalized `Quote` (record the live `quote.state` string and, if it isn't `"active"`, extend `fill_engine._ALLOWED_STATES`); (3) the external heartbeat job fires and a `PushNotification` reaches the operator; (4) note whether `ScheduleWakeup` survives an overnight sleep with the app open, or whether the nightly pause must be a session boundary (the operator restarts in the morning). If the wake/heartbeat cannot re-reach the MCP, STOP — do not run the loop; escalate (supervised-manual or the token-relay project).

## Config (ratified)
`account_number="987654321"` (NON-order-capable; the driver records this for all modes as of LIVE-03 — LIVE-02's first supervised run recorded `123456789`), `start_capital=2000.0`, `near_threshold=0.90`, `f=0.01`, `k=3.0`, `per_name_cap_frac=0.15`, `top_n=10`, `m=2.0`, `blackout_days=5`, `slippage_bps=3`, `MIN_NOTIONAL=50`, poll cadence ≈ 15 min during RTH. Saved scan `<your-scan-id>` (Volume>1M ∧ RSI>60 ∧ MarketCap>2B ∧ Close>10).

## Daily arm — ONCE per trading day, at/after the open
Gate on `session_clock.is_regular_session(now_et)` AND the broker quote `state` (authoritative). Then:
1. Probe auth (`get_accounts`). Refresh the forward calendar: `get_equity_historicals(["SPY"], start_time≈400d ago, interval="day", adjustment_type="split")` → `mcp_live.normalize_bars` → temp DataStore → `TradingCalendar.from_datastore(store, adjustment="split")`. Compute `signal_date = paper_monitor.resolve_signal_date(calendar, today_eastern)`.
2. `run_scan("<your-scan-id>…")` → `mcp_live.normalize_scan` → take the top ~30 by market cap (enough to find ~10 entries).
3. `get_equity_tradability(account, symbols≤10)` (batches) → gate. Enrich tradeable names: **delegate the large `get_equity_historicals` (≥253 settled bars, `adjustment_type="split"`) fetches to FOREGROUND subagents**, which write raw JSON to `data/live/paper_book/raw/hist_<SYM>.json`; the MAIN loop reads + `normalize_bars` CENTRALLY (never let a subagent normalize). Also `get_equity_fundamentals`, `get_equity_quotes`, `get_earnings_calendar(start=signal_date, days=5, filter=high_market_cap)`.
4. `python scripts/run_paper_book.py arm <signal_date_iso> <today_iso>` — normalizes centrally, loads-or-creates the book, runs `arm_day` (which runs `completed_bar_guard` per name → never arms off an unsettled bar), freezes the armed set + `target_notional`, checkpoints. Review the printed armed set.

## Poll loop — every ~15 min while RTH is open
While `session_clock.is_regular_session(now_et)` and the broker reports the session open:
1. `get_equity_quotes(held + armed symbols)`; write raw to `data/live/paper_book/raw/quotes.json`.
2. `python scripts/run_paper_book.py poll <ts_iso>` — sanity-gates quotes, fills triggered entries (@ ask + slippage), stops out open positions (@ bid, `bid ≤ stop`), ratchets stops up on sampled highs, marks-to-market (unfillable/missing quote → mark at cost + `n_marked_at_cost`), checkpoints `book.json` (atomic) then appends the `fills`/`equity_curve` rows. Review the poll summary.
3. `ScheduleWakeup(≈900s)` to the next poll. (Stay under 300s only if actively watching; otherwise ~15 min is the cadence.)

## Self-pacing, death, and pause
- **Hang timeout:** if a poll produces no `book.json` ts advance within N seconds, the next wake treats the prior poll as a stalled death — write an attributed gap note and `PushNotification` the operator. A hung MCP call must not hang forever silently.
- **External heartbeat (independent of the dying agent):** a tiny `CronCreate`/`scheduled-tasks` job (MCP-free, pure file read) checks `equity_curve.jsonl`'s last-row timestamp and `PushNotification`s the operator if it is stale > ~20 min during RTH. This is the only death signal that survives a hang/sleep/expiry.
- **Token expiry (~95h):** store `token_issue_ts`; refuse to `arm` a new day past `issue + ~90h`; ping "token expiring, re-auth needed" the morning before; treat expiry as a PLANNED stop.
- **Nightly close:** a pause, not a death — checkpoint and either long-sleep to the next open (if the app stays open, per the P0 finding) or stop and have the operator restart in the morning (resume from checkpoint).
- **Durable audit:** the on-disk record is the source of truth — compare calendar trading days to `equity_curve.jsonl` rows; a missing row is a detectable gap, never a clean absence. `PushNotification` is best-effort.
- **Off-machine copy:** periodically copy `book.json` off the laptop (the record lives only here).

## Hard guardrails
- **NEVER call any order-surface tool** — not `place_equity_order`, `cancel_equity_order`, `place_option_order`, `cancel_option_order`, nor `review_equity_order`. The broker is read-only (quotes / positions / scan / tradability / fundamentals / earnings). The package contains none of these tokens (`test_no_place_invariant.py` enforces it).
- **Never force-sell on missing/bad data:** a held name whose quote is missing OR fails `quote_is_fillable` (crossed bid/ask, halted, >50% move, not `active`, not `has_traded`) skips its stop-check + ratchet that poll; the resting virtual stop stands; the position is never sold on absent/insane data.
- **Pin `adjustment_type='split'`** on every historicals call.
- **Entries are settled-bar decisions:** the qualifying set is fixed once per day on the last settled bar; intraday only TIMES the entry (trigger) and manages stops. No intraday re-screen.
- **`signal_date`/`today` are computed by the agent** (calendar) and passed to the driver as argv; the driver does not consult the calendar.

## Honesty
The equity curve is a real-market forward track record of a strategy whose honest expectation is neg-to-breakeven after costs — it is NOT evidence of edge. State that on any view of the curve. `entries_taken=False` rows mark polls that ran before the day's arm (stale-arm skip); `n_marked_at_cost>0` rows mark polls whose equity is provisional (a held name had no usable quote).

## Autonomous (scheduled-poll) mode — LIVE-03

Four RECURRING scheduled tasks over the pure driver (see automation/README.md):
ARM, POLL, HEARTBEAT, EOD. Tier-0 only: runs while the Mac is awake + the app is
open; missed slots (sleep/close) are caught by the EOD slot-gap audit + heartbeat,
not replayed.

### Safety — real orders are structurally impossible
- PRIMARY (account lockdown): the loop records and reads account 987654321
  (agentic_allowed=false). A place_* against it is broker-rejected. OPERATOR
  ACTION REQUIRED before arming: also set agentic_allowed=false on 123456789 so
  NO agent-actionable account exists.
- SECONDARY (defense-in-depth): zero order-surface tokens in the package + loop
  scripts + prompts (test_no_place_invariant, widened fail-closed over scripts/*.py
  + automation/prompts); the driver makes no MCP calls; prompts forbid all six
  order tools; per-task approval covers only the read tools; the loop graph cannot
  import the order-executing surface (broker.py/LiveBroker) — enforced by
  test_loop_import_fence; runtime push if a stalled order is ever attempted.

### Death signal (the dropped wall-clock guard)
There is NO wall-clock OAuth-token guard. The death signal is a broker
auth/unreachable error at ARM or POLL -> "re-auth needed" push + stop; a dead
token also stales the curve, which the heartbeat catches. (The old ~95h guard
anchored to an unobservable value; it is dropped. An optional ~90h soft morning
warning is intentionally NOT built — YAGNI.)

### Off-machine record
The EOD task (eod_audit.py) snapshots book.json + equity_curve.jsonl + fills.jsonl
to a separate data branch in your own repo each day. One-time setup (orphan branch
+ git worktree) is documented in automation/README.md.

### Mode coexistence
Keep the supervised ScheduleWakeup RUNBOOK loop (above) as a maintained, tested
fallback until the scheduled path banks >= 5 clean autonomous days.

### Autonomous-mode GATING (DO NOT ARM FOR REAL until all pass)
1. Account lockdown: operator sets agentic_allowed=false on 123456789. Confirm
   the full loop's reads still work on 987654321 AND a place_* call is rejected.
2. Per-task approval scope (adversarial, AUTO-fired): approve only the ~7 read
   tools; in a throwaway recurring task with ONLY reads approved, have it attempt a
   named order call (e.g. a place_equity_order on a throwaway intent) and confirm it
   STALLS (no order placed) — approval is strictly per-tool, not broad. Run this in
   an actually AUTO-fired task (not just Run-now), since auto-fire is the mode the
   loop runs in and the untested axis. If a broad approval would let it through ->
   STOP, do not arm. Delete the throwaway task afterward.
3. Full read surface AUTO-fires: confirm an actually auto-fired (not Run-now)
   scheduled task reaches the full read surface (scan/quotes/tradability/
   historicals/fundamentals/earnings), not just get_accounts.
4. Kill-switch rehearsed: disable the four tasks + revoke broker approval in one
   pass; confirm the next fire does nothing.
Only after 1-4 pass: enable the four recurring tasks + re-enable HEARTBEAT.
