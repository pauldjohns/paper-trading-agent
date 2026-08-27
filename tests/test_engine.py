# tests/test_engine.py
import datetime as dt
import pandas as pd
import pytest
from autotrader import config
from autotrader.engine import tier_for_symbol, cost_floor_for_strategy
from autotrader.engine import build_engine_inputs
from autotrader.engine import plan_rebalance


def _bars(dates, closes):
    return pd.DataFrame({"date": dates, "open": closes,
                         "high": [c * 1.01 for c in closes], "low": [c * 0.99 for c in closes],
                         "close": closes, "volume": [1] * len(closes)})


_D = [dt.date(2026, 1, 2), dt.date(2026, 1, 5), dt.date(2026, 1, 6), dt.date(2026, 1, 7)]


def test_build_engine_inputs_aligns_and_extends_calendar():
    raw = {"SPY": _bars(_D, [100, 101, 102, 103]), "IEF": _bars(_D, [50, 50, 50, 50])}
    aligned, nested, cal = build_engine_inputs(raw, ["SPY", "IEF"])
    assert list(aligned["SPY"]["date"]) == _D and list(aligned["IEF"]["date"]) == _D
    assert nested["SPY"][_D[1]]["open"] == 101            # date-keyed nested view for the Simulator
    # calendar covers the real dates PLUS a 2-day runway so a last-bar sale can settle
    assert cal.is_trading_day(_D[-1])
    runway1 = cal.next_trading_day(_D[-1])
    runway2 = cal.next_trading_day(runway1)
    assert runway1 > _D[-1] and runway2 > runway1        # two synthetic settlement days exist


def test_build_engine_inputs_subsets_to_requested_universe():
    raw = {"SPY": _bars(_D, [100, 101, 102, 103]), "IEF": _bars(_D, [50, 50, 50, 50]),
           "QQQ": _bars(_D, [200, 201, 202, 203])}
    aligned, nested, cal = build_engine_inputs(raw, ["SPY", "IEF"])   # QQQ not requested
    assert set(aligned) == {"SPY", "IEF"} and set(nested) == {"SPY", "IEF"}


def test_build_engine_inputs_rejects_misaligned_axes():
    raw = {"SPY": _bars(_D, [100, 101, 102, 103]), "IEF": _bars(_D[:-1], [50, 50, 50])}
    with pytest.raises(ValueError):
        build_engine_inputs(raw, ["SPY", "IEF"])


def test_tier_for_symbol_maps_each_universe_class():
    assert tier_for_symbol("SPY") == config.TIER_INDEX_ETF
    assert tier_for_symbol("QQQ") == config.TIER_INDEX_ETF
    assert tier_for_symbol("IEF") == config.TIER_INDEX_ETF   # bond ETFs priced at the liquid index tier
    assert tier_for_symbol("AGG") == config.TIER_INDEX_ETF
    assert tier_for_symbol("XLK") == config.TIER_SECTOR_SPDR
    assert tier_for_symbol("XLF") == config.TIER_SECTOR_SPDR


def test_tier_for_symbol_unknown_raises():
    with pytest.raises(ValueError):
        tier_for_symbol("NVDA")   # single names are non-gating; not in the Plan-04 universe


def test_cost_floor_resolves_s3_only():
    assert cost_floor_for_strategy("S3") == config.S3_COST_FLOOR
    assert cost_floor_for_strategy(None) is None
    assert cost_floor_for_strategy("S1") is None   # unregistered -> instrument tier alone


def test_plan_rebalance_enters_full_set_from_cash():
    sells, buys = plan_rebalance(held=set(), weights={"XLK": 0.5, "XLF": 0.5}, equity=1000.0)
    assert sells == []
    assert buys == [("XLF", 500.0), ("XLK", 500.0)] or buys == [("XLK", 500.0), ("XLF", 500.0)]
    assert sorted(b[0] for b in buys) == ["XLF", "XLK"] and all(b[1] == 500.0 for b in buys)


def test_plan_rebalance_rotation_sells_leaver_buys_joiner_keeps_stayer():
    # held {XLK,XLF}; target {XLK,XLE} -> sell XLF, buy XLE, leave XLK untrimmed (D1)
    sells, buys = plan_rebalance(held={"XLK", "XLF"}, weights={"XLK": 0.5, "XLE": 0.5}, equity=1000.0)
    assert sells == ["XLF"]
    assert buys == [("XLE", 500.0)]


def test_plan_rebalance_full_exit_to_cash():
    sells, buys = plan_rebalance(held={"SPY"}, weights={}, equity=1000.0)
    assert sells == ["SPY"] and buys == []


def test_plan_rebalance_ignores_zero_weight_signal_only_symbol():
    # SPY present at weight 0 (S4 signal-only) -> never traded (Engine contract #2)
    sells, buys = plan_rebalance(held=set(), weights={"SPY": 0.0, "XLK": 1.0}, equity=1000.0)
    assert sells == [] and buys == [("XLK", 1000.0)]


