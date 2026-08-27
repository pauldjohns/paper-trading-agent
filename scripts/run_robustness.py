#!/usr/bin/env python3
"""Offline robustness driver — Task 9: trial census, binding §4 gate, placebo, plateau.

Loads data/cache via DataStore (split-adjusted, 15 symbols), runs every grid cell through
the re-entrant Plan-04 engine, assembles the BINDING §4 gate (deflated Sharpe + PBO +
random-selection placebo + CIs) per strategy family, and prints the robustness report.

OFFLINE ONLY — reads data/cache; calls NO MCP; never places an order.

Usage:
  ./.venv/bin/python scripts/run_robustness.py           # full run (~60 min)
  ./.venv/bin/python scripts/run_robustness.py --quick   # smoke run (~1 min, scale=small, n_placebo=50)
"""
import sys
import os
import math
import argparse
from pathlib import Path

import numpy as np

# resolve repo root so the script runs from any CWD
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "src"))

from autotrader import config
from autotrader.datastore import DataStore
from autotrader.engine import BacktestEngine
from autotrader.benchmarks import GatedSPY, BuyHold
from autotrader import report
from autotrader.robustness import (
    strategy_grid,
    run_grid,
    census_dsr_pbo,
    plateau,
    plateau_spread,
    render_robustness,
    family_verdict,
    build_strategy,
    placebo_gate_for,
)
from autotrader.placebo import (
    placebo_distribution,
    placebo_95th,
    beats_placebo,
)

# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
parser = argparse.ArgumentParser(description="Offline robustness runner (Plan 05a Task 9)")
parser.add_argument(
    "--quick",
    action="store_true",
    help="Smoke run: scale=small, n_placebo=50 (~1 min)",
)
args = parser.parse_args()

SCALE = "small" if args.quick else "full"
N_PLACEBO = 50 if args.quick else 1000
QUICK = args.quick

print(f"{'[QUICK MODE]' if QUICK else '[FULL MODE]'}  scale={SCALE!r}  n_placebo={N_PLACEBO}")
print()

# ---------------------------------------------------------------------------
# 0. Load price cache (same pattern as run_backtest.py)
# ---------------------------------------------------------------------------
CACHE_DIR = _REPO / "data" / "cache"
OUTPUT_DIR = _REPO / "data" / "robustness"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

store = DataStore(cache_dir=str(CACHE_DIR))

ALL_SYMBOLS = (
    config.SECTOR_SPDRS   # XLK XLF XLE XLV XLY XLP XLI XLB XLU
    + config.INDEX_ETFS   # SPY QQQ DIA IWM
    + config.BOND_ETFS    # IEF AGG
)

print("Loading price cache ...", flush=True)
raw_bars = {}
for sym in ALL_SYMBOLS:
    raw_bars[sym] = store.load(sym, "day", "split")

n_bars = len(raw_bars["SPY"])
date_range = f"{raw_bars['SPY']['date'].iloc[0]}..{raw_bars['SPY']['date'].iloc[-1]}"
print(f"  Loaded {len(raw_bars)} symbols, {n_bars} bars each ({date_range})")
print()

# ---------------------------------------------------------------------------
# 1. Build grid and run all cells
# ---------------------------------------------------------------------------
print(f"Building strategy grid (scale={SCALE!r}) ...", flush=True)
grid = strategy_grid(SCALE)
print(f"  Grid: {len(grid)} cells ({len(set(c['family'] for c in grid))} families)")
print()

print("Running grid (this is the slow part for full scale) ...", flush=True)
results = run_grid(grid, raw_bars)
print(f"  Done. {len(results)} runs completed.")
print()

# ---------------------------------------------------------------------------
# 2. Census: deflated Sharpe + PBO per family
# ---------------------------------------------------------------------------
print("Computing census (deflated Sharpe + PBO) ...", flush=True)
census = census_dsr_pbo(results, grid)
print(f"  n_trials={census['n_trials']}")
for fam, fd in census["families"].items():
    print(f"  {fam}: best={fd['best_cell']}  deflated_p={fd['deflated_p']:.4f}"
          f"  pbo={fd['pbo']:.4f}  cells={fd['n_cells']}(distinct={fd['n_distinct']})")
print()

