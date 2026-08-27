# Indicator Library — Implementation Plan (Plan 02 of 5) — v1.1

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the pure-function indicator library every strategy depends on — daily returns, SMA, Wilder RSI(2), Cumulative RSI(2,2), rolling high + 52-week-high nearness, trailing/skip-month momentum, and month-end resampling — each unit-tested against a source-verified oracle.

**Architecture:** A single stateless module `src/autotrader/indicators.py`. Every function takes a pandas `Series` of prices (or a `(dates, prices)` pair for month-end resampling) and returns a pandas `Series` aligned to the input, with `NaN` during the warm-up window. **No look-ahead is possible at this layer** — each output position uses only inputs at or before that position. All band/hysteresis/regime *state* lives in the strategy layer (Plan 03), not here. The functions are the building blocks; the strategies compose them.

**Tech Stack:** Python 3.11, pandas, numpy, pytest. No network, no MCP — pure math against in-test fixtures. Builds on the Plan 01 package/venv (already installed).

**Reads:** [`STRATEGY_TESTING_SPEC.md`](STRATEGY_TESTING_SPEC.md) §2 (signal definitions) and §3.4 (no look-ahead). Formula provenance is recorded inline in each task.

**Reviewed (v1.1):** three independent reviews (methodology/look-ahead, executable-as-written, scope/architecture). The executable review built the code in a scratch dir and ran it green; the methodology review independently recomputed and confirmed the RSI(2) oracle. Changes from review: added Task 9 `align_monthly_to_daily` (one shared, look-ahead-correct daily↔monthly bridge instead of three per-strategy reimplementations); documented the position-index contract on `_as_series` and the module; added a flat-price RSI edge test; tightened Task 5's `-k` filter; corrected the self-review's S2-rank wording (cross-sectional rank is Plan-03 strategy logic, not an indicator).

**Scope guardrails:**
- OFFLINE only — no MCP, no network, nothing trades.
- This plan is **indicators only**. Do NOT build strategies (S1-S4), the data-population runbook, the backtest engine, or metrics — those are Plans 03-05.
- Indicators are **stateless pure functions**. The Faber 1% band, S2 rank hysteresis, S3 regime/time-stop, and the trend gate are *strategy state* — they belong to Plan 03, not here.
- Match the locked spec definitions exactly; the "honest-expectation" framing in the spec is context, not a knob to tune.

**Formula provenance (verified against primary/secondary sources during planning):**
- **Wilder RSI** seeding + smoothing: Wilder 1978 (*New Concepts in Technical Trading Systems*, pp. 63-70); StockCharts ChartSchool RSI. Seed = simple mean of the first `period` gains/losses; then `avg_t = (avg_{t-1}·(period-1) + current_t)/period`. `avgLoss=0 → RSI=100`; `avgGain=0 → RSI=0`.
- **Cumulative RSI(2,2)** = RSI(2)ₜ + RSI(2)ₜ₋₁: Connors & Alvarez 2008 (*Short Term Trading Strategies That Work*). **NOT** the 2012 "ConnorsRSI" composite (RSI(3)+streak-RSI+PercentRank) — a documented naming collision.
- **52-week-high nearness** = price / trailing-252-day high of **closes**: George & Hwang 2004 (*J. Finance*). Closes, not intraday highs (intraday highs inflate the denominator and shift the cross-sectional ranking).
- **12-1 momentum** = return from t-12 to t-1 (skip the most recent month): Jegadeesh & Titman 1993. **Antonacci absolute momentum** = trailing 12-month total return, no skip (Antonacci 2014); per spec the hurdle is `> 0`, not T-bills (a deliberate spec simplification).
- **Faber month-end** = close of the last trading day of each calendar month, on total-return data (Faber 2007). Signal is computed on the *completed* month-end bar; execution is next period (the Plan 03 engine fills at next open per spec §3.4).

---

## File structure (locked here)

```
src/autotrader/indicators.py     # all indicator functions (this plan)
tests/test_indicators.py         # unit tests with source-verified oracles
```

