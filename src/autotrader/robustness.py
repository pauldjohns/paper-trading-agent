# src/autotrader/robustness.py
"""Offline robustness runner: enumerate the parameter-grid trial census, re-run the re-entrant
Plan-04 engine over every cell, and assemble the BINDING §4 gate (deflated Sharpe + PBO + placebo +
CIs) per strategy. Builds on the locked engine/metrics/report; the only new modelling object is the
random-selection placebo (placebo.py). Offline + deterministic; never calls the MCP, never trades.
"""
import itertools
import numpy as np
import pandas as pd
from autotrader import config
from autotrader.engine import BacktestEngine
from autotrader.strategies import (S1Trend, S2SectorMomentum, S3MeanReversion, S4TrendGatedMomentum)

# "gate off" = NO trend filter (gate ~always on). It is modeled as (sma_months=1, band=0.0): a 1-bar
# SMA equals the price, and with band=0.0 the rule `close >= SMA` is true whenever the close is at/above
# its own value -> on essentially every bar. (Modeling it as band=0.01 would make `close >= close*1.01`
# NEVER true -> permanently cash -> a dead all-cash cell that crashes the warm-up-trim; review B1.)
_GATE = {"on": (10, 0.01), "off": (1, 0.0)}              # (gate_sma_months, gate_band)


def build_strategy(family: str, params: dict):
    """Construct a strategy instance for a grid cell. `params` are scalar knobs; the gating universe
    is fixed per family (S2/S4 -> sector SPDRs, S3 -> index ETFs, S1 -> SPY/IEF)."""
    p = dict(params)
    if family == "S1":
        return S1Trend(**p)
    if family == "S2":
        if "gate" in p:                                  # gate on/off -> (gate_sma_months, gate_band)
            p["gate_sma_months"], p["gate_band"] = _GATE[p.pop("gate")]
        return S2SectorMomentum(config.SECTOR_SPDRS, **p)
    if family == "S3":
        return S3MeanReversion(config.INDEX_ETFS, **p)
    if family == "S4":
        return S4TrendGatedMomentum(config.SECTOR_SPDRS, **p)
    raise ValueError(f"unknown family {family!r}")


def placebo_gate_for(strategy):
    """The trend gate (gate_sma_months, gate_band) a gated sector strategy actually uses, so the §4
    placebo runs under the SAME gate (spec E2 — the placebo mirrors S2/S4 exactly except the rank).
    `S2SectorMomentum` exposes gate_sma_months/gate_band directly; `S4TrendGatedMomentum` carries the
    gate on its inner S2 sleeve (its `sma_months` knob drives that sleeve's gate_sma_months). Reading
    it off the built strategy means the control can never drift from the strategy's real gate — the
    review caught the driver hard-coding the default 10-month gate while the best S4 cell gates on 8."""
    if hasattr(strategy, "gate_sma_months"):
        return strategy.gate_sma_months, strategy.gate_band
    return strategy.s2.gate_sma_months, strategy.s2.gate_band


def _cells(family, axes):
    """Cartesian product of knob axes -> list of {name, family, params}. `axes` is an ordered dict
    of knob -> list of values; the name encodes every knob compactly and uniquely."""
    keys = list(axes)
    out = []
    for combo in itertools.product(*(axes[k] for k in keys)):
        params = dict(zip(keys, combo))
        tag = "_".join(f"{k}{_fmt(v)}" for k, v in params.items())
        out.append({"name": f"{family}_{tag}", "family": family, "params": params})
    return out


def _fmt(v):
    if v is None:
        return "X"
    if isinstance(v, bool):
        return "T" if v else "F"
    if isinstance(v, float):
        return str(v).replace(".", "p")
    return str(v)


def strategy_grid(scale: str = "full") -> list:
    """The full trial census (E1): literature-locked defaults + their immediate neighborhood per
    soft knob. scale="small" returns a tiny grid for tests (one cell per family)."""
    if scale == "small":
        return (_cells("S1", {"sma_months": [10], "stop_loss_pct": [0.20]})
                + _cells("S2", {"n_hold": [3], "buffer": [2], "gate": ["on"]})
                + _cells("S3", {"regime_sma": [200], "time_stop_days": [10]})
                + _cells("S4", {"n_hold": [3], "sma_months": [10]}))
    s1 = _cells("S1", {"sma_months": [8, 10, 12], "band": [0.0, 0.01, 0.02],
                       "use_abs_momentum": [False, True], "stop_loss_pct": [None, 0.20]})
    s2 = _cells("S2", {"n_hold": [2, 3, 4], "buffer": [1, 2], "gate": ["on", "off"],
                       "stop_loss_pct": [None, 0.20]})
    s3 = _cells("S3", {"regime_sma": [150, 200, 250], "cumrsi_entry": [30.0, 35.0, 40.0],
                       "time_stop_days": [5, 10], "stop_loss_pct": [None, 0.10]})
    s4 = _cells("S4", {"n_hold": [2, 3, 4], "buffer": [1, 2], "sma_months": [8, 10, 12],
                       "stop_loss_pct": [None, 0.20]})
    return s1 + s2 + s3 + s4


