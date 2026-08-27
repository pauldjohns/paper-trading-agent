# tests/test_indicators.py
import math
import numpy as np
import pandas as pd
import pytest
from autotrader.indicators import daily_returns
from autotrader.indicators import sma


def test_daily_returns_basic():
    r = daily_returns(pd.Series([100.0, 110.0, 99.0]))
    assert math.isnan(r.iloc[0])           # first bar has no prior close
    assert abs(r.iloc[1] - 0.10) < 1e-12   # 110/100 - 1
    assert abs(r.iloc[2] - (-0.10)) < 1e-12  # 99/110 - 1


def test_daily_returns_accepts_list_and_keeps_length():
    r = daily_returns([10.0, 12.0])
    assert len(r) == 2
    assert abs(r.iloc[1] - 0.2) < 1e-12


def test_sma_window_2():
    s = sma(pd.Series([1.0, 2.0, 3.0, 4.0]), window=2)
    assert math.isnan(s.iloc[0])          # warm-up: need `window` values
    assert s.iloc[1] == 1.5
    assert s.iloc[2] == 2.5
    assert s.iloc[3] == 3.5


def test_sma_window_3_warmup():
    s = sma(pd.Series([2.0, 4.0, 6.0, 8.0]), window=3)
    assert math.isnan(s.iloc[0]) and math.isnan(s.iloc[1])
    assert s.iloc[2] == 4.0                # (2+4+6)/3
    assert s.iloc[3] == 6.0                # (4+6+8)/3


def test_sma_rejects_bad_window():
    with pytest.raises(ValueError):
        sma(pd.Series([1.0, 2.0]), window=0)


from autotrader.indicators import rolling_high


def test_rolling_high_window_2():
    h = rolling_high(pd.Series([1.0, 3.0, 2.0, 5.0, 4.0]), window=2)
    assert math.isnan(h.iloc[0])
    assert h.iloc[1] == 3.0   # max(1,3)
    assert h.iloc[2] == 3.0   # max(3,2)
    assert h.iloc[3] == 5.0   # max(2,5)
    assert h.iloc[4] == 5.0   # max(5,4)


def test_rolling_high_full_window():
    h = rolling_high(pd.Series([10.0, 9.0, 8.0]), window=3)
    assert math.isnan(h.iloc[0]) and math.isnan(h.iloc[1])
    assert h.iloc[2] == 10.0


from autotrader.indicators import nearness_to_high


def test_nearness_at_fresh_high_is_one():
    n = nearness_to_high(pd.Series([10.0, 11.0, 12.0]), window=3)
    assert n.iloc[2] == 1.0   # 12 is the window high


def test_nearness_below_high_is_fraction():
    n = nearness_to_high(pd.Series([10.0, 12.0, 9.0]), window=3)
    assert math.isnan(n.iloc[0]) and math.isnan(n.iloc[1])
    assert abs(n.iloc[2] - (9.0 / 12.0)) < 1e-12   # 0.75


def test_nearness_uses_closes_not_intraday_highs():
    # Only closes are passed in; the function must not require/peek at any 'high' column.
    n = nearness_to_high([100.0, 90.0, 95.0], window=2)
    assert abs(n.iloc[2] - (95.0 / 95.0)) < 1e-12   # window=[90,95] -> high 95 -> 1.0


from autotrader.indicators import wilder_rsi

_RSI2_CLOSES = [44.00, 44.34, 44.09, 44.50, 44.22, 44.65, 44.85, 44.40, 44.90]
_RSI2_ORACLE = [None, None, 57.6271, 82.2695, 45.8498, 77.0519, 85.0600, 33.0929, 71.6214]


def test_wilder_rsi2_matches_oracle():
    r = wilder_rsi(pd.Series(_RSI2_CLOSES), period=2)
    assert math.isnan(r.iloc[0]) and math.isnan(r.iloc[1])   # warm-up = period+1 closes
    for i in range(2, len(_RSI2_CLOSES)):
        assert abs(r.iloc[i] - _RSI2_ORACLE[i]) < 1e-4, f"bar {i}: {r.iloc[i]} != {_RSI2_ORACLE[i]}"


def test_wilder_rsi2_all_up_is_100():
    r = wilder_rsi(pd.Series([10.0, 11.0, 12.0, 13.0]), period=2)
    assert r.iloc[2] == 100.0 and r.iloc[3] == 100.0   # avgLoss==0 -> 100


def test_wilder_rsi2_all_down_is_0():
    r = wilder_rsi(pd.Series([13.0, 12.0, 11.0, 10.0]), period=2)
    assert r.iloc[2] == 0.0 and r.iloc[3] == 0.0        # avgGain==0 -> 0


