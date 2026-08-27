# tests/live/test_session_clock.py
import datetime as dt
from zoneinfo import ZoneInfo
from autotrader_live import session_clock as sc

ET = ZoneInfo("America/New_York")

def test_regular_session_open_midday():
    assert sc.is_regular_session(dt.datetime(2026, 6, 23, 11, 0, tzinfo=ET)) is True

def test_before_open_and_after_close():
    assert sc.is_regular_session(dt.datetime(2026, 6, 23, 9, 0, tzinfo=ET)) is False
    assert sc.is_regular_session(dt.datetime(2026, 6, 23, 16, 30, tzinfo=ET)) is False

def test_weekend_closed():
    assert sc.is_regular_session(dt.datetime(2026, 6, 27, 11, 0, tzinfo=ET)) is False  # Saturday

def test_half_day_early_close():
    # 2026-11-27 (day after Thanksgiving) closes 13:00 ET
    assert sc.is_regular_session(dt.datetime(2026, 11, 27, 12, 30, tzinfo=ET)) is True
    assert sc.is_regular_session(dt.datetime(2026, 11, 27, 13, 30, tzinfo=ET)) is False

def test_minutes_to_close():
    now = dt.datetime(2026, 6, 23, 15, 30, tzinfo=ET)
    assert sc.minutes_to_close(now) == 30


def test_utc_aware_input_converted_to_et():
    # 15:00 UTC == 11:00 ET (midday RTH) — proves the ET conversion happens.
    utc_midday = dt.datetime(2026, 6, 23, 15, 0, tzinfo=ZoneInfo("UTC"))
    assert sc.is_regular_session(utc_midday) is True

def test_utc_aware_input_outside_rth_is_false():
    # 02:00 UTC == 22:00 ET prior day (outside RTH) — False after ET conversion.
    utc_overnight = dt.datetime(2026, 6, 23, 2, 0, tzinfo=ZoneInfo("UTC"))
    assert sc.is_regular_session(utc_overnight) is False
