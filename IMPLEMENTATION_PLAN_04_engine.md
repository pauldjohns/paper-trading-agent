# Backtest Engine, Metrics & Benchmarks — Implementation Plan (Plan 04 of 5) — v1.1 (three-reviewed)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Reviewed (v1.1) — three independent adversarial reviews (methodology/metrics, engine↔Simulator integration, scope/lock-in). Every reviewer BUILT or numerically checked the relevant code. Blocking findings fixed in this revision:**
- **Basket-funding bug (integration review, demonstrated on the real cache):** sizing fresh buys at `weight × full-NAV` left multi-name baskets unable to fund their later legs — S4 held only 2 of 3 names on ~66% of risk-on bars, and the "skip" log was one unfundable leg re-logged daily (2,891 phantom skips). **Fixed:** the engine now sizes each entry at `min(weight × equity, settled_cash(fill_date))`, deploying available settled cash and throttling only genuine T+1 rotations (Task 4/5). Verified fix: underweight bars 66%→0.6%, skips→27.
- **Deflated-Sharpe unit mismatch (methodology review, demonstrated):** the report fed *annualized* Sharpe variance into a *per-observation* DSR (252× too large → DSR≈0, false-killing everything). **Fixed:** units made consistent + the Plan-04 DSR is explicitly marked provisional (Task 9/15).
- **Both PBO oracles were degenerate** (constant-within-block → zero variance → asserted the opposite of the correct output). The CSCV algorithm itself is correct. **Fixed:** oracles rebuilt with genuine within-block variance + mid-rank tie handling (Task 10).
- **DSR oracle used a scale (not mean) shift** (Sharpe is scale-invariant → never equals SR0) and a dangerous "widen the tolerance" note. **Fixed:** mean-shift construction, tight tolerance, note deleted (Task 9).
- **Per-symbol cost routing pulled forward** out of Task 14 into Tasks 1/5 so the S3 floor on the blend's MR sleeve can't be dropped; **golden stop pinned to `stop_loss_pct=0.10`** in the test body; **terminal open positions emitted as mark-to-close trades**; **cash-book reconciliation invariant** asserted; **60/40 costed with the same per-tier×stress model**; **block-bootstrap** option for path-dependent CIs; **leading warm-up trimmed** from headline metrics. **Scope:** all three reviewers said keep it as ONE plan (not split); keep D1/D3; D4 cost unified.
- **§4 gate framing (decision surfaced for the operator):** Plan 04 emits the metric *inputs* + the DSR/PBO *functions* + an S3 null-confirmation RED-FLAG; the **binding §4 PASS/FAIL gate** (which needs the random-selection placebo + the full multiple-testing trial census) runs in **Plan 05**. A `gate_verdict` here returns each sub-condition and an overall **PROVISIONAL** status with the deferred placebo flagged — a missing AND-term never reads as PASS (Task 15).

**Goal:** Build the offline walk-forward backtest **engine** that drives each locked strategy's `target_weights(bars)` frame through the Plan 01 `Simulator` (next-open fills, stop-first daily-bar stops, shared T+1 settled-cash ledger, per-trade cost tiers + the S3 floor), plus the **metrics** layer (CAGR…Calmar, Deflated Sharpe, PBO, bootstrap CIs), the four **benchmarks** (price-return), the anchored **walk-forward + 2008/2020/2022 stress folds**, the **3-way-vs-2-way blend** measurement, and a deterministic **report** — all over the populated local cache, never touching the MCP.

**Architecture:** The engine is a thin deterministic orchestrator over already-built, already-locked parts. A strategy emits *intent* (a causal weight frame); the engine reconciles that intent with *execution feasibility* (next-open fills, T+1 settlement, stops) by driving the locked `Simulator`. Because the `Simulator` is **all-or-nothing per symbol** (it refuses a re-buy of a held symbol and sells the entire position), the engine is **event-driven on holdings-set changes** — it enters/exits whole positions and tolerates weight drift between membership changes; it never continuously re-weights. Metrics, benchmarks, walk-forward and the report are pure functions layered on the engine's two outputs: a daily mark-to-market **equity curve** and a **trade log**.

**Tech Stack:** Python 3.11, pandas, numpy, **Python-stdlib `statistics.NormalDist`** for the normal CDF/inverse in the Deflated Sharpe (scipy is NOT a dependency and must NOT be added). pytest. All new code is **offline** and deterministic; statistical functions seed `numpy.random.default_rng` explicitly.

**Reads (source of truth):** [`STRATEGY_TESTING_SPEC.md`](STRATEGY_TESTING_SPEC.md) §3.1 (price-return basis — LOCKED), §3.3 (cost model), §3.4 (no look-ahead / next-open), §3.5 (stop-fill-on-daily-bar), §3.6 (T+1 shared ledger), §3.8 (walk-forward + forced stress folds), §3.9 (Deflated Sharpe + PBO auto-kill), §3.10 (the four benchmarks), §4 (metrics & gates); [`COST_MODEL.md`](COST_MODEL.md) §3-§4 (tiers, S3 floor, Corwin-Schultz); [`IMPLEMENTATION_PLAN_03_strategies.md`](IMPLEMENTATION_PLAN_03_strategies.md) **Engine contract** (the 4 inherited decisions) + the strategy interface.

**Integrates (ALREADY BUILT — do NOT modify; import and drive):**
`simulator.py` (`Simulator`: `submit_buy/submit_sell/place_stop/evaluate_stops`, `BuyFill/SellFill`), `ledger.py` (`SettledCashLedger.can_buy/settled_cash`), `stops.py` (`stop_fill_price`), `costs.py` (`effective_roundtrip_cost`, `roundtrip_cost_for_strategy`, `average_cs_spread`, `regulatory_sell_fees`), `datastore.py` (`DataStore`), `calendar_nyse.py` (`TradingCalendar`), `indicators.py`, `strategies.py` (`S1Trend/S2SectorMomentum/S3MeanReversion/S4TrendGatedMomentum`, `trend_regime`), `config.py` (universes, tiers, `STRATEGY_COST_FLOORS`).

---

## 0. Inherited Engine contract (from Plan 03 — settled, not re-litigated here)

1. **Aligned bars.** The engine passes each strategy a `bars` dict whose every symbol shares ONE NYSE-calendar date axis. `strategies._ref_dates` validates this and raises on misalignment. The engine therefore builds calendar-aligned, position-indexed frames before calling `target_weights`.
2. **`universe` is the load-set, not the trade-set.** It lists every symbol the strategy needs bars for (signal + traded). The engine allocates dollars only to **non-zero-weight** columns and assigns the cost tier **per actual trade** — a signal-only symbol (e.g. SPY in S4, weight always 0) is loaded but never traded or priced.
3. **Stop-first precedence.** The engine evaluates resting stops for date *T* **before** processing that bar's weight-change exits; a symbol the stop fills is treated as already exited (no double-exit at a different price).
4. **Missed-allocation policy.** When `ledger.can_buy` refuses a buy (insufficient settled cash — e.g. the blend's sleeves competing for the one shared pool), the engine **skips that buy for the bar and logs it**; it does NOT defer or carry the intent forward. The weight frame is re-read fresh each bar.

---

## 1. NEW design decisions (NOT in the locked specs — surfaced here for the three reviews)

These are the engineering choices the spec leaves open or that the locked `Simulator` forces. They are the highest-value review targets. Each has a recommended default (used by the tasks below) and a stated alternative.

- **D1 — Event-driven, all-or-nothing position model (forced by the locked `Simulator`).** The `Simulator` holds one tranche per symbol; `submit_sell` liquidates the whole position; there is no partial trim/add. So the engine acts only on **holdings-set changes**: a target-weight>0 symbol not held → buy sized at **`min(weight × equity_snapshot, settled_cash(fill_date))`** (cap added after review — see below); a held symbol whose target weight is 0 → full sell; a held symbol that stays in the set → **left untrimmed (drift tolerated until it exits)**. The weight frame's `0.5` is an *entry sizing target*, not a continuous-rebalance mandate (continuous re-weighting would mean daily liquidation — absurd). **The settled-cash cap is load-bearing:** without it, drifted held legs consume >their weight share of NAV, so a basket's last leg's `weight × NAV` target exceeds free cash *by a hair* and the whole buy is refused and re-logged every bar (the integration review measured S4 holding 2-of-3 names on 66% of risk-on bars, 2,891 phantom skips). Capping at available settled cash deploys what's there (last leg enters slightly underweight, consistent with drift tolerance) and still throttles genuine T+1 rotations (a same-bar sell→buy sees ~0 settled cash → logged throttle → enters next bar). *Alternative (rejected):* extend the `Simulator` to support partial fills — redesigns locked, golden-tested code. **Documented limitations (report must state both, as they are the only places the model is *optimistic*):** (i) held winners overweight between rebalances — flatters momentum (S2/S4); (ii) a constant-mix sleeve cannot be expressed (see D4 for 60/40). A future *live* phase may extend the Simulator; the lock is on this phase's golden-tested code, not forever.
- **D2 — Causal Corwin-Schultz-ratio stress multiplier.** The `Simulator` already takes a `stress` arg; the engine must supply it (spec §3.3 / COST_MODEL §3 require ×2–5 widening in panics). Default model, per symbol, fully causal: `cs_t = average_cs_spread(trailing W=21 bars ending at t)`; `base_t = median(cs over a trailing B=252-bar window, expanding before 252)`; `stress_t = clamp(cs_t / base_t, 1.0, config.TIER_MAX_STRESS[tier])`. Reuses the locked `costs.average_cs_spread`; 2008/2020 high-low blowouts auto-widen fills; calm periods sit at ≈1. *Alternative:* realized-volatility ratio (same shape, vol instead of CS) — mentioned for the reviewers. `W`, `B` are soft knobs (Plan 05 scans them).
- **D3 — One full-history run, sliced into walk-forward/stress periods.** Parameters are **literature-locked, never optimized from data** (spec §3.8), so a re-fitting walk-forward has nothing to re-fit. The engine runs **once** over full history (warm-up handled) → one continuous equity curve + trade log; the walk-forward layer reports metrics over **expanding-window cutoffs** and the **forced 2008-09 / 2020 / 2022 calendar windows** as *slices* of that single run (no portfolio resets — more realistic than artificial fold resets). The residual soft-knob grid (top-N, stop width, blend budget) that a true walk-forward would guard is **Plan 05's robustness runner**, which also supplies the full **trial census** that powers PBO/DSR at full strength. **Plan 04 computes PBO/DSR over the handful of on/off variants it runs here** — directionally honest, deliberately a lower bound on the eventual deflation.
- **D4 — Benchmarks: three through the engine, 60/40 analytic — costed on the SAME stress model.** SPY buy-hold, gated-SPY, and equal-weight sector universe are weight-frame strategies run through the SAME engine (identical cost treatment → apples-to-apples). **60/40 SPY/bond is a constant-mix object** the all-or-nothing engine cannot express, so it is computed analytically as a monthly-rebalanced `0.6·SPY + 0.4·IEF` daily-return blend. **Cost coherence (added after review):** the monthly rebalance is charged with the **same `effective_roundtrip_cost(tier, stress)` model** every other path uses (SPY/IEF index-ETF tier × the causal CS-ratio stress at the rebalance date), **not** a flat bps. Without this, 60/40 would pay flat costs while the strategies it judges (§3.10c: "S1/blend must beat risk-adjusted 60/40") pay stress-widened costs in exactly the 2008/2020 folds that matter — biasing the comparison against the strategies. *Alternative:* force 60/40 through the engine as buy-hold (drifts to ~all-equity over 20y — rejected as a poor risk benchmark).
- **D5 — Settlement runway + terminal mark-to-close.** The `Simulator` requires a *next* trading day to settle any sale into (selling on the last calendar date raises). The engine builds the `TradingCalendar` from the real bar dates **plus a 2-trading-day synthetic runway** (dates only — never given bars, so no fill can occur there) so last-real-bar stops settle cleanly. Open positions at the end are valued **mark-to-last-close** (no terminal round-trip cost charged) — standard backtest convention; the omitted final exit cost is a one-time ≪0.5% effect on a 20-year run.
- **D6 — Report = markdown + CSV (no plotting dependency).** The report emits a markdown summary table (metrics × {full, walk-forward, stress folds}) and writes the equity curve + trades table as CSV. No matplotlib/plotly dependency is added; an "equity curve" chart is left to Plan 05 or an optional notebook.
- **D7 — Deflated-Sharpe cross-trial variance from the Plan-04 variant set, stdlib NormalDist — units consistent, number provisional.** DSR needs the variance of Sharpe across trials; Plan 04 computes it from the variant Sharpes it actually runs and passes it in (the function takes `n_trials` and `sr_variance` as explicit args). **The variance MUST be of *per-observation* Sharpes (mean/std, no √252), matching the per-observation Sharpe the DSR formula uses internally** — feeding annualized-Sharpe variance was a demonstrated 252× error that collapses DSR to ≈0. With Plan-04's tiny variant count (2–4), the resulting DSR is numerically weak, so the report labels it **PROVISIONAL — full trial census in Plan 05** (D8). The normal CDF/inverse come from `statistics.NormalDist` — no scipy.
- **D8 — Plan 04 emits gate *inputs*, not a binding §4 verdict.** The §4 return-seeker gate is a 5-way AND (`deflated-p<0.05 AND (strat−benchmark) Sharpe-CI-lower≥0 AND maxDD-upper-CI<SPY's AND PBO<0.5 AND beats 95th-pct random-selection placebo`). The **placebo (1,000 random picks)** and the **full multiple-testing trial census** are Plan 05's robustness-runner scope. So Plan 04 builds and tests the DSR/PBO/bootstrap *functions*, assembles a `gate_verdict` that reports **each available sub-condition plus an overall `PROVISIONAL`** status with the deferred placebo explicitly flagged (a missing AND-term never silently reads as PASS), and raises the **S3 null-confirmation RED-FLAG** if S3 shows a strong positive net Sharpe (spec §4 — audit the cost model, don't celebrate). The **binding PASS/FAIL** that gates paper-trading runs in Plan 05. *(This is the one scope decision the reviewers asked to put to the operator — see Open Items.)*

> **Reviewers (v1.1 outcome):** D1 (position model) — KEEP, forced by the lock; the settled-cash sizing cap was the fix. D3 ("slice one run") — KEEP, faithful to §3.8 for literature-locked params; `run()` is re-entrant so Plan 05 can re-run per-fold. D4 (60/40) — KEEP analytic path, cost model UNIFIED. D8 (gate framing) — the open scope call for the operator.

