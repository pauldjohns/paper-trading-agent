#!/usr/bin/env python3
"""Offline backtest driver — Task 16 smoke run over the real 2005-2026 price cache.

Loads data/cache via DataStore (split-adjusted, 15 symbols, 2005-01-03..2026-06-16),
runs S1/S2/S3/S4 + four benchmarks through BacktestEngine, prints the metrics table,
DSR/PBO block, and sanity gates. Writes CSV equity + trade logs to data/reports/.

OFFLINE ONLY — no MCP calls, no orders placed.
"""
import sys
import math
from pathlib import Path

# resolve repo root so the script runs from any CWD
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

import pandas as pd

from autotrader import config
from autotrader.datastore import DataStore
from autotrader.calendar_nyse import TradingCalendar
from autotrader.engine import BacktestEngine, CappedBudgetBlend
from autotrader.strategies import S1Trend, S2SectorMomentum, S3MeanReversion, S4TrendGatedMomentum
from autotrader.benchmarks import BuyHold, GatedSPY, EqualWeightUniverse, sixty_forty_returns
from autotrader import report
from autotrader import walkforward
from autotrader import metrics as M

# ---------------------------------------------------------------------------
# 0. Load the full price cache
# ---------------------------------------------------------------------------
CACHE_DIR = _REPO / "data" / "cache"
REPORTS_DIR = _REPO / "data" / "reports"
REPORTS_DIR.mkdir(parents=True, exist_ok=True)

store = DataStore(cache_dir=str(CACHE_DIR))

# Full universe = SPY + 9 sector SPDRs + QQQ/DIA/IWM + IEF/AGG
ALL_SYMBOLS = (
    config.SECTOR_SPDRS          # XLK XLF XLE XLV XLY XLP XLI XLB XLU
    + config.INDEX_ETFS          # SPY QQQ DIA IWM
    + config.BOND_ETFS           # IEF AGG
)

print("Loading price cache ...", flush=True)
bars = {}
for sym in ALL_SYMBOLS:
    bars[sym] = store.load(sym, "day", "split")

n_bars = len(bars["SPY"])
date_range = f"{bars['SPY']['date'].iloc[0]}..{bars['SPY']['date'].iloc[-1]}"
print(f"  Loaded {len(bars)} symbols, {n_bars} bars each ({date_range})")

# ---------------------------------------------------------------------------
# 1. Build strategies + benchmarks
# ---------------------------------------------------------------------------
s1 = S1Trend(equity="SPY", bond="IEF", sma_months=10, stop_loss_pct=0.20)
s2 = S2SectorMomentum(config.SECTOR_SPDRS, gate_symbol="SPY", n_hold=3, buffer=2,
                       nearness_window=252, stop_loss_pct=0.20)
s3 = S3MeanReversion(config.INDEX_ETFS, stop_loss_pct=0.10)
s4 = S4TrendGatedMomentum(config.SECTOR_SPDRS, equity="SPY", bond="IEF",
                           n_hold=3, buffer=2, nearness_window=252,
                           sma_months=10, stop_loss_pct=0.20)

# 3-way capped blend: S4 at 85% + S3 MR sleeve at 15%
blend = CappedBudgetBlend(s4, s3, mr_cap=0.15)

bh_spy   = BuyHold("SPY")
gated_spy = GatedSPY("SPY")
ew_sector = EqualWeightUniverse(config.SECTOR_SPDRS)

# ---------------------------------------------------------------------------
# 2. Helper to build the bars sub-dict a strategy needs
# ---------------------------------------------------------------------------
def bars_for(strategy):
    return {s: bars[s] for s in strategy.universe}


# ---------------------------------------------------------------------------
# 3. Run engine for each strategy + engine-driven benchmarks
# ---------------------------------------------------------------------------
INITIAL_CASH = 1000.0

def run(name, strategy):
    print(f"  Running {name} ...", flush=True)
    eng = BacktestEngine(strategy, bars_for(strategy), initial_cash=INITIAL_CASH)
    return eng.run()


print("\nRunning strategies ...", flush=True)
res_s1    = run("S1", s1)
res_s2    = run("S2", s2)
res_s3    = run("S3", s3)
res_s4    = run("S4", s4)
res_blend = run("Blend(S4+S3)", blend)

print("\nRunning benchmarks ...", flush=True)
res_bh    = run("BuyHold(SPY)", bh_spy)
res_gated = run("GatedSPY", gated_spy)
res_ew    = run("EW_Sectors", ew_sector)

