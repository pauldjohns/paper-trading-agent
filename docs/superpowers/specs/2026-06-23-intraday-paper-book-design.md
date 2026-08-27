# Design — LIVE-02: Intraday Forward Paper Book

_Draft 2026-06-23, **rev 2** after three independent cold reviews (methodology/honesty, architecture/firewall/correctness, operational/safety — all "ship-with-changes"). Builds on LIVE-01 (the review-only paper-monitor, on `main`). This adds the piece LIVE-01 consciously cut: a **forward paper simulator** — virtual fills against real live quotes + a running P&L/equity book — driven by an interactive session loop. Strategy logic is **unchanged and fixed**; this is a simulator + loop wrapped around it._

---

## 0. Goal & ratified decisions

**Goal:** Run the existing single-name trend/trailing-stop strategy forward on **real live Robinhood data**, fill **virtual** orders against live quotes, and maintain a running **paper book** (cash, positions, realized/unrealized P&L, equity curve) persisted to JSON — for as long as an interactive session stays open and the OAuth token is valid (~95h), or until the operator stops it. The broker is touched **read-only** (quotes/positions); **no order-surface call is ever made, not even `review_equity_order`**. Every fill is virtual.

**Deliverable:** an honest forward, real-market paper track record + operational learning. **Not** proven edge — expectation is neg-to-breakeven after costs. Do not pitch the equity curve as alpha; every artifact that renders it carries that caveat.

**Ratified by the operator (2026-06-23):**
- **Starting capital:** `$2,000`.
- **Strategy fixed:** existing trend/trailing-stop, ratified params — `near_threshold=0.90`, `f=0.01`, `k=3.0`, `per_name_cap_frac=0.15`, `top_n=10`, catastrophe `m=2.0`, `blackout_days=5`.
- **Entries:** daily settled-bar qualifying set + **intraday entry trigger**; **no-chase** (skip if it never triggers that day).
- **Cadence:** intraday polling every **15 minutes** during RTH (a *target*, not a guarantee — see §5).
- **Run model:** continuous, **agent-self-paced, session-bound** loop with checkpoint-resume + death-ping (see §5 for the honest mechanics and limits).
- **Settlement:** **instant cash** — reconciled as honest because arming is once-daily (§5.5).
- **Output:** JSON only.

**Resolved review decisions (rev 2):** entry trigger tightened (§2.2 Q3); symmetric spread-crossing fills (§2.2 Q1); sizing off morning realized+cash equity (§2.3 Q2); ratchet on sampled intraday highs, documented as an approximation (§2.2 Q5); affordability floor set (§2.2 Q4).

**Why intraday entries can't just "read the daily signal more often":** the entry rule is defined on the daily *close*; an in-progress price that satisfies it at 11am can violate it at 4pm, so acting on it is look-ahead (`completed_bar_guard` raises on a today-dated bar to forbid this). This phase keeps the **settled** qualifying set and only times *entry execution* intraday.

---

## 1. Architecture & invariants (non-negotiable)