The RSI(2) oracle values below were computed from the Wilder recurrence and cross-checked against an independent hand calculation at the seed bars; the full table is reproduced in Task 5.

---

## Task 1: Module scaffold + daily returns

**Files:** Create `src/autotrader/indicators.py`, `tests/test_indicators.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_indicators.py
import math
import numpy as np
import pandas as pd
import pytest
from autotrader.indicators import daily_returns


def test_daily_returns_basic():
    r = daily_returns(pd.Series([100.0, 110.0, 99.0]))
    assert math.isnan(r.iloc[0])           # first bar has no prior close
    assert abs(r.iloc[1] - 0.10) < 1e-12   # 110/100 - 1
    assert abs(r.iloc[2] - (-0.10)) < 1e-12  # 99/110 - 1


def test_daily_returns_accepts_list_and_keeps_length():
    r = daily_returns([10.0, 12.0])
    assert len(r) == 2
    assert abs(r.iloc[1] - 0.2) < 1e-12
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/test_indicators.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autotrader.indicators'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/indicators.py
"""Stateless indicator functions for the backtest harness.

Every function takes a pandas Series of prices (list/array also accepted) and returns a
float Series aligned to the input, with NaN during the warm-up window. No look-ahead: an
output at position t uses only inputs at positions <= t. Band/hysteresis/regime STATE lives
in the strategy layer (Plan 03), never here. Formula provenance is in each function's docstring.

POSITION-INDEX CONTRACT (read this): all price-series outputs are POSITION-indexed
(RangeIndex 0..n-1), aligned 1:1 to the input order. To attach a value to a calendar date,
zip it with the `date` column from the SAME `DataStore.load(...)` call (same row order, same
length). Do NOT pass a date-indexed Series in — its index is dropped. Daily<->monthly signals
are bridged by `align_monthly_to_daily` (Task 9), not by index joins.
"""
import bisect
import pandas as pd


def _as_series(prices) -> pd.Series:
    """Coerce a list/array/Series of prices to a float64 Series with a FRESH RangeIndex.
    Any incoming index (including a DatetimeIndex) is intentionally dropped — indicators are
    position-indexed; align to dates via the parallel `date` column from the DataStore load."""
    return pd.Series(prices, dtype="float64").reset_index(drop=True)


def daily_returns(prices) -> pd.Series:
    """Simple one-period returns: r_t = price_t / price_{t-1} - 1. First element is NaN."""
    p = _as_series(prices)
    return p / p.shift(1) - 1.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/pytest tests/test_indicators.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/indicators.py tests/test_indicators.py
git commit -m "feat: indicators module scaffold + daily returns"
```

---

## Task 2: Simple moving average (SMA)

**Files:** Modify `src/autotrader/indicators.py`, `tests/test_indicators.py`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_indicators.py
from autotrader.indicators import sma


def test_sma_window_2():
    s = sma(pd.Series([1.0, 2.0, 3.0, 4.0]), window=2)
    assert math.isnan(s.iloc[0])          # warm-up: need `window` values
    assert s.iloc[1] == 1.5
    assert s.iloc[2] == 2.5
    assert s.iloc[3] == 3.5


def test_sma_window_3_warmup():
    s = sma(pd.Series([2.0, 4.0, 6.0, 8.0]), window=3)
    assert math.isnan(s.iloc[0]) and math.isnan(s.iloc[1])
    assert s.iloc[2] == 4.0                # (2+4+6)/3
    assert s.iloc[3] == 6.0                # (4+6+8)/3


def test_sma_rejects_bad_window():
    with pytest.raises(ValueError):
        sma(pd.Series([1.0, 2.0]), window=0)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/test_indicators.py -k sma -v`
Expected: FAIL — `ImportError: cannot import name 'sma'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/indicators.py
def sma(prices, window: int) -> pd.Series:
    """Simple moving average over `window` bars. NaN until `window` values are available."""
    if window < 1:
        raise ValueError("window must be >= 1")
    return _as_series(prices).rolling(window).mean()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/pytest tests/test_indicators.py -k sma -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/indicators.py tests/test_indicators.py
