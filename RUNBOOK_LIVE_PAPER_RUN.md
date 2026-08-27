# RUNBOOK — LIVE-01 paper-monitor, one daily run (review-only)

_How an interactive session drives one forward paper-monitor run against the live Robinhood
MCP. **Review-only**: `review_equity_order` is the ONLY order call (it simulates — nothing is
placed). Account `123456789` is the $0 agentic account. This is the SUPERVISED week-1 plumbing
checkpoint, not autonomy._

## Why this is agent-driven (not a scheduled job)
Python cannot invoke MCP tools here, and unattended/scheduled/headless runs cannot reach the
broker MCP (auth spike + the token-refresh cluster). So **a human-supervised interactive session
(the agent in its main loop) makes every MCP call.** The `autotrader_live` package is pure
(normalizers + decision logic + the review-only broker); the agent fetches, the package computes.

## Timing
Run during **regular trading hours, ~09:45–10:00 ET**. The decision fires on the **last completed
(settled) daily bar** — never today's partial/interpolated bar — and the morning window makes the
`review_equity_order` spreads real. (Conscious deviation from spec §5's "compute on the 16:00
close," defensible because the signal is on the prior completed bar.)

## Preconditions (abort loudly if any fail)
1. Interactive session with the Robinhood MCP connected (probe `get_accounts` → confirm
   `123456789` is `agentic_allowed=true`).
2. Canonical scan exists: `get_scans` → `<your-scan-id>`
   (`Volume>1M ∧ RSI>60 ∧ MarketCap>2B ∧ Close>10`).
3. Working tree on branch `claude/live-01-paper-monitor`, `715+` tests green.
4. **Data is REAL live Robinhood pricing** (verified 2026-06-23: AMD/Micron match the live market to the cent). Paper P&L derived from it is real-market — the read-only market data is live even on the unfunded account.

## Config for the run (defaults — confirm with the operator)
- `account_number = "123456789"`, `equity = 1000.0` (nominal intended capital for would-be sizing;
  the account is $0 → `review` returns `EQUITY_NOT_ENOUGH_BP`), `near_threshold=0.90`,
  `f=0.01, k=3.0, per_name_cap_frac=0.15, m=2.0`, `blackout_days=5`, `top_n=10`,
  `enrich_count≈30` (top-by-market-cap candidates to enrich — enough to find 10 entries).
- `state_dir = "data/live/state"`, `telemetry_path = "data/live/telemetry.jsonl"`,
  `raw_dir = "data/live/raw"` (all gitignored).

## Procedure

### 0. Idempotency + signal date
- Build a CURRENT calendar: fetch fresh SPY daily history (`get_equity_historicals(["SPY"],
  start_time≈400d ago, interval="day", adjustment_type="split")`), `mcp_live.normalize_bars`,
  write to a temp DataStore, `TradingCalendar.from_datastore(store, adjustment="split")`.
  (The committed cache ends 2026-06-16; the calendar MUST be refreshed forward or the bar guard aborts.)
- `signal_date = paper_monitor.resolve_signal_date(calendar, today_eastern)` (last settled session).
- If `paper_monitor.is_day_complete(state_dir, signal_date)` → **STOP** (already recorded; no-op).

### 1. Universe scan
- `run_scan("<your-scan-id>")`. The result is large → it auto-saves to a file;
  `jq` the rows (or read in chunks). `mcp_live.normalize_scan(raw)` → `list[ScanRow]` (already
  market-cap-desc sorted). Take the top `enrich_count` symbols.

### 2. Tradability gate
- `get_equity_tradability(account_number, symbols)` in batches ≤10. `mcp_live.normalize_tradability`.
  (Drops synthetic/odd instruments — e.g. a tokenized "SpaceX" row.)

### 3. Enrich (BULK fetch — delegate to FOREGROUND subagents)
For the tradeable candidates:
- **Historicals** (≥253 settled bars each): `get_equity_historicals(symbols≤10, start_time≈400d ago,
  interval="day", adjustment_type="split")`. These payloads are LARGE → **dispatch foreground
  subagents to fetch and write the raw JSON to `data/live/raw/hist_<batch>.json`** (subagents CAN
  reach the broker; they absorb the large payloads), then the MAIN loop reads + `normalize_bars`
  CENTRALLY (per the "subagents fetch, controller normalizes" rule — never let a subagent normalize).
- **Fundamentals**: `get_equity_fundamentals(symbols≤10)` → `normalize_fundamentals` (cost tier + sanity).
- **Quotes**: `get_equity_quotes(symbols)` → `normalize_quotes` (authoritative settled close +
  spread). Reconcile each name's `normalize_bars` last settled date against `quote.settled_close_date`;
  on disagreement, **drop that name with an attributed note** (do not guess).
- **Earnings**: `get_earnings_calendar(start_date=signal_date, days=blackout_days,
  filter="high_market_cap")` → `normalize_earnings` (blackout).

### 4. Assemble + positions
- `market_data = mcp_live.StaticMarketData(scan_rows=…, historicals={sym:df}, quotes=…,
  tradability=…, fundamentals=…, earnings=…)`.
  (All parameters are keyword-only. Use the EXACT constructor names: `scan_rows=` not `scan=`,
  and `quotes=` is required.)
- `positions`: load yesterday's `DayRecord` (`load_day_record`) + `get_equity_positions(account)` →
  build `dict[str, Position]`. **Day 1: empty** (`{}`).

### 5. Plan the day
- `plan = paper_monitor.plan_day(market_data, positions, signal_date=signal_date,
  account_number="123456789", equity=1000.0, config={...})`.
- `plan.order_intents` = the would-be entries (market buy, dollar_amount) + catastrophe stops
  (stop_market GTC sell) + any held-name ratchets. `plan.held_halted` / `plan.skipped` /
  `plan.earnings_flags` / `plan.reconciled` carry the rest. **`plan.reconciled` must be True**
  (in-run determinism replay) — if `plan_day` raised on reconcile, ABORT → `record_skip`.

### 6. Review each would-be order (the agent makes the MCP calls)
- For each `intent` in `plan.order_intents`: call `review_equity_order(account_number=…, symbol=…,
  side=…, type=intent.order_type, quantity=intent.quantity OR dollar_amount=intent.dollar_amount,
  stop_price=intent.stop_price, time_in_force=intent.time_in_force)`. Collect `{intent.ref_id: raw_response}`.
- **Surface `market_data_disclosure` VERBATIM** for every reviewed order (compliance).
- Build `broker = PaperBroker(responder=lambda i: collected[i.ref_id])`.

### 7. Record + telemetry (atomic)
- `record = paper_monitor.run_day(market_data, broker, positions, signal_date=signal_date,
  account_number="123456789", equity=1000.0, state_dir=…, telemetry_path=…,
  run_timestamp=<inject ISO now>, config={...})`. (`run_day` is idempotent + composes
  plan→review→record→telemetry; since reviews were pre-collected into the broker's responder, it
  re-plans deterministically and records.) State writes atomically to `state_dir/<signal_date>.json`.

### 8. Present to the operator
- The day's would-be orders (symbol, side, type, size, stop), each with its `review` alert
  (`EQUITY_NOT_ENOUGH_BP` on the $0 buys) and the verbatim `market_data_disclosure`; the selected
  names + decisions; `held_halted`; `skipped`; earnings flags; `reconciled=True`.

## Loud-failure / missed-fire policy (NON-NEGOTIABLE)
- ANY auth/token/fetch/reconcile fault → `paper_monitor.record_skip(state_dir, signal_date,
  reason="<attributed>", run_timestamp=…, account_number=…)` so the day is recorded as an
  **attributed skip**, never a clean absence. A trading day with NO record file = a silent miss
  (audit: compare calendar trading days to recorded `state_dir` files).

## Hard guardrails
- **NEVER call `place_equity_order` / `cancel_equity_order` / `place_option_order`.** Only
  `review_equity_order` (simulate). `PaperBroker` raises `NoPlaceInPaperError` if a place path is hit.
- A held name that fails to fetch → its ratchet HALTS; the resting GTC catastrophe stop persists;
  it is NEVER silently sold (the loop emits no exit intent — verified by test).
- Pin `adjustment_type='split'` on every historicals call.
