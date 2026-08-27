# Implementation Plan — LIVE-01: Single-Name Paper-Monitor

_Draft 2026-06-22, **rev 2** after three independent reviews (goal-achievement, auth/scheduler/ops, architecture/lock-in — all "ship-with-changes"). This is the FIRST plan of a NEW "live track," separate from the offline backtest harness (Plans 01–05)._

---

## 0. Frame & ratified decisions

**Goal of LIVE-01:** stand up a review-only paper-monitor that runs the single-name trend/trailing-stop strategy forward on live MCP data, **places no real orders** (`review_equity_order` only), and proves **process integrity** — correct signals on completed bars, faithful would-be orders, working universe discovery, stable operation. Not P&L, not edge, and (see §6) **not** validation of the risk rails.

**Ratified by the operator (2026-06-22):**
- **Spec §5 consciously SUSPENDED** for this track. Single-name selection is un-backtestable (survivorship + forward-selection + today-only fundamentals), so "only §4-passers reach paper" cannot apply. Forward/paper-only by necessity; **risk management is the load-bearing part, not an entry edge.** Authorization recorded as a dated decision-log line in PROJECT_CONTEXT (T0.4), not just inline prose.
- **Paper at $0** — `review_equity_order` verified working unfunded (returns live quote + constant uninformative `EQUITY_NOT_ENOUGH_BP`). Real GFV/settlement alerts need funding.
- **Universe discovery first-class** and now solved server-side: Legend enabled; verified scan `volume>1M AND RSI(14)>60` → 398 market-wide matches with price/mktcap/volume/relvol/RSI attached.
- **Scheduler / unattended autonomy in scope** — gated behind the auth spike (Phase 0). **Note (review): a supervised fallback does NOT satisfy the autonomy goal; true autonomy may require building a standalone OAuth client, which is real engineering, not a config swap.**

---

## 1. Strategy under test (concise contract)

Long-only single names on a cash account, $0 in paper.
- **Entry signal** (on the last COMPLETED/SETTLED daily bar): `close > SMA200` AND `trailing-252d price return > 0` AND (`near 252-day high` OR `fresh 55-day-high breakout`). Client-side from split-adjusted daily bars.
- **Sizing:** notional = `(f · equity) / (k · ATR14) · price`, hard per-name cap.
- **Exit / risk:** (a) resting GTC `stop_market` catastrophe floor placed right after entry; (b) **daily-close** chandelier ratchet — recompute `max(high since entry) − k·ATR`; if higher, cancel + re-place the stop (monotonic-up only). Gap-through fills at the open below the stop → **sizing bounds loss, not the stop.**
- **Overlays:** earnings-avoidance; book-level loss-halt.
- **Cadence:** decide on the last settled session; would-be order targets the next open; run executes during regular hours (~09:45–10:00 ET). **This is a conscious deviation from spec §5's "compute on the official 16:00 close" — defensible because the signal is on the completed prior bar, not a live morning quote — and is flagged so it isn't later misread as a look-ahead/timing violation.**

---

## 2. Architecture & invariants (non-negotiable)

