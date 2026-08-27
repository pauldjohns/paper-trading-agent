# src/autotrader/metrics.py
"""Pure performance metrics on a date-indexed equity/returns Series and a list of Trade objects.
Annualization: 252 trading days for vol/Sharpe/Sortino; 365.25 calendar days/yr for CAGR. All
functions are deterministic; statistical functions (DSR, PBO, bootstrap) seed their RNG. No scipy
— the normal CDF/inverse come from statistics.NormalDist (Task 9). Formula provenance in docstrings.
"""
import itertools
import math
from statistics import NormalDist
import numpy as np
import pandas as pd

_ANN = 252
_NORM = NormalDist()


def _years(equity: pd.Series) -> float:
    days = (equity.index[-1] - equity.index[0]).days
    return days / 365.25 if days > 0 else float("nan")


def cagr(equity: pd.Series) -> float:
    """Compound annual growth rate on calendar time. equity[0]>0 assumed."""
    yrs = _years(equity)
    if not yrs or yrs != yrs or equity.iloc[0] <= 0:
        return float("nan")
    return (equity.iloc[-1] / equity.iloc[0]) ** (1.0 / yrs) - 1.0


def annualized_vol(returns: pd.Series) -> float:
    r = pd.Series(returns).dropna()
    return float(r.std(ddof=1) * math.sqrt(_ANN)) if len(r) > 1 else 0.0


def sharpe(returns: pd.Series, rf: float = 0.0) -> float:
    """Annualized Sharpe (rf per-period, default 0). Zero stdev -> 0.0 (never NaN)."""
    r = pd.Series(returns).dropna() - rf
    sd = r.std(ddof=1) if len(r) > 1 else 0.0
    return float(r.mean() / sd * math.sqrt(_ANN)) if sd and sd > 0 else 0.0


def sortino(returns: pd.Series, target: float = 0.0) -> float:
    """Annualized Sortino: mean excess / downside deviation (RMS of negative deviations vs target)."""
    r = pd.Series(returns).dropna() - target
    dd = np.sqrt(np.mean(np.minimum(r.values, 0.0) ** 2)) if len(r) else 0.0
    return float(r.mean() / dd * math.sqrt(_ANN)) if dd and dd > 0 else 0.0


def max_drawdown(equity: pd.Series) -> float:
    """Most-negative peak-to-trough fraction of the equity curve (e.g. -0.5). 0.0 if monotone up."""
    e = pd.Series(equity).astype("float64")
    dd = e / e.cummax() - 1.0
    return float(dd.min()) if len(e) else float("nan")


def calmar(equity: pd.Series) -> float:
    """CAGR / |max drawdown|. Zero drawdown -> nan (undefined)."""
    mdd = max_drawdown(equity)
    return float(cagr(equity) / abs(mdd)) if mdd and mdd < 0 else float("nan")


# ---------------------------------------------------------------------------
# Task 8: Trade & exposure metrics
# ---------------------------------------------------------------------------

def win_rate(trades) -> float:
    return sum(1 for t in trades if t.pnl > 0) / len(trades) if trades else float("nan")


def avg_win_loss(trades):
    wins = [t.pnl for t in trades if t.pnl > 0]
    losses = [t.pnl for t in trades if t.pnl < 0]
    return (float(np.mean(wins)) if wins else 0.0, float(np.mean(losses)) if losses else 0.0)


def profit_factor(trades) -> float:
    gains = sum(t.pnl for t in trades if t.pnl > 0)
    losses = -sum(t.pnl for t in trades if t.pnl < 0)
    if losses == 0:
        return float("inf") if gains > 0 else float("nan")
    return gains / losses


def turnover(trades, equity: pd.Series) -> float:
    """Annualized one-way turnover: total dollars bought / mean equity / years."""
    bought = sum(t.dollars for t in trades)
    yrs = _years(equity)
    mean_eq = float(pd.Series(equity).mean())
    if not yrs or yrs != yrs or mean_eq <= 0:
        return float("nan")
    return bought / mean_eq / yrs