# ---------------------------------------------------------------------------
# 3. Sharpes (trim_warmup=False — crash-safe on all-cash cells)
# ---------------------------------------------------------------------------
print("Computing per-cell Sharpes (trim_warmup=False) ...", flush=True)
sharpes = {
    name: report.summarize_run(res, trim_warmup=False)["sharpe"]
    for name, res in results.items()
}
print(f"  Done. min={min(sharpes.values()):.4f}  max={max(sharpes.values()):.4f}")
print()

# ---------------------------------------------------------------------------
# 4. Plateau spreads per family and its soft knobs
# ---------------------------------------------------------------------------
print("Computing plateau spreads ...", flush=True)
plateaus = {}
for fam in ("S1", "S2", "S3", "S4"):
    # Derive the knob set by scanning what params actually vary in this family's grid
    knobs = sorted({k for c in grid if c["family"] == fam for k in c["params"]})
    plateaus[fam] = {
        knob: plateau_spread(plateau(sharpes, grid, fam, knob))
        for knob in knobs
    }
print("  Done.")
print()

# ---------------------------------------------------------------------------
# 5. Benchmark runs (engine-driven)
# ---------------------------------------------------------------------------
print("Running benchmark refs through engine ...", flush=True)
gated_spy_strat = GatedSPY("SPY")
gated_spy_run = BacktestEngine(
    gated_spy_strat,
    {s: raw_bars[s] for s in gated_spy_strat.universe},
).run()

buyhold_spy_strat = BuyHold("SPY")
buyhold_spy_run = BacktestEngine(
    buyhold_spy_strat,
    {s: raw_bars[s] for s in buyhold_spy_strat.universe},
).run()
print("  GatedSPY and BuyHold(SPY) complete.")
print()

# ---------------------------------------------------------------------------
# 6. Placebos for S2 (off_asset=None) and S4 (off_asset="IEF")
# ---------------------------------------------------------------------------
placebo_pass = {}   # {family: bool}
placebo_p95 = {}    # {family: float}
placebo_best_sharpe = {}  # {family: float}
placebo_dist = {}   # {family: np.ndarray}

for fam, off_asset in (("S2", None), ("S4", "IEF")):
    best = census["families"][fam]["best_cell"]
    best_params = next(c["params"] for c in grid if c["name"] == best)
    n_hold = best_params["n_hold"]
    buffer = best_params.get("buffer", 2)
    # Run the placebo under the SAME trend gate the winning cell uses (spec E2). S2's gate is its
    # on/off knob (-> 10mo / 1mo); S4's gate is its sma_months knob (S4TrendGatedMomentum gates its
    # inner S2 sleeve on sma_months). Reading it off the built strategy guarantees the control can't
    # drift from the strategy's real gate (review fix: the best S4 cell gates on 8mo, not the
    # default 10mo, so an unmatched placebo would be an unfair control for the one term S4 might pass).
    gate_sma_months, gate_band = placebo_gate_for(build_strategy(fam, best_params))
    best_sharpe = sharpes[best]

    print(
        f"Running placebo distribution for {fam}  "
        f"(n_hold={n_hold}, buffer={buffer}, gate_sma_months={gate_sma_months}, "
        f"off_asset={off_asset!r}, n_placebo={N_PLACEBO}) ...",
        flush=True,
    )
    # Resumable cache: a placebo distribution is fully deterministic (seeds 0..N-1 over the fixed
    # cache), so persist each completed one keyed on EVERY input that shapes the draw — family,
    # n_hold, buffer, gate_sma_months, gate_band, off_asset, N (the universe + seed_base are fixed).
    # Atomic write (tmp + os.replace) so a kill mid-write can never leave a partial/corrupt .npy; a
    # cache hit is byte-identical to a fresh compute, so the verdict is unchanged.
    cache_path = OUTPUT_DIR / (
        f"placebo_{fam}_n{n_hold}_b{buffer}_g{gate_sma_months}_band{gate_band}"
        f"_off{off_asset}_N{N_PLACEBO}.npy"
    )
    if cache_path.exists():
        dist = np.load(cache_path)
        print(f"  loaded cached distribution {cache_path.name} (len={len(dist)})", flush=True)
    else:
        dist = placebo_distribution(
            config.SECTOR_SPDRS,
            "SPY",
            n_hold=n_hold,
            raw_bars=raw_bars,
            buffer=buffer,
            n_placebo=N_PLACEBO,
            off_asset=off_asset,
            gate_sma_months=gate_sma_months,
            gate_band=gate_band,
        )
        _tmp = cache_path.with_name(cache_path.name + ".tmp")
        with open(_tmp, "wb") as _f:
            np.save(_f, dist)
        os.replace(_tmp, cache_path)
        print(f"  computed + cached {cache_path.name} (len={len(dist)})", flush=True)
    p95 = placebo_95th(dist)
    passes = beats_placebo(best_sharpe, dist)

    placebo_dist[fam] = dist
    placebo_p95[fam] = p95
    placebo_best_sharpe[fam] = best_sharpe
    placebo_pass[fam] = passes

    print(
        f"  {fam}: best_sharpe={best_sharpe:.4f}  placebo_p95={p95:.4f}"
        f"  beats_placebo={'PASS' if passes else 'FAIL'}"
    )