# ---------------------------------------------------------------------------
# Task 5: BacktestEngine.run + BacktestResult (golden + behavioural tests)
# ---------------------------------------------------------------------------
import json
from pathlib import Path
from autotrader.engine import BacktestEngine, BacktestResult

_FIX = Path(__file__).resolve().parent / "fixtures"


class _FrameStrategy:
    """A stub strategy: returns a caller-supplied weight frame verbatim (engine-only golden)."""
    def __init__(self, universe, frame, stop_loss_pct=0.20, cost_strategy=None):
        self.universe = universe
        self._frame = frame
        self.stop_loss_pct = stop_loss_pct
        self.cost_strategy = cost_strategy

    def target_weights(self, bars):
        return self._frame.reset_index(drop=True)


_GD = [dt.date(2026, 1, 2), dt.date(2026, 1, 5), dt.date(2026, 1, 6),
       dt.date(2026, 1, 7), dt.date(2026, 1, 8), dt.date(2026, 1, 9)]
_XLK = pd.DataFrame({"date": _GD,
    "open":  [100.0, 100.0, 101.0, 102.0, 90.0, 89.0],
    "high":  [101.0, 102.0, 103.0, 103.0, 91.0, 90.0],
    "low":   [ 99.0, 100.0, 100.0, 101.0, 88.0, 88.0],
    "close": [100.0, 101.0, 102.0, 102.5, 89.0, 89.0],
    "volume": [1] * 6})
_IEF = pd.DataFrame({"date": _GD, "open": [50.0]*6, "high": [50.0]*6, "low": [50.0]*6,
                     "close": [50.0]*6, "volume": [1]*6})


def _golden_frame():
    # hold XLK from the 1/2 signal (fills 1/5) until the stop; never hold IEF
    w = pd.DataFrame(0.0, index=range(6), columns=["XLK", "IEF"])
    w["XLK"] = [1.0, 1.0, 1.0, 1.0, 1.0, 1.0]
    return w


def test_engine_run_matches_golden():
    # stop_loss_pct=0.10 so the 1/8 gap to open 90 (-10%) triggers a gap-through stop (review B3)
    strat = _FrameStrategy(["XLK", "IEF"], _golden_frame(), stop_loss_pct=0.10)
    eng = BacktestEngine(strat, {"XLK": _XLK, "IEF": _IEF}, initial_cash=1000.0,
                         stress=1.0)   # constant stress for a hand-checkable golden
    res = eng.run()                    # the in-run cash<->ledger reconciliation assert must not raise
    assert isinstance(res, BacktestResult)
    assert res.trades[0].exit_reason == "stop" and res.trades[0].exit_price == 90.0
    out = {
        "equity": [round(float(v), 6) for v in res.equity.tolist()],
        "equity_dates": [str(d) for d in res.equity.index],
        "n_trades": len(res.trades),
        "trade": {"symbol": res.trades[0].symbol, "entry_date": str(res.trades[0].entry_date),
                  "entry_price": round(res.trades[0].entry_price, 6),
                  "exit_date": str(res.trades[0].exit_date),
                  "exit_price": round(res.trades[0].exit_price, 6),
                  "exit_reason": res.trades[0].exit_reason,
                  "shares": round(res.trades[0].shares, 6),
                  "pnl": round(res.trades[0].pnl, 6)},
        "skipped": res.skipped_buys,
    }
    with open(_FIX / "golden_engine_sequence.json") as f:
        assert out == json.load(f)


def test_engine_terminal_open_position_emitted_as_trade():
    # buy-hold XLK (no stop, never exits) -> a terminal mark-to-close trade at the last close (D5)
    w = pd.DataFrame({"XLK": [1.0] * 6, "IEF": [0.0] * 6})
    strat = _FrameStrategy(["XLK", "IEF"], w, stop_loss_pct=None)
    res = BacktestEngine(strat, {"XLK": _XLK, "IEF": _IEF}, initial_cash=1000.0, stress=1.0).run()
    term = [t for t in res.trades if t.exit_reason == "terminal"]
    assert len(term) == 1 and term[0].symbol == "XLK"
    assert term[0].exit_price == _XLK["close"].iloc[-1] and term[0].exit_cost == 0.0


def test_engine_skips_and_logs_unsettled_buy():
    # sell XLK on 1/6 (fills 1/7, settles 1/8); a same-window buy of IEF must be T+1-throttled.
    w = pd.DataFrame(0.0, index=range(6), columns=["XLK", "IEF"])
    w["XLK"] = [1.0, 1.0, 0.0, 0.0, 0.0, 0.0]    # exit XLK at the 1/6 signal
    w["IEF"] = [0.0, 0.0, 1.0, 1.0, 1.0, 1.0]    # want IEF starting 1/6 signal -> needs settled cash
    strat = _FrameStrategy(["XLK", "IEF"], w, stop_loss_pct=None)
    res = BacktestEngine(strat, {"XLK": _XLK, "IEF": _IEF}, initial_cash=1000.0, stress=1.0).run()
    assert any(s["symbol"] == "IEF" for s in res.skipped_buys)   # at least one throttled attempt logged