def time_in_market(weights: pd.DataFrame) -> float:
    """Mean daily invested fraction (dollar-weighted exposure) = mean of row sums of realized weights."""
    if weights is None or len(weights) == 0:
        return float("nan")
    return float(weights.sum(axis=1).clip(upper=1.0).mean())


def trades_per_year(trades, equity: pd.Series) -> float:
    yrs = _years(equity)
    return len(trades) / yrs if yrs and yrs == yrs else float("nan")


def number_of_bets(weights: pd.DataFrame) -> int:
    """Count of dates on which the held SET (non-zero-weight columns) changed from the prior row —
    the spec's 'independent bets' (regime calls / rebalances), not raw trade count (spec §4)."""
    sets = [frozenset(c for c in weights.columns if weights[c].iloc[i] > 1e-12)
            for i in range(len(weights))]
    return sum(1 for i in range(1, len(sets)) if sets[i] != sets[i - 1])


# ---------------------------------------------------------------------------
# Task 9: Deflated Sharpe Ratio + p-value (Bailey & López de Prado 2014)
# ---------------------------------------------------------------------------

_EULER = 0.5772156649015329


def _sr0_expected_max(sr_variance: float, n_trials: int) -> float:
    """Expected maximum (per-observation) Sharpe under the null of `n_trials` independent trials
    with cross-trial Sharpe variance `sr_variance` (Bailey-LdP eq. for SR0)."""
    if n_trials < 2 or sr_variance <= 0:
        return 0.0
    z1 = _NORM.inv_cdf(1.0 - 1.0 / n_trials)
    z2 = _NORM.inv_cdf(1.0 - 1.0 / (n_trials * math.e))
    return math.sqrt(sr_variance) * ((1.0 - _EULER) * z1 + _EULER * z2)


def deflated_sharpe(returns, n_trials: int, sr_variance: float):
    """Deflated Sharpe Ratio (probability the true Sharpe > SR0) and its p-value = 1 - DSR.
    `returns`: per-period return Series. `n_trials`: number of configurations tried (multiple-
    testing count). `sr_variance`: variance of Sharpe across those trials. Spec §3.9 kills at
    p >= 0.05. Returns (dsr, p)."""
    r = pd.Series(returns).dropna()
    T = len(r)
    if T < 3:
        return (float("nan"), float("nan"))
    sd = r.std(ddof=1)
    if not sd or sd <= 0:
        return (float("nan"), float("nan"))
    sr = float(r.mean() / sd)                       # per-observation Sharpe
    g3 = float(r.skew())
    g4 = float(r.kurtosis() + 3.0)                  # pandas kurtosis is EXCESS; DSR wants raw (normal=3)
    sr0 = _sr0_expected_max(sr_variance, n_trials)
    denom = math.sqrt(max(1e-12, 1.0 - g3 * sr + ((g4 - 1.0) / 4.0) * sr * sr))
    z = (sr - sr0) * math.sqrt(T - 1) / denom
    dsr = _NORM.cdf(z)
    return (dsr, 1.0 - dsr)


# ---------------------------------------------------------------------------
# Task 10: Probability of Backtest Overfitting via CSCV
#          (Bailey-Borwein-LdP-Zhu 2017, SSRN 2326253)
# ---------------------------------------------------------------------------

def _block_sharpe(block: np.ndarray) -> np.ndarray:
    """Per-column Sharpe over a stacked block of rows (ddof=1). Zero-variance column -> 0."""
    mean = block.mean(axis=0)
    sd = block.std(axis=0, ddof=1)
    out = np.zeros_like(mean)
    nz = sd > 0
    out[nz] = mean[nz] / sd[nz]
    return out