git commit -m "feat: SMA indicator with warm-up + window validation"
```

---

## Task 3: Rolling high

**Files:** Modify `src/autotrader/indicators.py`, `tests/test_indicators.py`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_indicators.py
from autotrader.indicators import rolling_high


def test_rolling_high_window_2():
    h = rolling_high(pd.Series([1.0, 3.0, 2.0, 5.0, 4.0]), window=2)
    assert math.isnan(h.iloc[0])
    assert h.iloc[1] == 3.0   # max(1,3)
    assert h.iloc[2] == 3.0   # max(3,2)
    assert h.iloc[3] == 5.0   # max(2,5)
    assert h.iloc[4] == 5.0   # max(5,4)


def test_rolling_high_full_window():
    h = rolling_high(pd.Series([10.0, 9.0, 8.0]), window=3)
    assert math.isnan(h.iloc[0]) and math.isnan(h.iloc[1])
    assert h.iloc[2] == 10.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/test_indicators.py -k rolling_high -v`
Expected: FAIL — `ImportError: cannot import name 'rolling_high'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/indicators.py
def rolling_high(prices, window: int) -> pd.Series:
    """Rolling maximum over `window` bars. NaN until `window` values are available.
    Used with window=252 for the 52-week (trailing-252-trading-day) high."""
    if window < 1:
        raise ValueError("window must be >= 1")
    return _as_series(prices).rolling(window).max()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/pytest tests/test_indicators.py -k rolling_high -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/indicators.py tests/test_indicators.py
git commit -m "feat: rolling_high (basis for 52-week-high nearness)"
```

---

## Task 4: 52-week-high nearness

**Files:** Modify `src/autotrader/indicators.py`, `tests/test_indicators.py`.

Provenance: George & Hwang 2004. Nearness = close / trailing-252-day high **of closes** (not intraday highs). Bounded (0, 1]; equals 1.0 exactly at a fresh high. Computed on completed bars (both numerator and the rolling max are anchored at position t — no look-ahead).

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_indicators.py
from autotrader.indicators import nearness_to_high


def test_nearness_at_fresh_high_is_one():
    n = nearness_to_high(pd.Series([10.0, 11.0, 12.0]), window=3)
    assert n.iloc[2] == 1.0   # 12 is the window high


def test_nearness_below_high_is_fraction():
    n = nearness_to_high(pd.Series([10.0, 12.0, 9.0]), window=3)
    assert math.isnan(n.iloc[0]) and math.isnan(n.iloc[1])
    assert abs(n.iloc[2] - (9.0 / 12.0)) < 1e-12   # 0.75


def test_nearness_uses_closes_not_intraday_highs():
    # Only closes are passed in; the function must not require/peek at any 'high' column.
    n = nearness_to_high([100.0, 90.0, 95.0], window=2)
    assert abs(n.iloc[2] - (95.0 / 95.0)) < 1e-12   # window=[90,95] -> high 95 -> 1.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/test_indicators.py -k nearness -v`
Expected: FAIL — `ImportError: cannot import name 'nearness_to_high'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/indicators.py
def nearness_to_high(prices, window: int = 252) -> pd.Series:
    """52-week-high nearness (George-Hwang 2004): close / trailing-`window` high of CLOSES.
    In (0, 1]; 1.0 at a fresh high. NaN until `window` values exist. Uses closes only, by
    design — intraday highs would inflate the denominator and shift the cross-sectional rank."""
    p = _as_series(prices)
    return p / rolling_high(p, window)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/pytest tests/test_indicators.py -k nearness -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/indicators.py tests/test_indicators.py
git commit -m "feat: 52-week-high nearness on closes (George-Hwang)"
```

---

## Task 5: Wilder RSI(2) — the methodology canary

**Files:** Modify `src/autotrader/indicators.py`, `tests/test_indicators.py`.

