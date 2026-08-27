# tests/test_report.py
import datetime as dt
import numpy as np
import pandas as pd
import pytest
from autotrader.report import (summarize_run, build_variant_matrix, variant_dsr_pbo,
                               render_markdown, gate_verdict, first_active_index,
                               summarize_periods)


def _eq(vals, start=dt.date(2018, 1, 1)):
    idx = pd.Index([start + dt.timedelta(days=i) for i in range(len(vals))], name="date")
    return pd.Series([float(v) for v in vals], index=idx)


class _Res:   # minimal BacktestResult stand-in
    def __init__(self, equity, trades=(), weights=None):
        self.equity = equity
        self.returns = equity.pct_change()
        self.trades = list(trades)
        self.weights = weights if weights is not None else pd.DataFrame({"X": [1.0] * len(equity)})
        self.skipped_buys = []


def test_summarize_run_has_all_spec_metrics():
    res = _Res(_eq([1000 * (1.0003 ** i) for i in range(400)]))
    row = summarize_run(res)
    for key in ["cagr", "vol", "sharpe", "sortino", "max_dd", "calmar", "win_rate",
                "profit_factor", "avg_win", "avg_loss", "turnover", "time_in_market",
                "trades_per_year", "number_of_bets"]:
        assert key in row


def test_build_variant_matrix_aligns_configs_for_pbo():
    a = _Res(_eq([1000 * (1.0003 ** i) for i in range(300)]))
    b = _Res(_eq([1000 * (1.0001 ** i) for i in range(300)]))
    M = build_variant_matrix({"A": a, "B": b})
    assert list(M.columns) == ["A", "B"] and len(M) == 299    # daily returns, common length


def test_variant_dsr_uses_per_observation_units_not_annualized():
    # Two strong variants. With per-obs units DSR is a sane probability; the annualized-variance
    # bug (review B1) would collapse every DSR to ~0. Assert at least one variant is not ~0.
    a = _Res(_eq([1000 * (1.0006 ** i) for i in range(300)]))
    b = _Res(_eq([1000 * (1.0005 ** i) for i in range(300)]))
    out = variant_dsr_pbo({"A": a, "B": b})
    assert out["provisional"] is True
    assert max(d for d, _ in out["dsr"].values()) > 0.01     # not all false-killed by a unit error


def test_first_active_index_skips_leading_flat_cash():
    w = pd.DataFrame({"X": [0.0, 0.0, 0.0, 1.0, 1.0]})
    res = _Res(_eq([1000.0] * 5), weights=w)
    assert first_active_index(res) == 3                      # first bar a position is held


def test_gate_verdict_is_provisional_with_placebo_deferred():
    strat = _Res(_eq([1000 * (1.0004 ** i) for i in range(400)]))
    bench = _Res(_eq([1000 * (1.0002 ** i) for i in range(400)]))
    v = gate_verdict(strat, bench, spy=bench, dsr_p=0.01, pbo=0.2)
    assert v["overall"].startswith("PROVISIONAL")
    assert v["conditions"]["placebo_beats_95th"] == "DEFERRED-Plan05"
    assert set(["deflated_p_lt_0.05", "pbo_lt_0.5", "sharpe_ci_lower_ge_benchmark",
                "maxdd_upperci_lt_spy"]).issubset(v["conditions"])


def test_summarize_periods_computes_per_slice_metrics():
    import math
    from autotrader import metrics as M
    # Two named slices of daily returns
    r_bull = pd.Series([0.001] * 252)       # 252-bar gentle bull run
    r_bear = pd.Series([-0.002] * 100)      # 100-bar bear run
    out = summarize_periods({"bull": r_bull, "bear": r_bear})

    # Keys present
    assert set(out.keys()) == {"bull", "bear"}
    for name in ("bull", "bear"):
        assert set(out[name].keys()) == {"n", "cagr", "sharpe", "max_dd"}

    # n matches the input length
    assert out["bull"]["n"] == 252
    assert out["bear"]["n"] == 100

    # sharpe matches metrics.sharpe for the same series
    assert abs(out["bull"]["sharpe"] - M.sharpe(r_bull)) < 1e-9
    assert abs(out["bear"]["sharpe"] - M.sharpe(r_bear)) < 1e-9

    # max_dd <= 0 (bear run must be negative; bull run is monotone => 0.0)
    assert out["bull"]["max_dd"] <= 0.0
    assert out["bear"]["max_dd"] < 0.0

    # Edge: single-bar slice yields NaN metrics but correct n
    out_short = summarize_periods({"one": pd.Series([0.01])})
    assert out_short["one"]["n"] == 1
    assert math.isnan(out_short["one"]["cagr"])
    assert math.isnan(out_short["one"]["sharpe"])
    assert math.isnan(out_short["one"]["max_dd"])


def test_render_markdown_is_deterministic_text():
    res = _Res(_eq([1000 * (1.0002 ** i) for i in range(300)]))
    md1 = render_markdown({"S1": summarize_run(res)})
    md2 = render_markdown({"S1": summarize_run(res)})
    assert md1 == md2 and md1.startswith("|") and "sharpe" in md1.lower()