def pbo_cscv(matrix: pd.DataFrame, n_blocks: int = 16):
    """Probability of Backtest Overfitting via Combinatorially Symmetric Cross-Validation
    (Bailey-Borwein-LdP-Zhu 2017, SSRN 2326253). `matrix`: rows=time, cols=configurations of
    per-period returns. Splits rows into `n_blocks` even sub-blocks; for each C(S,S/2) IS/OOS
    split, takes the IS-argmax config and records whether its OOS performance is below median
    (logit lambda <= 0). Returns the fraction of splits that are overfit. Spec §3.9 kills at PBO >= 0.5.

    Tie handling note: `argsort` assigns ranks by index on exact ties, which is not a true
    mid-rank. Exact ties arise only from degenerate constant-variance inputs (which broke the
    original oracles); real strategy returns are continuous and won't tie. Oracles use noisy
    series specifically to avoid this. If a future caller feeds constant columns, the result is
    undefined rather than silently mis-ranking."""
    if n_blocks % 2 != 0:
        raise ValueError("n_blocks (S) must be even")
    M = matrix.to_numpy(dtype="float64")
    T, ncfg = M.shape
    if ncfg < 2 or T < n_blocks:
        raise ValueError("need >=2 configs and >= n_blocks rows")
    bounds = np.array_split(np.arange(T), n_blocks)
    blocks = [M[b, :] for b in bounds]
    overfit = 0
    total = 0
    for is_idx in itertools.combinations(range(n_blocks), n_blocks // 2):
        oos_idx = [j for j in range(n_blocks) if j not in is_idx]
        is_perf = _block_sharpe(np.vstack([blocks[j] for j in is_idx]))
        oos_perf = _block_sharpe(np.vstack([blocks[j] for j in oos_idx]))
        n_star = int(np.argmax(is_perf))
        # relative OOS rank of n_star in (0,1): fraction of configs it beats, mid-rank for ties
        order = oos_perf.argsort()                      # ascending
        ranks = np.empty(ncfg); ranks[order] = np.arange(1, ncfg + 1)
        omega = ranks[n_star] / (ncfg + 1)
        lam = math.log(omega / (1.0 - omega))
        overfit += 1 if lam <= 0 else 0
        total += 1
    return overfit / total


# ---------------------------------------------------------------------------
# Task 11: Bootstrap confidence intervals
# ---------------------------------------------------------------------------

def max_drawdown_from_returns(returns) -> float:
    """Max drawdown computed by rebuilding the equity path from a return series (so drawdown can be
    bootstrapped). max_drawdown takes an EQUITY curve; this is the adapter (review S1 wiring trap)."""
    eq = (1.0 + pd.Series(returns).fillna(0.0)).cumprod()
    return max_drawdown(eq)


def _resample(r: np.ndarray, rng, block_size: int) -> np.ndarray:
    """IID (block_size<=1) or overlapping moving-block resample to the original length."""
    nlen = len(r)
    if block_size <= 1 or block_size >= nlen:
        return r[rng.integers(0, nlen, nlen)]
    n_blocks = int(np.ceil(nlen / block_size))
    starts = rng.integers(0, nlen - block_size + 1, n_blocks)
    idx = np.concatenate([np.arange(s, s + block_size) for s in starts])[:nlen]
    return r[idx]


def bootstrap_ci(returns, statistic, n: int = 1000, seed: int = 0, ci: float = 0.95,
                 block_size: int = 1):
    """Percentile-bootstrap CI for `statistic(resampled_returns)`, seeded (deterministic).
    `block_size=1` -> IID (valid for Sharpe of near-IID returns); `block_size>1` -> moving-block
    (preserves serial dependence — REQUIRED for path-dependent stats like max-DD, and a less-
    optimistic Sharpe CI on autocorrelated returns, review S5). `statistic`: Series->float
    (metrics.sharpe; max_drawdown_from_returns; or a (strategy-benchmark) paired-difference Sharpe)."""
    r = pd.Series(returns).dropna().to_numpy()
    if len(r) < 2:
        return (float("nan"), float("nan"))
    rng = np.random.default_rng(seed)
    stats = np.array([statistic(pd.Series(_resample(r, rng, block_size))) for _ in range(n)])
    alpha = (1.0 - ci) / 2.0
    return (float(np.quantile(stats, alpha)), float(np.quantile(stats, 1.0 - alpha)))
