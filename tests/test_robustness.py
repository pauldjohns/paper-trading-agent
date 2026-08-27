# tests/test_robustness.py
import pytest
from autotrader import config
from autotrader.robustness import build_strategy, strategy_grid
from autotrader.strategies import S1Trend, S2SectorMomentum, S3MeanReversion, S4TrendGatedMomentum


def test_build_strategy_dispatches_each_family():
    s1 = build_strategy("S1", {"sma_months": 8, "stop_loss_pct": None})
    assert isinstance(s1, S1Trend) and s1.sma_months == 8 and s1.stop_loss_pct is None
    s2 = build_strategy("S2", {"n_hold": 2, "buffer": 1})
    assert isinstance(s2, S2SectorMomentum) and s2.n_hold == 2 and s2.sectors == config.SECTOR_SPDRS
    s3 = build_strategy("S3", {"regime_sma": 150})
    assert isinstance(s3, S3MeanReversion) and s3.regime_sma == 150 and s3.etfs == config.INDEX_ETFS
    s4 = build_strategy("S4", {"n_hold": 4, "sma_months": 12})
    assert isinstance(s4, S4TrendGatedMomentum) and s4.sma_months == 12


def test_build_strategy_gate_off_is_no_filter_not_all_cash():
    # "gate off" = NO trend filter (gate ~always on), NOT permanently cash. Assert BEHAVIOR, not just
    # the param — review B1 (a band=0.01 + sma=1 'off' makes trend_regime never fire -> all-cash crash).
    import datetime as dt
    import pandas as pd
    from autotrader.strategies import trend_regime
    s2_off = build_strategy("S2", {"n_hold": 3, "gate": "off"})
    s2_on = build_strategy("S2", {"n_hold": 3, "gate": "on"})
    assert (s2_off.gate_sma_months, s2_off.gate_band) == (1, 0.0)
    assert (s2_on.gate_sma_months, s2_on.gate_band) == (10, 0.01)
    # 500-day window: trend_regime is a MONTHLY system (daily signal forward-filled from month-ends),
    # so the ~29 days before the first month-end decision are unavoidably OFF (warm-up). A 120-day
    # window made that warm-up 24% of the span (caps at 91/120 = 75.8% < 80%); 500 days drops it to
    # ~6% -> ~94% on, which genuinely demonstrates "no filter / almost always invested" (v1.1 fixture fix).
    dates = [dt.date(2024, 1, 2) + dt.timedelta(days=i) for i in range(500)]
    on = trend_regime(dates, pd.Series([100 + i for i in range(500)]),    # steadily rising
                      s2_off.gate_sma_months, s2_off.gate_band)
    assert int(on.sum()) > int(0.8 * len(on))             # gate-off invests almost always (no filter)


def test_strategy_grid_full_census_size_and_uniqueness():
    grid = strategy_grid(scale="full")
    names = [c["name"] for c in grid]
    assert len(names) == len(set(names))                 # unique labels
    counts = {f: sum(1 for c in grid if c["family"] == f) for f in ("S1", "S2", "S3", "S4")}
    assert counts == {"S1": 36, "S2": 24, "S3": 36, "S4": 36}
    assert len(grid) == 132
    # the locked Plan-03 defaults must be a cell in the grid (so the census includes the reported config)
    assert any(c["family"] == "S2" and c["params"].get("n_hold") == 3 and c["params"].get("buffer") == 2
               for c in grid)


def test_strategy_grid_small_scale_is_tiny():
    grid = strategy_grid(scale="small")
    assert 0 < len(grid) <= 12 and {c["family"] for c in grid} == {"S1", "S2", "S3", "S4"}


# ---------------------------------------------------------------------------
# Task 2: run_grid
# ---------------------------------------------------------------------------
import datetime as dt
import pandas as pd
from autotrader.robustness import run_grid
from autotrader.engine import BacktestResult


def _bars(dates, closes):
    return pd.DataFrame({"date": dates, "open": closes,
                         "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
                         "close": closes, "volume": [1] * len(closes)})


def _synth_universe(n=420):
    dates = [dt.date(2024, 1, 2) + dt.timedelta(days=i) for i in range(n)]
    raw = {}
    for k, s in enumerate(config.SECTOR_SPDRS):
        raw[s] = _bars(dates, [10 + ((k + i) % 6) * 0.3 + 0.01 * i for i in range(n)])
    for s in config.INDEX_ETFS:
        raw[s] = _bars(dates, [100 + 0.05 * i for i in range(n)])
    for s in config.BOND_ETFS:
        raw[s] = _bars(dates, [50.0] * n)
    return raw


def test_run_grid_runs_every_cell_and_returns_results():
    raw = _synth_universe()
    # short-window cells so signals fire on the ~14-month fixture (production windows are in the grid)
    grid = [
        {"name": "S1_w3", "family": "S1", "params": {"sma_months": 3, "stop_loss_pct": 0.20}},
        {"name": "S2_w", "family": "S2",
         "params": {"n_hold": 2, "buffer": 1, "nearness_window": 5, "gate_sma_months": 3}},
        {"name": "S3_w", "family": "S3", "params": {"regime_sma": 10, "exit_sma": 3, "time_stop_days": 5}},
        {"name": "S4_w", "family": "S4", "params": {"n_hold": 2, "sma_months": 3, "nearness_window": 5}},
    ]
    results = run_grid(grid, raw)
    assert set(results) == {c["name"] for c in grid}
    for name, res in results.items():
        assert isinstance(res, BacktestResult)
        assert res.equity.notna().all() and res.equity.iloc[-1] > 0


