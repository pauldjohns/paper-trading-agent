# src/autotrader_live/strategy_trend.py
"""Today-decision function for the single-name trend strategy (LIVE-01, Task T1.2).

Computes ONE entry/exit decision for the LAST (completed/settled) row of a bars
DataFrame. The caller is responsible for ensuring the last row is a fully settled
bar — this module does NOT do the completed-bar guard (that is T3.1).

Reuses ONLY the already-trusted, read-only indicators from:
  - autotrader.indicators  (sma, trailing_return, nearness_to_high)
  - autotrader_live.indicators_ohlc  (atr, donchian)

FIREWALL: this module does NOT edit or import-mutate anything in src/autotrader/.

Sub-signal wiring (all position-indexed, no look-ahead):
  - sma200          = sma(close, 200)[t]
  - trend_ok        = close[t] > sma200
  - mom_252         = trailing_return(close, 252, skip=0)[t]
  - momentum_ok     = mom_252 > 0
  - nearness        = nearness_to_high(close, 252)[t]
  - near_high       = nearness >= near_threshold  (FLAG A)
  - prior_donch_upper = donchian(high, low, 55)["upper"].shift(1).iloc[t]  (FLAG B)
  - breakout_55     = close[t] > prior_donch_upper
  - entry           = trend_ok AND momentum_ok AND (near_high OR breakout_55)
  - atr14           = atr(high, low, close, 14)[t]

Burn-in: needs max(200, 252, 56) = 253 bars for every indicator to be non-NaN at t.
If any of sma200, mom_252, nearness, prior_donch_upper, atr14 is NaN, the decision
is "insufficient_history": entry=False, reason="insufficient_history".
"""
import dataclasses
import datetime
import math

import pandas as pd

from autotrader.indicators import sma, trailing_return, nearness_to_high
from autotrader_live.indicators_ohlc import atr, donchian

_REQUIRED_COLUMNS = {"date", "high", "low", "close"}


@dataclasses.dataclass(frozen=True)
class TrendDecision:
    """Frozen record for one today-decision.

    Booleans are guaranteed to be Python bool (not numpy.bool_). NaN floats are
    float('nan'). Serialise with dataclasses.asdict() or .to_dict().
    """
    signal_date: datetime.date
    symbol: str
    close: float
    sma200: float
    trend_ok: bool
    mom_252: float
    momentum_ok: bool
    nearness: float
    near_high: bool
    prior_donch_upper: float
    breakout_55: bool
    atr14: float
    entry: bool
    reason: str

    def to_dict(self) -> dict:
        """Return a plain dict suitable for flat-JSON serialisation."""
        return dataclasses.asdict(self)


def decide(bars: pd.DataFrame, symbol: str, *,
           near_threshold: float = 0.90) -> TrendDecision:
    """Compute the today-decision for the last row of `bars`.

    Parameters
    ----------
    bars:
        DataFrame with AT LEAST columns ["date", "high", "low", "close"].
        Must be ascending by date. ≥ 1 row required. Extra columns (open,
        volume) are accepted and ignored — the DataStore schema is
        [date, open, high, low, close, volume].
    symbol:
        Ticker string; stored verbatim in the returned record.
    near_threshold:
        Threshold for near_high (FLAG A): nearness >= near_threshold.
        Default 0.90 (within 10% of the trailing-252 high; ratified by the operator
        2026-06-22). Must be in (0, 1].

    Returns
    -------
    TrendDecision
        Frozen dataclass. reason=="ok" if history was sufficient, else
        "insufficient_history".

    Raises
    ------
    ValueError
        If required columns are missing or bars is empty.
    """
    # --- Validation ---
    if bars is None or len(bars) == 0:
        raise ValueError("bars must be a non-empty DataFrame")

    missing = _REQUIRED_COLUMNS - set(bars.columns)
    if missing:
        # Sort for deterministic error message
        raise ValueError(
            f"bars is missing required column(s): {sorted(missing)}"
        )

    if not (0.0 < near_threshold <= 1.0):
        raise ValueError(
            f"near_threshold must be in (0, 1]; got {near_threshold!r}"
        )

    t = len(bars) - 1

    # Extract sequences (position-indexed, RangeIndex safe — indicators call
    # _as_series internally and reset_index, so passing a slice is fine).
    close = bars["close"]
    high = bars["high"]
    low = bars["low"]

    signal_date = bars["date"].iloc[t]
    # Ensure date is a Python datetime.date (not Timestamp)
    if hasattr(signal_date, "date"):
        signal_date = signal_date.date()

    close_t = float(close.iloc[t])

    # --- Compute all indicators ---
    sma200_series = sma(close, 200)
    mom_252_series = trailing_return(close, 252, skip=0)
    nearness_series = nearness_to_high(close, 252)
    donchian_df = donchian(high, low, 55)
    prior_donch_upper_series = donchian_df["upper"].shift(1)
    atr14_series = atr(high, low, close, 14)

    sma200_val = float(sma200_series.iloc[t])
    mom_252_val = float(mom_252_series.iloc[t])
    nearness_val = float(nearness_series.iloc[t])
    prior_donch_upper_val = float(prior_donch_upper_series.iloc[t])
    atr14_val = float(atr14_series.iloc[t])

    # --- Burn-in check ---
    nan_guard = [sma200_val, mom_252_val, nearness_val,
                 prior_donch_upper_val, atr14_val]
    if any(math.isnan(v) for v in nan_guard):
        # Sentinel record: insufficient history → all signal booleans False, float
        # sub-signals may be NaN. Same field set as the normal path (below).
        return TrendDecision(
            signal_date=signal_date,
            symbol=symbol,
            close=close_t,
            sma200=sma200_val,
            trend_ok=False,
            mom_252=mom_252_val,
            momentum_ok=False,
            nearness=nearness_val,
            near_high=False,
            prior_donch_upper=prior_donch_upper_val,
            breakout_55=False,
            atr14=atr14_val,
            entry=False,
            reason="insufficient_history",
        )

    # --- Sub-signals ---
    trend_ok: bool = bool(close_t > sma200_val)
    momentum_ok: bool = bool(mom_252_val > 0)
    near_high: bool = bool(nearness_val >= near_threshold)
    breakout_55: bool = bool(close_t > prior_donch_upper_val)
    entry: bool = bool(trend_ok and momentum_ok and (near_high or breakout_55))

    return TrendDecision(
        signal_date=signal_date,
        symbol=symbol,
        close=close_t,
        sma200=sma200_val,
        trend_ok=trend_ok,
        mom_252=mom_252_val,
        momentum_ok=momentum_ok,
        nearness=nearness_val,
        near_high=near_high,
        prior_donch_upper=prior_donch_upper_val,
        breakout_55=breakout_55,
        atr14=atr14_val,
        entry=entry,
        reason="ok",
    )