1. **Firewall.** New code lives in `src/autotrader_live/` only. `src/autotrader` and the golden fixtures are **never edited**. **Read-only imports from `src/autotrader` are permitted and are the established pattern** (`strategy_trend` imports `autotrader.indicators`; the loop imports `autotrader.calendar_nyse.TradingCalendar`). The firewall forbids *editing* those files, not *consuming* them — do not duplicate `calendar_nyse` into the live tree.
2. **Agent-drives-MCP, package-is-pure.** Python cannot call MCP. The agent fetches scan/quotes/positions, writes raw JSON to `data/live/paper_book/raw/`, and the package normalizes + advances the book.
3. **No-place / no-order-surface invariant.** No broker-mutation tokens anywhere in `src/autotrader_live/`; `tests/live/test_no_place_invariant.py` (globs the package recursively) must stay green. **LIVE-02 makes no order-surface call at all** — not `review_equity_order` either. Spreads come from the quote bid/ask. The `cost_tier` estimate is recorded as comparison-only metadata, never deducted into P&L.
4. **Reuse boundary** (verified against the actual code): `universe.build_universe` (returns `UniverseResult.selected` ranked top-N `Candidate`s, each with its `TrendDecision`; `.candidates` carries dropped names + reason), `strategy_trend.decide`/`TrendDecision` (exposes `close`, `atr14`, `prior_donch_upper`, the signal booleans), `sizing.size` (returns `{shares, notional, capped}`), `exits.initial_catastrophe_stop`/`update_trailing_stop` (monotonic-up via `max`), `cost_tier`, `mcp_live` normalizers + `MarketData`/`StaticMarketData`, `autotrader.calendar_nyse.TradingCalendar`. The offline `Simulator`/`ledger` are **not reused** (irreducibly daily-bar, one-tranche/symbol, T+1 `SettledCashLedger` — verified contract mismatch; nothing reusable being needlessly rebuilt).
5. **Checkpoint = resume point (restated honestly).** `book.json` is the single durable source of truth, written atomically (tmp → `os.replace`) **first** each poll; the `fills.jsonl`/`equity_curve.jsonl` logs are appended **after** the replace succeeds and are reconciled against `book.json` on load by deterministic `fill_id`. Resume = load `book.json` + re-fetch fresh quotes; `advance_poll` is a pure function of `(book, sane_quotes)`, so an uncommitted poll is simply re-derived. A fill that no longer triggers on fresh data is recorded as a **poll-gap**, never silently dropped. `armed[]` and a per-day `filled_today` set live inside `book.json`, and a name is marked consumed the instant its fill commits — so a post-crash re-fetch cannot double-enter.
6. **Never act on bad/missing data.** A name whose quote is missing OR fails the sanity gate (§2.2) skips trigger/stop/ratchet that poll (prior virtual stop stands); it is never force-sold.
7. **Loud death, quiet pause, durable audit.** Auth/token/repeated-fetch/stall → checkpoint + attributed note + `PushNotification` (best-effort) + stop. Nightly market-close → checkpoint + pause. **The durable audit signal is on-disk** (calendar trading days vs `equity_curve.jsonl` rows): a missing row is a detectable gap, never a clean absence. An **external heartbeat** (§5.3) surfaces deaths the dying agent can't report.

---

## 2. Components

