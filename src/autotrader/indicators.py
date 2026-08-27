# src/autotrader/indicators.py
"""Stateless indicator functions for the backtest harness.

Every function takes a pandas Series of prices (list/array also accepted) and returns a
float Series aligned to the input, with NaN during the warm-up window. No look-ahead: an
output at position t uses only inputs at positions <= t. Band/hysteresis/regime STATE lives
in the strategy layer (Plan 03), never here. Formula provenance is in each function's docstring.

POSITION-INDEX CONTRACT (read this): all price-series outputs are POSITION-indexed
(RangeIndex 0..n-1), aligned 1:1 to the input order. To attach a value to a calendar date,
zip it with the `date` column from the SAME `DataStore.load(...)` call (same row order, same
length). Do NOT pass a date-indexed Series in -- its index is dropped. Daily<->monthly signals
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


def sma(prices, window: int) -> pd.Series:
    """Simple moving average over `window` bars. NaN until `window` values are available."""
    if window < 1:
        raise ValueError("window must be >= 1")
    return _as_series(prices).rolling(window).mean()


def rolling_high(prices, window: int) -> pd.Series:
    """Rolling maximum over `window` bars. NaN until `window` values are available.
    Used with window=252 for the 52-week (trailing-252-trading-day) high."""
    if window < 1:
        raise ValueError("window must be >= 1")
    return _as_series(prices).rolling(window).max()


def nearness_to_high(prices, window: int = 252) -> pd.Series:
    """52-week-high nearness (George-Hwang 2004): close / trailing-`window` high of CLOSES.
    In (0, 1]; 1.0 at a fresh high. NaN until `window` values exist. Uses closes only, by
    design — intraday highs would inflate the denominator and shift the cross-sectional rank."""
    p = _as_series(prices)
    return p / rolling_high(p, window)


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


def cumulative_rsi(prices, rsi_period: int = 2, lookback: int = 2) -> pd.Series:
    """Connors Cumulative RSI: rolling `lookback`-bar sum of RSI(`rsi_period`). The S3 signal
    is CumulativeRSI(2,2) = RSI(2)_t + RSI(2)_{t-1}. NOT the 2012 ConnorsRSI composite
    (RSI(3)+streak-RSI+PercentRank) — a different indicator that shares the Connors name."""
    if lookback < 1:
        raise ValueError("lookback must be >= 1")
    return wilder_rsi(prices, rsi_period).rolling(lookback).sum()


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


def monthly_closes(dates, closes) -> pd.DataFrame:
    """Reduce daily bars to the close of the LAST TRADING DAY of each calendar month.
    Returns a DataFrame with columns [date, close], sorted ascending. Use these monthly
    closes for the Faber 10-month SMA and the Antonacci/12-1 momentum signals."""
    df = pd.DataFrame({"date": list(dates), "close": list(closes)})
    df = df.sort_values("date").reset_index(drop=True)
    ym = df["date"].map(lambda d: d.year * 12 + d.month)
    idx = df.groupby(ym)["date"].idxmax()           # last trading day in each month
    return df.loc[idx, ["date", "close"]].sort_values("date").reset_index(drop=True)


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
