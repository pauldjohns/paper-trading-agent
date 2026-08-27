# tests/test_calendar_nyse.py
import datetime as dt, pytest
from autotrader.calendar_nyse import TradingCalendar

DAYS = [dt.date(2026, 1, 2), dt.date(2026, 1, 5), dt.date(2026, 1, 6), dt.date(2026, 1, 7)]

def test_is_trading_day():
    cal = TradingCalendar(DAYS)
    assert cal.is_trading_day(dt.date(2026, 1, 5)) is True
    assert cal.is_trading_day(dt.date(2026, 1, 3)) is False  # Saturday

def test_next_trading_day_skips_weekend():
    assert TradingCalendar(DAYS).next_trading_day(dt.date(2026, 1, 2)) == dt.date(2026, 1, 5)

def test_next_trading_day_from_nontrading_day():
    assert TradingCalendar(DAYS).next_trading_day(dt.date(2026, 1, 3)) == dt.date(2026, 1, 5)

def test_next_trading_day_past_end_raises():
    with pytest.raises(ValueError):
        TradingCalendar(DAYS).next_trading_day(dt.date(2026, 1, 7))

def test_add_trading_days():
    cal = TradingCalendar(DAYS)
    assert cal.add_trading_days(dt.date(2026, 1, 2), 2) == dt.date(2026, 1, 6)

def test_add_trading_days_zero_returns_input_unchanged():
    # n=0 is a no-op even on a non-trading day; it does NOT snap to the next trading day.
    assert TradingCalendar(DAYS).add_trading_days(dt.date(2026, 1, 3), 0) == dt.date(2026, 1, 3)  # Saturday

def test_add_trading_days_from_nontrading_day_advances():
    # From a non-trading day, n>=1 lands on the next trading day(s).
    assert TradingCalendar(DAYS).add_trading_days(dt.date(2026, 1, 3), 1) == dt.date(2026, 1, 5)
