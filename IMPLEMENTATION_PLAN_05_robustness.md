# Robustness Runner — Implementation Plan (Plan 05a of the roadmap) — v1.1 (three-reviewed)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Reviewed (v1.1) — three independent adversarial reviews (statistical validity, integration/executability, scope/lock-in). Two reviewers built + ran the code on the real cache. Blocking findings fixed in this revision:**
- **`gate=off` was a dead all-cash cell that crashed the driver (integration + methodology, reproduced).** Modeling "no gate" as `gate_sma_months=1` makes the locked `trend_regime` never turn on (the 1-bar SMA equals the price, so `close ≥ SMA·(1+band)` is never true), so all 12 S2 gate-off cells never invest — and `report.summarize_run`'s warm-up trim then indexes an empty slice (`IndexError`). **Fixed:** "gate off" is now `(sma_months=1, band=0.0)` (gate ~always-on, verified 5377/5396 days), and every runner Sharpe uses `summarize_run(..., trim_warmup=False)` (crash-safe on all-cash curves) — Tasks 1/4/6/10.
- **Deflated-Sharpe null was mis-specified (methodology, numerically demonstrated).** Taking the per-family best cell but deflating by the GLOBAL N=114 and a cross-family-contaminated Sharpe variance V (mixing S3≈0 with S2≈0.7) inflates the hurdle and can flip a genuine edge from PASS to KILL. **Fixed:** per-family N + within-family V (Task 6); the global "whole-search" deflation is reported as a separate labeled line, not the binding one (E5).
- **Correlated / byte-identical grid cells corrupted PBO + the trial count (both).** The 20% stop never fires on SPY/IEF → S1 has 12 identical column pairs → CSCV ranks degenerate (PBO biased to 0.5, a hard auto-kill) and N over-counts. **Fixed:** `census_dsr_pbo` de-duplicates byte-identical return columns within a family before PBO/DSR and reports the distinct-cell count (Task 6, E5).
- **Runtime was ~10× the estimate (scope, measured ~5 hr).** ~74% of each run is the engine re-computing the per-symbol stress series. **Fixed:** the runner precomputes the stress once per universe and passes it via the engine's existing `stress=` param across all placebo seeds + grid cells (verified 3.8× speedup, equity identical to 1e-6 → full run ≈ 75 min) — Tasks 2/4, E6.
- **Placebo made fair:** it now uses the SAME rank-hysteresis as S2/S4 (random rank order instead of nearness rank) so turnover/cost match — beating it isolates *selection skill*, not S2's lower churn; and its warm-up/off-asset path now mirrors S4 exactly (Task 3, E2). **S3 null** now requires the cell to have actually traded before "NULL-CONFIRMED" (a never-fired cell is "INACTIVE", not a confirmed null). **S1 clause** adds the spec's "material DD reduction + acceptable CAGR cost" guard. **S4** gains its stop on/off axis (→ 132 cells).
- **Scope:** all three reviewers said **DEFER rebalance-day dispersion** (the old Task 9) — it gates nothing (a §6/§3.8 diagnostic, not a §4 gate term), so it is **not worth touching locked model code**; deferred to whenever the strategies are next revised. The plan now has **9 tasks, no locked-code touch**. The ~114→132-cell grid is KEPT (the DSR is the forgiving term at daily T, not vacuous — verified).

**Goal:** Build the offline **robustness runner** that turns Plan 04's *provisional* §4 verdict into the **binding kill/keep decision**: enumerate the full parameter-grid **trial census**, re-run the (re-entrant) Plan-04 engine over every cell, compute the **census-strength Deflated Sharpe + PBO** per strategy, run the **1,000-pick random-selection placebo** for the selection strategies, scan each soft knob's **plateau** (robustness is a plateau, not a peak), and assemble the **binding §4 gate** (spec §4 + §3.9) with auto-kill thresholds. Output: a per-strategy PASS/FAIL the project can act on.

**Architecture:** A thin deterministic layer over already-built, already-locked parts. The Plan-04 `BacktestEngine` is re-entrant (all soft knobs are constructor args), so the runner just enumerates configs, runs each through the engine, and feeds the resulting return matrices into the already-tested `metrics.deflated_sharpe` / `metrics.pbo_cscv` / `metrics.bootstrap_ci` and `report.gate_verdict`. The only genuinely new modelling object is the **random-selection placebo strategy**. Everything is offline, deterministic (seeded), and runs over the local cache.

**Tech Stack:** Python 3.11, pandas, numpy, stdlib. pytest. No new dependency. The big batch run is offline and cached; tests use tiny synthetic grids + short fixtures.

**Reads (source of truth):** [`STRATEGY_TESTING_SPEC.md`](STRATEGY_TESTING_SPEC.md) §3.8 (anchored walk-forward + rebalance-timing dispersion), §3.9 (multiple-testing: enumerate the trial count, Deflated Sharpe + PBO for every strategy, **auto-kill PBO ≥ 0.5 or deflated-p ≥ 0.05**, "robustness is a plateau, not a peak"), §4 (the 5-condition gate incl. the **random-selection placebo** — beat the 95th percentile of 1,000 random picks from the same universe under the same gate), §6 (build step 6 — the runner: plateau scan, with/without stop & gate, rebalance-day dispersion on day 1/8/15/22). [`IMPLEMENTATION_PLAN_04_engine.md`](IMPLEMENTATION_PLAN_04_engine.md) §1 (D1-D8; the engine is all-or-nothing + re-entrant; the §4 gate is PROVISIONAL pending this plan).

**Integrates (ALREADY BUILT — do NOT modify; import and drive):** `engine.py` (`BacktestEngine`, `BacktestResult`, `CappedBudgetBlend`, `tier_for_symbol`), `strategies.py` (`S1Trend/S2SectorMomentum/S3MeanReversion/S4TrendGatedMomentum`, `trend_regime`), `metrics.py` (`deflated_sharpe`, `pbo_cscv`, `bootstrap_ci`, `sharpe`, `max_drawdown_from_returns`), `report.py` (`summarize_run`, `build_variant_matrix`, `_per_obs_sharpe`, `gate_verdict`, `render_markdown`), `benchmarks.py` (`GatedSPY`, `BuyHold`), `walkforward.py`, `indicators.py` (`monthly_closes`), `config.py`, `datastore.py`, `calendar_nyse.py`.

---

## 0. Scope boundary (read first)

- **This plan is the ROBUSTNESS RUNNER only** (Plan 05a). The **paper-monitor** (live, read-only, forward) is a separate later plan (Plan 05b) — it is the *only* MCP-touching phase and is explicitly out of scope here. Nothing in this plan calls the MCP or trades.
- **The binding gate is the deliverable.** Rebalance-day dispersion (§6) is a *diagnostic*, not one of the 5 §4 gate conditions — **DEFERRED** (all three reviews): it would require touching locked model code for a check that gates nothing, so it waits until the strategies are next legitimately revised. **No task in this plan touches a locked module.**

---

## 1. NEW design decisions (NOT in the locked specs — surfaced for the three reviews)

- **E1 — The trial-census grid (principled neighborhoods, not a fishing expedition).** The grid is the literature-locked defaults **plus their immediate neighborhood** on each soft knob, so the deflation reflects the real search and the plateau scan has cells to compare. Default grid (each knob a small odd neighborhood around the Plan-03 default):
  - **S1:** `sma_months ∈ {8,10,12}`, `band ∈ {0.0, 0.01, 0.02}`, `use_abs_momentum ∈ {False,True}`, `stop_loss_pct ∈ {None, 0.20}` → 36 cells.
  - **S2:** `n_hold ∈ {2,3,4}`, `buffer ∈ {1,2}`, `gate ∈ {on, off}`, `stop_loss_pct ∈ {None, 0.20}` → 24 cells.
  - **S3:** `regime_sma ∈ {150,200,250}`, `cumrsi_entry ∈ {30,35,40}`, `time_stop_days ∈ {5,10}`, `stop_loss_pct ∈ {None, 0.10}` → 36 cells.
  - **S4:** `n_hold ∈ {2,3,4}`, `buffer ∈ {1,2}`, `sma_months ∈ {8,10,12}`, `stop_loss_pct ∈ {None, 0.20}` → 36 cells (the stop axis was added after review — the primary blend must be tested with/without its stop).
  - **132 configs total** (the enumerated trial census). KEPT at ~this width after review (the Deflated Sharpe is the *forgiving* term at daily T≈5,000 — `SR0` grows only logarithmically in N — so this does not vacuously kill everything; the binding constraints are the placebo + the Sharpe-CI vs gated-SPY). **De-dup caveat:** byte-identical cells (e.g. a stop that never fires → stop-on ≡ stop-off) are collapsed before PBO/DSR (E5) so the count and the CSCV ranks are honest. Grid sizes are parameters (`scale="small"` for tests, `"full"` for the run).