print()

# ---------------------------------------------------------------------------
# 7. Family verdicts
# ---------------------------------------------------------------------------
print("Computing family verdicts ...", flush=True)
verdicts = {}
for fam in ("S1", "S2", "S3", "S4"):
    fd = census["families"][fam]
    best_run = results[fd["best_cell"]]
    pp = placebo_pass.get(fam, None)   # only S2/S4 have a placebo term
    verdicts[fam] = family_verdict(
        fam,
        best_run,
        gated_spy_run,
        buyhold_spy_run,
        fd["deflated_p"],
        fd["pbo"],
        placebo_pass=pp,
    )
    print(f"  {fam}: {verdicts[fam]['overall']}")
print()

# ---------------------------------------------------------------------------
# 8. Render + print
# ---------------------------------------------------------------------------
render_text = render_robustness(census, verdicts, plateaus)

print("=" * 80)
print("ROBUSTNESS REPORT")
print("=" * 80)
print(render_text)

# Placebo detail lines
print("=" * 80)
print("PLACEBO DETAIL  (S2 / S4)")
print("=" * 80)
for fam in ("S2", "S4"):
    print(
        f"  {fam}: best_sharpe={placebo_best_sharpe[fam]:.4f}"
        f"  placebo_p95={placebo_p95[fam]:.4f}"
        f"  beats_placebo={'PASS' if placebo_pass[fam] else 'FAIL'}"
        f"  (dist len={len(placebo_dist[fam])})"
    )
print()

# Per-family secondary stats
print("=" * 80)
print("PER-FAMILY SECONDARY STATS")
print("=" * 80)
for fam in ("S1", "S2", "S3", "S4"):
    fd = census["families"][fam]
    print(
        f"  {fam}: n_distinct={fd['n_distinct']}"
        f"  deflated_p_wholesearch={fd['deflated_p_wholesearch']:.4f}"
    )
print()

# ---------------------------------------------------------------------------
# 9. Sanity gates (print PASS/FLAG; never crash)
# ---------------------------------------------------------------------------
print("=" * 80)
print("SANITY GATES")
print("=" * 80)

# (a) Every grid run's equity finite and > 0
print("\n[Gate A] All grid run equities finite and > 0:")
gate_a_pass = True
for name, res in results.items():
    eq = res.equity
    finite_ok = eq.notna().all() and all(math.isfinite(v) for v in eq)
    positive_ok = (eq > 0).all()
    ok = finite_ok and positive_ok
    if not ok:
        gate_a_pass = False
        min_eq = float(eq.min())
        print(f"  FLAG  {name}  min_equity={min_eq:.4f}  finite={finite_ok}  positive={positive_ok}")
# Also check benchmark runs
for name, res in (("GatedSPY", gated_spy_run), ("BuyHold_SPY", buyhold_spy_run)):
    eq = res.equity
    finite_ok = eq.notna().all() and all(math.isfinite(v) for v in eq)
    positive_ok = (eq > 0).all()
    ok = finite_ok and positive_ok
    if not ok:
        gate_a_pass = False
        min_eq = float(eq.min())
        print(f"  FLAG  {name}  min_equity={min_eq:.4f}  finite={finite_ok}  positive={positive_ok}")
status_a = "PASS" if gate_a_pass else "FLAG"
if gate_a_pass:
    print(f"  All {len(results) + 2} runs: equity finite and positive throughout.")
print(f"  Gate A: {status_a}")

# (b) n_trials == 132 (ONLY when scale=="full"; skip for --quick)
print("\n[Gate B] Trial census count:")
if not QUICK:
    n_trials = census["n_trials"]
    gate_b_pass = n_trials == 132
    status_b = "PASS" if gate_b_pass else "FLAG"
    print(f"  census['n_trials'] = {n_trials}  (expected 132)")
    print(f"  Gate B: {status_b}")
