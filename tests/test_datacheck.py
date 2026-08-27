# tests/test_datacheck.py
import datetime as dt
import pandas as pd
import pytest
from autotrader.calendar_nyse import TradingCalendar
from autotrader.datacheck import verify_series

CAL = TradingCalendar([dt.date(2026, 1, 2), dt.date(2026, 1, 5), dt.date(2026, 1, 6),
                       dt.date(2026, 1, 7), dt.date(2026, 1, 8)])
_COLS = ["date", "open", "high", "low", "close", "volume"]

def _df(dates):
    n = len(dates)
    return pd.DataFrame({"date": dates, "open": [1.0] * n, "high": [1.0] * n, "low": [1.0] * n,
                         "close": [1.0] * n, "volume": [1] * n})[_COLS]


def test_clean_series_has_no_problems():
    df = _df([dt.date(2026, 1, 2), dt.date(2026, 1, 5), dt.date(2026, 1, 6), dt.date(2026, 1, 7)])
    assert verify_series(df, CAL, min_start=dt.date(2026, 1, 2), min_rows=3) == []


def test_gap_is_flagged():
    # missing 2026-01-06 (a calendar trading day) between 1/5 and 1/7
    df = _df([dt.date(2026, 1, 2), dt.date(2026, 1, 5), dt.date(2026, 1, 7)])
    probs = verify_series(df, CAL, min_start=dt.date(2026, 1, 2), min_rows=3)
    assert any("gap" in p for p in probs)


def test_late_start_and_too_few_rows_flagged():
    df = _df([dt.date(2026, 1, 6), dt.date(2026, 1, 7)])
    probs = verify_series(df, CAL, min_start=dt.date(2026, 1, 2), min_rows=3)
    assert any("starts" in p for p in probs) and any("rows" in p for p in probs)


def test_timestamp_date_column_flagged():
    df = _df(list(pd.to_datetime(["2026-01-02", "2026-01-05"])))  # Timestamps, not date
    assert any("datetime.date" in p for p in verify_series(df, CAL, dt.date(2026, 1, 2), 1))