1. **Firewall.** New top-level package `src/autotrader_live/` importing `autotrader` as a library. `src/autotrader` stays offline-only; the Task-8 golden simulator/stops/ledger and the `STRATEGY_COST_FLOORS` registry are **never edited**.
2. **Reuse boundary** (confirmed contract-honest by review). Reuse ONLY: close-series indicators (`sma`, `rolling_high`, `nearness_to_high`, `trailing_return`), `calendar_nyse.TradingCalendar`, `datastore`/`ingest`, `costs.corwin_schultz_spread`/`average_cs_spread`/`roundtrip_cost_for_strategy`, `datacheck.verify_series`, config fee constants. Do **not** import-mutate `Simulator`/`stops`/`ledger`/`engine`/`strategies`. The new OHLC indicators go in a **new module `autotrader_live/indicators_ohlc.py`**, NOT appended to the protected close-series `indicators.py`.
3. **Reconciliation is a DETERMINISM / regression check, NOT a signal-correctness check.** The offline oracle (T1.5) is a frozen replay of the *same* decision code the live loop runs, so a byte-match proves only that the live data pipeline fed uncorrupted bars. **Signal correctness is established separately in T1.0** (independent hand-worked numbers + cross-check against the already-trusted close-series indicators + a mutation test). The golden is frozen only AFTER T1.0 passes.
4. **Broker seam — and it is NOT a zero-divergence one-swap.** A `Broker` protocol covering the full lifecycle: `review`, `place_entry`, `place_stop`, `cancel`, `ratchet_replace`. `PaperBroker` (review-only) for this plan; `LiveBroker` stubbed. Documented divergences the swap introduces: fractional/dollar orders are market+RTH only (share-rounding differs from review); the catastrophe stop is a *separate* attached order and the ratchet is cancel+replace (an emulated bracket — no native OCO); funded alert shapes (GFV/PDT/settlement) differ from the $0 `EQUITY_NOT_ENOUGH_BP`. Treat the alert field as **opaque passthrough**; alert-schema is an unresolved contract for the funded phase.
5. **No-place invariant — enforced, not asserted.** The offline package's no-place is enforced now by a source-scan test (`tests/live/test_no_place_invariant.py`) that asserts none of the MCP broker-mutation tokens (`place_equity_order`, `place_option_order`, `cancel_equity_order`, `cancel_option_order`, `review_equity_order`) appear in any `*.py` file under `src/autotrader_live/`. The runtime monkeypatch tripwire — patching the order-placement MCP tools to fail-on-call across the `PaperBroker` import graph — lands with the broker seam at T2.3. The live cost tier is owned by `autotrader_live/cost_tier.py` (fail-closed `cost_tier_for` function) and does NOT call `costs.roundtrip_cost_for_strategy` or touch the protected `STRATEGY_COST_FLOORS` registry — this is the firewall-safe live cost-tier model.
6. **Idempotency.** Unit of work = `signal_date`. The per-date decision record is written **atomically (temp + `os.replace`, flat JSON) once, after all names are processed** (partial progress is discarded and recomputed on re-run — harmless for review-only). The loop no-ops if today's record is COMPLETE. **`ref_id` provides NO broker-side idempotency in the review-only week-1 path** (`review_equity_order` has no `ref_id` parameter); week-1 crash-safety rests entirely on the atomic state record. `ref_id` becomes load-bearing only for `LiveBroker.place`, where it must be `hash(signal_date, symbol, side, intent, stop_price/ratchet_seq)` so a cancel+replace ratchet is not deduped against the prior stop.
7. **No look-ahead / completed-bar guard — dedicated, not `verify_series`.** `verify_series` is a cache validator with the wrong contract. Write a dedicated live-bar guard: `signal_date` = last SETTLED session from the calendar; **detect-and-drop any partial current-session row** that `get_equity_historicals` may return during RTH; assert no bar with `date == today` is consumed for signals; assert the bar used has `date == signal_date` and is full. Fixture tests both WITH and WITHOUT a partial today-row assert the same `signal_date`. Pin `adjustment_type='split'` (price-return) everywhere; assert non-negative / monotonically dated.
8. **Loud failure + missed-fire policy.** Any auth/token/fetch fault aborts visibly and records the `signal_date` as a logged, attributed skip. Because delayed/missed fires are the COMMON case (box asleep, app closed, token expired), define a retry window and catch-up rule rather than treating a late fire as an exception. **A missed day must never look like a clean run.**

---

## 3. Universe pipeline (server-side, post-Legend)