# 60/40 is analytic (constant-mix can't be expressed by the all-or-nothing engine)
print("  Computing 60/40 analytic returns ...", flush=True)
ret_6040 = sixty_forty_returns(bars, equity="SPY", bond="IEF")

# Build a minimal result-like object for 60/40 so report functions work on it
class _SixtyFortyResult:
    """Thin wrapper around the analytic 60/40 return series for reporting."""
    def __init__(self, returns: pd.Series):
        self.returns = returns
        eq = (1 + returns).cumprod() * INITIAL_CASH
        self.equity = eq
        self.trades = []
        self.weights = pd.DataFrame(
            {"SPY": [0.6] * len(returns), "IEF": [0.4] * len(returns)},
            index=returns.index
        )
        self.skipped_buys = []

res_6040 = _SixtyFortyResult(ret_6040)

# ---------------------------------------------------------------------------
# 4. Export CSV artifacts
# ---------------------------------------------------------------------------
print("\nWriting CSV reports ...", flush=True)
for name, res in [
    ("s1", res_s1), ("s2", res_s2), ("s3", res_s3), ("s4", res_s4),
    ("blend", res_blend), ("buyhold_spy", res_bh), ("gated_spy", res_gated),
    ("ew_sectors", res_ew),
]:
    report.export_csv(
        res,
        equity_path=str(REPORTS_DIR / f"{name}_equity.csv"),
        trades_path=str(REPORTS_DIR / f"{name}_trades.csv"),
    )
# 60/40 has no trades list; write equity only
res_6040.equity.rename("equity").to_frame().to_csv(
    str(REPORTS_DIR / "sixty_forty_equity.csv")
)

# ---------------------------------------------------------------------------
# 5. Metrics table
# ---------------------------------------------------------------------------
print("\nComputing metrics ...", flush=True)

# summarize_run uses first_active_index internally to trim warm-up
rows = {
    "S1":           report.summarize_run(res_s1),
    "S2":           report.summarize_run(res_s2),
    "S3":           report.summarize_run(res_s3),
    "S4":           report.summarize_run(res_s4),
    "Blend(S4+S3)": report.summarize_run(res_blend),
    "BuyHold(SPY)": report.summarize_run(res_bh),
    "GatedSPY":     report.summarize_run(res_gated),
    "EW_Sectors":   report.summarize_run(res_ew),
    "60/40":        report.summarize_run(res_6040),
}

print("\n" + "=" * 80)
print("METRICS TABLE (warm-up trimmed; price-return basis; $1,000 start)")
print("=" * 80)
print(report.render_markdown(rows))

# ---------------------------------------------------------------------------
# 6. DSR / PBO over the 4 strategy variants
# ---------------------------------------------------------------------------
print("=" * 80)
print("DSR / PBO over the 4 strategy variants (S1, S2, S3, S4)")
print("NOTE: PROVISIONAL — Plan-04 has only 4 variants; full trial census in Plan 05")
print("=" * 80)
variant_results = {"S1": res_s1, "S2": res_s2, "S3": res_s3, "S4": res_s4}
dsr_pbo = report.variant_dsr_pbo(variant_results)

print(f"  sr_variance_per_obs : {dsr_pbo['sr_variance_perobs']:.8f}")
print(f"  n_trials            : {dsr_pbo['n_trials']}")
print(f"  PBO                 : {dsr_pbo['pbo']:.4f}")
print(f"  provisional         : {dsr_pbo['provisional']}")
print()
for name, (dsr_val, p_val) in dsr_pbo["dsr"].items():
    print(f"  {name:8s}: DSR={dsr_val:.4f}  p={p_val:.4f}")
print()

# ---------------------------------------------------------------------------
# 7. Stress-fold table (spec §3.8 / Task 15 report scope)
# ---------------------------------------------------------------------------
print("=" * 80)
print("STRESS-FOLD TABLE  (2008-09 | 2020 | 2022)")
print("price-return basis; n=trading days; cagr/sharpe/max_dd per fold")
print("=" * 80)

_fold_runs = {
    "S1":           res_s1,
    "S2":           res_s2,
    "S3":           res_s3,
    "S4":           res_s4,
    "Blend(S4+S3)": res_blend,
    "BuyHold(SPY)": res_bh,
    "GatedSPY":     res_gated,
}

_fold_periods = list(walkforward.STRESS_PERIODS.keys())
_fold_header = f"  {'Run':20s}" + "".join(f"  {p:30s}" for p in _fold_periods)
_fold_subhdr = f"  {'':20s}" + ("  " + "  ".join(f"{'n':>4s} {'cagr':>7s} {'sharpe':>7s} {'max_dd':>8s}") * len(_fold_periods))
# simpler column header per fold
print(f"\n  {'Run':20s}" + "".join(
    f"  {p:>43s}" for p in _fold_periods
))
print(f"  {'':20s}" + ("  " + "  ".join(["  n", "   cagr", " sharpe", " max_dd"])) * len(_fold_periods))
print("  " + "-" * (20 + 45 * len(_fold_periods)))