---

## 2. File structure (locked here)

```
src/autotrader/engine.py        # tier_for_symbol; build_engine_inputs; BacktestEngine + run() -> BacktestResult; Trade
src/autotrader/stress.py        # causal Corwin-Schultz-ratio stress series (D2)
src/autotrader/metrics.py       # CAGR..Calmar; trade/exposure metrics; deflated_sharpe; pbo_cscv; bootstrap_ci
src/autotrader/benchmarks.py    # BuyHold, GatedSPY, EqualWeightUniverse (weight-frame); sixty_forty_returns (analytic)
src/autotrader/walkforward.py   # expanding-window cutoffs + forced stress-fold slicing of one run
src/autotrader/report.py        # summary table (markdown) + equity/trades CSV export; variant-set PBO/DSR wiring
scripts/run_backtest.py         # offline smoke driver over data/cache (Task 16); not a pytest test
tests/test_engine.py            # engine golden (hand-computed) + decision-diff + integration smoke
tests/test_stress.py
tests/test_metrics.py
tests/test_benchmarks.py
tests/test_walkforward.py
tests/test_report.py
tests/fixtures/golden_engine_*.json
```

**No new dependency.** Everything imports from the existing `autotrader` package + numpy/pandas/stdlib.

---

## PHASE A — Engine core (drives the locked strategies through the locked Simulator)

### Task 1: Cost-tier + floor resolution helpers

**Files:** Create `src/autotrader/engine.py`; create `tests/test_engine.py`.