### 2.1 `paper_book.py` — book state + persistence (pure)
- **State:** `cash`, `positions: dict[str, PaperPosition]`, `armed: dict[str, ArmedEntry]`, `filled_today: set[str]`, `realized_pnl`, `last_arm_date`, `last_poll_ts`, run metadata (`start_capital`, `start_ts`, `data_source="robinhood_mcp_live"`, `token_issue_ts`).
- **`PaperPosition`** (frozen): `symbol, shares, entry_price, entry_ts, atr_at_entry, current_stop, highest_high_since_entry, ratchet_seq, cost_tier_bps`. `entry_price` IS the per-share cost basis; `cost_tier_bps` is comparison-only metadata. Replicates `paper_monitor.Position.__post_init__` guards verbatim (`shares>0`, `entry_price>0`, `current_stop>0`) **plus** `atr_at_entry>0`. Deliberately a distinct type from the review-only `Position` (it carries cash/cost fields).
- **`ArmedEntry`** (frozen): `symbol, entry_ref, ref_basis ('breakout'|'near_high'), target_notional, atr_at_arm, cost_tier_bps, arm_date`. `target_notional` is frozen at arm time (§2.3).
- **Interface:** `PaperBook.load(dir)` (atomic read + log reconcile by `fill_id`), `.save(dir)` (atomic, floats→10dp, `sort_keys=True, indent=2` — matching `paper_monitor.DayRecord.to_dict`), `.mark_to_market(quotes, ts) -> BookSnapshot` (carries `n_marked_at_cost` = held names with no usable quote this poll), `.apply_entry/.apply_exit/.replace_position`. `fill_id`: entries `f"{sym}:entry:{arm_date}"`, stops `f"{sym}:stop:{entry_date}:{ratchet_seq}"` (date-namespaced via `entry_ts[:10]` so a flat cross-day stop-out can't dedup-collide).
- **Depends on:** stdlib only.

### 2.2 `fill_engine.py` — virtual fills (pure functions)
Uses the **real `mcp_live.Quote` contract**: `last_trade_price: float`, `bid: Optional[float]`, `ask: Optional[float]`, `previous_close`, `state`, `has_traded` (bid/ask are `None` when the raw value was `0.000000`).

- **`quote_is_fillable(quote) -> bool`** (sanity gate): `last_trade_price > 0`; `bid` and `ask` present and not crossed (`bid <= ask`); `|last/previous_close - 1| <= 0.5`; `state` regular/active and `has_traded`. Failure → treat as missing (§1.6), log the rejected quote.
- **`entry_reference(decision: TrendDecision) -> tuple[float, str]`** = `(decision.prior_donch_upper, 'breakout')` if `decision.breakout_55` else `(decision.close, 'near_high')`. *(No Donchian recompute; `prior_donch_upper` is the frozen max of the prior 55 daily **highs** excluding today — the breakout SIGNAL is close-based, the LEVEL is high-based. `decision.close` is the prior settled close.)*
- **`should_trigger_entry(last_trade_price, entry_ref) -> bool`** = `last_trade_price > entry_ref` (**strict `>`**, matching `decide`'s `close_t > prior_donch_upper`).
- **`entry_fill(quote, armed, available_cash, ts, *, slippage_bps) -> Fill | None`** — BUY at `ask * (1 + slippage_bps/1e4)` (fallback `last_trade_price` if `ask is None`); `notional = min(armed.target_notional, available_cash)`; `shares = notional / fill_price`; skip-and-log if `notional < MIN_NOTIONAL ($50)` or `available_cash < MIN_NOTIONAL`.
- **`ratchet(position, last_trade_price, *, k) -> PaperPosition`** — returns a new position with `hh' = max(position.highest_high_since_entry, last_trade_price)`, `new_stop = max(position.current_stop, hh' - k*position.atr_at_entry)` (monotonic-up), and `ratchet_seq` incremented only when the stop actually rises. ATR fixed at entry's settled `atr14`. *(Documented approximation: `hh'` is the **highest sampled (15-min) poll price**, not the true intraday high — locked in the golden.)*
- **`stop_fill(position, quote, *, slippage_bps) -> Fill | None`** — trigger only when `quote.bid is not None and quote.bid <= position.current_stop` (NBBO-bid confirmation avoids a stale-`last_trade` wick). Fill SELL at `quote.bid * (1 - slippage_bps/1e4)`. Because `bid <= current_stop` at trigger, the fill is at/below the stop and **always crosses the sell-side spread** — a gentle touch fills ≈`stop − half_spread`, a gap-through fills at the low bid (loss bounded by sizing, not the stop). If `bid is None` → skip (never force-sell).
- **Cost model (Q1 resolved):** fills cross the **real observed spread on both sides** (buy@ask, sell@bid); Robinhood equity commission is `$0`. `slippage_bps` is a config knob (**default `3`**, ratified the operator 2026-06-23 — models adverse drift in the 15-min gap between the trigger read and the fill). Each fill records `entry_ref`, fill price, `bid/ask/last`, `previous_close`, `spread`, and the `cost_tier` estimate (comparison-only). *(Optional, deferred: a parallel "optimistic" mid-price series for diagnostics — not built in v1; the headline curve already crosses the real spread both ways.)*

### 2.3 Orchestrator — daily arm + per-poll advance
- **`arm_day(book, market_data, signal_date, today)`** (once/trading-day; guarded by `book.last_arm_date`; the driver re-refreshes the forward calendar from fresh SPY history every arm and computes `signal_date` upstream): `build_universe` → top-N not already held → **per name `completed_bar_guard(historicals, signal_date)` (skip on look-ahead/stale/missing — never arm off an unsettled bar)** → compute `entry_reference` + `target_notional` via `sizing.size(equity_morning, ...)` where **`equity_morning = cash + Σ(shares × entry_price)`** (open positions marked **at cost** = `entry_price`, so size doesn't ride unrealized intraday marks — Q2). Freeze `ArmedEntry` into `book.armed`. *(Note: on a $2k book `per_name_cap_frac=0.15` ($300) binds before the f/k risk target for most names — the vol-target is largely cosmetic at this size; recorded honestly in §9.)*
- **`advance_poll(book, quotes, market_data, *, ts, state_dir)`** (entries are taken ONLY when `book.last_arm_date == ts[:10]`; a poll that fires before today's arm — overnight wake/restart — manages open positions only and is flagged `entries_taken=False` in the curve): for each armed name with a fillable quote and `should_trigger_entry` → `entry_fill` → open position + catastrophe stop (`entry_fill_price − m*atr`; skip entry if stop ≤ 0) → mark consumed in `filled_today`. For each open position with a fillable quote → `stop_fill` (exit) else `ratchet`. Then mark-to-market, write `book.json` atomically (FIRST), append `equity_curve`/`fills` rows. Pure in `(book, sane quotes)`; idempotent per §1.5.
- **Depends on:** §2.1, §2.2, `universe`, `sizing`, `exits`.

### 2.4 `session_clock.py` — ET session timing (new, pure)
- **Purpose:** wall-clock timing the date-only `TradingCalendar` cannot provide. `zoneinfo("America/New_York")`; `is_regular_session(now)`, `minutes_to_close(now)`, early-close table (half-days). **Authoritative open/halt signal is the broker quote `state`/`has_traded`** (covers half-days + unscheduled halts for free); the local clock only paces sleeps. Pinned by a half-day fixture test.

### 2.5 `scripts/run_paper_book.py` — per-poll driver
Modes: `arm` (morning), `poll` (intraday), `status`. Reads the agent's raw MCP JSON → normalizes centrally (`mcp_live`) → `arm_day`/`advance_poll` → prints a compact summary.

### 2.6 The agent loop (procedure → RUNBOOK_PAPER_BOOK.md, not code) — see §5.

---

## 3. Data flow (one trading day)
```
morning (once, gated by session_clock + quote.state=open):
  agent: get_accounts (auth) -> refresh SPY calendar -> run_scan(<your-scan-id>) -> tradability
         -> enrich (historicals/fundamentals/quotes/earnings via foreground subagents)
  driver(arm): normalize -> build_universe -> top-N not held -> size off morning realized+cash equity
               -> entry_reference per name -> book.armed[], last_arm_date=today, atomic checkpoint
every ~15 min (RTH, quote.state=open):
  agent: get_equity_quotes(held + armed)
  driver(poll): sanity-gate quotes -> advance_poll:
        armed & last>entry_ref & fillable -> BUY @ ask -> open position + catastrophe stop -> consume
        open positions: bid<=stop & fillable -> SELL @ bid (spread crossed; gap-through honest)
                        else ratchet stop up on sampled high (monotonic)
        mark-to-market -> atomic book.json -> append equity_curve/fills
  agent: ScheduleWakeup(~15m)   # session-bound; see §5
close / token-expiry / death: §5
```

## 4. State on disk (`data/live/paper_book/`, gitignored)
`book.json` (atomic source of truth, incl. the full `fills` list), `fills.jsonl` (derived, reconciled from `book.json` on load), `equity_curve.jsonl` (one row/poll: ts, cash, positions_mv, total_equity, n_positions, realized_pnl_cum, unrealized_pnl, `n_marked_at_cost`, `entries_taken`), `raw/` (agent MCP dumps). **Add to RUNBOOK:** periodic off-machine copy of `book.json` (the record lives only on the operator's laptop).

## 5. Run model — honest mechanics & limits (operational review)
**5.1 Session-bound, not a daemon.** The agent self-paces via `ScheduleWakeup`, which re-invokes the agent **only while the interactive app/session stays open and the machine is awake**. It does **not** survive app close, laptop sleep, or OS quit, and unattended/scheduled runs in their own transcript **cannot reach the broker MCP** (LIVE-01 auth-spike: a scheduled probe FIRED but STALLED — connector absent, permission-prompt hang). So "continuous" means "continuous while the session is alive." Nightly close is a long in-session sleep if the app stays open, otherwise a session boundary (the operator restarts; resume from checkpoint).
**5.2 P0 spike (GATING, before P1–P5).** Prove the `ScheduleWakeup` loop actually re-reaches the Robinhood MCP and clears the tool-permission prompt across at least one poll interval and one nightly boundary. Do not assume it.
**5.3 External heartbeat watchdog.** A tiny scheduled job (`CronCreate`/`scheduled-tasks`, MCP-free — pure file read) checks `equity_curve.jsonl`'s last-row timestamp and pings the operator if it is stale during RTH. This surfaces death-by-hang / death-by-sleep / death-by-expiry **independently of the dying agent** — the only reliable death signal.
**5.4 Hang + expiry handling.** A hung MCP call never returns to hit the retry-or-die branch, so enforce a **loop-level timeout**: if a poll produced no normalized result file within N seconds, the next wake treats the prior poll as a stalled death (gap note + ping). Treat the ~95h token as a **planned stop**: store `token_issue_ts`, refuse to `arm` a new day past `issue+~90h`, and ping "token expiring, re-auth needed" the morning before.
**5.5 Settlement reconciliation.** Instant-cash is honest **because arming is once-daily**: cash freed by an intraday stop-out is never redeployed before the next morning's `arm_day`, which is already ≥ T+1 — so instant-cash and T+1 yield identical `available_cash` at every arm. *If intraday re-arming is ever added, real T+1 must be modeled.* Stated as a deviation-with-rationale, not a silent default.

## 6. Resolved knobs (the operator 2026-06-23)
- **Q-slippage → `slippage_bps = 3`.** Spread-crossing both sides plus a 3 bps adverse-drift haircut; leans conservative on a neg-to-breakeven strategy.
- **Q-ratchet basis → sampled intraday highs.** Ratchet up on the highest 15-min poll price (reactive, matches the trade-through-the-day intent); documented as a sampled approximation of the true intraday high and golden-locked.
- **Q-optimistic series → deferred.** No parallel mid-price curve in v1; the headline curve already crosses the real spread both ways. Revisit only if useful.

## 7. Testing
- **`fill_engine`** (hand-worked): entry trigger boundary (strict `>`); entry fill at ask + share math + affordability/dust skip; **stop fill at bid asserting a non-gap touch realizes strictly less than `current_stop`** (guards the asymmetric-cost regression); gap-through (bid≪stop); ratchet monotonicity + sampled-high update; sanity-gate rejections (last≤0, None/crossed bid-ask, >50% move, non-regular state).
- **`paper_book`:** atomic save/load round-trip; **crash-between-fill-decided-and-checkpoint** + re-arm-after-crash (prove no double-enter); log-vs-book reconcile by `fill_id`; mark-to-market math.
- **`session_clock`:** half-day fixture; open/close boundaries; tz correctness.
- **Golden replay:** scripted (armed set, multi-poll/multi-day quote snapshots) → byte-match `book.json` + `equity_curve.jsonl` (10dp, `sort_keys`) as the regression lock, with a `math.isclose` structural gate alongside (mirror `golden_replay.py`). Include a winner exiting on a ratchet-stop touch asserting a sell-side spread cost.
- **Invariants:** no-place source-scan green; never-force-sell-on-missing/bad-data exercised; look-ahead guard on the arming step.
- **Offline dry-run** against `StaticMarketData` before any live session.

## 8. Build phasing (for the implementation plan)
- **P0** (GATING) auth/wake spike (§5.2).
- **P1** `paper_book.py` + tests. **P2** `fill_engine.py` + `session_clock.py` + hand-worked tests. **P3** orchestrator (`arm_day`/`advance_poll`) + idempotency/checkpoint + golden replay. **P4** `scripts/run_paper_book.py` + `RUNBOOK_PAPER_BOOK.md` (loop procedure, ScheduleWakeup + heartbeat + PushNotification + timeout/expiry wiring). **P5** offline dry-run green → first **supervised** live session → review → decide on continuous run.

## 9. Honest expectation (restate)
Single-name momentum + trailing stop is the most-decayed, highest-variance version of what the backtest already killed cleanly; on a $2k book with real two-sided spreads it is likely neg-to-breakeven after costs. At this size the `per_name_cap_frac` cap binds before the vol-target, so position sizing is closer to equal-weight than risk-parity — recorded so it isn't misread. The value is an honest forward real-market record + operational learning. The equity curve is a track record, not evidence of edge, and every artifact that renders it says so.
