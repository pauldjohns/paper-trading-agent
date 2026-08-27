# src/autotrader_live/indicators_ohlc.py
"""OHLC-consuming indicator functions for the live/paper-monitor track.

Every function is pure and stateless: list/array/Series inputs in, pandas Series (or
DataFrame for donchian) out. All outputs are POSITION-indexed (RangeIndex 0..n-1),
aligned 1:1 to input order — any incoming index is dropped. NaN during warm-up; no
look-ahead (output at t uses only inputs at positions ≤ t).

FIREWALL: this module lives in autotrader_live, NOT in autotrader.indicators (the
protected offline harness). It defines its own _as_series helper — it does NOT import
the private autotrader.indicators._as_series.
"""
import pandas as pd


def _as_series(x) -> pd.Series:
    """Coerce a list/array/Series to a float64 Series with a fresh RangeIndex."""
    return pd.Series(x, dtype="float64").reset_index(drop=True)


def true_range(high, low, close) -> pd.Series:
    """Wilder True Range (Wilder 1978, New Concepts in Technical Trading Systems).

    TR[0] = high[0] - low[0]  (no prior close).
    TR[t] = max(high[t]-low[t], |high[t]-close[t-1]|, |low[t]-close[t-1]|) for t ≥ 1.

    No NaN (defined for every bar). Raises ValueError if input lengths differ.
    """
    h = _as_series(high)
    l = _as_series(low)
    c = _as_series(close)
    if not (len(h) == len(l) == len(c)):
        raise ValueError(
            f"high, low, close must have equal length; got {len(h)}, {len(l)}, {len(c)}"
        )
    n = len(h)
    tr = [0.0] * n
    tr[0] = h.iloc[0] - l.iloc[0]
    for t in range(1, n):
        hl = h.iloc[t] - l.iloc[t]
        hc = abs(h.iloc[t] - c.iloc[t - 1])
        lc = abs(l.iloc[t] - c.iloc[t - 1])
        tr[t] = max(hl, hc, lc)
    return pd.Series(tr, dtype="float64")


def atr(high, low, close, period: int = 14) -> pd.Series:
    """Wilder Average True Range (Wilder 1978).

    Seeded with a simple average of the first `period` TRs, then Wilder-smoothed:
        ATR[period-1] = mean(TR[0:period])
        ATR[t]        = (ATR[t-1]*(period-1) + TR[t]) / period  for t ≥ period

    NaN for index < period-1. If n < period, all values are NaN.
    Raises ValueError for period < 1 or mismatched input lengths.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    h = _as_series(high)
    l = _as_series(low)
    c = _as_series(close)
    if not (len(h) == len(l) == len(c)):
        raise ValueError(
            f"high, low, close must have equal length; got {len(h)}, {len(l)}, {len(c)}"
        )
    n = len(h)
    tr = true_range(h, l, c)
    out = [float("nan")] * n
    if n < period:
        return pd.Series(out, dtype="float64")
    # Seed: simple average of first `period` TRs
    seed = tr.iloc[:period].mean()
    out[period - 1] = seed
    for t in range(period, n):
        out[t] = (out[t - 1] * (period - 1) + tr.iloc[t]) / period
    return pd.Series(out, dtype="float64")


def donchian(high, low, window: int) -> pd.DataFrame:
    """Donchian Channel (Donchian 1960s).

    upper[t] = max(high[t-window+1 .. t])
    lower[t] = min(low[t-window+1 .. t])

    NaN until `window` bars are available. Returns a DataFrame with columns
    exactly ["upper", "lower"], position-indexed.
    Raises ValueError for window < 1 or mismatched input lengths.
    """
    if window < 1:
        raise ValueError("window must be >= 1")
    h = _as_series(high)
    l = _as_series(low)
    if len(h) != len(l):
        raise ValueError(f"high and low must have equal length; got {len(h)}, {len(l)}")
    upper = h.rolling(window).max().reset_index(drop=True)
    lower = l.rolling(window).min().reset_index(drop=True)
    return pd.DataFrame({"upper": upper, "lower": lower})


def ema(prices, period: int) -> pd.Series:
    """SMA-seeded Exponential Moving Average.

    alpha = 2 / (period + 1)
    EMA[period-1] = mean(prices[0:period])              (SMA seed)
    EMA[t]        = alpha*prices[t] + (1-alpha)*EMA[t-1]  for t ≥ period

    NaN for index < period-1. If n < period, all values are NaN.
    Raises ValueError for period < 1.
    """
    if period < 1:
        raise ValueError("period must be >= 1")
    p = _as_series(prices)
    n = len(p)
    out = [float("nan")] * n
    if n < period:
        return pd.Series(out, dtype="float64")
    alpha = 2.0 / (period + 1)
    # Seed: simple average of first `period` bars
    out[period - 1] = p.iloc[:period].mean()
    for t in range(period, n):
        out[t] = alpha * p.iloc[t] + (1 - alpha) * out[t - 1]
    return pd.Series(out, dtype="float64")