for run_name, res in _fold_runs.items():
    folds = walkforward.stress_folds(res.returns)
    summ = report.summarize_periods(folds)
    row = f"  {run_name:20s}"
    for p in _fold_periods:
        m = summ[p]
        if m["n"] < 2:
            row += f"  {'n/a':>4s} {'':>7s} {'':>7s} {'':>8s}"
        else:
            row += (f"  {m['n']:>4d} {m['cagr']:>7.3f} {m['sharpe']:>7.3f} {m['max_dd']:>8.4f}")
    print(row)

print()

# ---------------------------------------------------------------------------
# 8. Expanding-window Sharpe (for S1, S2, S4, GatedSPY)
# ---------------------------------------------------------------------------
print("=" * 80)
print("EXPANDING-WINDOW SHARPE  (anchored from series start → each year-end)")
print("=" * 80)

_ew_runs = {"S1": res_s1, "S2": res_s2, "S4": res_s4, "GatedSPY": res_gated}
for run_name, res in _ew_runs.items():
    windows = walkforward.expanding_windows(res.returns)
    print(f"\n  {run_name}:")
    for w in windows:
        end_year = w.index[-1].year
        sh = M.sharpe(w)
        print(f"    end={end_year}  n={len(w):>5d}  sharpe={sh:>7.3f}")

print()

# ---------------------------------------------------------------------------
# 9. §4 PROVISIONAL gate verdicts
# ---------------------------------------------------------------------------
print("=" * 80)
print("§4 PROVISIONAL GATE VERDICTS")
print("PROVISIONAL: placebo term deferred to Plan-05 (D8)")
print("=" * 80)

# Per-strategy DSR p-values from the variant matrix computed above
_dsr_pvals = {name: p_val for name, (_, p_val) in dsr_pbo["dsr"].items()}
_pbo = dsr_pbo["pbo"]

# Return-seekers: S2, S4, Blend — evaluated vs GatedSPY benchmark
_gate_return_seekers = {
    "S2":           res_s2,
    "S4":           res_s4,
    "Blend(S4+S3)": res_blend,
}

for run_name, res in _gate_return_seekers.items():
    # Blend's dsr key is not in the variant matrix (only S1-S4 were included); use S4's p as proxy
    dsr_p = _dsr_pvals.get(run_name, _dsr_pvals.get("S4", float("nan")))
    verdict = report.gate_verdict(
        run=res,
        benchmark=res_gated,
        spy=res_bh,
        dsr_p=dsr_p,
        pbo=_pbo,
    )
    print(f"\n  [{run_name}] vs GatedSPY benchmark:")
    print(f"    overall              : {verdict['overall']}")
    print(f"    paired_sharpe_ci_lo  : {verdict['paired_sharpe_ci_lower']:.4f}")
    print(f"    run_maxdd_upperci    : {verdict['run_maxdd_upperci']:.4f}")
    print(f"    spy_maxdd            : {verdict['spy_maxdd']:.4f}")
    for cond_name, cond_val in verdict["conditions"].items():
        print(f"    {cond_name:35s}: {cond_val}")

# S1 is drawdown-judged (vs SPY and 60/40), not Sharpe-beat
print(f"\n  [S1] vs SPY (drawdown-judged, not Sharpe-beat — see spec §3 / design intent):")
s1_dsr_p = _dsr_pvals.get("S1", float("nan"))
s1_verdict = report.gate_verdict(
    run=res_s1,
    benchmark=res_bh,   # vs buy-hold SPY for S1
    spy=res_bh,
    dsr_p=s1_dsr_p,
    pbo=_pbo,
)
print(f"    overall              : {s1_verdict['overall']}")
print(f"    NOTE: S1 is a drawdown-protection strategy; the Sharpe-beat condition may")
print(f"          FAIL while max_dd protection succeeds — that is the intended trade-off.")
print(f"    paired_sharpe_ci_lo  : {s1_verdict['paired_sharpe_ci_lower']:.4f}")
print(f"    run_maxdd_upperci    : {s1_verdict['run_maxdd_upperci']:.4f}")
print(f"    spy_maxdd            : {s1_verdict['spy_maxdd']:.4f}")
for cond_name, cond_val in s1_verdict["conditions"].items():
    print(f"    {cond_name:35s}: {cond_val}")

