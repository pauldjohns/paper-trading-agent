# src/autotrader/report.py
"""Deterministic backtest report (D6): a metrics table per run + variant-set DSR/PBO + a PROVISIONAL
§4 gate (placebo deferred to Plan 05, D8), rendered as markdown with CSV exports. Pure over
BacktestResult-shaped objects."""
import pandas as pd
from autotrader import metrics as M


_METRIC_KEYS = ["cagr", "vol", "sharpe", "sortino", "max_dd", "calmar", "win_rate", "profit_factor",
                "avg_win", "avg_loss", "turnover", "time_in_market", "trades_per_year", "number_of_bets"]


def first_active_index(res) -> int:
    """First row index at which any position is held (realized weight > 0) — used to trim the leading
    all-cash burn-in from headline metrics (review S2). 0 if invested from the start; len if never."""
    held = res.weights.sum(axis=1).to_numpy()
    nz = [i for i, v in enumerate(held) if v > 1e-12]
    return nz[0] if nz else len(held)


def summarize_run(res, trim_warmup: bool = True) -> dict:
    """Per-run metric row. If trim_warmup, return-based metrics start at first_active_index (so the
    leading flat-cash burn-in doesn't dilute Sharpe/vol or inflate the DSR sample length)."""
    i0 = first_active_index(res) if trim_warmup else 0
    eq, r, w = res.equity.iloc[i0:], res.returns.iloc[i0:], res.weights.iloc[i0:]
    tr = res.trades
    aw, al = M.avg_win_loss(tr)
    return {
        "cagr": M.cagr(eq), "vol": M.annualized_vol(r), "sharpe": M.sharpe(r),
        "sortino": M.sortino(r), "max_dd": M.max_drawdown(eq), "calmar": M.calmar(eq),
        "win_rate": M.win_rate(tr), "profit_factor": M.profit_factor(tr), "avg_win": aw, "avg_loss": al,
        "turnover": M.turnover(tr, eq), "time_in_market": M.time_in_market(w),
        "trades_per_year": M.trades_per_year(tr, eq), "number_of_bets": M.number_of_bets(w),
    }


def build_variant_matrix(named_results: dict) -> pd.DataFrame:
    """Returns matrix (rows=time, cols=variant) for PBO/DSR; truncated to the common min length
    (CSCV requires a rectangular matrix)."""
    cols = {name: res.returns.dropna().to_numpy() for name, res in named_results.items()}
    m = min(len(v) for v in cols.values())
    return pd.DataFrame({name: v[:m] for name, v in cols.items()})


def _per_obs_sharpe(col) -> float:
    r = pd.Series(col).dropna()
    sd = r.std(ddof=1)
    return float(r.mean() / sd) if sd and sd > 0 else 0.0


def variant_dsr_pbo(named_results: dict, n_blocks: int = 16) -> dict:
    """Per-variant DSR + a single PBO over the variant matrix. CRITICAL (review B1): the cross-
    variant Sharpe variance MUST be of PER-OBSERVATION Sharpes (mean/std, no √252) to match the
    per-observation Sharpe deflated_sharpe uses internally — feeding annualized variance is a 252×
    error that collapses every DSR to ~0. Plan-04's variant count is tiny, so this is flagged
    PROVISIONAL: the binding deflation uses Plan-05's full trial census (D3/D8)."""
    mat = build_variant_matrix(named_results)
    n_trials = len(mat.columns)
    sr_obs = {c: _per_obs_sharpe(mat[c]) for c in mat.columns}
    sr_var = float(pd.Series(list(sr_obs.values())).var(ddof=1)) if n_trials > 1 else 0.0
    dsr = {c: M.deflated_sharpe(mat[c], n_trials=max(n_trials, 2), sr_variance=max(sr_var, 1e-12))
           for c in mat.columns}
    pbo = M.pbo_cscv(mat, n_blocks=n_blocks) if (len(mat) >= n_blocks and n_trials >= 2) else float("nan")
    return {"dsr": dsr, "pbo": pbo, "sr_variance_perobs": sr_var, "n_trials": n_trials,
            "provisional": True}