def test_wilder_rsi2_flat_prices_is_100():
    # All deltas zero -> avgLoss==0 -> RSI 100 (avgLoss==0 is checked before avgGain==0).
    # Locked, documented convention; harmless for S3 (CumRSI 200 never triggers its <35 entry).
    r = wilder_rsi(pd.Series([10.0, 10.0, 10.0, 10.0]), period=2)
    assert r.iloc[2] == 100.0 and r.iloc[3] == 100.0


def test_wilder_rsi_too_short_is_all_nan():
    r = wilder_rsi(pd.Series([10.0, 11.0]), period=2)   # need period+1 closes
    assert r.isna().all()


from autotrader.indicators import cumulative_rsi


def test_cumulative_rsi22_is_sum_of_two_rsi2():
    closes = pd.Series(_RSI2_CLOSES)
    c = cumulative_rsi(closes, rsi_period=2, lookback=2)
    assert math.isnan(c.iloc[2])   # only one RSI value so far -> NaN
    # index 3 = RSI[2] + RSI[3] = 57.6271 + 82.2695
    assert abs(c.iloc[3] - (57.6271 + 82.2695)) < 1e-3
    # index 4 = RSI[3] + RSI[4] = 82.2695 + 45.8498
    assert abs(c.iloc[4] - (82.2695 + 45.8498)) < 1e-3


def test_cumulative_rsi_warmup_all_nan_when_too_short():
    c = cumulative_rsi(pd.Series([10.0, 11.0, 12.0]), rsi_period=2, lookback=2)
    assert c.isna().all()   # RSI valid only at index 2; need two RSI values for the sum


from autotrader.indicators import trailing_return


def test_trailing_return_no_skip():
    p = pd.Series([100.0, 110.0, 121.0])   # +10%, +10%
    r = trailing_return(p, lookback=2, skip=0)
    assert math.isnan(r.iloc[0]) and math.isnan(r.iloc[1])
    assert abs(r.iloc[2] - (121.0 / 100.0 - 1.0)) < 1e-12   # 0.21


def test_trailing_return_skip_one_drops_most_recent():
    p = pd.Series([100.0, 110.0, 121.0, 90.0])   # last bar is a crash, skipped by skip=1
    r = trailing_return(p, lookback=3, skip=1)
    # at t=3: p[t-1]/p[t-3] - 1 = 121/100 - 1, the -25% last bar is excluded
    assert abs(r.iloc[3] - (121.0 / 100.0 - 1.0)) < 1e-12


def test_trailing_return_validates_args():
    with pytest.raises(ValueError):
        trailing_return(pd.Series([1.0, 2.0]), lookback=1, skip=2)   # skip must be < lookback


import datetime as dt
from autotrader.indicators import monthly_closes


def test_monthly_closes_takes_last_trading_day_per_month():
    dates = [dt.date(2026, 1, 29), dt.date(2026, 1, 30),   # Jan: last is 1/30
             dt.date(2026, 2, 2), dt.date(2026, 2, 27)]    # Feb: last is 2/27
    closes = [100.0, 101.0, 102.0, 103.0]
    m = monthly_closes(dates, closes)
    assert list(m["date"]) == [dt.date(2026, 1, 30), dt.date(2026, 2, 27)]
    assert list(m["close"]) == [101.0, 103.0]


def test_monthly_closes_sorts_and_handles_single_month():
    dates = [dt.date(2026, 3, 31), dt.date(2026, 3, 2)]   # unsorted input
    closes = [310.0, 302.0]
    m = monthly_closes(dates, closes)
    assert list(m["date"]) == [dt.date(2026, 3, 31)]      # last trading day of March
    assert list(m["close"]) == [310.0]


from autotrader.indicators import align_monthly_to_daily


def test_align_monthly_to_daily_forward_fills():
    daily = [dt.date(2026, 1, 30), dt.date(2026, 2, 2), dt.date(2026, 2, 27), dt.date(2026, 3, 2)]
    m_dates = [dt.date(2026, 1, 30), dt.date(2026, 2, 27)]
    m_vals = [1.0, 0.0]
    a = align_monthly_to_daily(daily, m_dates, m_vals)
    assert a.iloc[0] == 1.0   # 1/30 is a month-end -> its own value
    assert a.iloc[1] == 1.0   # 2/2  -> latest month-end <= 2/2 is 1/30
    assert a.iloc[2] == 0.0   # 2/27 is a month-end -> its own value
    assert a.iloc[3] == 0.0   # 3/2  -> latest month-end <= 3/2 is 2/27


def test_align_monthly_to_daily_nan_before_first_month_end():
    a = align_monthly_to_daily([dt.date(2026, 1, 5)], [dt.date(2026, 1, 30)], [1.0])
    assert a.isna().all()     # no month-end on or before 1/5