def test_run_grid_is_deterministic():
    raw = _synth_universe()
    grid = [{"name": "S1_w3", "family": "S1", "params": {"sma_months": 3, "stop_loss_pct": 0.20}}]
    a, b = run_grid(grid, raw), run_grid(grid, raw)
    assert a["S1_w3"].equity.iloc[-1] == b["S1_w3"].equity.iloc[-1]


# ---------------------------------------------------------------------------
# Task 5: plateau
# ---------------------------------------------------------------------------
from autotrader.robustness import plateau, plateau_spread


def test_plateau_groups_by_knob_and_medians():
    grid = [
        {"name": "S1_a", "family": "S1", "params": {"sma_months": 8}},
        {"name": "S1_b", "family": "S1", "params": {"sma_months": 8}},
        {"name": "S1_c", "family": "S1", "params": {"sma_months": 10}},
        {"name": "S2_x", "family": "S2", "params": {"sma_months": 8}},   # different family, ignored
    ]
    sharpes = {"S1_a": 0.4, "S1_b": 0.6, "S1_c": 0.9, "S2_x": 5.0}
    p = plateau(sharpes, grid, "S1", "sma_months")
    assert p == {8: 0.5, 10: 0.9}                       # median of {0.4,0.6}=0.5 ; {0.9}=0.9
    assert abs(plateau_spread(p) - 0.4) < 1e-12          # max(0.9) - min(0.5)


def test_plateau_empty_for_unknown_knob():
    grid = [{"name": "S1_a", "family": "S1", "params": {"sma_months": 8}}]
    assert plateau({"S1_a": 0.4}, grid, "S1", "buffer") == {}


# ---------------------------------------------------------------------------
# Task 6: census_dsr_pbo
# ---------------------------------------------------------------------------
import numpy as np
from autotrader.robustness import census_dsr_pbo


class _StubRes:
    def __init__(self, returns):
        self.returns = pd.Series(returns)


def _grid_results(seed=0):
    rng = np.random.default_rng(seed)
    grid, results = [], {}
    # S1: a clearly-best cell + 3 mediocre ; S2: 4 mediocre cells (so each family has >=2 cells)
    specs = [("S1", "best", 0.004), ("S1", "m1", 0.0005), ("S1", "m2", 0.0004), ("S1", "m3", 0.0003),
             ("S2", "a", 0.0006), ("S2", "b", 0.0005), ("S2", "c", 0.0004), ("S2", "d", 0.0003)]
    for fam, tag, mu in specs:
        name = f"{fam}_{tag}"
        grid.append({"name": name, "family": fam, "params": {"k": tag}})
        results[name] = _StubRes(rng.normal(mu, 0.01, 300))
    return grid, results


def test_census_dsr_pbo_structure_and_best_cell():
    grid, results = _grid_results()
    out = census_dsr_pbo(results, grid, n_blocks=4)
    assert out["n_trials"] == 8                                  # global census N
    assert set(out["families"]) == {"S1", "S2"}
    s1 = out["families"]["S1"]
    assert s1["best_cell"] == "S1_best"                          # max per-obs Sharpe cell
    assert 0.0 <= s1["pbo"] <= 1.0 and np.isfinite(s1["deflated_p"])
    assert s1["n_cells"] == 4 and s1["n_distinct"] == 4          # stub returns are all distinct
    assert np.isfinite(s1["deflated_p_wholesearch"])             # secondary (global N/V) check present


def test_census_dsr_pbo_single_cell_family_has_nan_pbo():
    grid = [{"name": "S3_a", "family": "S3", "params": {"k": "a"}},
            {"name": "S1_a", "family": "S1", "params": {"k": "a"}},
            {"name": "S1_b", "family": "S1", "params": {"k": "b"}}]
    rng = np.random.default_rng(1)
    results = {c["name"]: _StubRes(rng.normal(0.0005, 0.01, 80)) for c in grid}
    out = census_dsr_pbo(results, grid, n_blocks=4)
    assert np.isnan(out["families"]["S3"]["pbo"])               # <2 cells -> PBO undefined


# ---------------------------------------------------------------------------
# Task 7: binding_verdict + family_verdict
# ---------------------------------------------------------------------------
from autotrader.robustness import binding_verdict, family_verdict


def _res(returns, equity=None):
    r = pd.Series(returns, dtype="float64")
    idx = pd.Index([dt.date(2020, 1, 1) + dt.timedelta(days=i) for i in range(len(r))], name="date")
    r.index = idx                                              # date-indexed so metrics.cagr works
    e = pd.Series(equity, dtype="float64") if equity is not None else (1.0 + r.fillna(0)).cumprod()
    e.index = idx
    class _R: pass
    o = _R(); o.returns = r; o.equity = e
    return o