def plateau(sharpes: dict, grid: list, family: str, knob: str) -> dict:
    """For `family`, group its cells by `knob` value -> {value: median Sharpe over those cells}.
    A plateau (low spread, all decent) = robust on that knob; a lone peak = fragile (§3.9)."""
    vals = {}
    for cell in grid:
        if cell["family"] != family or knob not in cell["params"]:
            continue
        vals.setdefault(cell["params"][knob], []).append(sharpes[cell["name"]])
    return {v: float(np.median(s)) for v, s in sorted(vals.items(), key=lambda kv: str(kv[0]))}


def plateau_spread(plateau_map: dict) -> float:
    """max - min of the per-value medians (low = a plateau; high = a peak). NaN if <1 value."""
    if not plateau_map:
        return float("nan")
    vals = list(plateau_map.values())
    return float(max(vals) - min(vals))


def run_grid(grid: list, raw_bars: dict, initial_cash: float = 1000.0) -> dict:
    """Run every config cell through the re-entrant engine over `raw_bars` -> {name: BacktestResult}.
    Deterministic (the engine's default causal CS-ratio stress is deterministic). Each cell uses its
    own params verbatim; `raw_bars` must contain every symbol any cell's strategy `.universe` needs
    (the engine subsets per strategy). Offline — no MCP."""
    results = {}
    for cell in grid:
        strat = build_strategy(cell["family"], cell["params"])
        results[cell["name"]] = BacktestEngine(strat, raw_bars, initial_cash=initial_cash).run()
    return results


from autotrader import metrics, report


def _dedup_cells(cells: list, grid_results: dict) -> list:
    """Drop cells whose return stream is BYTE-IDENTICAL to one already kept (e.g. a stop that never
    fires -> stop-on == stop-off). Exact duplicates over-count N and degenerate the CSCV OOS-ranks
    (review B3); keep the first of each distinct stream."""
    seen, distinct = set(), []
    for nm in cells:
        key = grid_results[nm].returns.dropna().to_numpy().tobytes()
        if key not in seen:
            seen.add(key)
            distinct.append(nm)
    return distinct


def census_dsr_pbo(grid_results: dict, grid: list, n_blocks: int = 16) -> dict:
    """Per-family Deflated Sharpe + PBO (§3.9). The BINDING deflation uses a CONSISTENT within-family
    null: best-of-family deflated by N = that family's DISTINCT-cell count and V = variance of
    per-observation Sharpes over those distinct cells (review B2 — a global N + cross-family-mixed V
    is the wrong null and can kill a real edge). PBO via CSCV is over the family's DISTINCT matrix.
    A secondary `deflated_p_wholesearch` (global N, global V) is reported as a more-conservative
    check, NOT the binding number. Families with <2 distinct cells / <n_blocks rows get PBO = NaN."""
    per_obs = {nm: report._per_obs_sharpe(res.returns.dropna().to_numpy())
               for nm, res in grid_results.items()}
    N_global = len(grid)
    V_global = float(pd.Series(list(per_obs.values())).var(ddof=1)) if N_global > 1 else 0.0
    families = {}
    for fam in sorted({c["family"] for c in grid}):
        cells = [c["name"] for c in grid if c["family"] == fam]
        distinct = _dedup_cells(cells, grid_results)
        Nf = len(distinct)
        Vf = float(pd.Series([per_obs[nm] for nm in distinct]).var(ddof=1)) if Nf > 1 else 0.0
        best = max(distinct, key=lambda nm: per_obs[nm])
        dsr, p = metrics.deflated_sharpe(grid_results[best].returns, n_trials=max(Nf, 2),
                                         sr_variance=max(Vf, 1e-12))                  # BINDING (per-family)
        _, p_global = metrics.deflated_sharpe(grid_results[best].returns, n_trials=max(N_global, 2),
                                              sr_variance=max(V_global, 1e-12))       # secondary check
        pbo = float("nan")
        if Nf >= 2:
            mat = report.build_variant_matrix({nm: grid_results[nm] for nm in distinct})
            if len(mat) >= n_blocks:
                pbo = metrics.pbo_cscv(mat, n_blocks=n_blocks)
        families[fam] = {"best_cell": best, "best_sharpe_perobs": per_obs[best],
                         "deflated_sharpe": dsr, "deflated_p": p, "deflated_p_wholesearch": p_global,
                         "pbo": pbo, "n_cells": len(cells), "n_distinct": Nf}
    return {"families": families, "n_trials": N_global, "sr_variance_global": V_global}