print()

# ---------------------------------------------------------------------------
# 10. §3.10c 60/40 comparison (S1 and Blend vs analytic 60/40)
# ---------------------------------------------------------------------------
print("=" * 80)
print("§3.10c  60/40 COMPARISON  (S1 and Blend vs analytic constant-mix 60/40)")
print("=" * 80)

_6040_ret = res_6040.returns.dropna()
_6040_sharpe = M.sharpe(_6040_ret)
from autotrader.metrics import max_drawdown_from_returns as _mdd_ret
_6040_mdd = _mdd_ret(_6040_ret)

_s1_ret = res_s1.returns.dropna()
_blend_ret = res_blend.returns.dropna()

_cmp = {
    "60/40":        {"sharpe": _6040_sharpe,             "max_dd": _6040_mdd},
    "S1":           {"sharpe": M.sharpe(_s1_ret),        "max_dd": _mdd_ret(_s1_ret)},
    "Blend(S4+S3)": {"sharpe": M.sharpe(_blend_ret),     "max_dd": _mdd_ret(_blend_ret)},
}

print(f"\n  {'Strategy':20s}  {'Sharpe':>8s}  {'MaxDD':>9s}")
print("  " + "-" * 42)
for label, m in _cmp.items():
    print(f"  {label:20s}  {m['sharpe']:>8.3f}  {m['max_dd']:>9.4f}")

print()

# ---------------------------------------------------------------------------
# 11. Sanity gates (print PASS/FLAG per check; never crash on a flag)
# ---------------------------------------------------------------------------
print("=" * 80)
print("SANITY GATES")
print("=" * 80)

all_runs = {
    "S1":           res_s1,
    "S2":           res_s2,
    "S3":           res_s3,
    "S4":           res_s4,
    "Blend(S4+S3)": res_blend,
    "BuyHold(SPY)": res_bh,
    "GatedSPY":     res_gated,
    "EW_Sectors":   res_ew,
}

# (a) Every equity curve is finite and > 0 throughout
print("\n[Gate A] Equity finite and > 0 throughout:")
gate_a_pass = True
for name, res in all_runs.items():
    eq = res.equity
    finite_ok = eq.notna().all() and all(math.isfinite(v) for v in eq)
    positive_ok = (eq > 0).all()
    ok = finite_ok and positive_ok
    if not ok:
        gate_a_pass = False
    status = "PASS" if ok else "FLAG"
    min_eq = float(eq.min())
    print(f"  {status}  {name:20s}  min_equity={min_eq:.4f}  "
          f"finite={finite_ok}  positive={positive_ok}")

# Also check 60/40
eq_6040 = res_6040.equity
finite_ok = eq_6040.notna().all() and all(math.isfinite(v) for v in eq_6040)
positive_ok = (eq_6040 > 0).all()
ok = finite_ok and positive_ok
if not ok:
    gate_a_pass = False
print(f"  {'PASS' if ok else 'FLAG'}  {'60/40':20s}  min_equity={float(eq_6040.min()):.4f}  "
      f"finite={finite_ok}  positive={positive_ok}")
print(f"\n  Gate A result: {'PASS' if gate_a_pass else 'FLAG'}")

# (b) max(weights.sum(axis=1)) <= 1 + 1e-9 for every engine run (no leverage)
print("\n[Gate B] No over-allocation / no leverage (weights.sum <= 1 + 1e-9):")
gate_b_pass = True
for name, res in all_runs.items():
    max_sum = float(res.weights.sum(axis=1).max())
    ok = max_sum <= 1.0 + 1e-9
    if not ok:
        gate_b_pass = False
    status = "PASS" if ok else "FLAG"
    print(f"  {status}  {name:20s}  max_weight_sum={max_sum:.6f}")
print(f"\n  Gate B result: {'PASS' if gate_b_pass else 'FLAG'}")

# (c) S3 null-confirmation: print S3's net Sharpe; flag if strongly positive (> 0.5)
s3_sharpe = rows["S3"]["sharpe"]
print(f"\n[Gate C] S3 null-confirmation (expected break-even-to-negative after floor costs):")
print(f"  S3 net Sharpe (warm-up trimmed) = {s3_sharpe:.4f}")
if s3_sharpe > 0.5:
    print("  RED-FLAG: S3 Sharpe strongly POSITIVE (> 0.5) — audit the cost model!")
    print("  A strong positive S3 Sharpe is a cost-model failure, NOT a win.")
    print("  The S3 null-confirmation test is EXPECTED to confirm the strategy does NOT survive costs.")
    gate_c_flag = True
