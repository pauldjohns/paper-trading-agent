# tests/test_placebo.py
import datetime as dt
import numpy as np
import pandas as pd
import pytest
from autotrader import config
from autotrader.placebo import RandomSelection
from autotrader.placebo import placebo_distribution, placebo_95th, beats_placebo


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
