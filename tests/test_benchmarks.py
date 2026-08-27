# tests/test_benchmarks.py
import datetime as dt
import numpy as np
import pandas as pd
import pytest
from autotrader.benchmarks import BuyHold, GatedSPY, EqualWeightUniverse, sixty_forty_returns
from autotrader.engine import BacktestEngine


def _bars(dates, closes):
    return pd.DataFrame({"date": dates, "open": closes,
                         "high": [c * 1.001 for c in closes], "low": [c * 0.999 for c in closes],
                         "close": closes, "volume": [1] * len(closes)})


_D = [dt.date(2025, 1, 2) + dt.timedelta(days=i) for i in range(300)]


def test_buyhold_is_always_fully_invested_after_entry():
    bh = BuyHold("SPY")
    assert bh.universe == ["SPY"] and bh.stop_loss_pct is None
    w = bh.target_weights({"SPY": _bars(_D, [100 + 0.1 * i for i in range(len(_D))])})
    assert (w["SPY"] == 1.0).all()


def test_gated_spy_matches_trend_regime():
    from autotrader.strategies import trend_regime
    g = GatedSPY(sma_months=3)
    spy = _bars(_D, [100 + (10 if i > 150 else -0.05 * i) for i in range(len(_D))])
    w = g.target_weights({"SPY": spy})
    on = trend_regime(list(spy["date"]), spy["close"], sma_months=3, band=0.01)
    assert (w["SPY"].values == on.astype(float).values).all()      # SPY when on, cash when off


def test_equal_weight_universe_sums_to_one_each_row():
    ew = EqualWeightUniverse(["XLK", "XLF", "XLE"])
    bars = {s: _bars(_D, [10 + 0.01 * i for i in range(len(_D))]) for s in ew.universe}
    w = ew.target_weights(bars)
    assert np.allclose(w.sum(axis=1), 1.0) and np.allclose(w["XLK"], 1 / 3)


def test_buyhold_runs_through_engine_priceonly_net_of_one_entry_cost():
    spy = _bars(_D, [100 * (1.0003 ** i) for i in range(len(_D))])
    res = BacktestEngine(BuyHold("SPY"), {"SPY": spy}, initial_cash=1000.0, stress=1.0).run()
    assert all(t.exit_reason == "terminal" for t in res.trades)  # no within-sample exits, only terminal mark-to-close
    assert res.equity.iloc[-1] > 1000.0    # grew with price, minus the entry half-cost
    assert res.skipped_buys == []


def test_sixty_forty_constant_mix_blends_daily_returns():
    spy = _bars(_D, [100 * (1.0004 ** i) for i in range(len(_D))])
    ief = _bars(_D, [50 * (1.0001 ** i) for i in range(len(_D))])
    r = sixty_forty_returns({"SPY": spy, "IEF": ief}, equity="SPY", bond="IEF", rebalance="ME")
    assert isinstance(r, pd.Series) and len(r) == len(_D)
    # on a non-rebalance day the blend sits between the two sleeve returns
    sr = spy["close"].pct_change(); br = ief["close"].pct_change()
    i = 5
    assert min(sr.iloc[i], br.iloc[i]) - 1e-6 <= r.iloc[i] <= max(sr.iloc[i], br.iloc[i]) + 1e-6


def test_sixty_forty_rebalance_cost_uses_tier_stress_and_reduces_return():
    spy = _bars(_D, [100 * (1.0004 ** i) for i in range(len(_D))])
    ief = _bars(_D, [50 * (1.0001 ** i) for i in range(len(_D))])
    gross = sixty_forty_returns({"SPY": spy, "IEF": ief}, charge_rebalance_cost=False).sum()
    net = sixty_forty_returns({"SPY": spy, "IEF": ief}, charge_rebalance_cost=True).sum()
    assert net < gross                              # the per-tier×stress rebalance cost is charged