def _winning_trio():
    rng = np.random.default_rng(0)
    run = _res(rng.normal(0.0012, 0.004, 400))                 # strong + low vol -> beats bench, shallow DD
    bench = _res(np.zeros(400))                                 # flat benchmark
    # spy: peak at 1.0 on day 0, then a -50% crash -> max_drawdown registers -0.5 (deep DD benchmark)
    spy = _res(rng.normal(0.0, 0.02, 400), equity=(1 + pd.Series([0.0, -0.5] + [0.0] * 398)).cumprod())
    return run, bench, spy


def test_binding_verdict_placebo_flips_pass_fail():
    run, bench, spy = _winning_trio()
    passed = binding_verdict(run, bench, spy, dsr_p=0.01, pbo=0.2, placebo_pass=True)
    failed = binding_verdict(run, bench, spy, dsr_p=0.01, pbo=0.2, placebo_pass=False)
    assert passed["overall"] == "PASS" and passed["conditions"]["placebo_beats_95th"] is True
    assert failed["overall"] == "FAIL"                         # placebo term alone sinks it


def test_binding_verdict_autokill_on_pbo_and_p():
    run, bench, spy = _winning_trio()
    assert binding_verdict(run, bench, spy, dsr_p=0.01, pbo=0.6, placebo_pass=True)["overall"] == "FAIL"
    assert binding_verdict(run, bench, spy, dsr_p=0.20, pbo=0.2, placebo_pass=True)["overall"] == "FAIL"


def test_family_verdict_s1_drawdown_clause_material_dd_and_cagr():
    run, bench, spy = _winning_trio()
    v = family_verdict("S1", run, spy, spy, dsr_p=0.5, pbo=0.2, placebo_pass=None)
    # S1 ignores the Sharpe-beat + placebo; PASS on PBO<0.5 AND materially shallower DD AND acceptable CAGR
    assert set(v["conditions"]) == {"pbo_lt_0.5", "maxdd_materially_lt_spy", "cagr_acceptable_vs_spy"}
    assert v["overall"] == "PASS"


def test_family_verdict_s3_null_inactive_and_redflag():
    rng = np.random.default_rng(0)
    neg = _res(rng.normal(-0.0003, 0.005, 300))                # steady net loss -> confirmed null
    pos = _res(rng.normal(0.002, 0.003, 300))                  # net positive -> red flag
    flat = _res(np.zeros(300))                                  # never invested -> INACTIVE, not a null
    assert family_verdict("S3", neg, neg, neg, dsr_p=0.9, pbo=0.3)["overall"] == "NULL-CONFIRMED"
    assert family_verdict("S3", pos, pos, pos, dsr_p=0.01, pbo=0.3)["overall"] == "RED-FLAG-AUDIT-COSTS"
    assert family_verdict("S3", flat, flat, flat, dsr_p=0.9, pbo=0.3)["overall"] == "INACTIVE"


# ---------------------------------------------------------------------------
# Task 8: render_robustness
# ---------------------------------------------------------------------------
from autotrader.robustness import render_robustness


def test_render_robustness_is_deterministic_and_labels_verdicts():
    census = {"n_trials": 132, "families": {
        "S2": {"best_cell": "S2_n3", "deflated_sharpe": 0.3, "deflated_p": 0.7, "pbo": 0.6,
               "n_cells": 24, "n_distinct": 22},
        "S1": {"best_cell": "S1_x", "deflated_sharpe": 0.8, "deflated_p": 0.19, "pbo": 0.2,
               "n_cells": 36, "n_distinct": 24}}}
    verdicts = {"S2": {"overall": "FAIL"}, "S1": {"overall": "FAIL"}}
    plateaus = {"S2": {"n_hold": 0.05}, "S1": {"sma_months": 0.03}}
    md1 = render_robustness(census, verdicts, plateaus)
    md2 = render_robustness(census, verdicts, plateaus)
    assert md1 == md2 and "n_trials=132" in md1 and "S2" in md1 and "FAIL" in md1
    assert md1.count("PASS") + md1.count("FAIL") >= 2          # a verdict per family


# ---------------------------------------------------------------------------
# Review fix: the §4 placebo must run under the SAME trend gate as the strategy (spec E2)
# ---------------------------------------------------------------------------
from autotrader.robustness import placebo_gate_for


def test_placebo_gate_for_matches_strategy_gate():
    assert placebo_gate_for(build_strategy("S2", {"n_hold": 3, "gate": "on"})) == (10, 0.01)
    assert placebo_gate_for(build_strategy("S2", {"n_hold": 3, "gate": "off"})) == (1, 0.0)
    # S4's gate is its sma_months knob (S4TrendGatedMomentum gates its inner S2 sleeve on sma_months)
    assert placebo_gate_for(build_strategy("S4", {"n_hold": 4, "sma_months": 8})) == (8, 0.01)
    assert placebo_gate_for(build_strategy("S4", {"n_hold": 3, "sma_months": 12})) == (12, 0.01)