- **E2 — The random-selection placebo (a FAIR selection-skill test).** `RandomSelection(sectors, gate_symbol, n_hold, buffer, seed, off_asset)` mirrors S2/S4 **exactly except the rank is random**: each rebalance month it draws a seeded random permutation of the sectors and runs it through the **same `select_with_hysteresis(random_order, held, n_hold, buffer)`** the real strategies use — so the placebo's **turnover/cost match the strategy's hysteresis-damped churn** (review fix: a no-hysteresis placebo churns far more, so beating it would conflate selection skill with S2's lower cost). Its gate/warm-up/off-asset path mirrors S4 (gate off → off-asset; gate-on-but-warming → cash; warmed → basket). Run **1,000 seeds** through the engine → 1,000 net Sharpes → the **95th percentile**; the strategy must beat it (spec §4). `n_placebo` is a parameter (default 1,000; tests ~20). Cost is the runner's bulk — see E6's precompute fix.
- **E3 — Rebalance-day dispersion: DEFERRED (all three reviews).** §6 wants each monthly strategy run on day 1/8/15/22 to expose timing luck, but the monthly decision lives inside the **locked** strategies (`monthly_closes` = last trading day), so shifting it cleanly would touch locked model code (an additive `rebalance_day` knob) — for a check that is a **diagnostic, not one of the 5 §4 gate conditions**. It gates nothing, so it is **deferred** to whenever the strategies are next legitimately revised. **No task here touches a locked module.**
- **E4 — Binding gate = reuse `report.gate_verdict` + the real placebo term.** `robustness.binding_verdict(...)` calls the Plan-04 `gate_verdict` to get the four non-placebo booleans, replaces the `"DEFERRED-Plan05"` placebo term with the **real** boolean (`strategy_sharpe > placebo_95th`), and recomputes `overall` as a **binding** `PASS`/`FAIL` — PASS iff all five conditions are True. The §3.9 auto-kill on `pbo ≥ 0.5` / `deflated_p ≥ 0.05` is enforced *because* `pbo_lt_0.5` and `deflated_p_lt_0.05` are two of the five AND-ed conditions (no separate short-circuit). One gate function, no logic duplicated.
- **E5 — DSR/PBO per family, consistent null, de-duplicated (review-corrected).** For each family the **best cell** is deflated by a **consistent within-family null**: `N = that family's distinct-cell count`, `V = variance of per-observation Sharpes across that family's distinct cells` (review fix: deflating a per-family max by a *global* N and a *cross-family-mixed* V is the wrong null — mixing S3≈0 with S2≈0.7 inflates V and can KILL a real edge). **PBO via CSCV** is over the family's **distinct** config matrix. **De-dup:** byte-identical return columns (e.g. a never-firing stop → stop-on ≡ stop-off) are collapsed first, so neither N nor the CSCV OOS-ranks are corrupted by exact duplicates. A separate **"whole-search" line** (global N=132, global V) is reported as a *secondary, more-conservative* diagnostic — clearly labeled, NOT the binding number. The report states each family's distinct-cell count so the effective trial count is visible; near-duplicate (not byte-identical) neighbors remain and are honest neighborhood-stability checks (the spec's "plateau" intent), not an independent-trials claim.
- **E6 — Determinism + runtime (precompute the stress — review fix).** Every random draw is seeded; the run is reproducible (the engine's default stress is RNG-free). **~74% of each engine run is `BacktestEngine.__init__` recomputing the per-symbol causal stress series** (measured), and all 1,000 placebo seeds share one `raw_bars`/universe → the runner **precomputes the stress once and passes it via the engine's existing `stress=` parameter** (verified 3.8× speedup, equity identical to 1e-6). Full run ≈ **~75 min** single-threaded (132 grid + ~2,000 placebo runs); a `--quick` flag (`scale="small"`, `n_placebo≈50`) gives a ~1-min dev loop. Results cached to `data/robustness/` (gitignored). Single-threaded is acceptable WITH the precompute; no multiprocessing, no new dependency.
- **E7 — "Plateau, not a peak."** For each soft knob, `plateau(results, family, knob)` reports the median Sharpe across the knob's neighborhood; robust = a **plateau** (low spread AND a decent *level* — both reported), not a lone peak. A *reported diagnostic* feeding the human read, not a hard gate term (the hard kill terms are PBO + deflated-p).

> **Reviewers (v1.1 outcome):** E1 (grid) — KEPT ~132 (DSR not vacuous at daily T) + S4 stop axis + de-dup. E2 (placebo) — made fair (hysteresis-matched, S4-mirrored warm-up). E3 (dispersion) — DEFERRED, no locked-code touch. E5 (null) — per-family N+V, de-duplicated. E6 (runtime) — stress precompute (~75 min).

---

## 2. File structure (locked here)

```
src/autotrader/robustness.py    # build_strategy, strategy_grid, run_grid, plateau, census_dsr_pbo, binding_verdict, family_verdict, render_robustness
src/autotrader/placebo.py       # RandomSelection strategy + placebo_distribution / placebo_95th
scripts/run_robustness.py       # offline batch driver over data/cache (the big run); not a pytest test
tests/test_robustness.py
tests/test_placebo.py
data/robustness/                # gitignored result artifacts
```

No new dependency. `report.gate_verdict` is reused (not modified). **No task touches a locked module** (rebalance-day dispersion, the only candidate for a locked-code touch, is deferred — E3).

---

## PHASE A — The trial-census grid (configs → return matrices)

### Task 1: `build_strategy` + `strategy_grid`

**Files:** Create `src/autotrader/robustness.py`, `tests/test_robustness.py`.

Pure config enumeration: a dispatcher that builds a strategy from `(family, params)`, and a grid generator producing the census of config dicts. No engine, no I/O.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_robustness.py
import pytest
from autotrader import config
from autotrader.robustness import build_strategy, strategy_grid
from autotrader.strategies import S1Trend, S2SectorMomentum, S3MeanReversion, S4TrendGatedMomentum


def test_build_strategy_dispatches_each_family():
    s1 = build_strategy("S1", {"sma_months": 8, "stop_loss_pct": None})
    assert isinstance(s1, S1Trend) and s1.sma_months == 8 and s1.stop_loss_pct is None
    s2 = build_strategy("S2", {"n_hold": 2, "buffer": 1})
    assert isinstance(s2, S2SectorMomentum) and s2.n_hold == 2 and s2.sectors == config.SECTOR_SPDRS
    s3 = build_strategy("S3", {"regime_sma": 150})
    assert isinstance(s3, S3MeanReversion) and s3.regime_sma == 150 and s3.etfs == config.INDEX_ETFS
    s4 = build_strategy("S4", {"n_hold": 4, "sma_months": 12})
    assert isinstance(s4, S4TrendGatedMomentum) and s4.sma_months == 12


def test_build_strategy_gate_off_is_no_filter_not_all_cash():
    # "gate off" = NO trend filter (gate ~always on), NOT permanently cash. Assert BEHAVIOR, not just
    # the param — review B1 (a band=0.01 + sma=1 'off' makes trend_regime never fire -> all-cash crash).
    import datetime as dt
    import pandas as pd
    from autotrader.strategies import trend_regime
    s2_off = build_strategy("S2", {"n_hold": 3, "gate": "off"})
    s2_on = build_strategy("S2", {"n_hold": 3, "gate": "on"})
    assert (s2_off.gate_sma_months, s2_off.gate_band) == (1, 0.0)
    assert (s2_on.gate_sma_months, s2_on.gate_band) == (10, 0.01)
    # 500-day window: trend_regime is a MONTHLY system (daily signal forward-filled from month-ends),
    # so the ~29 days before the first month-end decision are unavoidably OFF (warm-up). A 120-day
    # window made that warm-up 24% of the span (caps at 91/120 = 75.8% < 80%); 500 days drops it to
    # ~6% -> ~94% on, which genuinely demonstrates "no filter / almost always invested" (v1.1 fixture fix).
    dates = [dt.date(2024, 1, 2) + dt.timedelta(days=i) for i in range(500)]
    on = trend_regime(dates, pd.Series([100 + i for i in range(500)]),    # steadily rising
                      s2_off.gate_sma_months, s2_off.gate_band)
    assert int(on.sum()) > int(0.8 * len(on))             # gate-off invests almost always (no filter)


def test_strategy_grid_full_census_size_and_uniqueness():
    grid = strategy_grid(scale="full")
    names = [c["name"] for c in grid]
    assert len(names) == len(set(names))                 # unique labels
    counts = {f: sum(1 for c in grid if c["family"] == f) for f in ("S1", "S2", "S3", "S4")}
    assert counts == {"S1": 36, "S2": 24, "S3": 36, "S4": 36}
    assert len(grid) == 132
    # the locked Plan-03 defaults must be a cell in the grid (so the census includes the reported config)
    assert any(c["family"] == "S2" and c["params"].get("n_hold") == 3 and c["params"].get("buffer") == 2
               for c in grid)


def test_strategy_grid_small_scale_is_tiny():
    grid = strategy_grid(scale="small")
    assert 0 < len(grid) <= 12 and {c["family"] for c in grid} == {"S1", "S2", "S3", "S4"}
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError: No module named 'autotrader.robustness'`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/robustness.py
"""Offline robustness runner: enumerate the parameter-grid trial census, re-run the re-entrant
Plan-04 engine over every cell, and assemble the BINDING §4 gate (deflated Sharpe + PBO + placebo +
CIs) per strategy. Builds on the locked engine/metrics/report; the only new modelling object is the
random-selection placebo (placebo.py). Offline + deterministic; never calls the MCP, never trades.
"""
import itertools
from autotrader import config
from autotrader.strategies import (S1Trend, S2SectorMomentum, S3MeanReversion, S4TrendGatedMomentum)

# "gate off" = NO trend filter (gate ~always on). It is modeled as (sma_months=1, band=0.0): a 1-bar
# SMA equals the price, and with band=0.0 the rule `close >= SMA` is true whenever the close is at/above
# its own value -> on essentially every bar. (Modeling it as band=0.01 would make `close >= close*1.01`
# NEVER true -> permanently cash -> a dead all-cash cell that crashes the warm-up-trim; review B1.)
_GATE = {"on": (10, 0.01), "off": (1, 0.0)}              # (gate_sma_months, gate_band)


def build_strategy(family: str, params: dict):
    """Construct a strategy instance for a grid cell. `params` are scalar knobs; the gating universe
    is fixed per family (S2/S4 -> sector SPDRs, S3 -> index ETFs, S1 -> SPY/IEF)."""
    p = dict(params)
    if family == "S1":
        return S1Trend(**p)
    if family == "S2":
        if "gate" in p:                                  # gate on/off -> (gate_sma_months, gate_band)
            p["gate_sma_months"], p["gate_band"] = _GATE[p.pop("gate")]
        return S2SectorMomentum(config.SECTOR_SPDRS, **p)
    if family == "S3":
        return S3MeanReversion(config.INDEX_ETFS, **p)
    if family == "S4":
        return S4TrendGatedMomentum(config.SECTOR_SPDRS, **p)
    raise ValueError(f"unknown family {family!r}")


def _cells(family, axes):
    """Cartesian product of knob axes -> list of {name, family, params}. `axes` is an ordered dict
    of knob -> list of values; the name encodes every knob compactly and uniquely."""
    keys = list(axes)
    out = []
    for combo in itertools.product(*(axes[k] for k in keys)):
        params = dict(zip(keys, combo))
        tag = "_".join(f"{k}{_fmt(v)}" for k, v in params.items())
        out.append({"name": f"{family}_{tag}", "family": family, "params": params})
    return out


def _fmt(v):
    if v is None:
        return "X"
    if isinstance(v, bool):
        return "T" if v else "F"
    if isinstance(v, float):
        return str(v).replace(".", "p")
    return str(v)


def strategy_grid(scale: str = "full") -> list:
    """The full trial census (E1): literature-locked defaults + their immediate neighborhood per
    soft knob. scale="small" returns a tiny grid for tests (one cell per family)."""
    if scale == "small":
        return (_cells("S1", {"sma_months": [10], "stop_loss_pct": [0.20]})
                + _cells("S2", {"n_hold": [3], "buffer": [2], "gate": ["on"]})
                + _cells("S3", {"regime_sma": [200], "time_stop_days": [10]})
                + _cells("S4", {"n_hold": [3], "sma_months": [10]}))
    s1 = _cells("S1", {"sma_months": [8, 10, 12], "band": [0.0, 0.01, 0.02],
                       "use_abs_momentum": [False, True], "stop_loss_pct": [None, 0.20]})
    s2 = _cells("S2", {"n_hold": [2, 3, 4], "buffer": [1, 2], "gate": ["on", "off"],
                       "stop_loss_pct": [None, 0.20]})
    s3 = _cells("S3", {"regime_sma": [150, 200, 250], "cumrsi_entry": [30.0, 35.0, 40.0],
                       "time_stop_days": [5, 10], "stop_loss_pct": [None, 0.10]})
    s4 = _cells("S4", {"n_hold": [2, 3, 4], "buffer": [1, 2], "sma_months": [8, 10, 12],
                       "stop_loss_pct": [None, 0.20]})
    return s1 + s2 + s3 + s4
```

- [ ] **Step 4: Run** → PASS (4 passed). (`test_build_strategy_gate_off_is_no_filter_not_all_cash` pins the gate-off BEHAVIOR — ~always-on, not all-cash; `S2SectorMomentum` default `buffer=2`, so the small-grid S2 cell carries the locked default.)

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/robustness.py tests/test_robustness.py
git commit -m "feat(robustness): strategy-grid trial-census enumerator (E1)"
```

---

### Task 2: `run_grid` — drive every cell through the engine

**Files:** Modify `src/autotrader/robustness.py`, `tests/test_robustness.py`.

Run each config through the re-entrant `BacktestEngine` over a shared bars dict → `{name: BacktestResult}`. Deterministic; offline.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_robustness.py
import datetime as dt
import pandas as pd
from autotrader.robustness import run_grid
from autotrader.engine import BacktestResult


def _bars(dates, closes):
    return pd.DataFrame({"date": dates, "open": closes,
                         "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
                         "close": closes, "volume": [1] * len(closes)})


def _synth_universe(n=420):
    dates = [dt.date(2024, 1, 2) + dt.timedelta(days=i) for i in range(n)]
    raw = {}
    for k, s in enumerate(config.SECTOR_SPDRS):
        raw[s] = _bars(dates, [10 + ((k + i) % 6) * 0.3 + 0.01 * i for i in range(n)])
    for s in config.INDEX_ETFS:
        raw[s] = _bars(dates, [100 + 0.05 * i for i in range(n)])
    for s in config.BOND_ETFS:
        raw[s] = _bars(dates, [50.0] * n)
    return raw


def test_run_grid_runs_every_cell_and_returns_results():
    raw = _synth_universe()
    # short-window cells so signals fire on the ~14-month fixture (production windows are in the grid)
    grid = [
        {"name": "S1_w3", "family": "S1", "params": {"sma_months": 3, "stop_loss_pct": 0.20}},
        {"name": "S2_w", "family": "S2",
         "params": {"n_hold": 2, "buffer": 1, "nearness_window": 5, "gate_sma_months": 3}},
        {"name": "S3_w", "family": "S3", "params": {"regime_sma": 10, "exit_sma": 3, "time_stop_days": 5}},
        {"name": "S4_w", "family": "S4", "params": {"n_hold": 2, "sma_months": 3, "nearness_window": 5}},
    ]
    results = run_grid(grid, raw)
    assert set(results) == {c["name"] for c in grid}
    for name, res in results.items():
        assert isinstance(res, BacktestResult)
        assert res.equity.notna().all() and res.equity.iloc[-1] > 0


def test_run_grid_is_deterministic():
    raw = _synth_universe()
    grid = [{"name": "S1_w3", "family": "S1", "params": {"sma_months": 3, "stop_loss_pct": 0.20}}]
    a, b = run_grid(grid, raw), run_grid(grid, raw)
    assert a["S1_w3"].equity.iloc[-1] == b["S1_w3"].equity.iloc[-1]
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'run_grid'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/robustness.py
from autotrader.engine import BacktestEngine


def run_grid(grid: list, raw_bars: dict, initial_cash: float = 1000.0) -> dict:
    """Run every config cell through the re-entrant engine over `raw_bars` -> {name: BacktestResult}.
    Deterministic (the engine's default causal CS-ratio stress is deterministic). Each cell uses its
    own params verbatim; `raw_bars` must contain every symbol any cell's strategy `.universe` needs
    (the engine subsets per strategy). Offline — no MCP."""
    results = {}
    for cell in grid:
        strat = build_strategy(cell["family"], cell["params"])
        results[cell["name"]] = BacktestEngine(strat, raw_bars, initial_cash=initial_cash).run()
    return results
```

- [ ] **Step 4: Run** → PASS (2 passed). `run_grid` runs each cell verbatim — tests pass short-window configs directly (no special-casing inside `run_grid`).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/robustness.py tests/test_robustness.py
git commit -m "feat(robustness): run the trial-census grid through the re-entrant engine"
```

---

## PHASE B — The random-selection placebo (spec §4)

### Task 3: `RandomSelection` placebo strategy

**Files:** Create `src/autotrader/placebo.py`, `tests/test_placebo.py`.

A weight-frame strategy that **mirrors S2/S4 exactly except the selection rule**: same trend gate, same warm-up window, same off-asset — but each rebalance month it picks `n_hold` **random** sectors (seeded) instead of the momentum top-N. This is the "skill vs luck" control.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_placebo.py
import datetime as dt
import numpy as np
import pandas as pd
import pytest
from autotrader import config
from autotrader.placebo import RandomSelection


def _bars(dates, closes):
    return pd.DataFrame({"date": dates, "open": closes,
                         "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
                         "close": closes, "volume": [1] * len(closes)})


def _univ(n=400):
    dates = [dt.date(2024, 1, 2) + dt.timedelta(days=i) for i in range(n)]
    raw = {s: _bars(dates, [10 + 0.01 * i for i in range(n)]) for s in config.SECTOR_SPDRS}
    raw["SPY"] = _bars(dates, [100 + 0.1 * i for i in range(n)])     # rising -> gate on after warm-up
    raw["IEF"] = _bars(dates, [50.0] * n)
    return dates, raw


def test_random_selection_picks_n_equal_weight_under_gate():
    dates, raw = _univ()
    rs = RandomSelection(config.SECTOR_SPDRS, gate_symbol="SPY", n_hold=3, seed=0,
                         gate_sma_months=3, warmup=5, off_asset=None)
    w = rs.target_weights({s: raw[s] for s in rs.universe})
    rowsum = w[config.SECTOR_SPDRS].sum(axis=1)
    held_rows = rowsum[rowsum > 1e-9]
    assert (abs(held_rows - 1.0) < 1e-9).all()                       # equal-weight, sums to 1 when invested
    nonzero_per_row = (w[config.SECTOR_SPDRS] > 0).sum(axis=1)
    assert set(nonzero_per_row.unique()) <= {0, 3}                   # 0 (cash/warmup/gate-off) or exactly 3


def test_random_selection_is_seeded_reproducible_and_varies_by_seed():
    dates, raw = _univ()
    bars = {s: raw[s] for s in (config.SECTOR_SPDRS + ["SPY"])}
    w0a = RandomSelection(config.SECTOR_SPDRS, n_hold=3, seed=0, gate_sma_months=3, warmup=5).target_weights(bars)
    w0b = RandomSelection(config.SECTOR_SPDRS, n_hold=3, seed=0, gate_sma_months=3, warmup=5).target_weights(bars)
    w1 = RandomSelection(config.SECTOR_SPDRS, n_hold=3, seed=1, gate_sma_months=3, warmup=5).target_weights(bars)
    assert w0a.equals(w0b)                                           # same seed -> identical
    assert not w0a.equals(w1)                                        # different seed -> different picks


def test_random_selection_off_asset_held_when_gate_off():
    # falling SPY -> gate off -> hold the off_asset (IEF) instead of cash (mirrors S4)
    dates = [dt.date(2024, 1, 2) + dt.timedelta(days=i) for i in range(120)]
    raw = {s: _bars(dates, [10.0] * 120) for s in config.SECTOR_SPDRS}
    raw["SPY"] = _bars(dates, [100 - 0.2 * i for i in range(120)])   # falling -> gate off
    raw["IEF"] = _bars(dates, [50.0] * 120)
    rs = RandomSelection(config.SECTOR_SPDRS, n_hold=3, seed=0, gate_sma_months=2, warmup=3, off_asset="IEF")
    w = rs.target_weights({s: raw[s] for s in rs.universe})
    assert (w["IEF"].iloc[-1] == 1.0)                                # gate off at the end -> all bonds
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError: No module named 'autotrader.placebo'`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/placebo.py
"""Random-selection placebo (spec §4): the 'is the selection skill real?' control. RandomSelection
mirrors the gated sector-momentum strategies EXACTLY except that, each rebalance month, it picks
n_hold sectors at RANDOM (seeded) instead of the momentum top-N — same gate, same warm-up, same
off-asset. 1,000 seeded draws give the null distribution; a real strategy must beat its 95th pct.
Offline + deterministic. Reuses the locked trend_regime; never modifies a locked module."""
import numpy as np
import pandas as pd
from autotrader.strategies import trend_regime, select_with_hysteresis


class RandomSelection:
    """Gated random-N sector basket — a FAIR placebo: same interface, same trend gate, same warm-up,
    same off-asset, AND the same rank-hysteresis as S2/S4 (so its turnover/cost match — review fix:
    a no-hysteresis placebo churns far more, so beating it would conflate selection skill with the
    real strategy's lower churn). Each rebalance MONTH it draws a seeded random permutation of the
    sectors as the 'rank', then runs the locked `select_with_hysteresis` daily over it. Gate path
    mirrors S4: gate off -> off_asset (bonds; cash if off_asset=None); gate-on-but-warming -> cash;
    warmed -> the hysteresis-selected random basket."""
    def __init__(self, sectors, gate_symbol="SPY", n_hold=3, buffer=2, seed=0, gate_sma_months=10,
                 gate_band=0.01, warmup=252, off_asset=None):
        self.sectors = list(sectors)
        self.gate_symbol = gate_symbol
        self.n_hold, self.buffer, self.seed = n_hold, buffer, seed
        self.gate_sma_months, self.gate_band = gate_sma_months, gate_band
        self.warmup, self.off_asset = warmup, off_asset
        self.stop_loss_pct = None
        self.cost_strategy = None
        self.universe = self.sectors + [gate_symbol] + ([off_asset] if off_asset else [])

    def target_weights(self, bars):
        dates = list(bars[self.gate_symbol]["date"])
        n = len(dates)
        gate_on = trend_regime(dates, bars[self.gate_symbol]["close"],
                               self.gate_sma_months, self.gate_band).values
        ym = [d.year * 12 + d.month for d in dates]
        rng = np.random.default_rng(self.seed)
        held, cur_month, order = set(), None, None
        w = pd.DataFrame(0.0, index=range(n), columns=self.universe)
        for t in range(n):
            if not gate_on[t]:                              # gate off -> off-asset (bonds) or cash; flat basket
                held = set()
                if self.off_asset:
                    w.iloc[t, w.columns.get_loc(self.off_asset)] = 1.0
                continue
            if t < self.warmup:                             # gate on but warming -> cash (mirrors empty basket)
                held = set()
                continue
            if ym[t] != cur_month:                          # new rebalance month -> fresh random 'rank'
                cur_month = ym[t]
                order = list(rng.permutation(self.sectors))
            held = select_with_hysteresis(order, held, self.n_hold, self.buffer)   # daily, like S2/S4
            wt = 1.0 / len(held)
            for s in held:
                w.iloc[t, w.columns.get_loc(s)] = wt
        return w
```

- [ ] **Step 4: Run** → PASS (3 passed). The hysteresis keeps the held set at exactly `n_hold` (so the `{0, 3}` non-zero-per-row assertion holds), and the random order is refreshed monthly — matching S2/S4's rotation cadence so the placebo's turnover (and thus cost) is a fair control for selection skill.

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/placebo.py tests/test_placebo.py
git commit -m "feat(robustness): random-selection placebo strategy (§4 selection-skill control)"
```

---

### Task 4: `placebo_distribution` + `placebo_95th` + `beats_placebo`

**Files:** Modify `src/autotrader/placebo.py`, `tests/test_placebo.py`.

Run `n_placebo` seeded `RandomSelection` strategies through the engine → the null distribution of net Sharpes → the 95th percentile gate (spec §4). Seeded → reproducible.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_placebo.py
from autotrader.placebo import placebo_distribution, placebo_95th, beats_placebo


def test_placebo_distribution_is_seeded_and_right_length():
    dates, raw = _univ()
    d1 = placebo_distribution(config.SECTOR_SPDRS, "SPY", n_hold=3, raw_bars=raw,
                              n_placebo=20, gate_sma_months=3, warmup=5, seed_base=0)
    d2 = placebo_distribution(config.SECTOR_SPDRS, "SPY", n_hold=3, raw_bars=raw,
                              n_placebo=20, gate_sma_months=3, warmup=5, seed_base=0)
    assert len(d1) == 20 and np.allclose(d1, d2)                     # deterministic
    assert np.isfinite(d1).all()


def test_placebo_distribution_handles_all_cash_window_without_crashing():
    # falling SPY -> gate off all window -> every placebo is all-cash. trim_warmup=False must keep
    # summarize_run crash-safe (review B2: trim_warmup=True would index an empty slice -> IndexError).
    n = 120
    dates = [dt.date(2024, 1, 2) + dt.timedelta(days=i) for i in range(n)]
    raw = {s: _bars(dates, [10.0] * n) for s in config.SECTOR_SPDRS}
    raw["SPY"] = _bars(dates, [100 - 0.3 * i for i in range(n)])     # falling -> gate off throughout
    d = placebo_distribution(config.SECTOR_SPDRS, "SPY", n_hold=3, raw_bars=raw,
                             n_placebo=10, gate_sma_months=2, warmup=3, seed_base=0)
    assert len(d) == 10 and np.isfinite(d).all()                     # no crash; all-cash -> Sharpe 0.0


def test_placebo_95th_and_beats():
    dist = np.array([0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0])
    p95 = placebo_95th(dist)
    assert abs(p95 - np.quantile(dist, 0.95)) < 1e-12
    assert beats_placebo(1.2, dist) is True and beats_placebo(0.5, dist) is False
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'placebo_distribution'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/placebo.py
from autotrader.engine import BacktestEngine, build_engine_inputs, tier_for_symbol
from autotrader.stress import causal_stress_series
from autotrader import report


def precompute_stress(raw_bars, universe):
    """Build the engine's per-symbol causal CS-ratio stress ONCE for a universe and return it as a
    `stress(symbol, t)` callable. ~74% of an engine run is this recompute (review E6); all placebo
    seeds share one raw_bars/universe, so computing it once and passing it via the engine's `stress=`
    param is a ~3.8x speedup (equity identical to 1e-6 vs the default per-run recompute)."""
    aligned, _, _ = build_engine_inputs(raw_bars, universe)
    series = {s: causal_stress_series(aligned[s], tier_for_symbol(s)) for s in universe}
    return lambda sym, t, _ss=series: float(_ss[sym].iloc[t])


def placebo_distribution(sectors, gate_symbol, n_hold, raw_bars, buffer=2, n_placebo=1000,
                         gate_sma_months=10, gate_band=0.01, warmup=252, off_asset=None,
                         seed_base=0, initial_cash=1000.0) -> np.ndarray:
    """Net Sharpe of `n_placebo` seeded RandomSelection runs through the engine (the runner's bulk).
    Precomputes the shared stress ONCE (E6). Uses `summarize_run(..., trim_warmup=False)` so an
    all-cash placebo (gate off all window) yields Sharpe 0.0 instead of crashing (review B2).
    Seeds seed_base..seed_base+n_placebo-1 -> reproducible. n_placebo is a parameter (default 1000)."""
    universe = list(sectors) + [gate_symbol] + ([off_asset] if off_asset else [])
    stress_fn = precompute_stress(raw_bars, universe)
    out = np.empty(n_placebo)
    for i in range(n_placebo):
        rs = RandomSelection(sectors, gate_symbol=gate_symbol, n_hold=n_hold, buffer=buffer,
                             seed=seed_base + i, gate_sma_months=gate_sma_months, gate_band=gate_band,
                             warmup=warmup, off_asset=off_asset)
        res = BacktestEngine(rs, raw_bars, initial_cash=initial_cash, stress=stress_fn).run()
        out[i] = report.summarize_run(res, trim_warmup=False)["sharpe"]
    return out


def placebo_95th(distribution) -> float:
    return float(np.quantile(np.asarray(distribution, dtype="float64"), 0.95))


def beats_placebo(strategy_sharpe: float, distribution) -> bool:
    """The §4 placebo condition: the strategy's net Sharpe beats the 95th percentile of the null."""
    return bool(strategy_sharpe > placebo_95th(distribution))
```

- [ ] **Step 4: Run** → PASS (3 passed). Then `./.venv/bin/pytest tests/test_placebo.py -v` → all green (Tasks 3-4). The `precompute_stress` + `trim_warmup=False` combination is what makes the 1,000-run distribution both fast (E6) and crash-safe on all-cash windows (B2).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/placebo.py tests/test_placebo.py
git commit -m "feat(robustness): placebo null distribution + 95th-pct §4 gate"
```

---

## PHASE C — Plateau, census DSR/PBO, binding gate, report

### Task 5: `plateau` — robustness-is-a-plateau scan (§3.9)

**Files:** Modify `src/autotrader/robustness.py`, `tests/test_robustness.py`.

For a family + soft knob, group that family's grid cells by the knob value and report the median Sharpe across the neighborhood. A robust knob is a **plateau** (similar across values), not a lone **peak**. Pure (consumes a `{cell_name: sharpe}` map).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_robustness.py
from autotrader.robustness import plateau, plateau_spread


def test_plateau_groups_by_knob_and_medians():
    grid = [
        {"name": "S1_a", "family": "S1", "params": {"sma_months": 8}},
        {"name": "S1_b", "family": "S1", "params": {"sma_months": 8}},
        {"name": "S1_c", "family": "S1", "params": {"sma_months": 10}},
        {"name": "S2_x", "family": "S2", "params": {"sma_months": 8}},   # different family, ignored
    ]
    sharpes = {"S1_a": 0.4, "S1_b": 0.6, "S1_c": 0.9, "S2_x": 5.0}
    p = plateau(sharpes, grid, "S1", "sma_months")
    assert p == {8: 0.5, 10: 0.9}                       # median of {0.4,0.6}=0.5 ; {0.9}=0.9
    assert abs(plateau_spread(p) - 0.4) < 1e-12          # max(0.9) - min(0.5)


def test_plateau_empty_for_unknown_knob():
    grid = [{"name": "S1_a", "family": "S1", "params": {"sma_months": 8}}]
    assert plateau({"S1_a": 0.4}, grid, "S1", "buffer") == {}
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'plateau'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/robustness.py
import numpy as np
import pandas as pd


def plateau(sharpes: dict, grid: list, family: str, knob: str) -> dict:
    """For `family`, group its cells by `knob` value -> {value: median Sharpe over those cells}.
    A plateau (low spread, all decent) = robust on that knob; a lone peak = fragile (§3.9)."""
    vals = {}
    for cell in grid:
        if cell["family"] != family or knob not in cell["params"]:
            continue
        vals.setdefault(cell["params"][knob], []).append(sharpes[cell["name"]])
    return {v: float(np.median(s)) for v, s in sorted(vals.items(), key=lambda kv: str(kv[0]))}


def plateau_spread(plateau_map: dict) -> float:
    """max - min of the per-value medians (low = a plateau; high = a peak). NaN if <1 value."""
    if not plateau_map:
        return float("nan")
    vals = list(plateau_map.values())
    return float(max(vals) - min(vals))
```

- [ ] **Step 4: Run** → PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/robustness.py tests/test_robustness.py
git commit -m "feat(robustness): plateau scan over soft-knob neighborhoods (§3.9)"
```

---

### Task 6: `census_dsr_pbo` — Deflated Sharpe + PBO at full census strength (§3.9, E5)

**Files:** Modify `src/autotrader/robustness.py`, `tests/test_robustness.py`.

Per family: PBO via CSCV over that family's **distinct** config matrix; Deflated Sharpe of the family's **best** cell deflated by a **consistent within-family null** — `N` = that family's distinct-cell count, `V` = variance of per-observation Sharpes across those distinct cells (E5). A separate **secondary** whole-search line (global `N`, global `V`) is reported but is NOT the binding number. (Byte-identical cells are de-duplicated first so neither `N` nor the CSCV ranks are corrupted.)

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_robustness.py
import numpy as np
from autotrader.robustness import census_dsr_pbo


class _StubRes:
    def __init__(self, returns):
        self.returns = pd.Series(returns)


def _grid_results(seed=0):
    rng = np.random.default_rng(seed)
    grid, results = [], {}
    # S1: a clearly-best cell + 3 mediocre ; S2: 4 mediocre cells (so each family has >=2 cells)
    specs = [("S1", "best", 0.004), ("S1", "m1", 0.0005), ("S1", "m2", 0.0004), ("S1", "m3", 0.0003),
             ("S2", "a", 0.0006), ("S2", "b", 0.0005), ("S2", "c", 0.0004), ("S2", "d", 0.0003)]
    for fam, tag, mu in specs:
        name = f"{fam}_{tag}"
        grid.append({"name": name, "family": fam, "params": {"k": tag}})
        results[name] = _StubRes(rng.normal(mu, 0.01, 300))
    return grid, results


def test_census_dsr_pbo_structure_and_best_cell():
    grid, results = _grid_results()
    out = census_dsr_pbo(results, grid, n_blocks=4)
    assert out["n_trials"] == 8                                  # global census N
    assert set(out["families"]) == {"S1", "S2"}
    s1 = out["families"]["S1"]
    assert s1["best_cell"] == "S1_best"                          # max per-obs Sharpe cell
    assert 0.0 <= s1["pbo"] <= 1.0 and np.isfinite(s1["deflated_p"])
    assert s1["n_cells"] == 4 and s1["n_distinct"] == 4          # stub returns are all distinct
    assert np.isfinite(s1["deflated_p_wholesearch"])             # secondary (global N/V) check present


def test_census_dsr_pbo_single_cell_family_has_nan_pbo():
    grid = [{"name": "S3_a", "family": "S3", "params": {"k": "a"}},
            {"name": "S1_a", "family": "S1", "params": {"k": "a"}},
            {"name": "S1_b", "family": "S1", "params": {"k": "b"}}]
    rng = np.random.default_rng(1)
    results = {c["name"]: _StubRes(rng.normal(0.0005, 0.01, 80)) for c in grid}
    out = census_dsr_pbo(results, grid, n_blocks=4)
    assert np.isnan(out["families"]["S3"]["pbo"])               # <2 cells -> PBO undefined
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'census_dsr_pbo'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/robustness.py
from autotrader import metrics, report


def _dedup_cells(cells: list, grid_results: dict) -> list:
    """Drop cells whose return stream is BYTE-IDENTICAL to one already kept (e.g. a stop that never
    fires -> stop-on == stop-off). Exact duplicates over-count N and degenerate the CSCV OOS-ranks
    (review B3); keep the first of each distinct stream."""
    seen, distinct = set(), []
    for nm in cells:
        key = grid_results[nm].returns.dropna().to_numpy().tobytes()
        if key not in seen:
            seen.add(key)
            distinct.append(nm)
    return distinct


def census_dsr_pbo(grid_results: dict, grid: list, n_blocks: int = 16) -> dict:
    """Per-family Deflated Sharpe + PBO (§3.9). The BINDING deflation uses a CONSISTENT within-family
    null: best-of-family deflated by N = that family's DISTINCT-cell count and V = variance of
    per-observation Sharpes over those distinct cells (review B2 — a global N + cross-family-mixed V
    is the wrong null and can kill a real edge). PBO via CSCV is over the family's DISTINCT matrix.
    A secondary `deflated_p_wholesearch` (global N, global V) is reported as a more-conservative
    check, NOT the binding number. Families with <2 distinct cells / <n_blocks rows get PBO = NaN."""
    per_obs = {nm: report._per_obs_sharpe(res.returns.dropna().to_numpy())
               for nm, res in grid_results.items()}
    N_global = len(grid)
    V_global = float(pd.Series(list(per_obs.values())).var(ddof=1)) if N_global > 1 else 0.0
    families = {}
    for fam in sorted({c["family"] for c in grid}):
        cells = [c["name"] for c in grid if c["family"] == fam]
        distinct = _dedup_cells(cells, grid_results)
        Nf = len(distinct)
        Vf = float(pd.Series([per_obs[nm] for nm in distinct]).var(ddof=1)) if Nf > 1 else 0.0
        best = max(distinct, key=lambda nm: per_obs[nm])
        dsr, p = metrics.deflated_sharpe(grid_results[best].returns, n_trials=max(Nf, 2),
                                         sr_variance=max(Vf, 1e-12))                  # BINDING (per-family)
        _, p_global = metrics.deflated_sharpe(grid_results[best].returns, n_trials=max(N_global, 2),
                                              sr_variance=max(V_global, 1e-12))       # secondary check
        pbo = float("nan")
        if Nf >= 2:
            mat = report.build_variant_matrix({nm: grid_results[nm] for nm in distinct})
            if len(mat) >= n_blocks:
                pbo = metrics.pbo_cscv(mat, n_blocks=n_blocks)
        families[fam] = {"best_cell": best, "best_sharpe_perobs": per_obs[best],
                         "deflated_sharpe": dsr, "deflated_p": p, "deflated_p_wholesearch": p_global,
                         "pbo": pbo, "n_cells": len(cells), "n_distinct": Nf}
    return {"families": families, "n_trials": N_global, "sr_variance_global": V_global}
```

- [ ] **Step 4: Run** → PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/robustness.py tests/test_robustness.py
git commit -m "feat(robustness): census-strength Deflated Sharpe + per-family PBO (§3.9, E5)"
```

---

### Task 7: `binding_verdict` + `family_verdict` — the BINDING §4 gate (E4)

**Files:** Modify `src/autotrader/robustness.py`, `tests/test_robustness.py`.

`binding_verdict` reuses Plan-04's `report.gate_verdict` and replaces the deferred placebo term with the **real** boolean → a binding PASS/FAIL (PASS iff all five conditions True; auto-kill on PBO ≥ 0.5 or deflated-p ≥ 0.05 is enforced by those being gate conditions). `family_verdict` dispatches: S2/S4 get the 5-condition gate; **S1** gets the drawdown clause (PBO < 0.5 AND max-DD materially < SPY — spec §4 risk-sleeve); **S3** gets null-confirmation (success = NOT passing).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_robustness.py
from autotrader.robustness import binding_verdict, family_verdict


def _res(returns, equity=None):
    r = pd.Series(returns, dtype="float64")
    idx = pd.Index([dt.date(2020, 1, 1) + dt.timedelta(days=i) for i in range(len(r))], name="date")
    r.index = idx                                              # date-indexed so metrics.cagr works
    e = pd.Series(equity, dtype="float64") if equity is not None else (1.0 + r.fillna(0)).cumprod()
    e.index = idx
    class _R: pass
    o = _R(); o.returns = r; o.equity = e
    return o


def _winning_trio():
    rng = np.random.default_rng(0)
    run = _res(rng.normal(0.0012, 0.004, 400))                 # strong + low vol -> beats bench, shallow DD
    bench = _res(np.zeros(400))                                 # flat benchmark
    # spy: peak at 1.0 on day 0, then a -50% crash -> max_drawdown registers -0.5 (deep DD benchmark)
    spy = _res(rng.normal(0.0, 0.02, 400), equity=(1 + pd.Series([0.0, -0.5] + [0.0] * 398)).cumprod())
    return run, bench, spy


def test_binding_verdict_placebo_flips_pass_fail():
    run, bench, spy = _winning_trio()
    passed = binding_verdict(run, bench, spy, dsr_p=0.01, pbo=0.2, placebo_pass=True)
    failed = binding_verdict(run, bench, spy, dsr_p=0.01, pbo=0.2, placebo_pass=False)
    assert passed["overall"] == "PASS" and passed["conditions"]["placebo_beats_95th"] is True
    assert failed["overall"] == "FAIL"                         # placebo term alone sinks it


def test_binding_verdict_autokill_on_pbo_and_p():
    run, bench, spy = _winning_trio()
    assert binding_verdict(run, bench, spy, dsr_p=0.01, pbo=0.6, placebo_pass=True)["overall"] == "FAIL"
    assert binding_verdict(run, bench, spy, dsr_p=0.20, pbo=0.2, placebo_pass=True)["overall"] == "FAIL"


def test_family_verdict_s1_drawdown_clause_material_dd_and_cagr():
    run, bench, spy = _winning_trio()
    v = family_verdict("S1", run, spy, spy, dsr_p=0.5, pbo=0.2, placebo_pass=None)
    # S1 ignores the Sharpe-beat + placebo; PASS on PBO<0.5 AND materially shallower DD AND acceptable CAGR
    assert set(v["conditions"]) == {"pbo_lt_0.5", "maxdd_materially_lt_spy", "cagr_acceptable_vs_spy"}
    assert v["overall"] == "PASS"


def test_family_verdict_s3_null_inactive_and_redflag():
    rng = np.random.default_rng(0)
    neg = _res(rng.normal(-0.0003, 0.005, 300))                # steady net loss -> confirmed null
    pos = _res(rng.normal(0.002, 0.003, 300))                  # net positive -> red flag
    flat = _res(np.zeros(300))                                  # never invested -> INACTIVE, not a null
    assert family_verdict("S3", neg, neg, neg, dsr_p=0.9, pbo=0.3)["overall"] == "NULL-CONFIRMED"
    assert family_verdict("S3", pos, pos, pos, dsr_p=0.01, pbo=0.3)["overall"] == "RED-FLAG-AUDIT-COSTS"
    assert family_verdict("S3", flat, flat, flat, dsr_p=0.9, pbo=0.3)["overall"] == "INACTIVE"
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'binding_verdict'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/robustness.py
def binding_verdict(run, benchmark, spy, dsr_p, pbo, placebo_pass, seed: int = 0,
                    block_size: int = 21) -> dict:
    """The BINDING §4 return-seeker gate: Plan-04's gate_verdict + the REAL placebo term. PASS iff
    all five conditions True (auto-kill on pbo>=0.5 / deflated_p>=0.05 is enforced by those terms)."""
    v = report.gate_verdict(run, benchmark, spy, dsr_p, pbo, seed=seed, block_size=block_size)
    conds = dict(v["conditions"])
    conds["placebo_beats_95th"] = bool(placebo_pass)
    overall = "PASS" if all(bool(c) for c in conds.values()) else "FAIL"
    return {"overall": overall, "conditions": conds,
            "paired_sharpe_ci_lower": v["paired_sharpe_ci_lower"],
            "run_maxdd_upperci": v["run_maxdd_upperci"], "spy_maxdd": v["spy_maxdd"]}


def family_verdict(family, run, benchmark, spy, dsr_p, pbo, placebo_pass=None, seed: int = 0,
                   block_size: int = 21, material_frac: float = 0.75,
                   cagr_floor_frac: float = 0.5) -> dict:
    """Per-family §4 verdict. S2/S4 -> 5-condition binding gate. S1 (risk sleeve, spec §4) -> PBO<0.5
    AND max-DD MATERIALLY below SPY's (run upper-CI <= material_frac x SPY's) AND CAGR acceptable
    (>= cagr_floor_frac x max(SPY CAGR, 0)) — no Sharpe-beat / placebo. S3 (null) -> INACTIVE if it
    never traded, else NULL-CONFIRMED iff Sharpe <= 0 (success = failing), else RED-FLAG (audit costs).
    `material_frac`/`cagr_floor_frac` are soft thresholds (parameters)."""
    if family in ("S2", "S4"):
        return binding_verdict(run, benchmark, spy, dsr_p, pbo, placebo_pass, seed, block_size)
    if family == "S1":
        v = report.gate_verdict(run, benchmark, spy, dsr_p, pbo, seed=seed, block_size=block_size)
        run_cagr, spy_cagr = metrics.cagr(run.equity), metrics.cagr(spy.equity)
        conds = {
            "pbo_lt_0.5": v["conditions"]["pbo_lt_0.5"],
            "maxdd_materially_lt_spy": bool(v["run_maxdd_upperci"] <= material_frac * v["spy_maxdd"]),
            "cagr_acceptable_vs_spy": bool(run_cagr >= cagr_floor_frac * max(spy_cagr, 0.0)),
        }
        return {"overall": "PASS" if all(conds.values()) else "FAIL", "conditions": conds,
                "run_maxdd_upperci": v["run_maxdd_upperci"], "spy_maxdd": v["spy_maxdd"],
                "run_cagr": run_cagr, "spy_cagr": spy_cagr}
    if family == "S3":
        r = pd.Series(run.returns).dropna()
        if not bool((r.abs() > 1e-12).any()):              # never invested -> not a confirmed null
            return {"overall": "INACTIVE", "sharpe": float("nan"), "deflated_p": dsr_p, "pbo": pbo}
        sharpe = metrics.sharpe(r)
        return {"overall": "NULL-CONFIRMED" if sharpe <= 0.0 else "RED-FLAG-AUDIT-COSTS",
                "sharpe": sharpe, "deflated_p": dsr_p, "pbo": pbo}
    raise ValueError(f"unknown family {family!r}")
```

- [ ] **Step 4: Run** → PASS (4 passed). The integration review confirmed `_winning_trio` (seed 0) clears all four `gate_verdict` sub-conditions cleanly (paired-Sharpe CI lower ≈ +2.9; run-DD upper-CI ≈ 4% < SPY 50%), so the placebo flip and the S1 material-DD/CAGR clause exercise real logic — no fixture tuning needed.

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/robustness.py tests/test_robustness.py
git commit -m "feat(robustness): binding §4 gate + per-family verdict dispatcher (E4)"
```

---

### Task 8: `render_robustness` — the robustness report

**Files:** Modify `src/autotrader/robustness.py`, `tests/test_robustness.py`.

Assemble the per-family census (DSR/PBO/N), the verdicts, and the plateau spreads into a deterministic markdown report.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_robustness.py
from autotrader.robustness import render_robustness


def test_render_robustness_is_deterministic_and_labels_verdicts():
    census = {"n_trials": 132, "families": {
        "S2": {"best_cell": "S2_n3", "deflated_sharpe": 0.3, "deflated_p": 0.7, "pbo": 0.6,
               "n_cells": 24, "n_distinct": 22},
        "S1": {"best_cell": "S1_x", "deflated_sharpe": 0.8, "deflated_p": 0.19, "pbo": 0.2,
               "n_cells": 36, "n_distinct": 24}}}
    verdicts = {"S2": {"overall": "FAIL"}, "S1": {"overall": "FAIL"}}
    plateaus = {"S2": {"n_hold": 0.05}, "S1": {"sma_months": 0.03}}
    md1 = render_robustness(census, verdicts, plateaus)
    md2 = render_robustness(census, verdicts, plateaus)
    assert md1 == md2 and "n_trials=132" in md1 and "S2" in md1 and "FAIL" in md1
    assert md1.count("PASS") + md1.count("FAIL") >= 2          # a verdict per family
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'render_robustness'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/robustness.py
def render_robustness(census: dict, verdicts: dict, plateaus: dict) -> str:
    """Deterministic markdown: per-family census (best cell, deflated Sharpe + p, PBO, #cells),
    the §4 verdict, and the plateau spread per knob. `census` from census_dsr_pbo; `verdicts` from
    family_verdict; `plateaus` = {family: {knob: plateau_spread}}."""
    lines = [f"Trial census: n_trials={census['n_trials']}", "",
             "| family | best_cell | deflated_sharpe | deflated_p | pbo | cells(distinct) | verdict |",
             "|---|---|---|---|---|---|---|"]
    for fam in sorted(census["families"]):
        f = census["families"][fam]
        v = verdicts.get(fam, {}).get("overall", "n/a")
        cells = f"{f['n_cells']}({f.get('n_distinct', f['n_cells'])})"   # distinct count = effective trials
        lines.append(f"| {fam} | {f['best_cell']} | {f['deflated_sharpe']:.4f} | {f['deflated_p']:.4f} "
                     f"| {f['pbo']:.4f} | {cells} | {v} |")
    lines += ["", "Plateau spreads (max-min median Sharpe across each knob's neighborhood; low = robust):"]
    for fam in sorted(plateaus):
        spreads = ", ".join(f"{k}={s:.3f}" for k, s in sorted(plateaus[fam].items()))
        lines.append(f"  {fam}: {spreads}")
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run** → PASS (1 passed). Then `./.venv/bin/pytest tests/test_robustness.py -v` → all green (Tasks 1,2,5,6,7,8).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/robustness.py tests/test_robustness.py
git commit -m "feat(robustness): robustness report (census + verdicts + plateau spreads)"
```

---

### Rebalance-day dispersion — DEFERRED (not a task in this plan)

All three reviews recommended **deferring** the §6 rebalance-day dispersion diagnostic (day 1/8/15/22). It is **not one of the 5 §4 gate conditions** (it gates nothing), and doing it cleanly would require an additive `rebalance_day` knob on the **locked** `indicators.monthly_closes` + the monthly strategies — not worth touching locked model code for a diagnostic. It is deferred to whenever the strategies are next legitimately revised (when the knob can be added in-context). **This plan touches no locked module.** (If later built, note the `.nth(k-1)` short-month bug a reviewer flagged: clamp `k-1` to the month's length so a 20-day month doesn't drop its row.)

---

### Task 9: Offline driver + sanity + doc-sync + tag

**Files:** Create `scripts/run_robustness.py` (offline batch driver; not a pytest test); modify `PROJECT_CONTEXT.md`; tag `robustness-v1`.

- [ ] **Step 1: Full suite green.** `./.venv/bin/pytest -q` → the existing suite plus the new robustness/placebo tests, ALL PASS. Record the count.

- [ ] **Step 2: Write `scripts/run_robustness.py`** — offline, deterministic, reads ONLY `data/cache`:
  - Load the 15-symbol universe via `DataStore`; build `raw_bars`.
  - `grid = strategy_grid("full")` (132 cells); `results = run_grid(grid, raw_bars)` (~7 min).
  - `census = census_dsr_pbo(results, grid)`; `sharpes = {name: report.summarize_run(res, trim_warmup=False)["sharpe"] for name, res in results.items()}` (**`trim_warmup=False`** — crash-safe on any all-cash cell); `plateaus = {family: {knob: plateau_spread(plateau(sharpes, grid, family, knob))} for each scanned knob}`.
  - Run the benchmark refs: `GatedSPY()` and `BuyHold("SPY")` through the engine (the `benchmark`/`spy` args).
  - **Placebos (the bulk, ~50 min WITH the E6 precompute):** for **S2** (`off_asset=None`) and **S4** (`off_asset="IEF"`), `dist = placebo_distribution(config.SECTOR_SPDRS, "SPY", n_hold=<best cell's n_hold>, raw_bars, buffer=<best cell's buffer>, n_placebo=1000, off_asset=...)`; `placebo_pass = beats_placebo(best_cell_sharpe, dist)` where `best_cell_sharpe = sharpes[census["families"][fam]["best_cell"]]` (same `trim_warmup=False` basis as the distribution). Expose a `--quick` flag (`scale="small"`, `n_placebo≈50`) for a ~1-min sanity run.
  - `verdicts = {fam: family_verdict(fam, results[census["families"][fam]["best_cell"]], gated_spy_run, buyhold_spy_run, census["families"][fam]["deflated_p"], census["families"][fam]["pbo"], placebo_pass=<S2/S4 only>)}`.
  - Print `render_robustness(census, verdicts, plateaus)` + each placebo's (strategy Sharpe vs 95th pct) + each family's `n_distinct` and the secondary `deflated_p_wholesearch`. Write artifacts to `data/robustness/` (gitignored).
  - **It calls NO MCP.**
- [ ] **Step 3: Run it** (`--quick` first to smoke the wiring, then the full run, ~60 min). Capture the binding verdicts + the census table + the placebo results.
- [ ] **Step 4: Sanity gates (print PASS/FLAG; do not crash).** (a) every grid run finite + >0; (b) `census["n_trials"] == 132`; (c) **S3's best cell is NULL-CONFIRMED or INACTIVE** (Sharpe ≤ ~0 — if a cell clears with real trading, RED-FLAG the cost model, do not celebrate); (d) the run is **deterministic** (re-running gives identical verdicts). Expected, per the Plan-04 smoke: S2/S4 **FAIL** the binding gate (they don't beat the placebo / deflated-p / Sharpe-CI vs gated-SPY), S3 **NULL-CONFIRMED**, S1 reported on the **drawdown clause** (it cut the 2008 drawdown — it may be the one strategy that clears as risk insurance; report whatever the gate says).
- [ ] **Step 5: Doc-sync (same branch).** Update `PROJECT_CONTEXT.md` roadmap: **split Plan 05 into 05a (Robustness Runner ✅) + 05b (Paper-Monitor, next)**; record the binding verdicts (the kill/keep decision) + the trial census `N`=132. Update the `PROJECT_CONTEXT.md` LOCKED list to add the robustness runner (tag `robustness-v1`). (The controller appends the session-memory note.) Keep it short.
- [ ] **Step 6: Commit + tag.**

```bash
git add scripts/run_robustness.py PROJECT_CONTEXT.md PROJECT_CONTEXT.md
git commit -m "feat(robustness): offline trial-census runner + binding §4 gate + Plan 05a doc-sync"
git tag robustness-v1 -m "Robustness runner: trial census + binding §4 gate (deflated Sharpe, PBO, placebo, plateau) — verified offline"
```

---

## Self-review against the spec (completed by plan author)

- **§3.8 walk-forward:** the engine's per-fold/expanding-window slicing (Plan 04) is reused. Rebalance-day dispersion is **deferred** (a diagnostic, not a gate term; would touch locked code — all three reviews agreed).
- **§3.9 multiple-testing:** the full **trial census** is enumerated (`strategy_grid`, 132 cells, distinct-count reported per family); **Deflated Sharpe** (per-family N + within-family V, byte-identical cells de-duplicated; a secondary whole-search line reported) and **PBO via CSCV** (per family, over distinct cells) are computed for every strategy; **auto-kill** on PBO ≥ 0.5 or deflated-p ≥ 0.05 is enforced *because* those are two of the AND-ed gate conditions; **"plateau not a peak"** is the `plateau`/`plateau_spread` scan (spread + level reported).
- **§4 binding gate:** all five conditions assembled non-provisionally — deflated-p < 0.05, (strategy−benchmark) Sharpe-CI lower ≥ 0, max-DD upper-CI < SPY's, PBO < 0.5, AND **beats the 95th-pct of the 1,000-pick random-selection placebo** (now hysteresis-matched for a fair, cost-controlled test). S1 gets the risk-sleeve clause (PBO + **material** DD reduction + acceptable CAGR); S3 the null-confirmation (INACTIVE if it never traded).
- **§6 build step 6:** plateau scan ✓, with/without stop & gate as grid axes ✓ (S4 stop axis added), rebalance-day dispersion **deferred** (diagnostic).
- **Offline guardrail:** every task is offline; the only data source is `data/cache`; no order/review/cancel/MCP path exists in any new module. The paper-monitor (the MCP-touching phase) is explicitly Plan 05b, out of scope.
- **Locks:** the runner builds ON the locked engine/metrics/report/strategies — **no task touches a locked module** (the only candidate, rebalance-day dispersion, is deferred).

### Open items — RESOLVED (the operator approved 2026-06-18)
The reviewers settled: DEFER dispersion (no locked-code touch); KEEP the ~132-cell grid; per-family N + within-family V with de-dup; precompute the stress (~60 min, single-threaded OK); hysteresis-fair placebo. the operator's three calls, all adopted as the proposed defaults:
1. **S1 soft thresholds APPROVED:** `material_frac=0.75` ("cuts DD by ≥25% vs SPY") and `cagr_floor_frac=0.5` ("CAGR ≥ half SPY's"). These are the defaults wired into `family_verdict`.
2. **Runtime APPROVED:** ~60 min single-threaded (with the E6 precompute) is fine as a run-occasionally batch; no multiprocessing.
3. **Expected outcome APPROVED:** "most things FAIL, S3 NULL-CONFIRMED, S1 may PASS as drawdown insurance" is the anticipated, success-shaped result — report whatever the binding gate actually says.

---

## Roadmap position

Plan **05a** of the roadmap (the re-sequenced "Plan 05 — Robustness Runner & Paper-Monitor" split into 05a robustness + 05b paper-monitor). Builds on Plan 04's re-entrant engine + metrics + gate. Next: **Plan 05b — Paper-Monitor** (the first and only live, read-only, forward MCP phase — same deterministic logic run forward, calling `review_equity_order` without placing, for months). The binding verdicts this runner produces decide which strategies (if any) reach 05b.

