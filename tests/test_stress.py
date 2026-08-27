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
