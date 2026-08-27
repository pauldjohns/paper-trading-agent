# paper-trading-agent

A backtest harness and paper-trading loop for a small, fixed library of rules-based equity
strategies, built so that a strategy has to survive honest costs and honest statistics before it
gets anywhere near real money. An LLM orchestrates the loop; it never invents a strategy and never
free-form trades.

Nothing here places an order. The live half is review-only: it prices what it *would* do against a
broker’s real-time quotes and writes it to a paper book.

## The premise

Most retail backtests are wrong in the same handful of ways, and each one turns a losing strategy
into a winner on paper. This repo is mostly the countermeasures:

- **Costs are modelled, not assumed.** No flat 20 bps. Spread is estimated per-bar with the
  Corwin–Schultz high–low estimator, so it widens automatically in volatility; the live side charges
  the actual bid–ask. Short-horizon mean reversion is charged at least 0.45%, because it buys dips
  exactly when spreads are widest. Costs scale in the 2008 and 2020 folds. See `COST_MODEL.md`.
- **No look-ahead.** Signals fire on completed bars, fills happen at the next open, and the
  52-week-high comes from history rather than from a live fundamentals snapshot (which is today’s
  value, and would leak the future into 2009).
- **Gap-through stops fill at the gapped-down open**, never at the stop price. Filling at the stop
  invents downside protection and flips a FAIL to a PASS.
- **T+1 settled cash.** A sale frees buying power the next trading day, not the same day.
- **Multiple-testing correction.** Deflated Sharpe Ratio and Probability of Backtest Overfitting,
  with auto-kill thresholds, because testing twelve variants inflates the winner by luck alone.
- **Walk-forward with forced 2008 / 2020 / 2022 stress folds.** Behaviour in a deeper bear than
  those is labelled extrapolation, not measurement.
- **Survivorship discipline.** Cross-sectional momentum runs on the nine sector SPDRs, not on a
  2026 list of large caps backtested to 2005 – that list is rigged by construction, and only the
  survivorship-clean version is allowed to gate a decision.

## The strategies, and what each is for

| | Strategy | Judged on |
|---|---|---|
| S1 | Trend following | drawdown, explicitly **not** beating the index in a bull market |
| S2 | Cross-sectional momentum across sector ETFs | risk-adjusted return, survivorship-clean |
| S3 | Short-horizon mean reversion | **expected to fail** – it is the null-confirmation test |
| S4 | Trend-gated momentum (S1 ⊕ S2) | the blend, as one coherent object |

S3 earns its place by being the control. Its academic edge is an illiquidity premium that dies after
honest costs in liquid names, so a harness that reports it as profitable has a bug in the cost model.
Tuning S3 until it passes defeats the entire point of the repo.

## Layout

```
src/autotrader/        the offline engine - ingest, indicators, strategies, simulator, ledger,
                       stops, costs, metrics, walk-forward, robustness, stress, placebo, report
src/autotrader_live/   the paper loop - broker adapter, live strategy, book, telemetry
scripts/               run_backtest, run_paper_book, run_robustness, eod_audit, heartbeat_check
tests/                 offline unit and property tests
tests/live/            the paper loop, against captured broker fixtures - no network
automation/prompts/    the arm / poll / heartbeat / end-of-day prompts that drive the loop
docs/                  design specs and implementation plans, phase by phase
```

## Run it

Python 3.10+.

```bash
pip install -e .
python scripts/build_price_cache.py   # pulls history into data/cache/ (broker API)
python scripts/run_backtest.py        # offline from here - no broker, no network
pytest -q
```

The price cache is not in git and is not small. Without it, 798 tests pass and about 110 - the ones
that assert against real bars - fail with `FileNotFoundError` on `data/cache/*.parquet`. That is the
expected state of a fresh clone, in this repo and in the one it came from; build the cache first if
you want a green run.

The offline engine never contacts a broker. The live paper loop needs a broker MCP connection and is
driven from an interactive session – `RUNBOOK_PAPER_BOOK.md` and `RUNBOOK_LIVE_PAPER_RUN.md` walk
through arming it, polling it, and the end-of-day audit. Every account number in this repo
(`123456789`, `987654321`) is a placeholder; the tests assert that account identifiers never reach
stdout, and that assertion is worth keeping.

## What this is not

It is not investment advice, and it is not a system you should point at your money because it has
tests. The honest expected outcome for most rules-based retail strategies is that they do not beat a
broad index net of costs, and a harness that keeps telling you so is working correctly. Treat a
passing backtest as permission to paper-trade, not as a signal.

Data comes from the broker API rather than a paid feed, is price-return (ex-dividend), and reaches
back to 2005. `STRATEGY_TESTING_SPEC.md` §3.1 documents where that limitation bites – mainly the
bond sleeve and any 60/40 comparison.