This is the highest-risk indicator (a wrong RSI fabricates or kills S3's edge). The oracle below was computed from the Wilder recurrence and verified; the seed bars (2-3) were cross-checked against an independent hand calculation.

**Oracle.** Closes `[44.00, 44.34, 44.09, 44.50, 44.22, 44.65, 44.85, 44.40, 44.90]`, RSI(2):

| bar | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|----|----|----|--------|--------|--------|--------|--------|--------|--------|
| RSI(2) | NaN | NaN | 57.6271 | 82.2695 | 45.8498 | 77.0519 | 85.0600 | 33.0929 | 71.6214 |

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_indicators.py
from autotrader.indicators import wilder_rsi

_RSI2_CLOSES = [44.00, 44.34, 44.09, 44.50, 44.22, 44.65, 44.85, 44.40, 44.90]
_RSI2_ORACLE = [None, None, 57.6271, 82.2695, 45.8498, 77.0519, 85.0600, 33.0929, 71.6214]


def test_wilder_rsi2_matches_oracle():
    r = wilder_rsi(pd.Series(_RSI2_CLOSES), period=2)
    assert math.isnan(r.iloc[0]) and math.isnan(r.iloc[1])   # warm-up = period+1 closes
    for i in range(2, len(_RSI2_CLOSES)):
        assert abs(r.iloc[i] - _RSI2_ORACLE[i]) < 1e-4, f"bar {i}: {r.iloc[i]} != {_RSI2_ORACLE[i]}"


def test_wilder_rsi2_all_up_is_100():
    r = wilder_rsi(pd.Series([10.0, 11.0, 12.0, 13.0]), period=2)
    assert r.iloc[2] == 100.0 and r.iloc[3] == 100.0   # avgLoss==0 -> 100


def test_wilder_rsi2_all_down_is_0():
    r = wilder_rsi(pd.Series([13.0, 12.0, 11.0, 10.0]), period=2)
    assert r.iloc[2] == 0.0 and r.iloc[3] == 0.0        # avgGain==0 -> 0


def test_wilder_rsi2_flat_prices_is_100():
    # All deltas zero -> avgLoss==0 -> RSI 100 (avgLoss==0 is checked before avgGain==0).
    # Locked, documented convention; harmless for S3 (CumRSI 200 never triggers its <35 entry).
    r = wilder_rsi(pd.Series([10.0, 10.0, 10.0, 10.0]), period=2)
    assert r.iloc[2] == 100.0 and r.iloc[3] == 100.0


def test_wilder_rsi_too_short_is_all_nan():
    r = wilder_rsi(pd.Series([10.0, 11.0]), period=2)   # need period+1 closes
    assert r.isna().all()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/test_indicators.py -k wilder_rsi -v`
Expected: FAIL — `ImportError: cannot import name 'wilder_rsi'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/indicators.py
def wilder_rsi(prices, period: int = 2) -> pd.Series:
    """Wilder's RSI (1978). Seed avg gain/loss = simple mean of the first `period` deltas,
    then Wilder smoothing avg_t = (avg_{t-1}*(period-1) + current_t)/period. First valid
    value is at index `period` (warm-up = period+1 closes). avgLoss==0 -> 100; avgGain==0 -> 0.
    A flat day (delta==0) contributes 0 to both gain and loss."""
    if period < 1:
        raise ValueError("period must be >= 1")
    p = _as_series(prices)
    n = len(p)
    rsi = pd.Series([float("nan")] * n)
    if n <= period:
        return rsi
    delta = p.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)

    def _rsi(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        if ag == 0:
            return 0.0
        return 100.0 - 100.0 / (1.0 + ag / al)

    avg_gain = gain.iloc[1:period + 1].mean()
    avg_loss = loss.iloc[1:period + 1].mean()
    rsi.iloc[period] = _rsi(avg_gain, avg_loss)
    for i in range(period + 1, n):
        avg_gain = (avg_gain * (period - 1) + gain.iloc[i]) / period
        avg_loss = (avg_loss * (period - 1) + loss.iloc[i]) / period
        rsi.iloc[i] = _rsi(avg_gain, avg_loss)
    return rsi
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/pytest tests/test_indicators.py -k wilder_rsi -v`
Expected: PASS (5 passed). If `test_wilder_rsi2_matches_oracle` fails, STOP — the smoothing or the seed is wrong (the whole point of this test); do not edit the oracle to match the code.

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/indicators.py tests/test_indicators.py
git commit -m "feat: Wilder RSI(2) with source-verified oracle + edge cases"
```

---

## Task 6: Cumulative RSI(2,2)

**Files:** Modify `src/autotrader/indicators.py`, `tests/test_indicators.py`.

Provenance: Connors & Alvarez 2008. CumRSI(2,2)ₜ = RSI(2)ₜ + RSI(2)ₜ₋₁ (rolling 2-bar sum of RSI(2)). First valid value at index 3 (needs RSI(2) at both t and t-1). **Not** the 2012 ConnorsRSI composite.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_indicators.py
from autotrader.indicators import cumulative_rsi


def test_cumulative_rsi22_is_sum_of_two_rsi2():
    closes = pd.Series(_RSI2_CLOSES)
    c = cumulative_rsi(closes, rsi_period=2, lookback=2)
    assert math.isnan(c.iloc[2])   # only one RSI value so far -> NaN
    # index 3 = RSI[2] + RSI[3] = 57.6271 + 82.2695
    assert abs(c.iloc[3] - (57.6271 + 82.2695)) < 1e-3
    # index 4 = RSI[3] + RSI[4] = 82.2695 + 45.8498
    assert abs(c.iloc[4] - (82.2695 + 45.8498)) < 1e-3


def test_cumulative_rsi_warmup_all_nan_when_too_short():
    c = cumulative_rsi(pd.Series([10.0, 11.0, 12.0]), rsi_period=2, lookback=2)
    assert c.isna().all()   # RSI valid only at index 2; need two RSI values for the sum
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/test_indicators.py -k cumulative -v`
Expected: FAIL — `ImportError: cannot import name 'cumulative_rsi'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/indicators.py
def cumulative_rsi(prices, rsi_period: int = 2, lookback: int = 2) -> pd.Series:
    """Connors Cumulative RSI: rolling `lookback`-bar sum of RSI(`rsi_period`). The S3 signal
    is CumulativeRSI(2,2) = RSI(2)_t + RSI(2)_{t-1}. NOT the 2012 ConnorsRSI composite
    (RSI(3)+streak-RSI+PercentRank) — a different indicator that shares the Connors name."""
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    return wilder_rsi(prices, rsi_period).rolling(lookback).sum()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/pytest tests/test_indicators.py -k cumulative -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/indicators.py tests/test_indicators.py
git commit -m "feat: Cumulative RSI(2,2) (Connors), not the 2012 composite"
```

---

## Task 7: Trailing & skip-month momentum

**Files:** Modify `src/autotrader/indicators.py`, `tests/test_indicators.py`.

One function serves both signals: `trailing_return(prices, lookback, skip)` = `prices_{t-skip} / prices_{t-lookback} - 1`.
- Antonacci absolute momentum (monthly): `trailing_return(monthly, 12, 0)` → `m_t / m_{t-12} - 1`, hurdle `> 0`.
- 12-1 momentum (Jegadeesh-Titman skip-month): `trailing_return(monthly, 12, 1)` → `m_{t-1} / m_{t-12} - 1`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_indicators.py
from autotrader.indicators import trailing_return


def test_trailing_return_no_skip():
    p = pd.Series([100.0, 110.0, 121.0])   # +10%, +10%
    r = trailing_return(p, lookback=2, skip=0)
    assert math.isnan(r.iloc[0]) and math.isnan(r.iloc[1])
    assert abs(r.iloc[2] - (121.0 / 100.0 - 1.0)) < 1e-12   # 0.21


def test_trailing_return_skip_one_drops_most_recent():
    p = pd.Series([100.0, 110.0, 121.0, 90.0])   # last bar is a crash, skipped by skip=1
    r = trailing_return(p, lookback=3, skip=1)
    # at t=3: p[t-1]/p[t-3] - 1 = 121/100 - 1, the -25% last bar is excluded
    assert abs(r.iloc[3] - (121.0 / 100.0 - 1.0)) < 1e-12


def test_trailing_return_validates_args():
    with pytest.raises(ValueError):
        trailing_return(pd.Series([1.0, 2.0]), lookback=1, skip=2)   # skip must be < lookback
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/test_indicators.py -k trailing -v`
Expected: FAIL — `ImportError: cannot import name 'trailing_return'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/indicators.py
def trailing_return(prices, lookback: int, skip: int = 0) -> pd.Series:
    """Total return from `lookback` bars ago to `skip` bars ago: p_{t-skip}/p_{t-lookback} - 1.
    skip=0 -> Antonacci 12-month absolute momentum (hurdle > 0, no skip).
    skip=1 -> Jegadeesh-Titman 12-1 momentum (skip the most recent bar/month).
    Apply to MONTHLY closes (see monthly_closes) for the month-based momentum signals."""
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    if not (0 <= skip < lookback):
        raise ValueError("skip must satisfy 0 <= skip < lookback")
    p = _as_series(prices)
    return p.shift(skip) / p.shift(lookback) - 1.0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/pytest tests/test_indicators.py -k trailing -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/indicators.py tests/test_indicators.py
git commit -m "feat: trailing_return (Antonacci abs-mom + 12-1 skip-month)"
```

---

## Task 8: Month-end resampling

**Files:** Modify `src/autotrader/indicators.py`, `tests/test_indicators.py`.

Faber/Antonacci signals are monthly. Reduce daily bars to the close of the **last trading day** of each calendar month (handles holidays/short months by using actual dates, not calendar-month-end). Input is the `date` and `close` columns from `DataStore.load(...)`.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_indicators.py
import datetime as dt
from autotrader.indicators import monthly_closes


def test_monthly_closes_takes_last_trading_day_per_month():
    dates = [dt.date(2026, 1, 29), dt.date(2026, 1, 30),   # Jan: last is 1/30
             dt.date(2026, 2, 2), dt.date(2026, 2, 27)]    # Feb: last is 2/27
    closes = [100.0, 101.0, 102.0, 103.0]
    m = monthly_closes(dates, closes)
    assert list(m["date"]) == [dt.date(2026, 1, 30), dt.date(2026, 2, 27)]
    assert list(m["close"]) == [101.0, 103.0]


def test_monthly_closes_sorts_and_handles_single_month():
    dates = [dt.date(2026, 3, 31), dt.date(2026, 3, 2)]   # unsorted input
    closes = [310.0, 302.0]
    m = monthly_closes(dates, closes)
    assert list(m["date"]) == [dt.date(2026, 3, 31)]      # last trading day of March
    assert list(m["close"]) == [310.0]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/test_indicators.py -k monthly -v`
Expected: FAIL — `ImportError: cannot import name 'monthly_closes'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/indicators.py
def monthly_closes(dates, closes) -> pd.DataFrame:
    """Reduce daily bars to the close of the LAST TRADING DAY of each calendar month.
    Returns a DataFrame with columns [date, close], sorted ascending. Use these monthly
    closes for the Faber 10-month SMA and the Antonacci/12-1 momentum signals."""
    df = pd.DataFrame({"date": list(dates), "close": list(closes)})
    df = df.sort_values("date").reset_index(drop=True)
    ym = df["date"].map(lambda d: d.year * 12 + d.month)
    idx = df.groupby(ym)["date"].idxmax()           # last trading day in each month
    return df.loc[idx, ["date", "close"]].sort_values("date").reset_index(drop=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/pytest tests/test_indicators.py -k monthly -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/indicators.py tests/test_indicators.py
git commit -m "feat: month-end resampling (last trading day per month)"
```

---

## Task 9: Daily↔monthly alignment bridge

**Files:** Modify `src/autotrader/indicators.py`, `tests/test_indicators.py`.

The Faber/Antonacci/trend-gate signals are computed on `monthly_closes` (~240 rows) but the
backtest engine iterates DAILY bars (~5,000 rows). This pure helper forward-fills a per-month
value onto the daily date axis so every strategy uses ONE tested, look-ahead-correct bridge
instead of reinventing it (review finding). For each daily date `d` it returns the value of
the latest month-end with `month_end_date <= d` (NaN before the first month-end).

**Actionability contract (do NOT bake a lag in here):** this helper is execution-agnostic — on
a month-end date `d` it returns that month-end's own value. The backtest engine supplies the
one-period lag by FILLING any resulting target change at the next open (spec §3.4). So a signal
from month-end M (computed from M's completed close) is only ever TRADED on the first trading
day after M — no look-ahead — while strategies still read a clean daily-aligned signal.

- [ ] **Step 1: Write the failing test**

```python
# add to tests/test_indicators.py
from autotrader.indicators import align_monthly_to_daily


def test_align_monthly_to_daily_forward_fills():
    daily = [dt.date(2026, 1, 30), dt.date(2026, 2, 2), dt.date(2026, 2, 27), dt.date(2026, 3, 2)]
    m_dates = [dt.date(2026, 1, 30), dt.date(2026, 2, 27)]
    m_vals = [1.0, 0.0]
    a = align_monthly_to_daily(daily, m_dates, m_vals)
    assert a.iloc[0] == 1.0   # 1/30 is a month-end -> its own value
    assert a.iloc[1] == 1.0   # 2/2  -> latest month-end <= 2/2 is 1/30
    assert a.iloc[2] == 0.0   # 2/27 is a month-end -> its own value
    assert a.iloc[3] == 0.0   # 3/2  -> latest month-end <= 3/2 is 2/27


def test_align_monthly_to_daily_nan_before_first_month_end():
    a = align_monthly_to_daily([dt.date(2026, 1, 5)], [dt.date(2026, 1, 30)], [1.0])
    assert a.isna().all()     # no month-end on or before 1/5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/bin/pytest tests/test_indicators.py -k align -v`
Expected: FAIL — `ImportError: cannot import name 'align_monthly_to_daily'`.

- [ ] **Step 3: Write minimal implementation**

```python
# add to src/autotrader/indicators.py  (bisect is imported at the top of the module)
def align_monthly_to_daily(daily_dates, monthly_dates, monthly_values) -> pd.Series:
    """Forward-fill per-month values onto a daily date axis. For each daily date d, return the
    value of the latest month-end with date <= d (NaN before the first). Returns a position-
    indexed float Series aligned to `daily_dates` (assumed ascending, as DataStore returns).
    Execution-agnostic: the engine adds the next-open fill lag (see the contract above)."""
    pairs = sorted(zip(list(monthly_dates), list(monthly_values)), key=lambda x: x[0])
    m_dates = [d for d, _ in pairs]
    m_vals = [v for _, v in pairs]
    out = []
    for d in daily_dates:
        j = bisect.bisect_right(m_dates, d) - 1   # latest month-end on or before d
        out.append(m_vals[j] if j >= 0 else float("nan"))
    return pd.Series(out, dtype="float64")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `./.venv/bin/pytest tests/test_indicators.py -k align -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader/indicators.py tests/test_indicators.py
git commit -m "feat: align_monthly_to_daily bridge (one look-ahead-correct daily/monthly join)"
```

---

## Task 10: Full green + tag

- [ ] **Step 1: Run the entire suite** — `./.venv/bin/pytest -v` from the repo root.
Expected: ALL PASS. New indicator tests: daily_returns 2, sma 3, rolling_high 2, nearness 3, wilder_rsi 5, cumulative 2, trailing 3, monthly 2, align 2 = **24 new tests**, on top of the 49 from Plan 01 = **73 passed**.

- [ ] **Step 2: Tag** — `git tag indicators-v1 -m "Indicator library: returns, SMA, RSI(2)-Wilder, CumRSI, 52wk nearness, momentum, month-end, daily/monthly bridge — verified"`

---

## Self-review against the spec (completed by plan author)

- **§2 signal building blocks covered:** SMA (Faber 10-mo, S3 SMA200/SMA5), Wilder RSI(2) + CumRSI(2,2) (S3 entry/exit), 52-wk-high nearness (the per-ticker S2 input — the *cross-sectional* rank across the 9 SPDRs is strategy composition, Plan 03, not an indicator), trailing_return (Antonacci abs-mom + 12-1 cross-check), monthly_closes + align_monthly_to_daily (Faber/Antonacci monthly basis bridged to the daily engine), daily_returns (metrics/returns). The *application* of these (band, hysteresis, regime, gate, time-stop) is strategy state → Plan 03.
- **§3.4 no look-ahead:** every function is causal — output at t uses inputs ≤ t; warm-up emits NaN; nothing reads a future bar or the `get_equity_fundamentals` snapshot. RSI seed/recurrence and the 52-wk high both anchor on completed bars.
- **Oracle integrity:** the RSI(2) oracle is source-derived and the seed bars cross-checked independently; the matches-oracle test is the canary (do not edit the oracle to fit the code).
- **Naming-collision guard:** CumRSI(2,2) is the Connors 2008 sum-of-RSI(2), explicitly not the 2012 ConnorsRSI composite (in-code docstring).
- **Type consistency:** all price functions take `prices` (Series/list) and return a `pd.Series` of equal length, NaN warm-up; `monthly_closes` takes `(dates, closes)` and returns a `[date, close]` DataFrame. `_as_series` is the shared coercion helper.
- **No placeholders:** every task has the failing test, the exact implementation, the run command + expected output, and a commit.
- **Deferred (correctly):** S1-S4 strategy modules; the Faber 1% band + S2 cross-sectional rank & hysteresis + S3 regime/time-stop + trend gate (all stateful strategy logic); the regime boolean `close > sma(closes, 200)` (a trivial elementwise compare — NaN during the SMA warm-up reads as not-in-regime — standardized in Plan 03 so the three strategies don't each handle the warm-up differently); the live data-population runbook; the backtest engine; and metrics → Plans 03-05. Daily-aligned monthly signals are produced by `align_monthly_to_daily` (Task 9) and TRADED at the next open by the Plan 04 engine (the one-period actionability lag).

---

## Roadmap note — recommended re-sequencing

The original 4-plan roadmap bundled "Indicators & Strategies" as Plan 02. That single phase is ~20 TDD tasks across three separable subsystems (pure-math indicators, stateful strategies, and the live data pull). Per the writing-plans scope guidance, this plan covers **only the indicator library** so it stays focused, fully offline, and golden-testable on its own. Proposed sequence:

- **Plan 02 — Indicator Library** (this plan).
- **Plan 03 — Strategy Modules + Data Population:** S1 trend (Faber band + Antonacci filter), S2 sector-momentum (rank + hysteresis + trend gate), S3 mean-reversion null-test (regime/time-stop, wired to `roundtrip_cost_for_strategy("S3", …)`), S4 trend-gated blend; plus the read-only MCP data-population runbook (the first live MCP touch — gated on the operator's go-ahead) + a cache-integrity test.
- **Plan 04 — Backtest Engine, Metrics & Benchmarks:** walk-forward engine over the simulator; CS overnight-gap adjustment + stress-from-data; deflated Sharpe / PBO / bootstrap CIs; the four benchmarks; report.
- **Plan 05 — Robustness Runner & Paper-Monitor:** plateau scan, stress folds, rebalance-day dispersion, placebo; paper-monitor.

If you'd rather keep the original 4-plan numbering and fold strategies back in here, say so and I'll merge them — but I recommend the split.