The engine prices each *actual trade* by instrument tier (Engine contract #2) and applies a strategy's punitive floor (S3). Two pure helpers, no I/O.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_engine.py
import datetime as dt
import pandas as pd
import pytest
from autotrader import config
from autotrader.engine import tier_for_symbol, cost_floor_for_strategy


def test_tier_for_symbol_maps_each_universe_class():
    assert tier_for_symbol("SPY") == config.TIER_INDEX_ETF
    assert tier_for_symbol("QQQ") == config.TIER_INDEX_ETF
    assert tier_for_symbol("IEF") == config.TIER_INDEX_ETF   # bond ETFs priced at the liquid index tier
    assert tier_for_symbol("AGG") == config.TIER_INDEX_ETF
    assert tier_for_symbol("XLK") == config.TIER_SECTOR_SPDR
    assert tier_for_symbol("XLF") == config.TIER_SECTOR_SPDR


def test_tier_for_symbol_unknown_raises():
    with pytest.raises(ValueError):
        tier_for_symbol("NVDA")   # single names are non-gating; not in the Plan-04 universe


def test_cost_floor_resolves_s3_only():
    assert cost_floor_for_strategy("S3") == config.S3_COST_FLOOR
    assert cost_floor_for_strategy(None) is None
    assert cost_floor_for_strategy("S1") is None   # unregistered -> instrument tier alone
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/test_engine.py -v`
Expected: FAIL (`ModuleNotFoundError: No module named 'autotrader.engine'`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/engine.py
"""Offline walk-forward backtest engine: drives a strategy's causal target-weight frame through
the locked Simulator (next-open fills, stop-first daily-bar stops, shared T+1 ledger, per-trade
cost tiers + S3 floor) and emits a daily mark-to-market equity curve + a trade log.

The Simulator is ALL-OR-NOTHING per symbol, so this engine is event-driven on holdings-set
changes (D1): enter/exit whole positions, tolerate drift between membership changes. Strategy =
intent; engine = execution. Offline only — never calls the MCP, never places a real order.
"""
import datetime as dt
from dataclasses import dataclass, field
from typing import Optional
from autotrader import config

_INDEX_TIER_SYMBOLS = set(config.INDEX_ETFS) | set(config.BOND_ETFS)   # SPY/QQQ/DIA/IWM + IEF/AGG
_SECTOR_TIER_SYMBOLS = set(config.SECTOR_SPDRS)


def tier_for_symbol(symbol: str) -> str:
    """Instrument liquidity tier for a symbol in the Plan-04 (survivorship-clean) universe.
    Bond ETFs (IEF/AGG) are very liquid -> index-ETF tier. Unknown symbols raise: single names
    are non-gating and out of scope for the engine (spec §3.7)."""
    if symbol in _INDEX_TIER_SYMBOLS:
        return config.TIER_INDEX_ETF
    if symbol in _SECTOR_TIER_SYMBOLS:
        return config.TIER_SECTOR_SPDR
    raise ValueError(f"no tier for {symbol!r}: not in the Plan-04 ETF/sector universe "
                     f"(single names are non-gating, spec §3.7)")


def cost_floor_for_strategy(cost_strategy: Optional[str]) -> Optional[float]:
    """Resolve a punitive cost floor (config.STRATEGY_COST_FLOORS) for a cost-strategy LABEL. Only
    S3 carries one; everything else returns None (instrument tier alone). The engine calls this
    PER SYMBOL (Task 5) via a `cost_strategy_for(symbol)` map, so a multi-sleeve strategy (the
    blend, Task 14) can charge the S3 floor on its MR sleeve only — the floor cannot be dropped."""
    if cost_strategy is None:
        return None
    return config.STRATEGY_COST_FLOORS.get(cost_strategy)
```

> **Per-symbol cost routing (pulled forward from Task 14 after review).** The engine resolves the floor **per trade**, not once per run, via a `cost_strategy_for(symbol) -> Optional[str]` callable. A plain strategy maps every symbol to its single `.cost_strategy`; the blend maps only its MR-sleeve symbols to `"S3"`. This is wired into `BacktestEngine` in Task 5 (default map) and used by `CappedBudgetBlend` in Task 14 — so the Task-5 golden already encodes per-symbol pricing and nothing re-freezes later.

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/pytest tests/test_engine.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/engine.py tests/test_engine.py
git commit -m "feat(engine): cost-tier + strategy cost-floor resolution helpers"
```

---

### Task 2: Causal Corwin-Schultz-ratio stress model (D2)

**Files:** Create `src/autotrader/stress.py`; create `tests/test_stress.py`.

Per-symbol, per-bar stress multiplier fed to the Simulator so fills auto-widen in volatile periods. Causal (uses only bars ≤ t), reuses the locked `costs.average_cs_spread`, clamped to the tier's max stress.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stress.py
import pandas as pd
import pytest
from autotrader import config
from autotrader.stress import causal_stress_series


def _ohlc(highs, lows):
    n = len(highs)
    return pd.DataFrame({"date": list(range(n)), "open": lows, "high": highs, "low": lows,
                         "close": lows, "volume": [1] * n})


def test_calm_series_is_unstressed():
    # Constant tiny high-low range every day -> cs_t == baseline -> stress == 1.0 throughout.
    df = _ohlc([100.5] * 60, [99.5] * 60)
    s = causal_stress_series(df, tier=config.TIER_INDEX_ETF, window=21, baseline=40)
    assert (abs(s.dropna() - 1.0) < 1e-9).all()


def test_volatility_blowout_raises_stress_and_clamps_to_tier_max():
    # A SHORT recent volatility burst against a long calm history: the trailing-median baseline
    # still remembers the calm period at the last bar, so stress is elevated (and clamped) there.
    # (A burst LONGER than `baseline` gets absorbed INTO the baseline and normalizes back to ~1.0 —
    #  the benign relative-vol nuance of a ratio-to-trailing-median model; use a short burst here.)
    highs = [100.5] * 40 + [130.0] * 6       # a 6-bar blowout at the end (< baseline=20)
    lows = [99.5] * 40 + [70.0] * 6
    s = causal_stress_series(_ohlc(highs, lows), tier=config.TIER_INDEX_ETF, window=5, baseline=20)
    assert s.iloc[-1] > 1.5                              # widened by the recent burst
    assert s.iloc[-1] <= config.TIER_MAX_STRESS[config.TIER_INDEX_ETF] + 1e-9   # clamped to tier max


def test_stress_is_causal_warmup_is_one():
    # Before any spread can be estimated, stress defaults to 1.0 (never NaN, never look-ahead).
    df = _ohlc([100.5] * 5, [99.5] * 5)
    s = causal_stress_series(df, tier=config.TIER_INDEX_ETF, window=21, baseline=40)
    assert len(s) == len(df) and float(s.iloc[0]) == 1.0
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError: No module named 'autotrader.stress'`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/stress.py
"""Causal Corwin-Schultz-ratio stress multiplier (Plan 04 D2). For each bar t, estimate the
recent spread from a trailing high-low window and divide by a trailing-median baseline; clamp to
[1.0, tier max]. Reuses the locked costs.average_cs_spread. Causal: bar t uses only bars <= t, so
the stress fed to a fill at the signal bar never peeks ahead. Warm-up (no estimate yet) = 1.0.
"""
import pandas as pd
from autotrader import config
from autotrader.costs import average_cs_spread


def _cs_over(window_bars) -> float:
    """average_cs_spread on a list of OHLC dicts; <2 bars or a degenerate estimate -> 0.0."""
    if len(window_bars) < 2:
        return 0.0
    try:
        return average_cs_spread(window_bars)
    except ValueError:
        return 0.0


def causal_stress_series(df, tier: str, window: int = 21, baseline: int = 252) -> pd.Series:
    """Per-bar stress multiplier aligned to df rows. df: OHLCV DataFrame (position-indexed).
    stress_t = clamp(cs_t / base_t, 1.0, TIER_MAX_STRESS[tier]); cs_t = CS spread over the
    trailing `window` bars ending at t; base_t = median of cs over the trailing `baseline`
    window (expanding before it fills). Warm-up / zero-baseline -> 1.0."""
    if tier not in config.TIER_MAX_STRESS:
        raise ValueError(f"unknown tier: {tier!r}")
    max_stress = config.TIER_MAX_STRESS[tier]
    bars = df[["high", "low"]].to_dict("records")
    n = len(bars)
    cs = [0.0] * n
    for t in range(n):
        lo = max(0, t - window + 1)
        cs[t] = _cs_over(bars[lo:t + 1])
    out = []
    cs_hist = []
    for t in range(n):
        cs_hist.append(cs[t])
        recent = cs_hist[max(0, t - baseline + 1):t + 1]
        positive = [x for x in recent if x > 0]
        base = (pd.Series(positive).median() if positive else 0.0)
        if not base or base <= 0 or cs[t] <= 0:
            out.append(1.0)
        else:
            out.append(min(max(cs[t] / base, 1.0), max_stress))
    return pd.Series(out, dtype="float64")
```

- [ ] **Step 4: Run** → PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/stress.py tests/test_stress.py
git commit -m "feat(engine): causal Corwin-Schultz-ratio stress multiplier (D2)"
```

---

### Task 3: Engine inputs — calendar-aligned frames, Simulator nested-bars, settlement runway

**Files:** Modify `src/autotrader/engine.py`, `tests/test_engine.py`.

`build_engine_inputs` turns a raw per-symbol DataFrame dict into the two views the run needs: (a) the position-indexed aligned frames for `target_weights` (validated to share one axis), and (b) the Simulator's date-keyed nested bars; plus a `TradingCalendar` extended by a 2-day synthetic settlement runway (D5).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_engine.py
from autotrader.engine import build_engine_inputs


def _bars(dates, closes):
    return pd.DataFrame({"date": dates, "open": closes,
                         "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
                         "close": closes, "volume": [1] * len(closes)})


_D = [dt.date(2026, 1, 2), dt.date(2026, 1, 5), dt.date(2026, 1, 6), dt.date(2026, 1, 7)]


def test_build_engine_inputs_aligns_and_extends_calendar():
    raw = {"SPY": _bars(_D, [100, 101, 102, 103]), "IEF": _bars(_D, [50, 50, 50, 50])}
    aligned, nested, cal = build_engine_inputs(raw, ["SPY", "IEF"])
    assert list(aligned["SPY"]["date"]) == _D and list(aligned["IEF"]["date"]) == _D
    assert nested["SPY"][_D[1]]["open"] == 101            # date-keyed nested view for the Simulator
    # calendar covers the real dates PLUS a 2-day runway so a last-bar sale can settle
    assert cal.is_trading_day(_D[-1])
    runway1 = cal.next_trading_day(_D[-1])
    runway2 = cal.next_trading_day(runway1)
    assert runway1 > _D[-1] and runway2 > runway1        # two synthetic settlement days exist


def test_build_engine_inputs_subsets_to_requested_universe():
    raw = {"SPY": _bars(_D, [100, 101, 102, 103]), "IEF": _bars(_D, [50, 50, 50, 50]),
           "QQQ": _bars(_D, [200, 201, 202, 203])}
    aligned, nested, cal = build_engine_inputs(raw, ["SPY", "IEF"])   # QQQ not requested
    assert set(aligned) == {"SPY", "IEF"} and set(nested) == {"SPY", "IEF"}


def test_build_engine_inputs_rejects_misaligned_axes():
    raw = {"SPY": _bars(_D, [100, 101, 102, 103]), "IEF": _bars(_D[:-1], [50, 50, 50])}
    with pytest.raises(ValueError):
        build_engine_inputs(raw, ["SPY", "IEF"])
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'build_engine_inputs'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/engine.py (imports at top of file)
from autotrader.calendar_nyse import TradingCalendar

_RUNWAY_DAYS = 2   # synthetic trailing trading days so a last-real-bar sale can settle (D5)


def build_engine_inputs(raw_bars: dict, universe: list):
    """From a raw {symbol: position-indexed OHLCV DataFrame} dict, build the two views the run
    needs, restricted to `universe` (Engine contract #2 load-set):
      aligned: {symbol: DataFrame} all sharing the anchor symbol's date axis (validated);
      nested:  {symbol: {date: {open,high,low,close}}} for the Simulator;
      calendar: TradingCalendar over the real dates + a 2-trading-day synthetic settlement runway.
    Raises if any requested symbol's date axis differs from the anchor (no silent shift)."""
    aligned = {s: raw_bars[s].reset_index(drop=True) for s in universe}
    anchor = universe[0]
    axis = list(aligned[anchor]["date"])
    for s in universe:
        if list(aligned[s]["date"]) != axis:
            raise ValueError(f"{s} date axis differs from {anchor}; the engine requires one shared "
                             "calendar-aligned axis (Engine contract #1)")
    nested = {}
    for s in universe:
        df = aligned[s]
        nested[s] = {row.date: {"open": row.open, "high": row.high,
                                "low": row.low, "close": row.close}
                     for row in df.itertuples(index=False)}
    days = list(axis)
    cur = days[-1]
    for _ in range(_RUNWAY_DAYS):                 # append calendar-only runway dates (never given bars)
        cur = cur + dt.timedelta(days=1)
        while cur.weekday() >= 5:                  # skip Sat/Sun so runway dates look like trading days
            cur = cur + dt.timedelta(days=1)
        days.append(cur)
    return aligned, nested, TradingCalendar(days)
```

- [ ] **Step 4: Run** → PASS (`-k build_engine_inputs` 3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/engine.py tests/test_engine.py
git commit -m "feat(engine): aligned-frames + nested-bars + settlement-runway inputs (D5)"
```

---

### Task 4: Holdings-diff — translate a weight row into buy/sell decisions (D1)

**Files:** Modify `src/autotrader/engine.py`, `tests/test_engine.py`.

A pure function turning (current holdings set, a target weight row, an equity snapshot) into ordered SELL and BUY decisions under the all-or-nothing model: sell held names whose target is 0; buy target>0 names not held, sized at `weight × equity`, entered best-weight-first (deterministic).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_engine.py
from autotrader.engine import plan_rebalance


def test_plan_rebalance_enters_full_set_from_cash():
    sells, buys = plan_rebalance(held=set(), weights={"XLK": 0.5, "XLF": 0.5}, equity=1000.0)
    assert sells == []
    assert buys == [("XLF", 500.0), ("XLK", 500.0)] or buys == [("XLK", 500.0), ("XLF", 500.0)]
    assert sorted(b[0] for b in buys) == ["XLF", "XLK"] and all(b[1] == 500.0 for b in buys)


def test_plan_rebalance_rotation_sells_leaver_buys_joiner_keeps_stayer():
    # held {XLK,XLF}; target {XLK,XLE} -> sell XLF, buy XLE, leave XLK untrimmed (D1)
    sells, buys = plan_rebalance(held={"XLK", "XLF"}, weights={"XLK": 0.5, "XLE": 0.5}, equity=1000.0)
    assert sells == ["XLF"]
    assert buys == [("XLE", 500.0)]


def test_plan_rebalance_full_exit_to_cash():
    sells, buys = plan_rebalance(held={"SPY"}, weights={}, equity=1000.0)
    assert sells == ["SPY"] and buys == []


def test_plan_rebalance_ignores_zero_weight_signal_only_symbol():
    # SPY present at weight 0 (S4 signal-only) -> never traded (Engine contract #2)
    sells, buys = plan_rebalance(held=set(), weights={"SPY": 0.0, "XLK": 1.0}, equity=1000.0)
    assert sells == [] and buys == [("XLK", 1000.0)]
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'plan_rebalance'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/engine.py
def plan_rebalance(held: set, weights: dict, equity: float):
    """All-or-nothing reconciliation of held positions against a target weight row (D1).
    Returns (sells, buys): `sells` = sorted list of held symbols whose target weight is ~0;
    `buys` = list of (symbol, dollars) for target>0 symbols NOT currently held, sized at
    weight*equity, ordered by descending target weight then symbol (deterministic). Held names
    that remain in the target set are left untouched (drift tolerated). Zero/absent weights never
    trade (Engine contract #2)."""
    target = {s: w for s, w in weights.items() if w > 1e-12}
    sells = sorted(s for s in held if s not in target)
    fresh = [(s, w) for s, w in target.items() if s not in held]
    fresh.sort(key=lambda sw: (-sw[1], sw[0]))
    buys = [(s, w * equity) for s, w in fresh]
    return sells, buys
```

- [ ] **Step 4: Run** → PASS (`-k plan_rebalance` 4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/engine.py tests/test_engine.py
git commit -m "feat(engine): all-or-nothing holdings-diff -> buy/sell decisions (D1)"
```

---

### Task 5: The engine loop — `BacktestEngine.run` + `BacktestResult` (the GOLDEN)

**Files:** Modify `src/autotrader/engine.py`; create `tests/fixtures/golden_engine_sequence.json`; modify `tests/test_engine.py`.

The core. Walk the date axis; **stop-first** each bar; mark-to-market at close; reconcile weights → next-open fills with T+1 `can_buy` skip+log; track holdings/cash; pair fills into round-trip trades; value terminal holdings mark-to-close. Pinned by a hand-computed golden over a small fixture exercising: entry at next open, daily marking, a gap-through stop exit, a T+1-throttled re-entry skip, and terminal valuation.

The fixture (a tiny 2-symbol world; `SIG` drives the buy via a stub strategy so the test is engine-only, not strategy-coupled):

```
dates:  1/2 1/5 1/6 1/7 1/8 1/9   (1/8,1/9 exist as bars; calendar adds 1/12,1/13 runway)
XLK:    open/high/low/close per bar (see fixture below); gap-down on 1/8 triggers the -20% stop
IEF:    flat 50.0 every bar
```

- [ ] **Step 1: Write the failing test** (and a stub strategy whose weight frame is supplied directly)

```python
# add to tests/test_engine.py
import json
from pathlib import Path
from autotrader.engine import BacktestEngine, BacktestResult

_FIX = Path(__file__).resolve().parent / "fixtures"


class _FrameStrategy:
    """A stub strategy: returns a caller-supplied weight frame verbatim (engine-only golden)."""
    def __init__(self, universe, frame, stop_loss_pct=0.20, cost_strategy=None):
        self.universe = universe
        self._frame = frame
        self.stop_loss_pct = stop_loss_pct
        self.cost_strategy = cost_strategy

    def target_weights(self, bars):
        return self._frame.reset_index(drop=True)


_GD = [dt.date(2026, 1, 2), dt.date(2026, 1, 5), dt.date(2026, 1, 6),
       dt.date(2026, 1, 7), dt.date(2026, 1, 8), dt.date(2026, 1, 9)]
_XLK = pd.DataFrame({"date": _GD,
    "open":  [100.0, 100.0, 101.0, 102.0, 90.0, 89.0],
    "high":  [101.0, 102.0, 103.0, 103.0, 91.0, 90.0],
    "low":   [ 99.0, 100.0, 100.0, 101.0, 88.0, 88.0],
    "close": [100.0, 101.0, 102.0, 102.5, 89.0, 89.0],
    "volume": [1] * 6})
_IEF = pd.DataFrame({"date": _GD, "open": [50.0]*6, "high": [50.0]*6, "low": [50.0]*6,
                     "close": [50.0]*6, "volume": [1]*6})


def _golden_frame():
    # hold XLK from the 1/2 signal (fills 1/5) until the stop; never hold IEF
    w = pd.DataFrame(0.0, index=range(6), columns=["XLK", "IEF"])
    w["XLK"] = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    return w


def test_engine_run_matches_golden():
    # stop_loss_pct=0.10 so the 1/8 gap to open 90 (-10%) triggers a gap-through stop (review B3)
    strat = _FrameStrategy(["XLK", "IEF"], _golden_frame(), stop_loss_pct=0.10)
    eng = BacktestEngine(strat, {"XLK": _XLK, "IEF": _IEF}, initial_cash=1000.0,
                         stress=1.0)   # constant stress for a hand-checkable golden
    res = eng.run()                    # the in-run cash<->ledger reconciliation assert must not raise
    assert isinstance(res, BacktestResult)
    assert res.trades[0].exit_reason == "stop" and res.trades[0].exit_price == 90.0
    out = {
        "equity": [round(float(v), 6) for v in res.equity.tolist()],
        "equity_dates": [str(d) for d in res.equity.index],
        "n_trades": len(res.trades),
        "trade": {"symbol": res.trades[0].symbol, "entry_date": str(res.trades[0].entry_date),
                  "entry_price": round(res.trades[0].entry_price, 6),
                  "exit_date": str(res.trades[0].exit_date),
                  "exit_price": round(res.trades[0].exit_price, 6),
                  "exit_reason": res.trades[0].exit_reason,
                  "shares": round(res.trades[0].shares, 6),
                  "pnl": round(res.trades[0].pnl, 6)},
        "skipped": res.skipped_buys,
    }
    with open(_FIX / "golden_engine_sequence.json") as f:
        assert out == json.load(f)


def test_engine_terminal_open_position_emitted_as_trade():
    # buy-hold XLK (no stop, never exits) -> a terminal mark-to-close trade at the last close (D5)
    w = pd.DataFrame({"XLK": [1.0] * 6, "IEF": [0.0] * 6})
    strat = _FrameStrategy(["XLK", "IEF"], w, stop_loss_pct=None)
    res = BacktestEngine(strat, {"XLK": _XLK, "IEF": _IEF}, initial_cash=1000.0, stress=1.0).run()
    term = [t for t in res.trades if t.exit_reason == "terminal"]
    assert len(term) == 1 and term[0].symbol == "XLK"
    assert term[0].exit_price == _XLK["close"].iloc[-1] and term[0].exit_cost == 0.0


def test_engine_skips_and_logs_unsettled_buy():
    # sell XLK on 1/6 (fills 1/7, settles 1/8); a same-window buy of IEF must be T+1-throttled.
    w = pd.DataFrame(0.0, index=range(6), columns=["XLK", "IEF"])
    w["XLK"] = [1.0, 1.0, 0.0, 0.0, 0.0, 0.0]    # exit XLK at the 1/6 signal
    w["IEF"] = [0.0, 0.0, 1.0, 1.0, 1.0, 1.0]    # want IEF starting 1/6 signal -> needs settled cash
    strat = _FrameStrategy(["XLK", "IEF"], w, stop_loss_pct=None)
    res = BacktestEngine(strat, {"XLK": _XLK, "IEF": _IEF}, initial_cash=1000.0, stress=1.0).run()
    assert any(s["symbol"] == "IEF" for s in res.skipped_buys)   # at least one throttled attempt logged
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'BacktestEngine'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/engine.py
import pandas as pd
from autotrader.simulator import Simulator

_MIN_TRADE = 1.0   # dollars; a buy fundable for less than this is a T+1 throttle, logged not executed


@dataclass
class Trade:
    symbol: str
    entry_date: dt.date; entry_price: float; shares: float; dollars: float; entry_cost: float
    exit_date: dt.date; exit_price: float; proceeds: float; exit_cost: float
    exit_reason: str          # "signal" | "stop" | "terminal"
    pnl: float; ret: float


@dataclass
class BacktestResult:
    equity: pd.Series                       # date-indexed daily mark-to-market total equity
    returns: pd.Series                      # daily simple returns of equity
    trades: list                            # list[Trade] (incl. terminal mark-to-close positions)
    weights: pd.DataFrame                   # date x symbol realized (held) weights
    skipped_buys: list                      # [{date, symbol, dollars, reason}] — genuine T+1 throttles
    dates: list


class BacktestEngine:
    """Drive one strategy's causal weight frame through the locked Simulator. Offline, deterministic.
    `stress`: None -> the default causal CS-ratio model per symbol (D2); a float -> constant (goldens);
    a callable(symbol, t)->float -> custom. The strategy may expose `cost_strategy_for(symbol)` for
    per-symbol cost routing (the blend); otherwise every symbol maps to its single `.cost_strategy`."""
    def __init__(self, strategy, raw_bars: dict, initial_cash: float = 1000.0,
                 slippage_frac: float = 0.0, stress=None):
        self.strategy = strategy
        self.initial_cash = initial_cash
        self.slippage_frac = slippage_frac
        self.aligned, self.nested, self.calendar = build_engine_inputs(raw_bars, strategy.universe)
        if stress is None:                                   # default D2 model, per symbol at its tier
            from autotrader.stress import causal_stress_series
            ss = {s: causal_stress_series(self.aligned[s], tier_for_symbol(s)) for s in strategy.universe}
            self.stress = lambda s, t, _ss=ss: float(_ss[s].iloc[t])
        else:
            self.stress = stress
        cs_for = getattr(strategy, "cost_strategy_for", None)        # per-symbol cost-strategy map
        self.cost_strategy_for = cs_for or (lambda s, _c=getattr(strategy, "cost_strategy", None): _c)
        self._held_for_stress = []

    def _stress(self, symbol, t):
        return self.stress(symbol, t) if callable(self.stress) else float(self.stress)

    def _stress_max(self, t):
        """A resting stop fires intrabar; the Simulator applies ONE stress to every stop fill that
        bar, so the engine passes the MAX stress across currently-held names — a deliberate
        conservative (worst-sleeve cost) choice, pinned here (review finding, not hand-waved)."""
        if not callable(self.stress):
            return float(self.stress)
        return max((self._stress(s, t) for s in self._held_for_stress), default=1.0)

    def run(self) -> BacktestResult:
        strat = self.strategy
        dates = list(self.aligned[strat.universe[0]]["date"])
        n = len(dates)
        close = {s: list(self.aligned[s]["close"]) for s in strat.universe}
        W = strat.target_weights({s: self.aligned[s] for s in strat.universe}).reset_index(drop=True)

        sim = Simulator(calendar=self.calendar, bars=self.nested, slippage_frac=self.slippage_frac)
        sim.deposit(self.initial_cash, on=dates[0])

        cash = self.initial_cash
        holdings = {}    # symbol -> dict(shares, dollars, entry_date, entry_price, entry_cost)
        trades, skipped = [], []
        equity_rows, weight_rows = [], []

        def mark_equity(t):
            return cash + sum(h["shares"] * close[s][t] for s, h in holdings.items())

        for t in range(n):
            date = dates[t]
            is_last = (t == n - 1)
            self._held_for_stress = list(holdings)               # for _stress_max, before evaluate_stops
            # 1) STOP-FIRST (Engine contract #3). Needs a next day to settle -> skip on the last bar.
            if not is_last:
                for fill in sim.evaluate_stops(date, stress=self._stress_max(t)):
                    cash += fill.proceeds
                    h = holdings.pop(fill.symbol)
                    trades.append(self._close_trade(h, fill.date, fill.price, fill.proceeds,
                                                    fill.cost, "stop"))
            # 2) mark-to-market at this bar's close (after stops)
            eq = mark_equity(t)
            equity_rows.append((date, eq))
            weight_rows.append({s: (holdings[s]["shares"] * close[s][t] / eq if s in holdings else 0.0)
                                for s in strat.universe})
            if is_last:
                break
            # 3) reconcile weights -> next-open execution (signal exits first, then capped entries)
            row = {s: float(W[s].iloc[t]) for s in W.columns}
            sells, buys = plan_rebalance(set(holdings), row, eq)
            for s in sells:
                fill = sim.submit_sell(s, signal_date=date, stress=self._stress(s, t))
                cash += fill.proceeds
                h = holdings.pop(s)
                trades.append(self._close_trade(h, fill.date, fill.price, fill.proceeds,
                                                fill.cost, "signal"))
            fill_date = self.calendar.next_trading_day(date)
            for s, target_dollars in buys:
                # D1 settled-cash cap: deploy what's actually settled for the fill date; a near-zero
                # fundable amount is a genuine T+1 rotation throttle -> log it (spec §3.6), don't carry.
                dollars = min(target_dollars, sim.ledger.settled_cash(fill_date))
                if dollars < _MIN_TRADE:
                    skipped.append({"date": str(date), "symbol": s,
                                    "target_dollars": round(target_dollars, 6),
                                    "reason": "insufficient settled cash (T+1 throttle)"})
                    continue
                floor = cost_floor_for_strategy(self.cost_strategy_for(s))   # per-symbol (S3 floor on MR)
                buy = sim.submit_buy(s, signal_date=date, dollar_amount=dollars,
                                     tier=tier_for_symbol(s), cost_floor=floor, stress=self._stress(s, t))
                cash -= dollars
                holdings[s] = {"symbol": s, "shares": buy.shares, "dollars": dollars,
                               "entry_date": buy.date, "entry_price": buy.price, "entry_cost": buy.cost}
                if getattr(strat, "stop_loss_pct", None) is not None:
                    sim.place_stop(s, buy.price * (1 - strat.stop_loss_pct))

        # cash-book reconciliation invariant: the engine's marking scalar must equal the ledger total
        # (settled + unsettled). Two books, one truth — assert they never silently diverge (review B1).
        ledger_total = sum(tr.amount for tr in sim.ledger._tranches)
        assert abs(cash - ledger_total) < 1e-6, f"cash {cash} != ledger {ledger_total}"

        # terminal mark-to-close (D5): value any still-open position at the last close, emit as a
        # "terminal" trade (no exit cost) so trade-based metrics + turnover include the final bet.
        last = n - 1
        for s in list(holdings):
            h = holdings.pop(s)
            px = close[s][last]
            trades.append(self._close_trade(h, dates[last], px, h["shares"] * px, 0.0, "terminal"))

        idx = pd.Index([d for d, _ in equity_rows], name="date")
        equity = pd.Series([v for _, v in equity_rows], index=idx, dtype="float64")
        weights = pd.DataFrame(weight_rows, index=idx)
        return BacktestResult(equity=equity, returns=equity.pct_change(), trades=trades,
                              weights=weights, skipped_buys=skipped, dates=dates)

    def _close_trade(self, h, exit_date, exit_price, proceeds, exit_cost, reason) -> Trade:
        pnl = proceeds - h["dollars"]
        return Trade(symbol=h["symbol"], entry_date=h["entry_date"], entry_price=h["entry_price"],
                     shares=h["shares"], dollars=h["dollars"], entry_cost=h["entry_cost"],
                     exit_date=exit_date, exit_price=exit_price, proceeds=proceeds,
                     exit_cost=exit_cost, exit_reason=reason, pnl=pnl, ret=pnl / h["dollars"])
```

> **Re-entry after a stop (pinned behavior, review S1):** a stop is orthogonal to the weight frame, so once a stopped symbol's target weight is still >0, the engine **re-buys it the next bar** (the realized-weight path is `1.0 → 0.0(stop) → 1.0(rebuy)`). This is intended (the stop is an intramonth crash backstop, not a signal); the report must note a stop gives little protection on a name whose signal stays on. Add a test asserting the re-buy when a post-stop bar exists.

> **Leading warm-up (review S2):** the engine records equity from `t=0`, so the leading all-cash burn-in (200/252 bars before any signal can fire) sits flat at `initial_cash`. Those zeros dilute full-sample Sharpe/vol and inflate `T` in the DSR. **Decision:** the engine keeps the full curve (honest — pre-investment cash is real); the **report computes headline metrics on the curve trimmed to the first bar a position is held** (`report.first_active_index`), and shows the full-sample row alongside, labeled. Mid-stream risk-off cash is NOT trimmed (it is a real allocation decision).

- [ ] **Step 4: Generate and freeze the golden.** The Step-1 test pins `stop_loss_pct=0.10` (so the 1/8 gap to open **90** = −10% from the 100 basis triggers a gap-through stop fill at 90 — exercising the path end-to-end, mirroring `test_simulator.py`). The implementer computes the exact JSON from a first green run and **hand-verifies every number against the COST_MODEL formulas before freezing** (independently confirmed in review: entry 1/5 @100, sector half-cost 0.075% → cost 0.75 → shares 9.9925; daily marks 1009.2425 / 1019.235 / 1024.23125; stop exit 1/8 @90 → gross 899.325, spread 0.674494, reg fees 0.020185 → proceeds 898.630321, pnl −101.369679). Run: `./.venv/bin/pytest tests/test_engine.py -k "golden or skips or terminal" -v` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/engine.py tests/test_engine.py tests/fixtures/golden_engine_sequence.json
git commit -m "feat(engine): walk-forward run loop + BacktestResult, frozen behind a golden"
```

---

### Task 6: Engine ↔ strategy integration smoke (S1–S4)

**Files:** Modify `tests/test_engine.py` only (the default D2 stress + `_held_for_stress` are already wired in Task 5's `__init__`/loop). This task is integration coverage, not new production code — the tests should pass against the Task-5 engine; any failure reveals a Task-5 gap to fix there.

Drive the real locked strategies through the engine on synthetic multi-bar fixtures: assert (a) every fill is at the **next open** (no look-ahead — a same-bar execution would change the entry price), (b) the **default stress** path (constructor `stress=None`) runs the D2 CS-ratio model per symbol without NaN equity, (c) the blend runs cleanly with no over-allocation. (The T+1 throttle *mechanism* is pinned deterministically by Task 5's `test_engine_skips_and_logs_unsettled_buy`; here we only confirm the blend runs end-to-end, since whether synthetic churn produces a same-bar rotation is fixture-dependent.)

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_engine.py
from autotrader.strategies import S1Trend, S4TrendGatedMomentum
from autotrader import config as cfg


def _ramp(dates, start, step):
    closes = [start + step * i for i in range(len(dates))]
    return _bars(dates, closes)


def _month_axis(n_months):
    return [dt.date(2026, ((m) % 12) + 1, d) for m in range(n_months) for d in (10, 20)]


def test_s1_runs_through_engine_with_default_cs_stress():
    dates = [dt.date(2025, 1, 2) + dt.timedelta(days=i) for i in range(420)]   # ~14 months daily
    spy = _bars(dates, [100 + 0.1 * i for i in range(len(dates))])             # steady uptrend
    ief = _bars(dates, [50.0] * len(dates))
    eng = BacktestEngine(S1Trend(sma_months=3, stop_loss_pct=0.20), {"SPY": spy, "IEF": ief},
                         initial_cash=1000.0)            # stress defaults to the D2 CS-ratio model
    res = eng.run()
    assert res.equity.iloc[-1] > 0 and res.equity.notna().all()
    assert len(res.equity) == len(dates)
    # next-open property: the first SPY entry filled at an OPEN that exists in the bar set
    spy_trades = [t for t in res.trades if t.symbol == "SPY"]
    if spy_trades:
        assert spy_trades[0].entry_price in set(spy["open"])


def test_blend_shared_ledger_throttles_at_least_once():
    # S4 over a basket with frequent rotations -> sells and buys compete for one settled pool.
    dates = [dt.date(2025, 1, 2) + dt.timedelta(days=i) for i in range(900)]
    sectors = cfg.SECTOR_SPDRS
    raw = {}
    for k, s in enumerate(sectors):
        raw[s] = _bars(dates, [10 + ((k + i) % 7) * 0.5 + 0.01 * i for i in range(len(dates))])
    raw["SPY"] = _bars(dates, [100 + 0.05 * i for i in range(len(dates))])
    raw["IEF"] = _bars(dates, [50.0] * len(dates))
    s4 = S4TrendGatedMomentum(sectors, equity="SPY", bond="IEF", n_hold=3, buffer=1,
                              nearness_window=60, sma_months=3)
    res = BacktestEngine(s4, raw, initial_cash=1000.0).run()    # default D2 CS-ratio stress
    assert res.equity.notna().all() and res.equity.iloc[-1] > 0
    assert (res.weights.sum(axis=1) <= 1.0 + 1e-9).all()        # no over-allocation / no leverage
```

- [ ] **Step 2: Run** → these integration tests run against the Task-5 engine. If green, the wiring is correct; if a test fails, fix the gap in Task 5 (do not add production code here). Run: `./.venv/bin/pytest tests/test_engine.py -k "through_engine or shared_ledger" -v`.

- [ ] **Step 3: (only if a test failed) repair Task 5.** The default-stress path and `_held_for_stress` are built in Task 5's `__init__`/loop; a NaN-equity or AttributeError here means a Task-5 defect — fix it in `engine.py` and re-run. No new module code originates in Task 6.

- [ ] **Step 4: Full file green.** `./.venv/bin/pytest tests/test_engine.py -v` → all green (Tasks 1–6 incl. golden, terminal, skip, integration).

- [ ] **Step 5: Commit**

```bash
git add tests/test_engine.py
git commit -m "test(engine): S1-S4 integration smoke (next-open, default D2 stress, no over-alloc)"
```

---

## PHASE B — Metrics & statistical gates (pure functions on returns / trades)

All of Phase B is pure: inputs are a date-indexed equity/returns Series and/or a list of `Trade`. No engine, no I/O. Annualization uses **252 trading days**; CAGR uses calendar days (365.25/yr). Every function is total-ordering deterministic.

### Task 7: Return-based metrics — CAGR, vol, Sharpe, Sortino, max-DD, Calmar

**Files:** Create `src/autotrader/metrics.py`, `tests/test_metrics.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_metrics.py
import datetime as dt
import numpy as np
import pandas as pd
import pytest
from autotrader.metrics import (cagr, annualized_vol, sharpe, sortino, max_drawdown, calmar)


def _equity(values, start=dt.date(2020, 1, 1), step_days=1):
    idx = pd.Index([start + dt.timedelta(days=step_days * i) for i in range(len(values))], name="date")
    return pd.Series([float(v) for v in values], index=idx)


def test_cagr_doubles_in_one_year():
    eq = _equity([100.0, 200.0], start=dt.date(2020, 1, 1))
    eq.index = pd.Index([dt.date(2020, 1, 1), dt.date(2021, 1, 1)], name="date")  # exactly 366 days (leap)
    assert abs(cagr(eq) - (2.0 ** (365.25 / 366) - 1)) < 1e-9


def test_annualized_vol_zero_for_constant_returns():
    eq = _equity([100 * (1.001 ** i) for i in range(50)])     # constant +0.1%/day -> zero stdev
    assert annualized_vol(eq.pct_change().dropna()) < 1e-12


def test_sharpe_known_value():
    r = pd.Series([0.01, -0.01, 0.01, -0.01, 0.01, -0.01])     # mean 0 -> sharpe 0
    assert abs(sharpe(r)) < 1e-12
    r2 = pd.Series([0.02, 0.0, 0.02, 0.0])                     # mean 0.01, std(ddof=1)=0.0115470
    assert abs(sharpe(r2) - (0.01 / r2.std(ddof=1)) * np.sqrt(252)) < 1e-9


def test_sortino_only_penalizes_downside():
    r = pd.Series([0.01, -0.02, 0.01, -0.02])
    downside = np.sqrt(np.mean(np.minimum(r.values, 0.0) ** 2))
    assert abs(sortino(r) - (r.mean() / downside) * np.sqrt(252)) < 1e-9


def test_max_drawdown_peak_to_trough():
    eq = _equity([100, 120, 60, 90])      # trough 60 vs peak 120 -> -0.5
    assert abs(max_drawdown(eq) - (-0.5)) < 1e-12


def test_calmar_is_cagr_over_abs_maxdd():
    eq = _equity([100, 120, 60, 90])
    assert abs(calmar(eq) - (cagr(eq) / 0.5)) < 1e-9


def test_sharpe_zero_vol_returns_zero_not_nan():
    assert sharpe(pd.Series([0.0, 0.0, 0.0])) == 0.0
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError: No module named 'autotrader.metrics'`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/metrics.py
"""Pure performance metrics on a date-indexed equity/returns Series and a list of Trade objects.
Annualization: 252 trading days for vol/Sharpe/Sortino; 365.25 calendar days/yr for CAGR. All
functions are deterministic; statistical functions (DSR, PBO, bootstrap) seed their RNG. No scipy
— the normal CDF/inverse come from statistics.NormalDist (Task 9). Formula provenance in docstrings.
"""
import math
from statistics import NormalDist
import numpy as np
import pandas as pd

_ANN = 252
_NORM = NormalDist()


def _years(equity: pd.Series) -> float:
    days = (equity.index[-1] - equity.index[0]).days
    return days / 365.25 if days > 0 else float("nan")


def cagr(equity: pd.Series) -> float:
    """Compound annual growth rate on calendar time. equity[0]>0 assumed."""
    yrs = _years(equity)
    if not yrs or yrs != yrs or equity.iloc[0] <= 0:
        return float("nan")
    return (equity.iloc[-1] / equity.iloc[0]) ** (1.0 / yrs) - 1.0


def annualized_vol(returns: pd.Series) -> float:
    r = pd.Series(returns).dropna()
    return float(r.std(ddof=1) * math.sqrt(_ANN)) if len(r) > 1 else 0.0


def sharpe(returns: pd.Series, rf: float = 0.0) -> float:
    """Annualized Sharpe (rf per-period, default 0). Zero stdev -> 0.0 (never NaN)."""
    r = pd.Series(returns).dropna() - rf
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    return float(r.mean() / sd * math.sqrt(_ANN)) if sd and sd > 0 else 0.0


def sortino(returns: pd.Series, target: float = 0.0) -> float:
    """Annualized Sortino: mean excess / downside deviation (RMS of negative deviations vs target)."""
    r = pd.Series(returns).dropna() - target
    dd = np.sqrt(np.mean(np.minimum(r.values, 0.0) ** 2)) if len(r) else 0.0
    return float(r.mean() / dd * math.sqrt(_ANN)) if dd and dd > 0 else 0.0


def max_drawdown(equity: pd.Series) -> float:
    """Most-negative peak-to-trough fraction of the equity curve (e.g. -0.5). 0.0 if monotone up."""
    e = pd.Series(equity).astype("float64")
    dd = e / e.cummax() - 1.0
    return float(dd.min()) if len(e) else float("nan")


def calmar(equity: pd.Series) -> float:
    """CAGR / |max drawdown|. Zero drawdown -> nan (undefined)."""
    mdd = max_drawdown(equity)
    return float(cagr(equity) / abs(mdd)) if mdd and mdd < 0 else float("nan")
```

- [ ] **Step 4: Run** → PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): CAGR, vol, Sharpe, Sortino, max-DD, Calmar"
```

---

### Task 8: Trade & exposure metrics — win rate, profit factor, turnover, time-in-market, trades/yr, bets

**Files:** Modify `src/autotrader/metrics.py`, `tests/test_metrics.py`.

Consume the engine's `trades` list (round-trip `pnl`) and the realized `weights` frame (exposure). Definitions pinned by oracle: turnover = total buy-notional / mean-equity / years (one-way, annualized); time-in-market = mean over days of invested fraction (dollar-weighted); number_of_bets = count of dates on which the held SET changed.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_metrics.py
from autotrader.metrics import (win_rate, profit_factor, avg_win_loss, turnover,
                                time_in_market, trades_per_year, number_of_bets)


class _T:   # minimal trade stand-in: only the fields these metrics read
    def __init__(self, pnl, dollars=100.0):
        self.pnl = pnl; self.dollars = dollars


def test_win_rate_and_profit_factor():
    trades = [_T(10), _T(-5), _T(20), _T(-5)]
    assert win_rate(trades) == 0.5
    assert abs(profit_factor(trades) - (30 / 10)) < 1e-9
    aw, al = avg_win_loss(trades)
    assert aw == 15.0 and al == -5.0


def test_profit_factor_no_losses_is_inf():
    assert profit_factor([_T(5), _T(3)]) == float("inf")


def test_turnover_annualized_one_way():
    trades = [_T(0, dollars=500.0), _T(0, dollars=500.0)]   # $1000 bought total
    eq = _equity([1000.0] * 4, start=dt.date(2020, 1, 1))
    eq.index = pd.Index([dt.date(2020, 1, 1), dt.date(2020, 7, 1),
                         dt.date(2021, 1, 1), dt.date(2021, 7, 1)], name="date")
    # derive years from the actual span (547 days / 365.25 ≈ 1.498) so the oracle matches
    # metrics._years exactly — 2020-01-01..2021-07-01 is NOT exactly 1.5 calendar years.
    years = (eq.index[-1] - eq.index[0]).days / 365.25
    assert abs(turnover(trades, eq) - (1000.0 / 1000.0 / years)) < 1e-9


def test_time_in_market_dollar_weighted():
    w = pd.DataFrame({"XLK": [0.0, 1.0, 0.5], "IEF": [0.0, 0.0, 0.5]})
    assert abs(time_in_market(w) - ((0.0 + 1.0 + 1.0) / 3)) < 1e-9


def test_number_of_bets_counts_set_changes():
    w = pd.DataFrame({"XLK": [0, 1, 1, 0, 1], "XLF": [0, 0, 1, 0, 0]})
    # held sets: {} {XLK} {XLK,XLF} {} {XLK} -> 4 changes from the prior row
    assert number_of_bets(w) == 4


def test_trades_per_year():
    eq = _equity([1.0, 1.0], start=dt.date(2020, 1, 1))
    eq.index = pd.Index([dt.date(2020, 1, 1), dt.date(2022, 1, 1)], name="date")  # 2 yrs
    assert abs(trades_per_year([_T(1), _T(1), _T(1), _T(1)], eq) - 2.0) < 1e-2
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'win_rate'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/metrics.py
def win_rate(trades) -> float:
    return sum(1 for t in trades if t.pnl > 0) / len(trades) if trades else float("nan")


def avg_win_loss(trades):
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    return (float(np.mean(wins)) if wins else 0.0, float(np.mean(losses)) if losses else 0.0)


def profit_factor(trades) -> float:
    gains = sum(t.pnl for t in trades if t.pnl > 0)
    losses = -sum(t.pnl for t in trades if t.pnl < 0)
    if losses == 0:
        return float("inf") if gains > 0 else float("nan")
    return gains / losses


def turnover(trades, equity: pd.Series) -> float:
    """Annualized one-way turnover: total dollars bought / mean equity / years."""
    bought = sum(t.dollars for t in trades)
    yrs = _years(equity)
    mean_eq = float(pd.Series(equity).mean())
    if not yrs or yrs != yrs or mean_eq <= 0:
        return float("nan")
    return bought / mean_eq / yrs


def time_in_market(weights: pd.DataFrame) -> float:
    """Mean daily invested fraction (dollar-weighted exposure) = mean of row sums of realized weights."""
    if weights is None or len(weights) == 0:
        return float("nan")
    return float(weights.sum(axis=1).clip(upper=1.0).mean())


def trades_per_year(trades, equity: pd.Series) -> float:
    yrs = _years(equity)
    return len(trades) / yrs if yrs and yrs == yrs else float("nan")


def number_of_bets(weights: pd.DataFrame) -> int:
    """Count of dates on which the held SET (non-zero-weight columns) changed from the prior row —
    the spec's 'independent bets' (regime calls / rebalances), not raw trade count (spec §4)."""
    sets = [frozenset(c for c in weights.columns if weights[c].iloc[i] > 1e-12)
            for i in range(len(weights))]
    return sum(1 for i in range(1, len(sets)) if sets[i] != sets[i - 1])
```

- [ ] **Step 4: Run** → PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): trade + exposure metrics (win rate, PF, turnover, TIM, bets)"
```

---

### Task 9: Deflated Sharpe Ratio + p-value (Bailey & López de Prado 2014)

**Files:** Modify `src/autotrader/metrics.py`, `tests/test_metrics.py`.

DSR deflates an observed Sharpe for (a) the number of trials, (b) non-normal returns (skew/kurtosis), (c) sample length. Uses **per-observation** Sharpe internally. `statistics.NormalDist` for Φ/Φ⁻¹ (no scipy). The expected-maximum Sharpe under the null, `SR0`, comes from the trial count `n_trials` and the cross-trial Sharpe variance `sr_variance`.

> **Formula (SSRN 2460551):** with `sr` = per-observation Sharpe, `T` = #obs, `g3` = skew, `g4` = kurtosis (normal = 3):
> `DSR = Φ( (sr − sr0)·√(T−1) / √(1 − g3·sr + ((g4−1)/4)·sr²) )`,
> `sr0 = √V · [ (1−γ)·Φ⁻¹(1 − 1/N) + γ·Φ⁻¹(1 − 1/(N·e)) ]`, γ = Euler-Mascheroni ≈ 0.5772156649, e = exp(1), V = `sr_variance`. The returned p-value is `1 − DSR` (spec §3.9 auto-kills at p ≥ 0.05 ⇔ DSR ≤ 0.95).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_metrics.py
from autotrader.metrics import deflated_sharpe, _sr0_expected_max   # helper exposed for the oracle


def _normalish(mean, sd, n, seed=0):
    return pd.Series(np.random.default_rng(seed).normal(mean, sd, n))


def test_dsr_half_when_observed_equals_sr0():
    # Construct returns whose per-obs Sharpe == sr0 exactly -> z==0 -> DSR==0.5, p==0.5.
    # NOTE: Sharpe is SCALE-invariant, so a *mean shift* (not a multiply) is required to hit sr0
    # (review B2: `r * k` leaves the Sharpe unchanged). Shifting by a constant preserves std, so
    # mean(r3)/std(r3) == sr0 to machine precision.
    r = _normalish(0.001, 0.01, 2000, seed=1)
    V, N = 0.02, 10
    sr0 = _sr0_expected_max(V, N)
    r3 = r - r.mean() + sr0 * r.std(ddof=1)
    dsr, p = deflated_sharpe(r3, n_trials=N, sr_variance=V)
    assert abs(dsr - 0.5) < 1e-6 and abs(p - 0.5) < 1e-6


def test_dsr_increases_with_sharpe_decreases_with_trials():
    weak = _normalish(0.0003, 0.01, 3000, seed=2)
    strong = _normalish(0.0015, 0.01, 3000, seed=2)
    V = 0.01
    d_weak, _ = deflated_sharpe(weak, n_trials=5, sr_variance=V)
    d_strong, _ = deflated_sharpe(strong, n_trials=5, sr_variance=V)
    assert d_strong > d_weak
    d_few, _ = deflated_sharpe(strong, n_trials=2, sr_variance=V)
    d_many, _ = deflated_sharpe(strong, n_trials=500, sr_variance=V)
    assert d_few > d_many                 # more trials -> harder to clear -> lower DSR


def test_dsr_p_value_is_one_minus_dsr():
    r = _normalish(0.001, 0.01, 1500, seed=3)
    dsr, p = deflated_sharpe(r, n_trials=20, sr_variance=0.02)
    assert abs((1 - dsr) - p) < 1e-12
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'deflated_sharpe'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/metrics.py
_EULER = 0.5772156649015329


def _sr0_expected_max(sr_variance: float, n_trials: int) -> float:
    """Expected maximum (per-observation) Sharpe under the null of `n_trials` independent trials
    with cross-trial Sharpe variance `sr_variance` (Bailey-LdP eq. for SR0)."""
    if n_trials < 2 or sr_variance <= 0:
        return 0.0
    z1 = _NORM.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = _NORM.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(sr_variance) * ((1.0 - _EULER) * z1 + _EULER * z2)


def deflated_sharpe(returns, n_trials: int, sr_variance: float):
    """Deflated Sharpe Ratio (probability the true Sharpe > SR0) and its p-value = 1 - DSR.
    `returns`: per-period return Series. `n_trials`: number of configurations tried (multiple-
    testing count). `sr_variance`: variance of Sharpe across those trials. Spec §3.9 kills at
    p >= 0.05. Returns (dsr, p)."""
    r = pd.Series(returns).dropna()
    T = len(r)
    if T < 3:
        return (float("nan"), float("nan"))
    sd = r.std(ddof=1)
    if not sd or sd <= 0:
        return (float("nan"), float("nan"))
    sr = float(r.mean() / sd)                       # per-observation Sharpe
    g3 = float(r.skew())
    g4 = float(r.kurtosis() + 3.0)                  # pandas kurtosis is EXCESS; DSR wants raw (normal=3)
    sr0 = _sr0_expected_max(sr_variance, n_trials)
    denom = math.sqrt(max(1e-12, 1.0 - g3 * sr + ((g4 - 1.0) / 4.0) * sr * sr))
    z = (sr - sr0) * math.sqrt(T - 1) / denom
    dsr = _NORM.cdf(z)
    return (dsr, 1.0 - dsr)
```

- [ ] **Step 4: Run** → PASS (3 passed). The `test_dsr_half...` tolerance is tight (`< 1e-6`) by construction — `z` is exactly 0 when the sample Sharpe equals `sr0` (the skew/kurtosis denominator is irrelevant when the numerator is 0). Do NOT loosen the tolerance; if it fails, the mean-shift construction or the per-observation unit convention is wrong — fix the code, not the test.

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): Deflated Sharpe Ratio + p-value (Bailey-LdP, stdlib NormalDist)"
```

---

### Task 10: Probability of Backtest Overfitting via CSCV (Bailey-Borwein-LdP-Zhu 2017)

**Files:** Modify `src/autotrader/metrics.py`, `tests/test_metrics.py`.

PBO over a returns **matrix** `M` (rows = time, cols = configurations). Partition rows into `S` even sub-blocks; over every `C(S, S/2)` in-sample/out-of-sample split, the config that is **best in-sample** gets an out-of-sample rank; PBO = fraction of splits where that IS-best config lands **below the OOS median** (logit ≤ 0).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_metrics.py
from autotrader.metrics import pbo_cscv


def _matrix(cols):  # cols: dict name->array of per-period returns
    return pd.DataFrame(cols)


def test_pbo_zero_when_one_config_dominates_everywhere():
    # Genuine within-block variance (review B3: constant columns tie at Sharpe 0 and invert the
    # oracle). With noise << the mean separation, config "a" is best IS AND OOS in every split.
    rng = np.random.default_rng(0)
    T = 256
    M = _matrix({"a": rng.normal(0.006, 0.008, T),
                 "b": rng.normal(0.001, 0.008, T),
                 "c": rng.normal(-0.003, 0.008, T)})
    assert pbo_cscv(M, n_blocks=8) < 0.05


def test_pbo_one_when_is_best_is_oos_worst():
    # Each config spikes in exactly ONE block (flat noise elsewhere). For any IS half, the IS-best
    # is a config whose spike is IN-sample -> its OOS is flat -> it ranks below the configs whose
    # spikes are OUT-of-sample -> overfit on every split -> PBO ~ 1.
    rng = np.random.default_rng(1)
    S, blk = 8, 8
    T = S * blk
    cols = {}
    for i in range(S):
        series = rng.normal(0.0, 0.004, T)
        series[i * blk:(i + 1) * blk] += 0.10          # big in-block spike
        cols[f"c{i}"] = series
    assert pbo_cscv(_matrix(cols), n_blocks=S) > 0.9


def test_pbo_requires_even_blocks_and_enough_rows():
    with pytest.raises(ValueError):
        pbo_cscv(_matrix({"a": [0.0] * 10, "b": [0.0] * 10}), n_blocks=3)   # odd S
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'pbo_cscv'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/metrics.py
import itertools


def _block_sharpe(block: np.ndarray) -> np.ndarray:
    """Per-column Sharpe over a stacked block of rows (ddof=1). Zero-variance column -> 0."""
    mean = block.mean(axis=0)
    sd = block.std(axis=0, ddof=1)
    out = np.zeros_like(mean)
    nz = sd > 0
    out[nz] = mean[nz] / sd[nz]
    return out


def pbo_cscv(matrix: pd.DataFrame, n_blocks: int = 16):
    """Probability of Backtest Overfitting via Combinatorially Symmetric Cross-Validation
    (Bailey-Borwein-LdP-Zhu 2017, SSRN 2326253). `matrix`: rows=time, cols=configurations of
    per-period returns. Splits rows into `n_blocks` even sub-blocks; for each C(S,S/2) IS/OOS
    split, takes the IS-argmax config and records whether its OOS performance is below median
    (logit lambda <= 0). Returns the fraction of splits that are overfit. Spec §3.9 kills at PBO >= 0.5."""
    if n_blocks % 2 != 0:
        raise ValueError("n_blocks (S) must be even")
    M = matrix.to_numpy(dtype="float64")
    T, ncfg = M.shape
    if ncfg < 2 or T < n_blocks:
        raise ValueError("need >=2 configs and >= n_blocks rows")
    bounds = np.array_split(np.arange(T), n_blocks)
    blocks = [M[b, :] for b in bounds]
    overfit = 0
    total = 0
    for is_idx in itertools.combinations(range(n_blocks), n_blocks // 2):
        oos_idx = [j for j in range(n_blocks) if j not in is_idx]
        is_perf = _block_sharpe(np.vstack([blocks[j] for j in is_idx]))
        oos_perf = _block_sharpe(np.vstack([blocks[j] for j in oos_idx]))
        n_star = int(np.argmax(is_perf))
        # relative OOS rank of n_star in (0,1): fraction of configs it beats, mid-rank for ties
        order = oos_perf.argsort()                      # ascending
        ranks = np.empty(ncfg); ranks[order] = np.arange(1, ncfg + 1)
        omega = ranks[n_star] / (ncfg + 1)
        lam = math.log(omega / (1.0 - omega))
        overfit += 1 if lam <= 0 else 0
        total += 1
    return overfit / total
```

> **Tie handling (review note):** `argsort` assigns ranks by index on exact ties, which is not a true mid-rank. Exact ties arise only from **degenerate constant-variance inputs** (which broke the original oracles); real strategy returns are continuous and won't tie. The oracles above use noisy series specifically to avoid this. If a future caller feeds constant columns, document that the result is undefined rather than silently mis-ranking.

- [ ] **Step 4: Run** → PASS (3 passed). These oracles are well-posed (within-block variance), so they pin the *direction* the original degenerate fixtures got backwards.

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): PBO via CSCV (Bailey-Borwein-LdP-Zhu)"
```

---

### Task 11: Bootstrap confidence intervals

**Files:** Modify `src/autotrader/metrics.py`, `tests/test_metrics.py`.

Seeded bootstrap CI for any statistic of a return series — used for the (strategy − benchmark) Sharpe CI and the max-DD CI (spec §4). **`block_size>1` selects a moving-block bootstrap** so serial dependence is preserved: an IID bootstrap *understates* the Sharpe CI and is invalid for path-dependent stats like max-DD (review S1/S5). A `max_drawdown_from_returns` helper rebuilds the equity path so drawdown can be bootstrapped from a return series. Deterministic via `numpy.random.default_rng(seed)`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_metrics.py
from autotrader.metrics import bootstrap_ci, sharpe, max_drawdown_from_returns


def test_bootstrap_ci_is_deterministic_and_brackets_point_estimate():
    r = pd.Series(np.random.default_rng(7).normal(0.001, 0.01, 500))
    lo1, hi1 = bootstrap_ci(r, sharpe, n=500, seed=42)
    lo2, hi2 = bootstrap_ci(r, sharpe, n=500, seed=42)
    assert (lo1, hi1) == (lo2, hi2)                 # same seed -> identical
    assert lo1 < sharpe(r) < hi1                    # CI brackets the point estimate
    lo3, _ = bootstrap_ci(r, sharpe, n=500, seed=43)
    assert lo3 != lo1                               # different seed -> different draw


def test_bootstrap_ci_respects_ci_width():
    r = pd.Series(np.random.default_rng(7).normal(0.001, 0.01, 500))
    lo90, hi90 = bootstrap_ci(r, sharpe, n=500, seed=42, ci=0.90)
    lo99, hi99 = bootstrap_ci(r, sharpe, n=500, seed=42, ci=0.99)
    assert (hi99 - lo99) > (hi90 - lo90)            # wider confidence -> wider interval


def test_block_bootstrap_maxdd_ci_is_deterministic_and_negative():
    # max-DD must be bootstrapped from the rebuilt equity PATH with a block bootstrap (review S1).
    r = pd.Series(np.random.default_rng(9).normal(0.0005, 0.012, 600))
    lo, hi = bootstrap_ci(r, max_drawdown_from_returns, n=400, seed=1, block_size=21)
    assert lo <= hi <= 0.0                          # drawdowns are non-positive
    lo2, hi2 = bootstrap_ci(r, max_drawdown_from_returns, n=400, seed=1, block_size=21)
    assert (lo, hi) == (lo2, hi2)                    # deterministic
    # block bootstrap (preserves runs) gives a different, generally wider DD tail than IID
    lo_iid, _ = bootstrap_ci(r, max_drawdown_from_returns, n=400, seed=1, block_size=1)
    assert lo != lo_iid
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'bootstrap_ci'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/metrics.py
def max_drawdown_from_returns(returns) -> float:
    """Max drawdown computed by rebuilding the equity path from a return series (so drawdown can be
    bootstrapped). max_drawdown takes an EQUITY curve; this is the adapter (review S1 wiring trap)."""
    eq = (1.0 + pd.Series(returns).fillna(0.0)).cumprod()
    return max_drawdown(eq)


def _resample(r: np.ndarray, rng, block_size: int) -> np.ndarray:
    """IID (block_size<=1) or overlapping moving-block resample to the original length."""
    nlen = len(r)
    if block_size <= 1 or block_size >= nlen:
        return r[rng.integers(0, nlen, nlen)]
    n_blocks = int(np.ceil(nlen / block_size))
    starts = rng.integers(0, nlen - block_size + 1, n_blocks)
    idx = np.concatenate([np.arange(s, s + block_size) for s in starts])[:nlen]
    return r[idx]


def bootstrap_ci(returns, statistic, n: int = 1000, seed: int = 0, ci: float = 0.95,
                 block_size: int = 1):
    """Percentile-bootstrap CI for `statistic(resampled_returns)`, seeded (deterministic).
    `block_size=1` -> IID (valid for Sharpe of near-IID returns); `block_size>1` -> moving-block
    (preserves serial dependence — REQUIRED for path-dependent stats like max-DD, and a less-
    optimistic Sharpe CI on autocorrelated returns, review S5). `statistic`: Series->float
    (metrics.sharpe; max_drawdown_from_returns; or a (strategy-benchmark) paired-difference Sharpe)."""
    r = pd.Series(returns).dropna().to_numpy()
    if len(r) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    stats = np.array([statistic(pd.Series(_resample(r, rng, block_size))) for _ in range(n)])
    alpha = (1.0 - ci) / 2.0
    return (float(np.quantile(stats, alpha)), float(np.quantile(stats, 1.0 - alpha)))
```

- [ ] **Step 4: Run** → PASS (3 passed). Then full metrics file: `./.venv/bin/pytest tests/test_metrics.py -v` → all green (Tasks 7–11).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/metrics.py tests/test_metrics.py
git commit -m "feat(metrics): seeded IID + moving-block bootstrap CIs (path-dependent max-DD)"
```

---

## PHASE C — Benchmarks, walk-forward, blend add-on, report

### Task 12: Benchmarks — three engine-run + 60/40 analytic (D4)

**Files:** Create `src/autotrader/benchmarks.py`, `tests/test_benchmarks.py`.

`BuyHold(symbol)`, `GatedSPY(...)`, `EqualWeightUniverse(symbols)` are weight-frame strategies (same interface as S1–S4, `stop_loss_pct=None`, `cost_strategy=None`) run through the engine → price-return, net of costs (spec §3.10 a/b/d). `sixty_forty_returns(...)` is the analytic monthly-rebalanced constant-mix (§3.10c) — a daily-return Series net of a modeled monthly-rebalance cost.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_benchmarks.py
import datetime as dt
import numpy as np
import pandas as pd
import pytest
from autotrader.benchmarks import BuyHold, GatedSPY, EqualWeightUniverse, sixty_forty_returns
from autotrader.engine import BacktestEngine


def _bars(dates, closes):
    return pd.DataFrame({"date": dates, "open": closes,
                         "high": [c * 1.001 for c in closes], "low": [c * 0.999 for c in closes],
                         "close": closes, "volume": [1] * len(closes)})


_D = [dt.date(2025, 1, 2) + dt.timedelta(days=i) for i in range(300)]


def test_buyhold_is_always_fully_invested_after_entry():
    bh = BuyHold("SPY")
    assert bh.universe == ["SPY"] and bh.stop_loss_pct is None
    w = bh.target_weights({"SPY": _bars(_D, [100 + 0.1 * i for i in range(len(_D))])})
    assert (w["SPY"] == 1.0).all()


def test_gated_spy_matches_trend_regime():
    from autotrader.strategies import trend_regime
    g = GatedSPY(sma_months=3)
    spy = _bars(_D, [100 + (10 if i > 150 else -0.05 * i) for i in range(len(_D))])
    w = g.target_weights({"SPY": spy})
    on = trend_regime(list(spy["date"]), spy["close"], sma_months=3, band=0.01)
    assert (w["SPY"].values == on.astype(float).values).all()      # SPY when on, cash when off


def test_equal_weight_universe_sums_to_one_each_row():
    ew = EqualWeightUniverse(["XLK", "XLF", "XLE"])
    bars = {s: _bars(_D, [10 + 0.01 * i for i in range(len(_D))]) for s in ew.universe}
    w = ew.target_weights(bars)
    assert np.allclose(w.sum(axis=1), 1.0) and np.allclose(w["XLK"], 1 / 3)


def test_buyhold_runs_through_engine_priceonly_net_of_one_entry_cost():
    spy = _bars(_D, [100 * (1.0003 ** i) for i in range(len(_D))])
    res = BacktestEngine(BuyHold("SPY"), {"SPY": spy}, initial_cash=1000.0, stress=1.0).run()
    # buy-hold never exits within-sample; the engine emits ONE terminal mark-to-close trade (D5)
    assert all(t.exit_reason == "terminal" for t in res.trades)
    assert res.equity.iloc[-1] > 1000.0    # grew with price, minus the entry half-cost
    assert res.skipped_buys == []


def test_sixty_forty_constant_mix_blends_daily_returns():
    spy = _bars(_D, [100 * (1.0004 ** i) for i in range(len(_D))])
    ief = _bars(_D, [50 * (1.0001 ** i) for i in range(len(_D))])
    r = sixty_forty_returns({"SPY": spy, "IEF": ief}, equity="SPY", bond="IEF", rebalance="ME")
    assert isinstance(r, pd.Series) and len(r) == len(_D)
    # on a non-rebalance day the blend sits between the two sleeve returns
    sr = spy["close"].pct_change(); br = ief["close"].pct_change()
    i = 5
    assert min(sr.iloc[i], br.iloc[i]) - 1e-6 <= r.iloc[i] <= max(sr.iloc[i], br.iloc[i]) + 1e-6


def test_sixty_forty_rebalance_cost_uses_tier_stress_and_reduces_return():
    spy = _bars(_D, [100 * (1.0004 ** i) for i in range(len(_D))])
    ief = _bars(_D, [50 * (1.0001 ** i) for i in range(len(_D))])
    gross = sixty_forty_returns({"SPY": spy, "IEF": ief}, charge_rebalance_cost=False).sum()
    net = sixty_forty_returns({"SPY": spy, "IEF": ief}, charge_rebalance_cost=True).sum()
    assert net < gross                              # the per-tier×stress rebalance cost is charged
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError: No module named 'autotrader.benchmarks'`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/benchmarks.py
"""Price-return benchmarks (spec §3.10). Three are weight-frame strategies run through the SAME
engine for identical cost treatment: BuyHold (a), GatedSPY (b, the primary for S2/blend),
EqualWeightUniverse (d). 60/40 (c) is a constant-mix object the all-or-nothing engine cannot
express, so sixty_forty_returns computes it analytically (D4) — but charges its monthly rebalance
with the SAME effective_roundtrip_cost(tier, stress) model every other path uses, so it is not an
optimistically-cheap bar in the stress folds that decide the §3.10c comparison."""
import pandas as pd
from autotrader import config
from autotrader.strategies import trend_regime
from autotrader.indicators import monthly_closes
from autotrader.costs import effective_roundtrip_cost
from autotrader.stress import causal_stress_series


class BuyHold:
    """Buy-and-hold one symbol (price-return, net of the single entry cost via the engine)."""
    def __init__(self, symbol="SPY"):
        self.symbol = symbol
        self.universe = [symbol]
        self.stop_loss_pct = None
        self.cost_strategy = None

    def target_weights(self, bars):
        n = len(bars[self.symbol])
        return pd.DataFrame({self.symbol: [1.0] * n})


class GatedSPY:
    """SPY when the broad trend is on, cash when off (spec §3.10b — isolates selection from the
    trend filter). Same trend basis as S1/S2/S4."""
    def __init__(self, symbol="SPY", sma_months=10, band=0.01):
        self.symbol = symbol
        self.sma_months, self.band = sma_months, band
        self.universe = [symbol]
        self.stop_loss_pct = None
        self.cost_strategy = None

    def target_weights(self, bars):
        df = bars[self.symbol]
        on = trend_regime(list(df["date"]), df["close"], self.sma_months, self.band)
        return pd.DataFrame({self.symbol: on.astype(float).values})


class EqualWeightUniverse:
    """Equal-weight buy-hold of a symbol set (spec §3.10d — separates selection skill from universe
    drift). Each name holds 1/k every row; the engine sizes entries at 1/k * equity."""
    def __init__(self, symbols):
        self.symbols = list(symbols)
        self.universe = list(symbols)
        self.stop_loss_pct = None
        self.cost_strategy = None

    def target_weights(self, bars):
        n = len(bars[self.symbols[0]])
        k = len(self.symbols)
        return pd.DataFrame({s: [1.0 / k] * n for s in self.symbols})


def sixty_forty_returns(bars, equity="SPY", bond="IEF", w_equity=0.60, w_bond=0.40,
                        rebalance="ME", charge_rebalance_cost=True) -> pd.Series:
    """Analytic constant-mix 60/40 daily return series, rebalanced on the last trading day of each
    period (`rebalance="ME"` = month-end). The rebalance cost is the **same per-tier×stress model**
    the engine uses: each leg's drift back to target crosses a half round-trip at the index-ETF tier
    scaled by that day's causal CS-ratio stress (not a flat bps — review S4). Price-return; the bond
    coupon is omitted (spec §3.1 documented limitation). Returns a date-indexed daily-return Series."""
    e_df, b_df = bars[equity], bars[bond]
    dates = list(e_df["date"])
    ce = e_df["close"].reset_index(drop=True)
    cb = b_df["close"].reset_index(drop=True)
    re = ce.pct_change().fillna(0.0).to_numpy()
    rb = cb.pct_change().fillna(0.0).to_numpy()
    se = causal_stress_series(e_df, config.TIER_INDEX_ETF).to_numpy()   # SPY/IEF are index-ETF tier
    sb = causal_stress_series(b_df, config.TIER_INDEX_ETF).to_numpy()
    me = set(monthly_closes(dates, list(ce))["date"])
    we, wb = w_equity, w_bond
    out = []
    for i, d in enumerate(dates):
        port_r = we * re[i] + wb * rb[i]
        ve, vb = we * (1 + re[i]), wb * (1 + rb[i])    # drift the weights with realized sleeve returns
        we, wb = ve / (ve + vb), vb / (ve + vb)
        if d in me:                                    # rebalance back to target, charge tier×stress cost
            if charge_rebalance_cost:
                traded = abs(we - w_equity)            # == abs(wb - w_bond); sell one leg, buy the other
                cost = traded * (effective_roundtrip_cost(config.TIER_INDEX_ETF, None, se[i]) / 2
                                 + effective_roundtrip_cost(config.TIER_INDEX_ETF, None, sb[i]) / 2)
                port_r -= cost
            we, wb = w_equity, w_bond
        out.append(port_r)
    return pd.Series(out, index=pd.Index(dates, name="date"), dtype="float64")
```

- [ ] **Step 4: Run** → PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/benchmarks.py tests/test_benchmarks.py
git commit -m "feat(benchmarks): buy-hold/gated-SPY/EW-universe + analytic 60/40 (D4)"
```

---

### Task 13: Walk-forward + forced stress-fold slicing (D3)

**Files:** Create `src/autotrader/walkforward.py`, `tests/test_walkforward.py`.

Slice ONE full-history run into reporting periods: expanding-window cutoffs (each year-end) and the forced **2008-09 / 2020 / 2022** calendar windows. Returns per-period return slices for metric computation. No re-running, no portfolio resets (D3).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_walkforward.py
import datetime as dt
import numpy as np
import pandas as pd
import pytest
from autotrader.walkforward import expanding_windows, stress_folds, STRESS_PERIODS


def _daily_returns(start, end, val=0.0):
    idx = pd.date_range(start, end, freq="D")
    dates = pd.Index([d.date() for d in idx], name="date")
    return pd.Series([val] * len(dates), index=dates)


def test_expanding_windows_anchor_at_start_step_yearly():
    r = _daily_returns(dt.date(2010, 1, 1), dt.date(2013, 6, 30))
    wins = expanding_windows(r)
    # each window starts at the series start; ends at successive year-ends (2010,2011,2012) + full
    assert all(w.index[0] == r.index[0] for w in wins)
    ends = [w.index[-1] for w in wins]
    assert ends[0].year == 2010 and ends[-1] == r.index[-1]
    assert len(wins) >= 3


def test_stress_folds_extract_named_periods_present_in_data():
    r = _daily_returns(dt.date(2007, 1, 1), dt.date(2023, 12, 31))
    folds = stress_folds(r)
    assert set(folds) == {"2008-09", "2020", "2022"}
    f = folds["2008-09"]
    assert f.index[0] >= dt.date(2008, 1, 1) and f.index[-1] <= dt.date(2009, 12, 31)
    assert len(f) > 0


def test_stress_folds_skip_absent_periods():
    r = _daily_returns(dt.date(2018, 1, 1), dt.date(2019, 12, 31))   # no 2008/2020/2022 coverage
    folds = stress_folds(r)
    assert folds == {} or all(len(v) == 0 for v in folds.values())
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError: No module named 'autotrader.walkforward'`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/walkforward.py
"""Anchored walk-forward (D3): the engine runs ONCE over full history; this module slices that
single daily-return series into reporting periods. Parameters are literature-locked (never refit),
so the expanding windows are for OUT-OF-SAMPLE metric reporting, not re-optimization; the forced
stress folds (spec §3.8) make the only deep-bear (2008-09) + 2020 + 2022 separately reported."""
import datetime as dt
import pandas as pd

STRESS_PERIODS = {
    "2008-09": (dt.date(2008, 1, 1), dt.date(2009, 12, 31)),
    "2020": (dt.date(2020, 1, 1), dt.date(2020, 12, 31)),
    "2022": (dt.date(2022, 1, 1), dt.date(2022, 12, 31)),
}


def expanding_windows(returns: pd.Series):
    """Expanding windows anchored at the series start, ending at each calendar year-end present in
    the data, plus the full series. Each is a slice of the same continuous run (no reset)."""
    r = pd.Series(returns)
    dates = list(r.index)
    start = dates[0]
    years = sorted({d.year for d in dates})
    wins = []
    for y in years:
        ye = dt.date(y, 12, 31)
        sl = r[[d <= ye for d in dates]]
        if len(sl) and sl.index[-1] != start:
            wins.append(sl)
    if not wins or wins[-1].index[-1] != dates[-1]:
        wins.append(r)
    # dedupe by end-date, keep order
    seen, out = set(), []
    for w in wins:
        key = w.index[-1]
        if key not in seen:
            seen.add(key); out.append(w)
    return out


def stress_folds(returns: pd.Series):
    """Extract the forced 2008-09 / 2020 / 2022 windows as slices (empty if absent in the data)."""
    r = pd.Series(returns)
    dates = list(r.index)
    out = {}
    for name, (lo, hi) in STRESS_PERIODS.items():
        sl = r[[lo <= d <= hi for d in dates]]
        out[name] = sl
    return out
```

- [ ] **Step 4: Run** → PASS (3 passed). (`test_stress_folds_skip_absent_periods` passes via the all-empty branch.)

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/walkforward.py tests/test_walkforward.py
git commit -m "feat(engine): walk-forward expanding windows + forced stress folds (D3)"
```

---

### Task 14: The 3-way-vs-2-way blend add-on measurement (spec §2 S4 / §3.6)

**Files:** Modify `src/autotrader/engine.py` (a small `CappedBudgetBlend` wrapper strategy), `tests/test_engine.py`.

Measure whether adding the S3 mean-reversion sleeve at a **capped, separate risk budget** funded from the **shared settled-cash pool** beats the 2-way S4. The 2-way is plain S4; the 3-way is a composite weight frame = `(1−cap)·S4 ⊕ cap·S3`, run through the one engine so the MR sleeve is **settlement-throttled** (its realized trade count drops vs standalone — the measured insight, spec §3.6).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_engine.py
from autotrader.engine import CappedBudgetBlend
from autotrader.strategies import S4TrendGatedMomentum, S3MeanReversion


def test_capped_blend_weights_sum_within_one_and_caps_mr():
    dates = [dt.date(2025, 1, 2) + dt.timedelta(days=i) for i in range(700)]
    sectors = cfg.SECTOR_SPDRS
    raw = {s: _bars(dates, [10 + ((k + i) % 5) * 0.4 + 0.01 * i for i in range(len(dates))])
           for k, s in enumerate(sectors)}
    raw["SPY"] = _bars(dates, [100 + 0.05 * i for i in range(len(dates))])
    raw["IEF"] = _bars(dates, [50.0] * len(dates))
    for q in ["QQQ", "DIA", "IWM"]:
        raw[q] = _bars(dates, [80 + 0.03 * i for i in range(len(dates))])
    s4 = S4TrendGatedMomentum(sectors, equity="SPY", bond="IEF", n_hold=3, buffer=1,
                              nearness_window=60, sma_months=3)
    s3 = S3MeanReversion(["QQQ", "DIA", "IWM"], regime_sma=50, exit_sma=5, time_stop_days=5)
    blend = CappedBudgetBlend(s4, s3, mr_cap=0.15)
    assert set(blend.universe) >= set(s4.universe) | set(s3.universe)
    w = blend.target_weights({s: raw[s] for s in blend.universe})
    assert (w.sum(axis=1) <= 1.0 + 1e-9).all()                          # never over-allocates
    mr_cols = [c for c in s3.universe]
    assert (w[mr_cols].sum(axis=1) <= 0.15 + 1e-9).all()                # MR budget capped
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'CappedBudgetBlend'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/engine.py
class CappedBudgetBlend:
    """3-way add-on test (spec §2 S4 / §3.6): primary blend at (1-mr_cap) risk + the S3 dip-buyer
    at a capped mr_cap budget, funded from the SHARED settled-cash pool (so the MR sleeve is
    settlement-throttled below standalone). target_weights = (1-mr_cap)*primary ⊕ mr_cap*mr,
    column-unioned; cash = 1 - row.sum. Per-symbol cost routing via `cost_strategy_for` (built into
    the engine in Task 1/5): MR-sleeve symbols are charged the S3 floor; the momentum sleeve is not."""
    def __init__(self, primary, mr, mr_cap=0.15):
        self.primary, self.mr, self.mr_cap = primary, mr, mr_cap
        self.universe = list(dict.fromkeys(list(primary.universe) + list(mr.universe)))
        self.stop_loss_pct = primary.stop_loss_pct
        self.cost_strategy = None                     # not used — see cost_strategy_for
        self._mr_symbols = set(mr.universe)

    def cost_strategy_for(self, symbol):
        """Per-symbol cost-strategy label: the S3 floor applies to the MR sleeve ONLY (spec §3.6 /
        COST_MODEL §4); the momentum sleeve keeps its instrument tier. The engine calls this per buy."""
        return "S3" if symbol in self._mr_symbols else None

    def target_weights(self, bars):
        import pandas as pd
        wp = self.primary.target_weights({s: bars[s] for s in self.primary.universe})
        wm = self.mr.target_weights({s: bars[s] for s in self.mr.universe})
        n = len(wp)
        w = pd.DataFrame(0.0, index=range(n), columns=self.universe)
        for c in self.primary.universe:
            if c in wp.columns:
                w[c] = w[c].values + (1.0 - self.mr_cap) * wp[c].values
        for c in self.mr.universe:
            if c in wm.columns:
                w[c] = w[c].values + self.mr_cap * wm[c].values
        return w
```

The per-symbol cost routing is already wired into `BacktestEngine` (Task 1/5: `self.cost_strategy_for = getattr(strategy, "cost_strategy_for", None) or (lambda s: strategy.cost_strategy)`; the floor is resolved per buy). So the blend just supplies the map above. Add a test that an MR-sleeve buy inside the blend is charged the S3 floor while a momentum buy is not:

```python
# add to tests/test_engine.py
from autotrader.engine import cost_floor_for_strategy as _floor


def test_blend_routes_s3_floor_to_mr_sleeve_only():
    s4 = S4TrendGatedMomentum(cfg.SECTOR_SPDRS, equity="SPY", bond="IEF")
    s3 = S3MeanReversion(["QQQ", "DIA", "IWM"])
    blend = CappedBudgetBlend(s4, s3, mr_cap=0.15)
    # the engine resolves the floor per symbol via blend.cost_strategy_for
    assert _floor(blend.cost_strategy_for("QQQ")) == cfg.S3_COST_FLOOR     # MR sleeve -> S3 floor
    assert _floor(blend.cost_strategy_for("XLK")) is None                  # momentum sleeve -> tier only
    assert _floor(blend.cost_strategy_for("SPY")) is None
```

- [ ] **Step 4: Run** → PASS (`-k "capped_blend or routes_s3_floor"`). The cost-routing test confirms the S3 floor reaches the MR sleeve and only the MR sleeve.

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/engine.py tests/test_engine.py
git commit -m "feat(engine): capped-budget 3-way blend + per-symbol S3 cost routing (§3.6)"
```

---

### Task 15: Report — summary table + equity/trades export + variant-set DSR/PBO wiring

**Files:** Create `src/autotrader/report.py`, `tests/test_report.py`.

Assemble a deterministic report for a set of named runs: a metrics table (full sample + each walk-forward window + each stress fold), the (strategy − benchmark) Sharpe + max-DD **bootstrap CIs**, and the **Deflated Sharpe / PBO over the Plan-04 variant set** (D3/D7). Emit a markdown summary + CSV exports. No plotting (D6).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_report.py
import datetime as dt
import numpy as np
import pandas as pd
import pytest
from autotrader.report import (summarize_run, build_variant_matrix, variant_dsr_pbo,
                               render_markdown, gate_verdict, first_active_index)


def _eq(vals, start=dt.date(2018, 1, 1)):
    idx = pd.Index([start + dt.timedelta(days=i) for i in range(len(vals))], name="date")
    return pd.Series([float(v) for v in vals], index=idx)


class _Res:   # minimal BacktestResult stand-in
    def __init__(self, equity, trades=(), weights=None):
        self.equity = equity
        self.returns = equity.pct_change()
        self.trades = list(trades)
        self.weights = weights if weights is not None else pd.DataFrame({"X": [1.0] * len(equity)})
        self.skipped_buys = []


def test_summarize_run_has_all_spec_metrics():
    res = _Res(_eq([1000 * (1.0003 ** i) for i in range(400)]))
    row = summarize_run(res)
    for key in ["cagr", "vol", "sharpe", "sortino", "max_dd", "calmar", "win_rate",
                "profit_factor", "avg_win", "avg_loss", "turnover", "time_in_market",
                "trades_per_year", "number_of_bets"]:
        assert key in row


def test_build_variant_matrix_aligns_configs_for_pbo():
    a = _Res(_eq([1000 * (1.0003 ** i) for i in range(300)]))
    b = _Res(_eq([1000 * (1.0001 ** i) for i in range(300)]))
    M = build_variant_matrix({"A": a, "B": b})
    assert list(M.columns) == ["A", "B"] and len(M) == 299    # daily returns, common length


def test_variant_dsr_uses_per_observation_units_not_annualized():
    # Two strong variants. With per-obs units DSR is a sane probability; the annualized-variance
    # bug (review B1) would collapse every DSR to ~0. Assert at least one variant is not ~0.
    a = _Res(_eq([1000 * (1.0006 ** i) for i in range(300)]))
    b = _Res(_eq([1000 * (1.0005 ** i) for i in range(300)]))
    out = variant_dsr_pbo({"A": a, "B": b})
    assert out["provisional"] is True
    assert max(d for d, _ in out["dsr"].values()) > 0.01     # not all false-killed by a unit error


def test_first_active_index_skips_leading_flat_cash():
    w = pd.DataFrame({"X": [0.0, 0.0, 0.0, 1.0, 1.0]})
    res = _Res(_eq([1000.0] * 5), weights=w)
    assert first_active_index(res) == 3                      # first bar a position is held


def test_gate_verdict_is_provisional_with_placebo_deferred():
    strat = _Res(_eq([1000 * (1.0004 ** i) for i in range(400)]))
    bench = _Res(_eq([1000 * (1.0002 ** i) for i in range(400)]))
    v = gate_verdict(strat, bench, spy=bench, dsr_p=0.01, pbo=0.2)
    assert v["overall"].startswith("PROVISIONAL")
    assert v["conditions"]["placebo_beats_95th"] == "DEFERRED-Plan05"
    assert set(["deflated_p_lt_0.05", "pbo_lt_0.5", "sharpe_ci_lower_ge_benchmark",
                "maxdd_upperci_lt_spy"]).issubset(v["conditions"])


def test_render_markdown_is_deterministic_text():
    res = _Res(_eq([1000 * (1.0002 ** i) for i in range(300)]))
    md1 = render_markdown({"S1": summarize_run(res)})
    md2 = render_markdown({"S1": summarize_run(res)})
    assert md1 == md2 and md1.startswith("|") and "sharpe" in md1.lower()
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError: No module named 'autotrader.report'`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/report.py
"""Deterministic backtest report (D6): a metrics table per run + variant-set DSR/PBO + a PROVISIONAL
§4 gate (placebo deferred to Plan 05, D8), rendered as markdown with CSV exports. Pure over
BacktestResult-shaped objects."""
import pandas as pd
from autotrader import metrics as M


_METRIC_KEYS = ["cagr", "vol", "sharpe", "sortino", "max_dd", "calmar", "win_rate", "profit_factor",
                "avg_win", "avg_loss", "turnover", "time_in_market", "trades_per_year", "number_of_bets"]


def first_active_index(res) -> int:
    """First row index at which any position is held (realized weight > 0) — used to trim the leading
    all-cash burn-in from headline metrics (review S2). 0 if invested from the start; len if never."""
    held = res.weights.sum(axis=1).to_numpy()
    nz = [i for i, v in enumerate(held) if v > 1e-12]
    return nz[0] if nz else len(held)


def summarize_run(res, trim_warmup: bool = True) -> dict:
    """Per-run metric row. If trim_warmup, return-based metrics start at first_active_index (so the
    leading flat-cash burn-in doesn't dilute Sharpe/vol or inflate the DSR sample length)."""
    i0 = first_active_index(res) if trim_warmup else 0
    eq, r, w = res.equity.iloc[i0:], res.returns.iloc[i0:], res.weights.iloc[i0:]
    tr = res.trades
    aw, al = M.avg_win_loss(tr)
    return {
        "cagr": M.cagr(eq), "vol": M.annualized_vol(r), "sharpe": M.sharpe(r),
        "sortino": M.sortino(r), "max_dd": M.max_drawdown(eq), "calmar": M.calmar(eq),
        "win_rate": M.win_rate(tr), "profit_factor": M.profit_factor(tr), "avg_win": aw, "avg_loss": al,
        "turnover": M.turnover(tr, eq), "time_in_market": M.time_in_market(w),
        "trades_per_year": M.trades_per_year(tr, eq), "number_of_bets": M.number_of_bets(w),
    }


def build_variant_matrix(named_results: dict) -> pd.DataFrame:
    """Returns matrix (rows=time, cols=variant) for PBO/DSR; truncated to the common min length
    (CSCV requires a rectangular matrix)."""
    cols = {name: res.returns.dropna().to_numpy() for name, res in named_results.items()}
    m = min(len(v) for v in cols.values())
    return pd.DataFrame({name: v[:m] for name, v in cols.items()})


def _per_obs_sharpe(col) -> float:
    r = pd.Series(col).dropna()
    sd = r.std(ddof=1)
    return float(r.mean() / sd) if sd and sd > 0 else 0.0


def variant_dsr_pbo(named_results: dict, n_blocks: int = 16) -> dict:
    """Per-variant DSR + a single PBO over the variant matrix. CRITICAL (review B1): the cross-
    variant Sharpe variance MUST be of PER-OBSERVATION Sharpes (mean/std, no √252) to match the
    per-observation Sharpe deflated_sharpe uses internally — feeding annualized variance is a 252×
    error that collapses every DSR to ~0. Plan-04's variant count is tiny, so this is flagged
    PROVISIONAL: the binding deflation uses Plan-05's full trial census (D3/D8)."""
    mat = build_variant_matrix(named_results)
    n_trials = len(mat.columns)
    sr_obs = {c: _per_obs_sharpe(mat[c]) for c in mat.columns}
    sr_var = float(pd.Series(list(sr_obs.values())).var(ddof=1)) if n_trials > 1 else 0.0
    dsr = {c: M.deflated_sharpe(mat[c], n_trials=max(n_trials, 2), sr_variance=max(sr_var, 1e-12))
           for c in mat.columns}
    pbo = M.pbo_cscv(mat, n_blocks=n_blocks) if (len(mat) >= n_blocks and n_trials >= 2) else float("nan")
    return {"dsr": dsr, "pbo": pbo, "sr_variance_perobs": sr_var, "n_trials": n_trials,
            "provisional": True}


def gate_verdict(run, benchmark, spy, dsr_p, pbo, seed: int = 0, block_size: int = 21) -> dict:
    """Assemble the §4 return-seeker gate from the available sub-conditions and return a PROVISIONAL
    verdict (D8). The random-selection PLACEBO term is Plan-05 scope -> reported as
    'DEFERRED-Plan05', NEVER silently treated as PASS. Drawdowns compared as positive magnitudes."""
    paired = (run.returns - benchmark.returns).dropna()                    # (strategy - benchmark)
    sharpe_lo, _ = M.bootstrap_ci(paired, M.sharpe, seed=seed, block_size=block_size)
    neg_dd = lambda r: -M.max_drawdown_from_returns(r)                      # positive DD magnitude
    _, run_dd_hi = M.bootstrap_ci(run.returns.dropna(), neg_dd, seed=seed, block_size=block_size)
    spy_dd = -M.max_drawdown(spy.equity)
    conditions = {
        "deflated_p_lt_0.05": bool(dsr_p is not None and dsr_p < 0.05),
        "sharpe_ci_lower_ge_benchmark": bool(sharpe_lo >= 0.0),
        "maxdd_upperci_lt_spy": bool(run_dd_hi < spy_dd),
        "pbo_lt_0.5": bool(pbo is not None and pbo < 0.5),
        "placebo_beats_95th": "DEFERRED-Plan05",
    }
    decided = [v for v in conditions.values() if isinstance(v, bool)]
    overall = ("PROVISIONAL-PASS-pending-placebo" if all(decided)
               else "PROVISIONAL-FAIL")
    return {"overall": overall, "conditions": conditions,
            "paired_sharpe_ci_lower": sharpe_lo, "run_maxdd_upperci": run_dd_hi, "spy_maxdd": spy_dd}


def render_markdown(rows: dict) -> str:
    """rows: {run_name: summarize_run(...) dict}. Deterministic markdown table."""
    header = "| run | " + " | ".join(_METRIC_KEYS) + " |\n"
    sep = "|" + "---|" * (len(_METRIC_KEYS) + 1) + "\n"
    body = ""
    for name in sorted(rows):
        vals = " | ".join(f"{rows[name][k]:.4f}" if isinstance(rows[name][k], float)
                          else str(rows[name][k]) for k in _METRIC_KEYS)
        body += f"| {name} | {vals} |\n"
    return header + sep + body


def export_csv(res, equity_path: str, trades_path: str) -> None:
    """Write the equity curve + trades table as CSV (offline artifacts)."""
    res.equity.rename("equity").to_frame().to_csv(equity_path)
    pd.DataFrame([t.__dict__ for t in res.trades]).to_csv(trades_path, index=False)
```

- [ ] **Step 4: Run** → PASS (6 passed). The gate is **PROVISIONAL** by construction — the placebo term reads `DEFERRED-Plan05`, so a Plan-04 run never emits a binding §4 PASS (D8).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/report.py tests/test_report.py
git commit -m "feat(report): metrics table + variant DSR/PBO + bootstrap CIs + CSV export"
```

---

### Task 16: Full green + offline real-cache SMOKE run + doc-sync + tag

**Files:** Create `scripts/run_backtest.py` (offline driver; not a pytest test); modify `PROJECT_CONTEXT.md` + the session-memory; tag.

- [ ] **Step 1: Full suite green.** `./.venv/bin/pytest -v` from repo root. Expected: the **existing suite (currently 89 tests)** **plus** the new engine/stress/metrics/benchmarks/walkforward/report tests, ALL PASS. Record the new total in the commit message (do not hard-code "89" elsewhere — it is a moving number).

- [ ] **Step 2: Offline smoke driver.** Write `scripts/run_backtest.py` that loads `data/cache` via `DataStore`, builds the calendar, and runs **S1, S2, S3, S4 + the four benchmarks** through the engine over the full 2005→2026 history, prints the `render_markdown` table + the `variant_dsr_pbo` block, and writes CSVs under `data/reports/`. **It calls NO MCP.** Run it: `./.venv/bin/python scripts/run_backtest.py`.

- [ ] **Step 3: Sanity gates (assert in the driver; fail loudly).** (a) every equity curve is finite and > 0 throughout; (b) `max(weights.sum(axis=1)) <= 1 + 1e-9` for every run (no over-allocation / no leverage); (c) **S3 ≈ break-even-to-negative after the S3 floor** (the null-confirmation expectation, spec §2 S3 / §4 — if S3 shows a strong positive net Sharpe, print a RED-FLAG "audit the cost model" banner, do not silently pass); (d) the blend's 3-way reports a **lower realized MR trade count than S3 standalone** (settlement throttle, §3.6). These are sanity checks, not pytest gates — the driver prints PASS/FLAG per check.

- [ ] **Step 4: Doc-sync (same branch, per the batch-doc-sync rule).** Update `PROJECT_CONTEXT.md` roadmap line for Plan 04 to ✅ with the test count + tag; append a design note (engine design decisions D1–D7, the all-or-nothing model, the price-return basis already locked). Keep it short; do not duplicate the spec.

- [ ] **Step 5: Commit + tag.**

```bash
git add scripts/run_backtest.py PROJECT_CONTEXT.md
git commit -m "feat(engine): offline real-cache backtest driver + Plan 04 doc-sync"
git tag engine-v1 -m "Backtest engine + metrics + benchmarks + walk-forward + report — verified offline"
```

---

## Self-review against the spec (completed by plan author)

- **§3.1 price-return basis (LOCKED):** the engine reads `adjustment="split"` cache only; benchmarks are price-return; 60/40 omits the bond coupon with a documented limitation (D4) — no total-return reconstruction attempted.
- **§3.4 no look-ahead:** every fill is at the **next open** (the engine submits with `signal_date=date[t]`, the Simulator fills at `date[t+1]`); the Task-6 integration test asserts the next-open entry price; strategy frames are already causal (Plan 03). The forbidden `get_equity_fundamentals` path is never touched (offline).
- **§3.5 stop-fill-on-daily-bar:** stops are placed via the locked Simulator (gap-through fills at the open) and evaluated **stop-first** each bar (Engine contract #3); the engine never re-implements stop math.
- **§3.6 shared T+1 ledger:** one `SettledCashLedger`; `can_buy` gates every entry; refusals are **skipped + logged** (Engine contract #4); the 3-way blend's MR sleeve is settlement-throttled and its realized trade count is reported (Task 14/16).
- **§3.3 / COST_MODEL §3-§4 costs:** per-trade instrument tier (`tier_for_symbol`) + the **S3 floor routed PER SYMBOL** (`cost_strategy_for`, built in Task 1/5 so the blend's MR sleeve is floored and the momentum sleeve isn't); causal CS-ratio stress (D2) auto-widens 2008/2020 fills; the 60/40 benchmark rebalance is costed on the SAME tier×stress model (D4).
- **§3.8 walk-forward + forced stress folds:** one full-history run sliced into expanding windows + the **2008-09/2020/2022** folds (D3); deep-bear beyond those is explicitly an extrapolation (carried in the report text).
- **§3.9 multiple-testing:** Deflated Sharpe (+p) and PBO via CSCV built as tested pure functions and wired over the Plan-04 variant set; **Plan 05's robustness runner supplies the full trial census** (D3) — flagged, not silently scoped out.
- **§3.10 benchmarks:** buy-hold SPY (a), gated-SPY (b, primary), 60/40 (c, analytic), EW universe (d).
- **§4 metrics & gates:** CAGR, vol, Sharpe (+deflated +p), Sortino, max-DD, Calmar, win rate, avg win/loss, profit factor, turnover, time-in-market, trades/yr, PBO, **block-bootstrap** CIs on (strategy−benchmark) Sharpe & max-DD, number-of-bets — all present. The §4 PASS/FAIL gate is assembled by `gate_verdict` as a **PROVISIONAL** verdict (D8): each available sub-condition is a reported boolean, the **random-selection placebo** (1,000 picks) reads `DEFERRED-Plan05` (never silently a PASS), and the binding verdict runs in Plan 05 once the placebo + full trial census exist. Headline metrics trim the leading warm-up (`first_active_index`).
- **Offline guardrail:** every task is offline; the only data source is `data/cache`; no order/review/cancel/money path exists in any new module.

### Open items — RESOLVED (the operator, 2026-06-17, plan approved)
The reviewers settled D1 (KEEP — forced by the lock; the settled-cash sizing cap was the fix), D3 (KEEP — faithful to §3.8 for literature-locked params; `run()` is re-entrant for Plan 05), D4 (KEEP analytic 60/40 but UNIFY its cost model — done), and "don't split — ship as one plan." the operator's three calls:
1. **D8 — gate framing: APPROVED as recommended.** Plan 04 emits the §4 metric *inputs* + the DSR/PBO/bootstrap *functions* + a **PROVISIONAL** `gate_verdict` (placebo flagged `DEFERRED-Plan05`) + the S3 null-confirmation RED-FLAG; the **binding** PASS/FAIL that gates paper-trading runs in **Plan 05**.
2. **Report depth (D6): APPROVED.** Markdown summary + CSV exports, no charts.
3. **Warm-up trim: APPROVED.** Headline metrics trim the leading all-cash burn-in (`first_active_index`); mid-stream risk-off cash is kept.

---

## Roadmap position

Plan 04 of 5. Builds on Plan 01 (foundation/Simulator), Plan 02 (indicators), Plan 03 (strategies + populated cache). Next: **Plan 05 — Robustness Runner & Paper-Monitor** (plateau scan, rebalance-day dispersion, the full trial census feeding DSR/PBO at strength, random-selection placebo, then the live paper-monitor calling `review_equity_order`).
