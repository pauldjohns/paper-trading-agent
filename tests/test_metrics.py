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


# ---------------------------------------------------------------------------
# Task 8: Trade & exposure metrics
# ---------------------------------------------------------------------------
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
                         dt.date(2021, 1, 1), dt.date(2021, 7, 1)], name="date")  # ~1.5 yrs
    # _years uses (last - first).days / 365.25; 547 days / 365.25 ≈ 1.4976 yrs (not exactly 1.5)
    yrs = (dt.date(2021, 7, 1) - dt.date(2020, 1, 1)).days / 365.25
    assert abs(turnover(trades, eq) - (1000.0 / 1000.0 / yrs)) < 1e-9


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


# ---------------------------------------------------------------------------
# Task 9: Deflated Sharpe Ratio + p-value (Bailey & López de Prado 2014)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Task 10: PBO via CSCV (Bailey-Borwein-LdP-Zhu 2017)
# ---------------------------------------------------------------------------
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


# ---------------------------------------------------------------------------
# Task 11: Bootstrap confidence intervals
# ---------------------------------------------------------------------------
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
