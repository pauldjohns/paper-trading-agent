# tests/test_strategies.py
import datetime as dt
import pandas as pd
import pytest
from autotrader.strategies import select_with_hysteresis, trend_regime


def _bars(dates, closes):
    return pd.DataFrame({"date": dates, "open": closes,
                         "high": [c * 1.001 for c in closes], "low": [c * 0.999 for c in closes],
                         "close": closes, "volume": [1] * len(closes)})


def test_hysteresis_keeps_held_name_within_buffer():
    # held A,B; now ranks A=1,C=2,B=3. With N=2, buffer=1 (keep rank<=3), B is kept over C.
    assert select_with_hysteresis(["A", "C", "B", "D"], {"A", "B"}, 2, 1) == {"A", "B"}


def test_hysteresis_drops_held_name_past_buffer():
    # B falls to rank 4 (> N+buffer=3) -> sold; fresh rank-2 C takes the slot.
    assert select_with_hysteresis(["A", "C", "D", "B"], {"A", "B"}, 2, 1) == {"A", "C"}


def test_hysteresis_fresh_picks_top_n():
    assert select_with_hysteresis(["A", "B", "C", "D"], set(), 2, 1) == {"A", "B"}


def test_hysteresis_both_held_within_buffer_kept():
    assert select_with_hysteresis(["C", "A", "B", "D"], {"A", "B"}, 2, 1) == {"A", "B"}


# 8 months, 2 bars/month; sma_months=3 -> regime off until month 4, on months 4-6, off months 7-8.
_TR_MC = [100, 100, 100, 110, 110, 110, 95, 95]
def _tr_axis():
    dates, closes = [], []
    for i, c in enumerate(_TR_MC):
        dates += [dt.date(2026, i + 1, 15), dt.date(2026, i + 1, 28)]
        closes += [c, c]
    return dates, closes


def test_trend_regime_flips_on_then_off():
    dates, closes = _tr_axis()
    reg = trend_regime(dates, pd.Series(closes), sma_months=3, band=0.01)
    month_end = [bool(reg.iloc[i]) for i in range(1, len(dates), 2)]   # the 28th of each month
    assert month_end == [False, False, False, True, True, True, False, False]


from autotrader.strategies import S1Trend


def test_s1_holds_equity_on_risk_on_bond_on_risk_off():
    dates, closes = _tr_axis()
    bars = {"SPY": _bars(dates, closes), "IEF": _bars(dates, [50.0] * len(dates))}
    w = S1Trend(sma_months=3).target_weights(bars)
    spy_me = [w["SPY"].iloc[i] for i in range(1, len(dates), 2)]
    ief_me = [w["IEF"].iloc[i] for i in range(1, len(dates), 2)]
    assert spy_me == [0, 0, 0, 1, 1, 1, 0, 0]
    assert ief_me == [1, 1, 1, 0, 0, 0, 1, 1]
    assert S1Trend().cost_strategy is None and S1Trend().stop_loss_pct == 0.20
    # the engine must pass a calendar-aligned bars dict; misalignment raises loudly
    with pytest.raises(ValueError):
        S1Trend(sma_months=3).target_weights(
            {"SPY": _bars(dates, closes), "IEF": _bars(dates[:-1], [50.0] * (len(dates) - 1))})


from autotrader.strategies import S2SectorMomentum

_S2_MDAYS = [8, 18, 27]
def _s2_axis():
    return [dt.date(2026, m, x) for m in [1, 2, 3, 4, 5] for x in _S2_MDAYS]
_S2_SECTORS = {
    "XLK": [10, 10.1, 10.2, 10.3, 10.4, 10.5, 10.6, 10.7, 10.8, 10.9, 11.0, 11.1, 11.2, 11.3, 11.4],
    "XLF": [10, 10, 10, 10.2, 10.4, 10.6, 10.8, 11.0, 11.2, 11.0, 10.8, 10.6, 10.4, 10.2, 10.0],
    "XLE": [10, 9.8, 9.6, 9.7, 9.9, 10.1, 10.4, 10.8, 11.2, 11.6, 12.0, 12.4, 12.8, 13.2, 13.6],
    "XLV": [10, 9.9, 9.8, 9.7, 9.6, 9.5, 9.4, 9.3, 9.2, 9.1, 9.0, 8.9, 8.8, 8.7, 8.6],
}
def _s2_bars():
    dates = _s2_axis()
    bars = {k: _bars(dates, v) for k, v in _S2_SECTORS.items()}
    bars["SPY"] = _bars(dates, [v for v in [100, 106, 112, 118, 124] for _ in _S2_MDAYS])  # rising -> gate on from m3
    return dates, bars