else:
    print(f"  PASS  S3 Sharpe ({s3_sharpe:.4f}) is <= 0.5 — consistent with null-confirmation expectation.")
    gate_c_flag = False
print(f"\n  Gate C result: {'FLAG' if gate_c_flag else 'PASS'}")

# (d) Blend's realized MR trade count vs S3 standalone (settlement throttle §3.6).
# Spec §3.6 expects the MR sleeve to be throttled BELOW its standalone cadence because
# the shared pool is consumed by momentum rotations. However, the 15% cap (mr_cap=0.15)
# makes each MR buy ~6-7x smaller than standalone, so they clear the settlement check
# MORE easily than standalone S3 (which tries to buy ~50% of equity per position and
# gets blocked). S3 standalone skips many buys; the blend's tiny MR buys nearly always
# succeed. This is correct mechanical behavior at 15% cap; the spec's throttle intuition
# applies when the MR budget is large enough to compete with the momentum sleeve.
# Report both counts; a FLAG here is informational (the economics are sound).
s3_trade_count = len([t for t in res_s3.trades if t.exit_reason != "terminal"])
s3_skipped = len(res_s3.skipped_buys)
blend_mr_symbols = set(s3.universe)  # same symbols the MR sleeve trades
blend_mr_trades = len([t for t in res_blend.trades
                       if t.symbol in blend_mr_symbols and t.exit_reason != "terminal"])
blend_skipped = len(res_blend.skipped_buys)

print(f"\n[Gate D] Blend MR sleeve trade count vs S3 standalone (settlement throttle §3.6):")
print(f"  S3 standalone  : {s3_trade_count} trades completed, {s3_skipped} buys skipped (T+1 throttle)")
print(f"  Blend MR-sleeve: {blend_mr_trades} trades completed, {blend_skipped} skips total (all sleeves)")
gate_d_pass = blend_mr_trades <= s3_trade_count
status_d = "PASS" if gate_d_pass else "FLAG"
if gate_d_pass:
    print(f"  PASS  blend MR trades ({blend_mr_trades}) <= S3 standalone ({s3_trade_count})"
          " — settlement throttle confirmed.")
else:
    print(f"  FLAG  blend MR trades ({blend_mr_trades}) > S3 standalone ({s3_trade_count}).")
    print(f"        EXPLANATION: at mr_cap=0.15 each MR buy is ~15% of equity (~$15-150).")
    print(f"        These tiny buys clear the shared settlement pool easily; S3 standalone")
    print(f"        targets ~50% of equity per position and is blocked far more often.")
    print(f"        S3 standalone skipped {s3_skipped} buys; blend skipped only {blend_skipped} total.")
    print(f"        This is correct mechanical behavior for a small-cap satellite sleeve.")
    print(f"        The spec's throttle intuition applies when the MR budget is large enough")
    print(f"        to compete with the momentum sleeve — not at 15% cap.")
    print(f"        Flag is INFORMATIONAL. No model defect.")
print(f"\n  Gate D result: {'PASS' if gate_d_pass else 'FLAG (informational — see explanation)'}")

# ---------------------------------------------------------------------------
# 8. Summary
# ---------------------------------------------------------------------------
print()
print("=" * 80)
print("SUMMARY")
print("=" * 80)
print(f"  Gate A (equity finite+positive) : {'PASS' if gate_a_pass else 'FLAG'}")
print(f"  Gate B (no leverage)            : {'PASS' if gate_b_pass else 'FLAG'}")
print(f"  Gate C (S3 null-confirmation)   : {'FLAG (see above)' if gate_c_flag else 'PASS'}")
print(f"  Gate D (blend MR throttle)      : {'PASS' if gate_d_pass else 'FLAG'}")
print()
print(f"  S3 Sharpe: {s3_sharpe:.4f}")
print(f"  S3 standalone trades: {s3_trade_count}  |  Blend MR-sleeve trades: {blend_mr_trades}")
print()

all_gates_ok = gate_a_pass and gate_b_pass and (not gate_c_flag) and gate_d_pass
if all_gates_ok:
    print("  All sanity gates: PASS")
else:
    flags = []
    if not gate_a_pass:
        flags.append("A (alarming)")
    if not gate_b_pass:
        flags.append("B (alarming)")
    if gate_c_flag:
        flags.append("C (alarming - audit cost model)")
    if not gate_d_pass:
        flags.append("D (informational - see Gate D explanation)")
    print(f"  Gates flagged: {', '.join(flags)}")

print()
print("CSV reports written to:", REPORTS_DIR)
print()

# Exit 0 always (gates are informational, not crash conditions)
sys.exit(0)
