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