def _held(w, sectors, t):
    return {s: round(w[s].iloc[t], 3) for s in sectors if w[s].iloc[t] > 0}


def test_s2_gated_topn_with_hysteresis():
    dates, bars = _s2_bars()
    w = S2SectorMomentum(list(_S2_SECTORS), gate_symbol="SPY", n_hold=2, buffer=1,
                         nearness_window=3, gate_sma_months=3, gate_band=0.01).target_weights(bars)
    for t in range(8):                                   # gate off -> all cash
        assert _held(w, _S2_SECTORS, t) == {}
    assert _held(w, _S2_SECTORS, 8) == {"XLK": 0.5, "XLF": 0.5}   # gate on; top-2 by nearness
    assert _held(w, _S2_SECTORS, 9) == {"XLK": 0.5, "XLF": 0.5}   # XLF kept by the buffer
    assert _held(w, _S2_SECTORS, 10) == {"XLK": 0.5, "XLE": 0.5}  # XLF falls past buffer -> XLE rotates in
    assert _held(w, _S2_SECTORS, 14) == {"XLK": 0.5, "XLE": 0.5}


from autotrader.strategies import S3MeanReversion

_S3_BASE = [100, 101, 99, 100, 102, 98, 101, 100, 99, 102, 140, 139, 141, 140]

def _s3_weights(closes, time_stop):
    dates = [dt.date(2026, 3, 1) + dt.timedelta(days=i) for i in range(len(closes))]
    w = S3MeanReversion(["QQQ"], regime_sma=10, exit_sma=3, cumrsi_entry=35, rsi_exit=65,
                        time_stop_days=time_stop).target_weights({"QQQ": _bars(dates, closes)})
    return [round(x, 2) for x in w["QQQ"].values]


def test_s3_enters_on_cumrsi_then_exits_on_sma():
    # sharp 2-day dip (CumRSI<35, close stays > SMA10) -> enter t15; close jumps > SMA3 -> exit t16.
    assert _s3_weights(_S3_BASE + [130, 122, 142, 143], 5) == [0.0] * 15 + [1.0, 0.0, 0.0]


def test_s3_time_stop_forces_exit():
    # same entry t15; price grinds below SMA3 (no SMA/RSI exit) -> time-stop after 3 days -> flat t18.
    assert _s3_weights(_S3_BASE + [130, 122, 121, 120, 119, 118, 124], 3) == [0.0] * 15 + [1.0, 1.0, 1.0, 0.0, 0.0, 0.0]


def test_s3_regime_blocks_entry_when_below_sma():
    # a deeper dip drives close BELOW SMA10 at the oversold bar -> regime filter blocks entry.
    assert _s3_weights(_S3_BASE + [118, 108, 120], 5) == [0.0] * 17


def test_s3_cost_strategy_is_floored():
    assert S3MeanReversion(["SPY"]).cost_strategy == "S3" and S3MeanReversion(["SPY"]).stop_loss_pct == 0.10


from autotrader.strategies import S4TrendGatedMomentum


def test_s4_bonds_when_off_basket_when_on():
    dates, bars = _s2_bars()
    bars = dict(bars); bars["IEF"] = _bars(dates, [50.0] * len(dates))
    s4 = S4TrendGatedMomentum(list(_S2_SECTORS), equity="SPY", bond="IEF", n_hold=2, buffer=1,
                              nearness_window=3, sma_months=3, band=0.01)
    w = s4.target_weights(bars)
    for t in range(8):                                       # regime off -> bonds
        assert _held(w, s4.universe, t) == {"IEF": 1.0}
    assert _held(w, s4.universe, 8) == {"XLK": 0.5, "XLF": 0.5}    # regime on -> S2 basket
    assert _held(w, s4.universe, 10) == {"XLK": 0.5, "XLE": 0.5}
    # invariant: no row over-allocates (would catch a bonds+sectors double-count if the
    # regime signal ever diverged between the S4 bond leg and the S2 gate)
    assert (w[s4.universe].sum(axis=1) <= 1.0 + 1e-9).all()