def binding_verdict(run, benchmark, spy, dsr_p, pbo, placebo_pass, seed: int = 0,
                    block_size: int = 21) -> dict:
    """The BINDING §4 return-seeker gate: Plan-04's gate_verdict + the REAL placebo term. PASS iff
    all five conditions True (auto-kill on pbo>=0.5 / deflated_p>=0.05 is enforced by those terms)."""
    v = report.gate_verdict(run, benchmark, spy, dsr_p, pbo, seed=seed, block_size=block_size)
    conds = dict(v["conditions"])
    conds["placebo_beats_95th"] = bool(placebo_pass)
    overall = "PASS" if all(bool(c) for c in conds.values()) else "FAIL"
    return {"overall": overall, "conditions": conds,
            "paired_sharpe_ci_lower": v["paired_sharpe_ci_lower"],
            "run_maxdd_upperci": v["run_maxdd_upperci"], "spy_maxdd": v["spy_maxdd"]}


def family_verdict(family, run, benchmark, spy, dsr_p, pbo, placebo_pass=None, seed: int = 0,
                   block_size: int = 21, material_frac: float = 0.75,
                   cagr_floor_frac: float = 0.5) -> dict:
    """Per-family §4 verdict. S2/S4 -> 5-condition binding gate. S1 (risk sleeve, spec §4) -> PBO<0.5
    AND max-DD MATERIALLY below SPY's (run upper-CI <= material_frac x SPY's) AND CAGR acceptable
    (>= cagr_floor_frac x max(SPY CAGR, 0)) — no Sharpe-beat / placebo. S3 (null) -> INACTIVE if it
    never traded, else NULL-CONFIRMED iff Sharpe <= 0 (success = failing), else RED-FLAG (audit costs).
    `material_frac`/`cagr_floor_frac` are soft thresholds (parameters)."""
    if family in ("S2", "S4"):
        return binding_verdict(run, benchmark, spy, dsr_p, pbo, placebo_pass, seed, block_size)
    if family == "S1":
        v = report.gate_verdict(run, benchmark, spy, dsr_p, pbo, seed=seed, block_size=block_size)
        run_cagr, spy_cagr = metrics.cagr(run.equity), metrics.cagr(spy.equity)
        conds = {
            "pbo_lt_0.5": v["conditions"]["pbo_lt_0.5"],
            "maxdd_materially_lt_spy": bool(v["run_maxdd_upperci"] <= material_frac * v["spy_maxdd"]),
            "cagr_acceptable_vs_spy": bool(run_cagr >= cagr_floor_frac * max(spy_cagr, 0.0)),
        }
        return {"overall": "PASS" if all(conds.values()) else "FAIL", "conditions": conds,
                "run_maxdd_upperci": v["run_maxdd_upperci"], "spy_maxdd": v["spy_maxdd"],
                "run_cagr": run_cagr, "spy_cagr": spy_cagr}
    if family == "S3":
        r = pd.Series(run.returns).dropna()
        if not bool((r.abs() > 1e-12).any()):              # never invested -> not a confirmed null
            return {"overall": "INACTIVE", "sharpe": float("nan"), "deflated_p": dsr_p, "pbo": pbo}
        sharpe = metrics.sharpe(r)
        return {"overall": "NULL-CONFIRMED" if sharpe <= 0.0 else "RED-FLAG-AUDIT-COSTS",
                "sharpe": sharpe, "deflated_p": dsr_p, "pbo": pbo}
    raise ValueError(f"unknown family {family!r}")


def render_robustness(census: dict, verdicts: dict, plateaus: dict) -> str:
    """Deterministic markdown: per-family census (best cell, deflated Sharpe + p, PBO, #cells),
    the §4 verdict, and the plateau spread per knob. `census` from census_dsr_pbo; `verdicts` from
    family_verdict; `plateaus` = {family: {knob: plateau_spread}}."""
    lines = [f"Trial census: n_trials={census['n_trials']}", "",
             "| family | best_cell | deflated_sharpe | deflated_p | pbo | cells(distinct) | verdict |",
             "|---|---|---|---|---|---|---|"]
    for fam in sorted(census["families"]):
        f = census["families"][fam]
        v = verdicts.get(fam, {}).get("overall", "n/a")
        cells = f"{f['n_cells']}({f.get('n_distinct', f['n_cells'])})"   # distinct count = effective trials
        lines.append(f"| {fam} | {f['best_cell']} | {f['deflated_sharpe']:.4f} | {f['deflated_p']:.4f} "
                     f"| {f['pbo']:.4f} | {cells} | {v} |")
    lines += ["", "Plateau spreads (max-min median Sharpe across each knob's neighborhood; low = robust):"]
    for fam in sorted(plateaus):
        spreads = ", ".join(f"{k}={s:.3f}" for k, s in sorted(plateaus[fam].items()))
        lines.append(f"  {fam}: {spreads}")
    return "\n".join(lines) + "\n"
