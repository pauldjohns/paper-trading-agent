# Resume / kickoff prompt — LIVE-01 (paste into a fresh session after /clear)

> Resuming the LIVE-01 paper-monitor. **Phases 1, 2, and 3 are BUILT and 3-reviewed**
> (overnight 2026-06-22). Orient BEFORE acting:
>
> 1. Read memory `paper-monitor-plan-and-decisions.md` (the RESUME area + the "PHASE 2 + PHASE 3
>    BUILT + 3-REVIEWED" block at the end) and `trailing-stop-goal-and-live-gate.md` in the
>    project notes dir.
> 2. Work in the git worktree:
>    `<worktree>`
>    (branch `claude/live-01-paper-monitor`, off robustness-05). Read `RUNBOOK_LIVE_PAPER_RUN.md`
>    (the supervised live-run procedure) and `IMPLEMENTATION_PLAN_LIVE_01_paper_monitor.md`.
> 3. State of the branch: 13 commits tonight (`9a63968`→HEAD), **737 tests green**, the protected
>    `src/autotrader/` harness was NEVER touched, the golden is byte-identical. Run
>    `.venv/bin/python -m pytest -q` to confirm.
>
> **What's built (offline, tested):** `src/autotrader_live/` = the full forward paper-monitor —
> `indicators_ohlc, strategy_trend, sizing, exits` (Phase 1 canary, T1.0 gate + golden);
> `mcp_live` (MCP-response normalizers + `MarketData`/`StaticMarketData`), `cost_tier` (fail-closed,
> firewall-safe), `universe` (scan→gate→decide→tier→rank), `broker` (review-only `PaperBroker`,
> no-place tripwire), `paper_monitor` (`plan_day` + completed-bar guard + `record_day`/`run_day`).
> Canonical Legend scan `<your-scan-id>` is built live (Volume>1M∧RSI>60∧MarketCap>2B∧Close>10).
>
> **Two morning tasks (in order):**
> 1. **the operator reviews + approves the merge** of `claude/live-01-paper-monitor`. (Branch is local/unpushed
>    unless the operator asked to push.) The 3 overnight reviews are summarized in memory; nothing they found
>    blocks a supervised run — the clear fixes already landed (`417d411`→`e648831`).
> 2. **Run paper-monitor day 1 — SUPERVISED, ~09:45–10:00 ET** by executing `RUNBOOK_LIVE_PAPER_RUN.md`
>    step by step IN THE MAIN LOOP (the agent makes every live-MCP call; unattended/scheduled CANNOT
>    reach the broker). It is **review-only** (`review_equity_order` simulates; account 123456789 is
>    $0; `PaperBroker` blocks every place path). Day 1 has an empty book. Confirm with the operator:
>    `equity=1000` (nominal sizing capital), `top_n`, and that he wants to run today. Present the
>    would-be orders + each `review` alert + the verbatim `market_data_disclosure`. Note: **the MCP
>    data is REAL live pricing** (verified 2026-06-23 — AMD/Micron match the live market to the cent) —
>    but selected names mean the pipeline screened correctly, NOT "buy these" (the strategy's honest
>    expectation is neg-to-breakeven). Any fault → `record_skip` (a missed day must never look clean).
>
> **Funded-phase carry-forwards (documented in memory, do NOT block wk1):** aggregate/portfolio
> exposure cap; a `LiveConfig` (params' defaults are declared twice → drift risk); make `reconciled`
> a real signal; collapse the sizing dict into the decision-record dataclass; confirm earnings
> blackout = calendar vs trading days; a Position-assembly helper. **wk1-green ≠ risk rails validated.**
>
> Conventions: live-MCP + git stay in the MAIN loop; bulk historicals fetch via FOREGROUND subagents
> writing raw JSON, controller normalizes centrally; NEVER call place/cancel; firewall = never edit
> `src/autotrader/` or the cost-floor registry.