1. `run_scan` a saved Legend scan with liquidity + momentum floors (`price>$10`, volume floor, `market_cap` floor, optional RSI band). **the operator, 2026-06-22: I build this canonical scan** — refine the existing scan `<your-scan-id>` by adding the price + market-cap floors, once the exact `FILTER_TYPE_PRICE`/`FILTER_TYPE_MARKET_CAP` enums are confirmed from the scanner-filter-specs (the `FILTER_TYPE_VOLUME`/`RSI` enums are already verified). Quirk: `FILTER_TYPE_VOLUME` needs `length≥1`. Cap: ≤200 rows returned even when `total_items` is larger → tighten filters so `total_items ≤ 200`, or page.
2. Per name: `get_equity_tradability` gate (tradeable + fractional + not halted) — drops synthetic/odd instruments (e.g. the "SPCX/SpaceX" $2.4T row).
3. Enrich survivors with split-adjusted daily `get_equity_historicals` (≤10/call) for the client-side trend signals.
4. Assign a single-name **cost tier** from scan fundamentals (mktcap/avgvol → `TIER_MEGA_CAP` 0.20% vs `TIER_OTHER` 0.50%) via `autotrader_live.cost_tier.cost_tier_for`. **Pin the resolved tier into the per-date decision record** (fundamentals are today-only → otherwise the frozen oracle is non-reproducible). This is the first exercise of the single-name cost path — it needs its own golden coverage. NOTE: the live track owns its own fail-closed `cost_tier.py` and does NOT touch the protected `autotrader.costs` registry or `roundtrip_cost_for_strategy` — this is a deliberate firewall (see §2.5).

---

## 4. Phases & tasks

### Phase 0 — Auth spike + setup (GATING)
- **T0.1 Auth spike — split, because the two topologies are different spikes.**
  - *Host decided AFTER the spike (the operator, 2026-06-22):* run both sub-spikes against a candidate always-on context, then choose the host (Modal per the heavy-batch memory, or a local `launchd`/`cron` box) from what actually authenticates. **The in-app Claude-Code scheduled-tasks primitive is REJECTED** regardless (fires only while the app is open, runs context-free).
  - **T0.1a (Option A):** does a scheduled/headless invocation carry the interactive Robinhood MCP connection? Probe read-only `get_accounts` over several days; log auth success + token age.
  - **T0.1b (Option B):** can a standalone client obtain + refresh a Robinhood OAuth token WITHOUT the MCP, surviving the ~95h expiry? **May be infeasible if the refresh token is not extractable from the MCP-mediated session.**
  - *Decision rule:* A passes → use A; A fails → attempt B; both fail → escalate to the operator with the supervised interim. **Phases 1–3 (canary + loop) are independent of the scheduler and ship + run supervised regardless of T0.1.**
