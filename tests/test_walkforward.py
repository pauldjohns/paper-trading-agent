# tests/test_walkforward.py
import datetime as dt
import numpy as np
import pandas as pd
import pytest
from autotrader.walkforward import expanding_windows, stress_folds, STRESS_PERIODS


def _daily_returns(start, end, val=0.0):
    idx = pd.date_range(start, end, freq="D")
    dates = pd.Index([d.date() for d in idx], name="date")
    return pd.Series([val] * len(dates), index=dates)


def test_expanding_windows_anchor_at_start_step_yearly():
    r = _daily_returns(dt.date(2010, 1, 1), dt.date(2013, 6, 30))
    wins = expanding_windows(r)
    # each window starts at the series start; ends at successive year-ends (2010,2011,2012) + full
    assert all(w.index[0] == r.index[0] for w in wins)
    ends = [w.index[-1] for w in wins]
    assert ends[0].year == 2010 and ends[-1] == r.index[-1]
    assert len(wins) >= 3


def test_stress_folds_extract_named_periods_present_in_data():
    r = _daily_returns(dt.date(2007, 1, 1), dt.date(2023, 12, 31))
    folds = stress_folds(r)
    assert set(folds) == {"2008-09", "2020", "2022"}
    f = folds["2008-09"]
    assert f.index[0] >= dt.date(2008, 1, 1) and f.index[-1] <= dt.date(2009, 12, 31)
    assert len(f) > 0


def test_stress_folds_skip_absent_periods():
    r = _daily_returns(dt.date(2018, 1, 1), dt.date(2019, 12, 31))   # no 2008/2020/2022 coverage
    folds = stress_folds(r)
    assert folds == {} or all(len(v) == 0 for v in folds.values())
