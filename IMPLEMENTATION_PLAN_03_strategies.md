# Strategy Modules + Data Population — Implementation Plan (Plan 03 of 5) — v1.1

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the four deterministic strategy rule modules (S1 trend, S2 sector momentum, S3 mean-reversion null-test, S4 trend-gated blend) on top of the Plan 02 indicators, plus the offline cache-integrity checker and the (gated) live data-population runbook that fills the real cache for the Plan 04 backtest engine.

**Architecture — the strategy interface (the contract the Plan 04 engine drives):** Each strategy is a small class with `.universe` (symbols it needs), `.stop_loss_pct` (the engine places a protective stop at `fill_price*(1-pct)`), `.cost_strategy` (`"S3"` → the punitive cost floor via `costs.roundtrip_cost_for_strategy`; else `None` = instrument-tier cost), and `.target_weights(bars) -> pd.DataFrame` (indexed by the reference daily date axis, columns = symbols, values = target weight in `[0,1]`; cash = `1 - row.sum()`). **Every row is causal — it uses only bars at or before that date.** Stateful rules (Faber 1% band, S2 rank buffer, S3 time-stop) are computed by a causal forward scan *inside* `target_weights`, so the frame is self-contained and look-ahead-free. **Strategy = intent; engine = execution:** the Plan 04 engine reads the weight frame and executes *changes* at the **next open** (spec §3.4 — never the same bar that produced the signal), places/evaluates stops via the Simulator, and applies T+1 settlement feasibility. Daily↔monthly signals are bridged by `indicators.align_monthly_to_daily` (forward-fill the latest completed month-end; the engine's next-open rule supplies the one-period actionability lag).

**Engine contract (decisions Plan 04 inherits — settled here so the engine doesn't guess):**
1. **Aligned bars.** The engine passes a `bars` dict whose every symbol shares one NYSE-calendar date axis (it aligns each `DataStore` series to the calendar before calling `target_weights`). `_ref_dates` validates this and raises loudly on any misalignment — a shorter-history symbol can never silently shift the position-indexed frame.
2. **`universe` is the load-set, not the trade-set.** It lists every symbol the strategy needs bars for (signal + traded). The engine allocates dollars only to **non-zero-weight** columns and assigns the cost tier **per actual trade** — so a signal-only symbol (e.g. SPY in S4, weight always 0) is loaded but never traded or priced.
3. **Stop-first precedence.** The engine evaluates resting stops for date T *before* processing that bar's weight-change exits; for any symbol a stop fills, the weight target is treated as already satisfied (no double-exit at a different price).
4. **Missed-allocation policy.** When the T+1 ledger's `can_buy` refuses a buy (insufficient settled cash — e.g. the blend's sleeves competing for the one shared pool, spec §3.6), the engine **skips that buy for the bar and logs it**; it does not defer or carry the intent forward. The weight frame is re-read fresh each bar (strategy = intent; these four rules define how the engine reconciles it with execution feasibility).

**Tech Stack:** Python 3.11, pandas, pytest. Strategy tests run on small **synthetic** OHLCV fixtures with small indicator windows (the production windows — 10-month SMA, 252-day high, SMA200 — are parameters; tests use short windows to exercise the logic on short fixtures). No MCP except the one explicitly-gated data-population task.

**Reads:** [`STRATEGY_TESTING_SPEC.md`](STRATEGY_TESTING_SPEC.md) §2 (the four strategies — locked rules), §3.1-§3.7 (methodology), and [`COST_MODEL.md`](COST_MODEL.md) §4 (S3 floor). [`MCP_CAPABILITIES.md`](MCP_CAPABILITIES.md) §4 for the data-population runbook.

**Soft-knob defaults (spec gives ranges; these are the picked defaults, all parameters so Plan 05's robustness scan can vary them):** defensive asset `IEF`; S2 `n_hold=3`, `buffer=2` (sell a held name only when rank > 5); S1/S2 stop `−20%`; S3 catastrophe stop `−10%`, `time_stop_days=10`; Antonacci absolute-momentum filter on S1 **off by default** (a separately-tested variant; the gate basis for S2/S4 is Faber-only).

**Validated:** the strategy logic in this plan was prototyped end-to-end and run against the synthetic oracles below before writing (the prototype caught and fixed a rank-hysteresis bug — see Task 3). Production defaults are unchanged; tests use short windows.

**Reviewed (v1.1):** three independent reviews (methodology/look-ahead, executable-as-written, interface-fit). The executable review built the code in a scratch dir and ran it green (16/16); the methodology review independently reconfirmed every strategy oracle and the look-ahead-free property. Changes from review: `_ref_dates` now validates that all symbols share one date axis (raises loudly — prevents a shorter-history symbol silently shifting the frame); the **Engine contract** below now pins the four decisions Plan 04 inherits (aligned bars, load-set vs trade-set, stop-first precedence, missed-allocation policy); S3 documents that regime is an entry-only filter; the S4 test asserts the no-over-allocation invariant.

---

## File structure (locked here)

```
src/autotrader/strategies.py     # trend_regime, select_with_hysteresis, S1Trend, S2SectorMomentum, S3MeanReversion, S4TrendGatedMomentum
src/autotrader/datacheck.py      # verify_series — offline cache-integrity check
tests/test_strategies.py         # synthetic-fixture oracles for all four strategies + the helpers
tests/test_datacheck.py          # cache-integrity checker unit tests
data/raw/  data/cache/  data/manifest.csv   # populated by Task 7 (raw gitignored; manifest tracked)
```

---

## Task 1: Strategy helpers — `select_with_hysteresis` + `trend_regime`

**Files:** Create `src/autotrader/strategies.py`, `tests/test_strategies.py`.

These two pure helpers are shared by S1/S2/S4. `select_with_hysteresis` picks the N-name portfolio keeping held names still within the rank buffer; `trend_regime` is the shared Faber risk-on/off signal.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_strategies.py
import datetime as dt
import pandas as pd
import pytest
from autotrader.strategies import select_with_hysteresis, trend_regime


def _bars(dates, closes):
    return pd.DataFrame({"date": dates, "open": closes,
                         "high": [c * 1.001 for c in closes], "low": [c * 0.999 for c in closes],
                         "close": closes, "volume": [1] * len(closes)})


def test_hysteresis_keeps_held_name_within_buffer():
    # held A,B; now ranks A=1,C=2,B=3. With N=2, buffer=1 (keep rank<=3), B is kept over C.
    assert select_with_hysteresis(["A", "C", "B", "D"], {"A", "B"}, 2, 1) == {"A", "B"}


def test_hysteresis_drops_held_name_past_buffer():
    # B falls to rank 4 (> N+buffer=3) -> sold; fresh rank-2 C takes the slot.
    assert select_with_hysteresis(["A", "C", "D", "B"], {"A", "B"}, 2, 1) == {"A", "C"}


def test_hysteresis_fresh_picks_top_n():
    assert select_with_hysteresis(["A", "B", "C", "D"], set(), 2, 1) == {"A", "B"}


def test_hysteresis_both_held_within_buffer_kept():
    assert select_with_hysteresis(["C", "A", "B", "D"], {"A", "B"}, 2, 1) == {"A", "B"}


# 8 months, 2 bars/month; sma_months=3 -> regime off until month 4, on months 4-6, off months 7-8.
_TR_MC = [100, 100, 100, 110, 110, 110, 95, 95]
def _tr_axis():
    dates, closes = [], []
    for i, c in enumerate(_TR_MC):
        dates += [dt.date(2026, i + 1, 15), dt.date(2026, i + 1, 28)]
        closes += [c, c]
    return dates, closes


def test_trend_regime_flips_on_then_off():
    dates, closes = _tr_axis()
    reg = trend_regime(dates, pd.Series(closes), sma_months=3, band=0.01)
    month_end = [bool(reg.iloc[i]) for i in range(1, len(dates), 2)]   # the 28th of each month
    assert month_end == [False, False, False, True, True, True, False, False]
```

- [ ] **Step 2: Run test to verify it fails** — `./.venv/bin/pytest tests/test_strategies.py -v` → FAIL (`ModuleNotFoundError: No module named 'autotrader.strategies'`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/strategies.py
"""Deterministic strategy rule modules (spec §2). Each strategy emits a CAUSAL target-weight
DataFrame (date x symbols, weights in [0,1]; cash = 1 - row.sum) that the Plan 04 engine
executes at the NEXT open. Stateful rules are computed by a causal forward scan inside
target_weights, so the frame never looks ahead. Strategy = intent; engine = execution.
"""
import pandas as pd
from autotrader.indicators import (sma, nearness_to_high, wilder_rsi, cumulative_rsi,
                                    trailing_return, monthly_closes, align_monthly_to_daily)


def _ref_dates(bars, anchor):
    """Reference daily date axis = the anchor symbol's bar dates. The Plan 04 engine MUST pass a
    bars dict whose every symbol is aligned to one shared NYSE-calendar date axis; this validates
    that contract and raises loudly on any misalignment, so a shorter-history symbol can never
    silently shift the position-indexed weight frame."""
    dates = list(bars[anchor]["date"])
    for s, df in bars.items():
        if list(df["date"]) != dates:
            raise ValueError(f"{s} bars are not aligned to the {anchor} date axis "
                             "(the engine must pass a calendar-aligned bars dict)")
    return dates


def select_with_hysteresis(order, held, n_hold, buffer):
    """Pick the N-name portfolio with rank hysteresis. `order` = symbols best-ranked first.
    Keep currently-held names still within rank <= N+buffer (priority, best-ranked first),
    then fill remaining slots with the best-ranked fresh names. A held name is sold only once
    it falls below rank N+buffer. Returns a set."""
    rank_of = {s: i + 1 for i, s in enumerate(order)}
    keep = [s for s in order if s in held and rank_of[s] <= n_hold + buffer]
    if len(keep) >= n_hold:
        return set(keep[:n_hold])
    fill = [s for s in order if s not in keep][:n_hold - len(keep)]
    return set(keep) | set(fill)


def trend_regime(dates, closes, sma_months=10, band=0.01, use_abs_momentum=False, abs_lookback=12):
    """Shared Faber trend signal as a daily boolean Series (risk-on/off), forward-filled from
    monthly decisions. Monthly close vs the `sma_months`-month SMA of monthly closes, with a
    +/-band no-action zone (Siegel whipsaw filter -> hysteresis: hold the prior state inside
    the band). Optional Antonacci absolute-momentum AND-filter (trailing `abs_lookback`-month
    total return > 0). Causal: a month-end decision uses only that month's completed close,
    aligned to the daily axis by date <= d. Warm-up / pre-first-signal = OFF (out of market)."""
    mc = monthly_closes(dates, closes)
    m_sma = sma(mc["close"], sma_months)
    m_mom = trailing_return(mc["close"], abs_lookback, skip=0) if use_abs_momentum else None
    state = False
    monthly_state = []
    for i in range(len(mc)):
        s = m_sma.iloc[i]
        c = mc["close"].iloc[i]
        if pd.isna(s):
            monthly_state.append(False)
            continue
        if c >= s * (1 + band):
            state = True
        elif c <= s * (1 - band):
            state = False
        if use_abs_momentum:
            mom = m_mom.iloc[i]
            on = state and (not pd.isna(mom)) and (mom > 0)
        else:
            on = state
        monthly_state.append(on)
    daily = align_monthly_to_daily(dates, list(mc["date"]),
                                   [1.0 if s else 0.0 for s in monthly_state])
    return (daily == 1.0).reset_index(drop=True)
```

- [ ] **Step 4: Run test to verify it passes** — `./.venv/bin/pytest tests/test_strategies.py -v` → PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/strategies.py tests/test_strategies.py
git commit -m "feat: strategy helpers — rank hysteresis + shared Faber trend regime"
```

---

## Task 2: S1 — Trend-following

**Files:** Modify `src/autotrader/strategies.py`, `tests/test_strategies.py`.

Hold the equity (SPY) when the trend regime is on, the bond ETF (IEF) when off. Faber MA-cross with the 1% band, optional Antonacci filter.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_strategies.py
from autotrader.strategies import S1Trend


def test_s1_holds_equity_on_risk_on_bond_on_risk_off():
    dates, closes = _tr_axis()
    bars = {"SPY": _bars(dates, closes), "IEF": _bars(dates, [50.0] * len(dates))}
    w = S1Trend(sma_months=3).target_weights(bars)
    spy_me = [w["SPY"].iloc[i] for i in range(1, len(dates), 2)]
    ief_me = [w["IEF"].iloc[i] for i in range(1, len(dates), 2)]
    assert spy_me == [0, 0, 0, 1, 1, 1, 0, 0]
    assert ief_me == [1, 1, 1, 0, 0, 0, 1, 1]
    assert S1Trend().cost_strategy is None and S1Trend().stop_loss_pct == 0.20
    # the engine must pass a calendar-aligned bars dict; misalignment raises loudly
    with pytest.raises(ValueError):
        S1Trend(sma_months=3).target_weights(
            {"SPY": _bars(dates, closes), "IEF": _bars(dates[:-1], [50.0] * (len(dates) - 1))})
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'S1Trend'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/strategies.py
class S1Trend:
    """Faber MA-cross (+ optional Antonacci filter). Hold the equity when risk-on, the bond
    ETF when risk-off (spec §2 S1)."""
    def __init__(self, equity="SPY", bond="IEF", sma_months=10, band=0.01,
                 use_abs_momentum=False, stop_loss_pct=0.20):
        self.equity, self.bond = equity, bond
        self.sma_months, self.band = sma_months, band
        self.use_abs_momentum = use_abs_momentum
        self.stop_loss_pct = stop_loss_pct
        self.cost_strategy = None
        self.universe = [equity, bond]

    def target_weights(self, bars):
        dates = _ref_dates(bars, self.equity)
        on = trend_regime(dates, bars[self.equity]["close"], self.sma_months, self.band,
                          self.use_abs_momentum)
        w = pd.DataFrame(0.0, index=range(len(dates)), columns=self.universe)
        w[self.equity] = on.astype(float).values
        w[self.bond] = (~on).astype(float).values
        return w
```

- [ ] **Step 4: Run** → PASS (1 passed for `-k s1`; full file 6 passed).

- [ ] **Step 5: Commit** — `git add src/autotrader/strategies.py tests/test_strategies.py && git commit -m "feat: S1 trend-following (Faber band, equity/bond switch)"`

---

## Task 3: S2 — Cross-sectional sector momentum

**Files:** Modify `src/autotrader/strategies.py`, `tests/test_strategies.py`.

Rank the sector SPDRs by 52-week-high nearness, hold the top N equal-weight with rank-buffer hysteresis, gated by the broad (SPY) trend (cash when the gate is off). **Note:** the prototype's first hysteresis attempt trimmed to N *after* unioning, which silently dropped a held name still inside the buffer; the fix routes selection through `select_with_hysteresis` (Task 1). The oracle below pins the corrected behavior — XLF is retained through `t=9` (still within the buffer) and only drops at `t=10`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_strategies.py
from autotrader.strategies import S2SectorMomentum

_S2_MDAYS = [8, 18, 27]
def _s2_axis():
    return [dt.date(2026, m, x) for m in [1, 2, 3, 4, 5] for x in _S2_MDAYS]
_S2_SECTORS = {
    "XLK": [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 11.0, 11.1, 11.2, 11.3, 11.4],
    "XLF": [10, 10, 10, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 11.0, 10.8, 10.6, 10.4, 10.2, 10.0],
    "XLE": [10, 9.8, 9.6, 9.7, 9.9, 10.1, 10.4, 10.8, 11.2, 11.6, 12.0, 12.4, 12.8, 13.2, 13.6],
    "XLV": [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.2, 9.1, 9.0, 8.9, 8.8, 8.7, 8.6],
}
def _s2_bars():
    dates = _s2_axis()
    bars = {k: _bars(dates, v) for k, v in _S2_SECTORS.items()}
    bars["SPY"] = _bars(dates, [v for v in [100, 106, 112, 118, 124] for _ in _S2_MDAYS])  # rising -> gate on from m3
    return dates, bars


def _held(w, sectors, t):
    return {s: round(w[s].iloc[t], 3) for s in sectors if w[s].iloc[t] > 0}


def test_s2_gated_topn_with_hysteresis():
    dates, bars = _s2_bars()
    w = S2SectorMomentum(list(_S2_SECTORS), gate_symbol="SPY", n_hold=2, buffer=1,
                         nearness_window=3, gate_sma_months=3, gate_band=0.01).target_weights(bars)
    for t in range(8):                                   # gate off -> all cash
        assert _held(w, _S2_SECTORS, t) == {}
    assert _held(w, _S2_SECTORS, 8) == {"XLK": 0.5, "XLF": 0.5}   # gate on; top-2 by nearness
    assert _held(w, _S2_SECTORS, 9) == {"XLK": 0.5, "XLF": 0.5}   # XLF kept by the buffer
    assert _held(w, _S2_SECTORS, 10) == {"XLK": 0.5, "XLE": 0.5}  # XLF falls past buffer -> XLE rotates in
    assert _held(w, _S2_SECTORS, 14) == {"XLK": 0.5, "XLE": 0.5}
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'S2SectorMomentum'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/strategies.py
class S2SectorMomentum:
    """Top-N sector SPDRs by 52-week-high nearness, equal-weight, with rank-buffer hysteresis,
    held only while the broad (gate_symbol) trend is on; else cash (spec §2 S2)."""
    def __init__(self, sectors, gate_symbol="SPY", n_hold=3, buffer=2, nearness_window=252,
                 gate_sma_months=10, gate_band=0.01, stop_loss_pct=0.20):
        self.sectors = list(sectors)
        self.gate_symbol = gate_symbol
        self.n_hold, self.buffer = n_hold, buffer
        self.nearness_window = nearness_window
        self.gate_sma_months, self.gate_band = gate_sma_months, gate_band
        self.stop_loss_pct = stop_loss_pct
        self.cost_strategy = None
        self.universe = self.sectors + [gate_symbol]

    def target_weights(self, bars):
        dates = _ref_dates(bars, self.gate_symbol)
        n = len(dates)
        near = pd.DataFrame({s: nearness_to_high(bars[s]["close"], self.nearness_window).values
                             for s in self.sectors})
        gate_on = trend_regime(dates, bars[self.gate_symbol]["close"],
                               self.gate_sma_months, self.gate_band).values
        held = set()
        w = pd.DataFrame(0.0, index=range(n), columns=self.universe)
        for t in range(n):
            row_valid = near.iloc[t].dropna()
            if not gate_on[t] or row_valid.empty:
                held = set()
                continue
            order = list(row_valid.sort_values(ascending=False).index)   # best-ranked first
            held = select_with_hysteresis(order, held, self.n_hold, self.buffer)
            if held:
                wt = 1.0 / len(held)
                for s in held:
                    w.iloc[t, w.columns.get_loc(s)] = wt
        return w
```

- [ ] **Step 4: Run** → PASS (`-k s2` 1 passed; full file 7 passed).

- [ ] **Step 5: Commit** — `git add src/autotrader/strategies.py tests/test_strategies.py && git commit -m "feat: S2 sector momentum (nearness rank, buffer hysteresis, trend gate)"`

---

## Task 4: S3 — Short-term mean reversion (the null-confirmation test)

**Files:** Modify `src/autotrader/strategies.py`, `tests/test_strategies.py`.

Per-ETF: regime `Close > SMA(regime)`; enter on `CumRSI(2,2) < entry`; exit on `RSI(2) > exit` OR `Close > SMA(exit)`; hard time-stop. Equal-weight across whatever is currently held. `cost_strategy="S3"` routes it to the punitive floor. Three tests pin entry/exit, the time-stop, and the regime block. (Synthetic note: the entry needs a sharp 2-day dip that keeps `close` above the regime SMA — a low baseline inside the SMA window achieves that with short test windows.)

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_strategies.py
from autotrader.strategies import S3MeanReversion

_S3_BASE = [100, 101, 99, 100, 102, 98, 101, 100, 99, 102, 140, 139, 141, 140]

def _s3_weights(closes, time_stop):
    dates = [dt.date(2026, 3, 1) + dt.timedelta(days=i) for i in range(len(closes))]
    w = S3MeanReversion(["QQQ"], regime_sma=10, exit_sma=3, cumrsi_entry=35, rsi_exit=65,
                        time_stop_days=time_stop).target_weights({"QQQ": _bars(dates, closes)})
    return [round(x, 2) for x in w["QQQ"].values]


def test_s3_enters_on_cumrsi_then_exits_on_sma():
    # sharp 2-day dip (CumRSI<35, close stays > SMA10) -> enter t15; close jumps > SMA3 -> exit t16.
    assert _s3_weights(_S3_BASE + [130, 122, 142, 143], 5) == [0.0] * 15 + [1.0, 0.0, 0.0]


def test_s3_time_stop_forces_exit():
    # same entry t15; price grinds below SMA3 (no SMA/RSI exit) -> time-stop after 3 days -> flat t18.
    assert _s3_weights(_S3_BASE + [130, 122, 121, 120, 119, 118, 124], 3) == [0.0] * 15 + [1.0, 1.0, 1.0, 0.0, 0.0, 0.0]


def test_s3_regime_blocks_entry_when_below_sma():
    # a deeper dip drives close BELOW SMA10 at the oversold bar -> regime filter blocks entry.
    assert _s3_weights(_S3_BASE + [118, 108, 120], 5) == [0.0] * 17


def test_s3_cost_strategy_is_floored():
    assert S3MeanReversion(["SPY"]).cost_strategy == "S3" and S3MeanReversion(["SPY"]).stop_loss_pct == 0.10
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'S3MeanReversion'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/strategies.py
class S3MeanReversion:
    """Short-term mean reversion null-test (spec §2 S3). Per-ETF: regime Close>SMA(regime_sma);
    enter on CumRSI(2,2)<cumrsi_entry; exit on RSI(2)>rsi_exit OR Close>SMA(exit_sma) OR after
    time_stop_days. Equal-weight across names in a position. Routed to the punitive S3 cost floor."""
    def __init__(self, etfs, regime_sma=200, exit_sma=5, cumrsi_entry=35.0, rsi_exit=65.0,
                 time_stop_days=10, stop_loss_pct=0.10):
        self.etfs = list(etfs)
        self.regime_sma, self.exit_sma = regime_sma, exit_sma
        self.cumrsi_entry, self.rsi_exit = cumrsi_entry, rsi_exit
        self.time_stop_days = time_stop_days
        self.stop_loss_pct = stop_loss_pct
        self.cost_strategy = "S3"
        self.universe = list(etfs)

    def target_weights(self, bars):
        dates = _ref_dates(bars, self.etfs[0])
        n = len(dates)
        sig = {}
        for s in self.etfs:
            c = bars[s]["close"]
            sig[s] = dict(close=c.reset_index(drop=True), sma_regime=sma(c, self.regime_sma),
                          sma_exit=sma(c, self.exit_sma), rsi=wilder_rsi(c), cumrsi=cumulative_rsi(c))
        in_pos = {s: None for s in self.etfs}
        flags = pd.DataFrame(0.0, index=range(n), columns=self.etfs)
        for t in range(n):
            for s in self.etfs:
                d = sig[s]
                close, sreg, sx = d["close"].iloc[t], d["sma_regime"].iloc[t], d["sma_exit"].iloc[t]
                rsi, cum = d["rsi"].iloc[t], d["cumrsi"].iloc[t]
                if in_pos[s] is None:
                    if (not pd.isna(sreg) and not pd.isna(cum) and close > sreg
                            and cum < self.cumrsi_entry):
                        in_pos[s] = t
                        flags.iloc[t, flags.columns.get_loc(s)] = 1.0
                else:
                    # regime (Close > SMA) is an ENTRY filter only (Connors); there is no
                    # regime-off exit — a held position leaves only on RSI/SMA5/time-stop.
                    held_days = t - in_pos[s]
                    exit_now = ((not pd.isna(rsi) and rsi > self.rsi_exit)
                                or (not pd.isna(sx) and close > sx)
                                or held_days >= self.time_stop_days)
                    if exit_now:
                        in_pos[s] = None
                    else:
                        flags.iloc[t, flags.columns.get_loc(s)] = 1.0
        counts = flags.sum(axis=1)
        return flags.div(counts.where(counts > 0, 1.0), axis=0)
```

- [ ] **Step 4: Run** → PASS (`-k s3` 4 passed; full file 11 passed).

- [ ] **Step 5: Commit** — `git add src/autotrader/strategies.py tests/test_strategies.py && git commit -m "feat: S3 mean-reversion null-test (regime, CumRSI entry, time-stop, S3 cost floor)"`

---

## Task 5: S4 — Trend-gated momentum blend

**Files:** Modify `src/autotrader/strategies.py`, `tests/test_strategies.py`.

Antonacci dual-momentum: the S1 trend regime decides risk-on/off; risk-on holds the S2 sector basket, risk-off holds the bond ETF. One coherent object (the primary blend).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_strategies.py
from autotrader.strategies import S4TrendGatedMomentum


def test_s4_bonds_when_off_basket_when_on():
    dates, bars = _s2_bars()
    bars = dict(bars); bars["IEF"] = _bars(dates, [50.0] * len(dates))
    s4 = S4TrendGatedMomentum(list(_S2_SECTORS), equity="SPY", bond="IEF", n_hold=2, buffer=1,
                              nearness_window=3, sma_months=3, band=0.01)
    w = s4.target_weights(bars)
    for t in range(8):                                       # regime off -> bonds
        assert _held(w, s4.universe, t) == {"IEF": 1.0}
    assert _held(w, s4.universe, 8) == {"XLK": 0.5, "XLF": 0.5}    # regime on -> S2 basket
    assert _held(w, s4.universe, 10) == {"XLK": 0.5, "XLE": 0.5}
    # invariant: no row over-allocates (would catch a bonds+sectors double-count if the
    # regime signal ever diverged between the S4 bond leg and the S2 gate)
    assert (w[s4.universe].sum(axis=1) <= 1.0 + 1e-9).all()
```

- [ ] **Step 2: Run** → FAIL (`ImportError: cannot import name 'S4TrendGatedMomentum'`).

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/strategies.py
class S4TrendGatedMomentum:
    """Trend-gated momentum (spec §2 S4 primary blend): S1's trend regime decides risk-on/off;
    risk-on hold the S2 sector basket, risk-off hold the bond ETF. The S2 basket is gated by the
    same trend on `equity`, so the sleeves share one regime signal."""
    def __init__(self, sectors, equity="SPY", bond="IEF", n_hold=3, buffer=2, nearness_window=252,
                 sma_months=10, band=0.01, stop_loss_pct=0.20):
        self.s2 = S2SectorMomentum(sectors, gate_symbol=equity, n_hold=n_hold, buffer=buffer,
                                   nearness_window=nearness_window, gate_sma_months=sma_months,
                                   gate_band=band, stop_loss_pct=stop_loss_pct)
        self.equity, self.bond = equity, bond
        self.sma_months, self.band = sma_months, band
        self.stop_loss_pct = stop_loss_pct
        self.cost_strategy = None
        self.universe = list(sectors) + [equity, bond]

    def target_weights(self, bars):
        dates = _ref_dates(bars, self.equity)
        on = trend_regime(dates, bars[self.equity]["close"], self.sma_months, self.band)
        basket = self.s2.target_weights(bars)
        w = pd.DataFrame(0.0, index=range(len(dates)), columns=self.universe)
        for s in self.s2.sectors:
            w[s] = basket[s].values
        w[self.bond] = (~on).astype(float).values
        return w
```

- [ ] **Step 4: Run** → PASS (`-k s4` 1 passed; full file 12 passed).

- [ ] **Step 5: Commit** — `git add src/autotrader/strategies.py tests/test_strategies.py && git commit -m "feat: S4 trend-gated momentum blend (S1 regime over S2 basket / bonds)"`

---

## Task 6: Cache-integrity checker (offline)

**Files:** Create `src/autotrader/datacheck.py`, `tests/test_datacheck.py`.

A pure checker that the data-population runbook (Task 7) runs on every fetched series. Unit-tested here on a synthetic calendar + DataFrames so the suite stays hermetic (no real cache needed).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_datacheck.py
import datetime as dt
import pandas as pd
import pytest
from autotrader.calendar_nyse import TradingCalendar
from autotrader.datacheck import verify_series

CAL = TradingCalendar([dt.date(2026, 1, 2), dt.date(2026, 1, 5), dt.date(2026, 1, 6),
                       dt.date(2026, 1, 7), dt.date(2026, 1, 8)])
_COLS = ["date", "open", "high", "low", "close", "volume"]

def _df(dates):
    n = len(dates)
    return pd.DataFrame({"date": dates, "open": [1.0] * n, "high": [1.0] * n, "low": [1.0] * n,
                         "close": [1.0] * n, "volume": [1] * n})[_COLS]


def test_clean_series_has_no_problems():
    df = _df([dt.date(2026, 1, 2), dt.date(2026, 1, 5), dt.date(2026, 1, 6), dt.date(2026, 1, 7)])
    assert verify_series(df, CAL, min_start=dt.date(2026, 1, 2), min_rows=3) == []


def test_gap_is_flagged():
    # missing 2026-01-06 (a calendar trading day) between 1/5 and 1/7
    df = _df([dt.date(2026, 1, 2), dt.date(2026, 1, 5), dt.date(2026, 1, 7)])
    probs = verify_series(df, CAL, min_start=dt.date(2026, 1, 2), min_rows=3)
    assert any("gap" in p for p in probs)


def test_late_start_and_too_few_rows_flagged():
    df = _df([dt.date(2026, 1, 6), dt.date(2026, 1, 7)])
    probs = verify_series(df, CAL, min_start=dt.date(2026, 1, 2), min_rows=3)
    assert any("starts" in p for p in probs) and any("rows" in p for p in probs)


def test_timestamp_date_column_flagged():
    df = _df(list(pd.to_datetime(["2026-01-02", "2026-01-05"])))  # Timestamps, not date
    assert any("datetime.date" in p for p in verify_series(df, CAL, dt.date(2026, 1, 2), 1))
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError: No module named 'autotrader.datacheck'`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/datacheck.py
"""Offline integrity check for a cached OHLCV series populated from the MCP (Task 7). Pure:
takes a loaded DataFrame + a TradingCalendar; returns a list of problem strings (empty = clean)."""
import datetime as dt

_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


def verify_series(df, calendar, min_start, min_rows):
    if list(df.columns) != _COLUMNS:
        return [f"unexpected columns: {list(df.columns)}"]
    dates = df["date"].tolist()
    if not dates:
        return ["empty series"]
    # Date dtype first, and return early: a Timestamp column would crash the date comparisons below.
    if any(isinstance(d, dt.datetime) for d in dates) or not all(isinstance(d, dt.date) for d in dates):
        return ["date column must hold datetime.date"]
    problems = []
    if dates != sorted(dates):
        problems.append("dates not sorted ascending")
    if len(set(dates)) != len(dates):
        problems.append("duplicate dates")
    if df[["open", "high", "low", "close"]].isna().any().any():
        problems.append("NaN in OHLC")
    if len(df) < min_rows:
        problems.append(f"too few rows: {len(df)} < {min_rows}")
    if dates[0] > min_start:
        problems.append(f"history starts {dates[0]} > required {min_start}")
    # Gap check: consecutive cached dates must be ADJACENT calendar trading days.
    for i in range(len(dates) - 1):
        if not calendar.is_trading_day(dates[i]):
            problems.append(f"{dates[i]} is not a calendar trading day")
            break
        if calendar.next_trading_day(dates[i]) != dates[i + 1]:
            problems.append(f"gap after {dates[i]}")
            break
    return problems
```

- [ ] **Step 4: Run** → PASS (4 passed).

- [ ] **Step 5: Commit** — `git add src/autotrader/datacheck.py tests/test_datacheck.py && git commit -m "feat: offline cache-integrity checker (gaps, dtype, coverage)"`

---

## Task 7: Data-population runbook — the gated live MCP pull

**Files:** populate `data/raw/`, `data/cache/`, append `data/manifest.csv`. **No pytest test** — this is an operational procedure whose acceptance gate is `verify_series` (Task 6) returning `[]` for every symbol.

> **LIVE / GATED — STOP HERE.** This is the project's first MCP call. It is **read-only** (`get_equity_historicals` — historical prices; no `place_/review_/cancel_equity_order`, no money). **Do NOT run this task without the operator's explicit go-ahead at execution time** (per `PROJECT_CONTEXT.md` hard guardrails and the promise to checkpoint before any live MCP touch). Python code never calls the MCP — the agent fetches JSON and the tested ingester (`parse_historicals`) normalizes it.

- [ ] **Step 1 (GATED — get explicit go-ahead first):** For the universe `SPY + config.SECTOR_SPDRS (9) + config.INDEX_ETFS (QQQ,DIA,IWM) + config.BOND_ETFS (IEF,AGG)` (16 symbols), call `get_equity_historicals` in ≤10-symbol batches (2 batches), `interval="day"`, `adjustment_type="all"`, bounds `regular`, paging the range back to `2005-01-01` in ≤2,500-bar windows (~3 pages each → ~6 calls). Save each raw response verbatim to `data/raw/<symbol>_day_all_p<page>.json`.

- [ ] **Step 2: Ingest + cache.** For each symbol: `parse_historicals` each page, `pd.concat`, `drop_duplicates(subset="date")`, sort by date, then `DataStore(cache_dir="data/cache").write(symbol, "day", "all", df)` (its validation enforces sorted/unique/date-dtype). Drop the `interpolated` last bar is already handled by `parse_historicals`.

- [ ] **Step 3: Manifest.** Append one row per symbol to `data/manifest.csv`: `symbol,interval,adjustment,fetched_at,start,end,n_rows,sha256` (sha256 of the cached parquet bytes) so every backtest verdict is reproducible from version control.

- [ ] **Step 4: Acceptance gate.** Build `cal = TradingCalendar.from_datastore(store, "SPY")`. For every symbol run `verify_series(store.load(sym,"day","all"), cal, min_start=date(2005,2,1), min_rows=4000)`. **Every symbol must return `[]`.** A non-empty result blocks the task — fix the fetch (re-page the gap) before committing. (Sector ETFs with later inception — e.g. XLRE/XLC are excluded from the 9; the 9 originals + SPY/QQQ/DIA/IWM/IEF/AGG all date to ≥2005, but if a symbol legitimately starts later, record its true start in the manifest and pass its own `min_start`.)

- [ ] **Step 5: Commit** — `git add data/manifest.csv && git commit -m "chore: populate cache for the universe (read-only MCP pull) + provenance manifest"` (raw pulls and parquet are gitignored; the manifest is tracked).

---

## Task 8: Full green + tag

- [ ] **Step 1: Run the entire suite** — `./.venv/bin/pytest -v` from the repo root.
Expected: ALL PASS. New tests: strategies (helpers 5, S1 1, S2 1, S3 4, S4 1 = 12) + datacheck 4 = **16 new**, on top of the 73 from Plans 01-02 = **89 passed**.

- [ ] **Step 2: Tag** — `git tag strategies-v1 -m "Strategy modules (S1-S4) + cache-integrity checker + populated cache — verified"`

---

## Self-review against the spec (completed by plan author)

- **§2 strategies:** S1 (Faber band + optional Antonacci, equity/bond switch), S2 (nearness rank + buffer hysteresis + trend gate), S3 (regime + CumRSI entry + RSI/SMA5 + time-stop, `cost_strategy="S3"`), S4 (trend-gated momentum = S1 regime over S2 basket / bonds). Each emits a causal weight frame; the 3-way S3 add-on *measurement* and benchmark comparisons are Plan 04.
- **§3.4 no look-ahead:** every weight row uses bars ≤ that date; monthly signals are forward-filled by `align_monthly_to_daily` and only *traded* at the engine's next open. Stateful scans (band, buffer, time-stop) advance forward only.
- **Cost wiring:** S3 carries `cost_strategy="S3"` so the Plan 04 engine prices it via `costs.roundtrip_cost_for_strategy("S3", tier, stress)` — the floor cannot be omitted.
- **Soft knobs:** all spec-range knobs are parameters with documented defaults (Plan 05 scans them); production windows used by default, short windows only in tests.
- **Data path:** offline `verify_series` is unit-tested; the live pull is a single **gated, read-only** runbook task with `verify_series([])` as its acceptance gate. Provenance manifest tracked; raw/parquet gitignored.
- **Deferred (correctly) → Plan 04+:** the backtest engine (walk-forward over the Simulator, next-open execution, stop placement, T+1 feasibility), benchmarks (gated-SPY, 60/40, EW-universe), metrics (deflated Sharpe, PBO, bootstrap CIs), the 3-way-vs-2-way blend measurement, and the robustness runner + paper-monitor.

---

## Roadmap position

Plan 03 of 5 (per the re-sequenced roadmap in `PROJECT_CONTEXT.md`). Builds on Plan 01 (foundation) + Plan 02 (indicators). Next: **Plan 04 — Backtest Engine, Metrics & Benchmarks** (drives these strategies' weight frames through the Simulator over the populated cache), then **Plan 05 — Robustness Runner & Paper-Monitor**.