- **T0.2 Branch base (the operator, 2026-06-22: push the stack first).** `engine-04`/`robustness-05` are LOCAL-ONLY (origin has only `foundation-01`/`indicators-02`/`strategies-03`/`main`). **Push the `indicators-02 → strategies-03 → engine-04 → robustness-05` stack to origin as an integration ref, then branch the live track off `robustness-05`** so it carries the full assembled lib (close-series indicators, calendar, costs, datastore, `datacheck`). Pushing this stack is independently valuable — it currently exists only on this laptop. (Lighter alt considered and declined: branch off pushed `indicators-02` + cherry-pick `datacheck.py`.) **First verify the real local branch/push state** (memory notes ambiguity about whether `main` locally is `foundation-01` or carries the robustness merge). Run the PROJECT_CONTEXT.md parallel-work scan (`gh pr list --state open`; `git branch -a | grep claude/`) as a **hard gate**; serialize if any base is mid-review. (Verified: the only other claude branch doesn't touch the protected stack.)
- **T0.3** `src/autotrader_live/` skeleton + test scaffold.
- **T0.4 Docs + governance.** Record the operator's §5-suspension as a **dated, attributed decision-log line** in PROJECT_CONTEXT (auditable authorization, one-ticket-one-commit). Fix the stale `adjustment_type='all'` "verified working" claims — note the scope: the three "verified working" carriers (PROJECT_CONTEXT L28, MCP_CAPABILITIES L65, SPEC §3.1) are corrected now; the **deeper dividend/total-return basis contradiction** in the offline spec (PROJECT_CONTEXT L34; SPEC §3.10/§6/§7/AppB L118/L147/L152/L170 + the total-return strategy definitions) is logged as a **separate ticket** with a one-line note that the offline harness realized a **price-return** basis (per the `auto-trader-price-return-basis` memory).

### Phase 1 — Canary (offline, no MCP)
- **T1.0 (NEW — independent signal-correctness gate, runs before any freeze).** (a) Hand-compute the full decision (signal booleans, size, initial stop, one chandelier step) for ≥2 cached names and assert `strategy_trend`+`sizing`+`exits` match those numbers. (b) Cross-check the close-only sub-signals (`close>SMA200`, 252-high nearness, trailing-252 return) against the already-trusted `indicators.{sma,rolling_high,nearness_to_high,trailing_return}` — divergence flags a port bug. (c) Mutation test: perturb one indicator, assert the hand-worked oracle FAILS (proves the check has teeth).
- **Phase-1 precondition (NEW):** the canary OHLCV cache does **not exist** in this worktree (`data/cache` is empty; manifest records only `SPY,day,all`). (Re)build it on `adjustment='split'` (the live basis) and assert it. State the cache key (symbol, interval, adjustment) explicitly. Construct the live `TradingCalendar` with `adjustment='split'` **explicitly** — `calendar_nyse.from_datastore` already defaults to `'split'` (`calendar_nyse.py:13`), so this is good practice for clarity rather than a fix for a broken default.
- **T1.1** `indicators_ohlc.py` — `atr`/`true_range`/`donchian`/`ema` (OHLC-consuming), unit-tested against hand-worked values. New module, not an edit to `indicators.py`.
- **T1.2** `strategy_trend.py` — a **today-decision** function (one decision row), NOT a target-weight frame.
- **T1.3** `sizing.py` — `(f·E)/(k·ATR)` + per-name cap.
- **T1.4** `exits.py` — initial catastrophe stop + chandelier level; **monotonic-up guard** (refuse downward); daily cadence.
- **T1.5** Freeze the deterministic offline replay golden (under `autotrader_live`, NOT shadowing `tests/fixtures/golden_engine_sequence.json`) — **only after T1.0 passes.**

### Phase 2 — Live adapters
- **T2.1** `mcp_live.py` — thin read wrappers (scan, historicals, quotes, fundamentals, earnings, tradability, portfolio/positions), ≤10-symbol batching, `adjustment='split'` pinned + asserted, **forward calendar refresh** (pull fresh SPY history each run so the calendar carries future trading days — the cached calendar ends at build date), trivial try/except+sleep (no backoff framework).
- **T2.2** `universe.py` — the §3 pipeline; saved-scan id is config; pins the cost tier into the decision record.
- **T2.3** `Broker` protocol + `PaperBroker` (full lifecycle surface per §2.4; review-only; place tripwire-blocked per §2.5). `LiveBroker` stub.

### Phase 3 — The daily loop (`run_paper_monitor.py`)
- **T3.1** Calendar + dedicated completed-bar guard (§2.7) → resolve `signal_date`; idempotent no-op if its record is COMPLETE.
- **T3.2** Pull universe + data; **partial-batch rule**: a failed candidate is excluded + logged; a **held name that fails to fetch HALTS only its ratchet — the resting GTC catastrophe stop at the exchange PERSISTS, the position is NEVER silently exited.** Test this invariant.
- **T3.3** Compute signals on the settled bar; build would-be entries/exits/ratchets (exact envelope incl. the resting GTC stop and the daily-close ratchet level — both COMPUTED-and-logged in wk1).
- **T3.4** `broker.review(...)` each would-be order during RTH; capture quote/spread/alert (label the $0 alert constant/uninformative).
- **T3.5** **In-run determinism check:** re-run `decide()` on the SAME fetched live bars that the loop used and assert byte-equality against the loop's recorded decisions. This proves (a) the deployed code path is deterministic and (b) the data pipeline did not corrupt the bars between fetch and decision. **This is NOT a comparison against the 15-ETF golden (T1.5):** the ETF golden is a static regression lock over a fixed past snapshot and shares no symbols or dates with the live single-name universe — it cannot validate live orders. The ETF golden remains a separate, independent static regression lock; T3.5 is a live-loop internal replay check.
- **T3.6** Earnings-blackout + loss-halt **detection logged** (would-have-blocked), not enforced; tested adversarially per §6.
- **T3.7** Atomic state commit → telemetry row. No virtual fills, no virtual P&L.

### Phase 4 — Scheduler (only after T0.1 resolves a working topology)
- **T4.1** Wrap the loop in the resolved topology; daily ~09:45–10:00 ET.
- **T4.2** Token-freshness preflight that **attempts refresh per the chosen topology, then HARD-FAILS loudly** if refresh fails (call out the Friday-expiry→Monday-fail case); records the skipped day.

### Phase 5 — Run
- **T5.1** Run forward (weeks→month for any behavioral read; week 1 is the plumbing checkpoint). If T0.1 is unresolved, run SUPERVISED as an **interim only** (not the autonomy goal), with the operator's OK.

---

## 5. Config defaults (dials)
**Pinned `account_number = 123456789`** (`review_equity_order` requires it; the schema forbids defaulting from `get_accounts`). Saved-scan id; floors `price>$10 / volume>1M / mktcap>$2B`; `N`=8–12; `f`=0.5–1%; `k`=2.5–3×ATR; catastrophe stop 1.5–2× the ATR distance; loss-halt 2–3%/day, 10–15% total; rebalance weekly; ratchet cadence = daily; missed-fire retry window.

## 6. Success criteria — split into (A) cleared this window vs (B) explicitly NOT cleared
**(A) Cleared by wk1 (genuinely testable now):** loop runs and **records every trading day as either a clean run or a logged, attributed skip**; signals only on settled prior bars (partial-row-drop tested both ways); would-be orders byte-match the offline oracle (**determinism**); the **independent T1.0 signal-correctness gate** passed before freeze; the universe scan returns a sane, tradability-gated set; the resting-GTC-stop PRICE and chandelier ratchet LEVEL the loop would place match the oracle, and the **monotonic-up refusal is exercised on live-shaped data**; review calls succeed (the $0 alert captured with its uninformative caveat); state survives a mid-loop crash + re-run.
**Adversarial rows (detection-only is fine, but must have teeth):** earnings-blackout suppresses a confirmed-report name AND does NOT suppress a matched no-report control; tradability gate drops an injected synthetic/halted symbol AND passes a known-good one; loss-halt fires on a SYNTHETIC fed book breaching the threshold (a $0 book can never trigger it).
**(B) Explicitly NOT cleared — carried to the funded phase:** ENFORCED earnings-blackout and loss-halt; real GFV/settlement/PDT alert telemetry; intraday-ratchet behavior; real-gap stop-fill; the single-name cost path beyond its first golden. **wk1-green does NOT mean the risk rails are validated.**

## 7. Risks & open questions
- **Auth (T0.1) is the top risk** and **autonomy is a project risk, not a config toggle** — Option B may require a standalone OAuth client (real work); both options may fail.
- **Branch base** is local-only — must resolve push state before branching.
- **Forward calendar** must be refreshed each run or the completed-bar guard aborts past the cache's last date.
- **Gold dependency / scan row cap / un-vetted instruments** — mitigations in §3.
- **§5 timing + basis deviations** flagged consciously; the offline spec's total-return basis contradiction is a separate ticket.
- **Alert schema** unresolved until funded (opaque passthrough now).

## 8. Decisions — RESOLVED (the operator, 2026-06-22)
1. **Scheduler host:** decide AFTER the auth spike — spike both topologies, then pick the host from what authenticates (T0.1).
2. **Branch base:** push the `indicators-02→…→robustness-05` stack to origin first, then branch the live track off `robustness-05` (T0.2).
3. **Saved scan:** I build the canonical floored scan (refining `<your-scan-id>`) (§3).
4. **Paper-window length:** week-1 plumbing checkpoint first, then review and decide whether to extend (Phase 5).