def gate_verdict(run, benchmark, spy, dsr_p, pbo, seed: int = 0, block_size: int = 21) -> dict:
    """Assemble the §4 return-seeker gate from the available sub-conditions and return a PROVISIONAL
    verdict (D8). The random-selection PLACEBO term is Plan-05 scope -> reported as
    'DEFERRED-Plan05', NEVER silently treated as PASS. Drawdowns compared as positive magnitudes."""
    paired = (run.returns - benchmark.returns).dropna()                    # (strategy - benchmark)
    sharpe_lo, _ = M.bootstrap_ci(paired, M.sharpe, seed=seed, block_size=block_size)
    neg_dd = lambda r: -M.max_drawdown_from_returns(r)                      # positive DD magnitude
    _, run_dd_hi = M.bootstrap_ci(run.returns.dropna(), neg_dd, seed=seed, block_size=block_size)
    spy_dd = -M.max_drawdown(spy.equity)
    conditions = {
        "deflated_p_lt_0.05": bool(dsr_p is not None and dsr_p < 0.05),
        "sharpe_ci_lower_ge_benchmark": bool(sharpe_lo >= 0.0),
        "maxdd_upperci_lt_spy": bool(run_dd_hi < spy_dd),
        "pbo_lt_0.5": bool(pbo is not None and pbo < 0.5),
        "placebo_beats_95th": "DEFERRED-Plan05",
    }
    decided = [v for v in conditions.values() if isinstance(v, bool)]
    overall = ("PROVISIONAL-PASS-pending-placebo" if all(decided)
               else "PROVISIONAL-FAIL")
    return {"overall": overall, "conditions": conditions,
            "paired_sharpe_ci_lower": sharpe_lo, "run_maxdd_upperci": run_dd_hi, "spy_maxdd": spy_dd}


def summarize_periods(period_returns: dict) -> dict:
    """Per-period return metrics for walk-forward windows / stress folds (Task 15 report scope).
    period_returns: {name: returns Series (a slice of one run's daily returns)}.
    Returns {name: {"n": int, "cagr": annualized geometric return, "sharpe": M.sharpe,
    "max_dd": M.max_drawdown_from_returns}}. Empty/short slices yield NaN metrics, n=len."""
    import math
    out = {}
    for name, r in period_returns.items():
        rr = pd.Series(r).dropna()
        n = len(rr)
        if n < 2:
            out[name] = {"n": n, "cagr": float("nan"), "sharpe": float("nan"), "max_dd": float("nan")}
            continue
        cagr = float((1.0 + rr).prod() ** (252.0 / n) - 1.0)
        out[name] = {"n": n, "cagr": cagr, "sharpe": M.sharpe(rr),
                     "max_dd": M.max_drawdown_from_returns(rr)}
    return out


def render_markdown(rows: dict) -> str:
    """rows: {run_name: summarize_run(...) dict}. Deterministic markdown table."""
    header = "| run | " + " | ".join(_METRIC_KEYS) + " |\n"
    sep = "|" + "---|" * (len(_METRIC_KEYS) + 1) + "\n"
    body = ""
    for name in sorted(rows):
        vals = " | ".join(f"{rows[name][k]:.4f}" if isinstance(rows[name][k], float)
                          else str(rows[name][k]) for k in _METRIC_KEYS)
        body += f"| {name} | {vals} |\n"
    return header + sep + body


def export_csv(res, equity_path: str, trades_path: str) -> None:
    """Write the equity curve + trades table as CSV (offline artifacts)."""
    res.equity.rename("equity").to_frame().to_csv(equity_path)
    pd.DataFrame([t.__dict__ for t in res.trades]).to_csv(trades_path, index=False)