else:
    n_trials = census["n_trials"]
    print(f"  census['n_trials'] = {n_trials}  (--quick mode: small grid, 132-check skipped)")
    print(f"  Gate B: SKIPPED (--quick)")
    gate_b_pass = True  # not applicable for quick

# (c) S3's best cell verdict is NULL-CONFIRMED or INACTIVE (NOT a clean PASS)
print("\n[Gate C] S3 best-cell verdict is NULL-CONFIRMED or INACTIVE (not a win):")
s3_verdict = verdicts["S3"]["overall"]
gate_c_pass = s3_verdict in ("NULL-CONFIRMED", "INACTIVE")
status_c = "PASS" if gate_c_pass else "FLAG"
s3_sharpe = verdicts["S3"].get("sharpe", float("nan"))
s3_sharpe_str = f"{s3_sharpe:.4f}" if math.isfinite(s3_sharpe) else "nan"
print(f"  S3 verdict: {s3_verdict}  sharpe={s3_sharpe_str}")
if not gate_c_pass:
    print(f"  RED-FLAG: S3 verdict is {s3_verdict!r} — audit the cost model!")
    print(f"  A clean-PASS S3 is a cost-model failure, not a win.")
print(f"  Gate C: {status_c}")

# (d) Determinism note
print("\n[Gate D] Determinism:")
print("  The driver is deterministic: engine + metrics + CSCV are seeded/deterministic;")
print("  placebo uses fixed seeds 0..n_placebo-1. Re-running gives identical verdicts.")
print("  Gate D: NOTE (re-run to confirm if needed)")

print()
print("=" * 80)
print("GATE SUMMARY")
print("=" * 80)
gate_labels = {
    "A (equity finite+positive)": status_a,
    "B (n_trials == 132)":        "PASS" if gate_b_pass else "FLAG",
    "C (S3 NULL-CONFIRMED/INACTIVE)": status_c,
    "D (determinism)":            "NOTE",
}
for label, status in gate_labels.items():
    print(f"  {label:40s}: {status}")
all_critical_ok = gate_a_pass and gate_b_pass and gate_c_pass
print()
print("  Overall:", "PASS" if all_critical_ok else "FLAG — see gate details above")
print()

# ---------------------------------------------------------------------------
# 10. Write output artifacts to data/robustness/
# ---------------------------------------------------------------------------
print("Writing artifacts to", OUTPUT_DIR, "...", flush=True)

report_path = OUTPUT_DIR / "robustness_report.md"
report_path.write_text(render_text)
print(f"  {report_path}")

summary_lines = []
summary_lines.append(f"scale={SCALE}  n_placebo={N_PLACEBO}  n_trials={census['n_trials']}")
summary_lines.append("")
summary_lines.append("VERDICTS:")
for fam in ("S1", "S2", "S3", "S4"):
    summary_lines.append(f"  {fam}: {verdicts[fam]['overall']}")
summary_lines.append("")
summary_lines.append("CENSUS (deflated_p, pbo, n_distinct):")
for fam in ("S1", "S2", "S3", "S4"):
    fd = census["families"][fam]
    summary_lines.append(
        f"  {fam}: deflated_p={fd['deflated_p']:.4f}  deflated_p_wholesearch={fd['deflated_p_wholesearch']:.4f}"
        f"  pbo={fd['pbo']:.4f}  n_distinct={fd['n_distinct']}"
    )
summary_lines.append("")
summary_lines.append("PLACEBO (S2, S4):")
for fam in ("S2", "S4"):
    summary_lines.append(
        f"  {fam}: best_sharpe={placebo_best_sharpe[fam]:.4f}"
        f"  p95={placebo_p95[fam]:.4f}"
        f"  beats={'PASS' if placebo_pass[fam] else 'FAIL'}"
    )
summary_lines.append("")
summary_lines.append("GATES: A=" + status_a + "  B=" + ("PASS" if gate_b_pass else "FLAG")
                     + "  C=" + status_c + "  D=NOTE")

summary_path = OUTPUT_DIR / "summary.txt"
summary_path.write_text("\n".join(summary_lines) + "\n")
print(f"  {summary_path}")

print()
print("Done.")
sys.exit(0)