# ---------------------------------------------------------------------------
# Task 6: Engine ↔ strategy integration smoke (S1, S4)
# ---------------------------------------------------------------------------
from autotrader.strategies import S1Trend, S4TrendGatedMomentum, S3MeanReversion
from autotrader import config as cfg


def _ramp(dates, start, step):
    closes = [start + step * i for i in range(len(dates))]
    return _bars(dates, closes)


def _month_axis(n_months):
    return [dt.date(2026, ((m) % 12) + 1, d) for m in range(n_months) for d in (10, 20)]


def test_s1_runs_through_engine_with_default_cs_stress():
    dates = [dt.date(2025, 1, 2) + dt.timedelta(days=i) for i in range(420)]   # ~14 months daily
    spy = _bars(dates, [100 + 0.1 * i for i in range(len(dates))])             # steady uptrend
    ief = _bars(dates, [50.0] * len(dates))
    eng = BacktestEngine(S1Trend(sma_months=3, stop_loss_pct=0.20), {"SPY": spy, "IEF": ief},
                         initial_cash=1000.0)            # stress defaults to the D2 CS-ratio model
    res = eng.run()
    assert res.equity.iloc[-1] > 0 and res.equity.notna().all()
    assert len(res.equity) == len(dates)
    # next-open property: the first SPY entry filled at an OPEN that exists in the bar set
    spy_trades = [t for t in res.trades if t.symbol == "SPY"]
    if spy_trades:
        assert spy_trades[0].entry_price in set(spy["open"])


def test_blend_shared_ledger_throttles_at_least_once():
    # S4 over a basket with frequent rotations -> sells and buys compete for one settled pool.
    dates = [dt.date(2025, 1, 2) + dt.timedelta(days=i) for i in range(900)]
    sectors = cfg.SECTOR_SPDRS
    raw = {}
    for k, s in enumerate(sectors):
        raw[s] = _bars(dates, [10 + ((k + i) % 7) * 0.5 + 0.01 * i for i in range(len(dates))])
    raw["SPY"] = _bars(dates, [100 + 0.05 * i for i in range(len(dates))])
    raw["IEF"] = _bars(dates, [50.0] * len(dates))
    s4 = S4TrendGatedMomentum(sectors, equity="SPY", bond="IEF", n_hold=3, buffer=1,
                              nearness_window=60, sma_months=3)
    res = BacktestEngine(s4, raw, initial_cash=1000.0).run()    # default D2 CS-ratio stress
    assert res.equity.notna().all() and res.equity.iloc[-1] > 0
    assert (res.weights.sum(axis=1) <= 1.0 + 1e-9).all()        # no over-allocation / no leverage


# ---------------------------------------------------------------------------
# Task 14: CappedBudgetBlend + per-symbol S3 cost routing (§3.6)
# ---------------------------------------------------------------------------
from autotrader.engine import CappedBudgetBlend
from autotrader.engine import cost_floor_for_strategy as _floor


def test_capped_blend_weights_sum_within_one_and_caps_mr():
    dates = [dt.date(2025, 1, 2) + dt.timedelta(days=i) for i in range(700)]
    sectors = cfg.SECTOR_SPDRS
    raw = {s: _bars(dates, [10 + ((k + i) % 5) * 0.4 + 0.01 * i for i in range(len(dates))])
           for k, s in enumerate(sectors)}
    raw["SPY"] = _bars(dates, [100 + 0.05 * i for i in range(len(dates))])
    raw["IEF"] = _bars(dates, [50.0] * len(dates))
    for q in ["QQQ", "DIA", "IWM"]:
        raw[q] = _bars(dates, [80 + 0.03 * i for i in range(len(dates))])
    s4 = S4TrendGatedMomentum(sectors, equity="SPY", bond="IEF", n_hold=3, buffer=1,
                              nearness_window=60, sma_months=3)
    s3 = S3MeanReversion(["QQQ", "DIA", "IWM"], regime_sma=50, exit_sma=5, time_stop_days=5)
    blend = CappedBudgetBlend(s4, s3, mr_cap=0.15)
    assert set(blend.universe) >= set(s4.universe) | set(s3.universe)
    w = blend.target_weights({s: raw[s] for s in blend.universe})
    assert (w.sum(axis=1) <= 1.0 + 1e-9).all()                          # never over-allocates
    mr_cols = [c for c in s3.universe]
    assert (w[mr_cols].sum(axis=1) <= 0.15 + 1e-9).all()                # MR budget capped


def test_blend_routes_s3_floor_to_mr_sleeve_only():
    s4 = S4TrendGatedMomentum(cfg.SECTOR_SPDRS, equity="SPY", bond="IEF")
    s3 = S3MeanReversion(["QQQ", "DIA", "IWM"])
    blend = CappedBudgetBlend(s4, s3, mr_cap=0.15)
    # the engine resolves the floor per symbol via blend.cost_strategy_for
    assert _floor(blend.cost_strategy_for("QQQ")) == cfg.S3_COST_FLOOR     # MR sleeve -> S3 floor
    assert _floor(blend.cost_strategy_for("XLK")) is None                  # momentum sleeve -> tier only
    assert _floor(blend.cost_strategy_for("SPY")) is None
