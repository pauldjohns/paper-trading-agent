# src/autotrader/datacheck.py
"""Offline integrity check for a cached OHLCV series populated from the MCP (Task 7). Pure:
takes a loaded DataFrame + a TradingCalendar; returns a list of problem strings (empty = clean)."""
import datetime as dt

_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


def verify_series(df, calendar, min_start, min_rows):
    if list(df.columns) != _COLUMNS:
        return [f"unexpected columns: {list(df.columns)}"]
    dates = df["date"].tolist()
    if not dates:
        return ["empty series"]
    # Date dtype first, and return early: a Timestamp column would crash the date comparisons below.
    if any(isinstance(d, dt.datetime) for d in dates) or not all(isinstance(d, dt.date) for d in dates):
        return ["date column must hold datetime.date"]
    problems = []
    if dates != sorted(dates):
        problems.append("dates not sorted ascending")
    if len(set(dates)) != len(dates):
        problems.append("duplicate dates")
    if df[["open", "high", "low", "close"]].isna().any().any():
        problems.append("NaN in OHLC")
    if len(df) < min_rows:
        problems.append(f"too few rows: {len(df)} < {min_rows}")
    if dates[0] > min_start:
        problems.append(f"history starts {dates[0]} > required {min_start}")
    # Gap check: consecutive cached dates must be ADJACENT calendar trading days.
    for i in range(len(dates) - 1):
        if not calendar.is_trading_day(dates[i]):
            problems.append(f"{dates[i]} is not a calendar trading day")
            break
        if calendar.next_trading_day(dates[i]) != dates[i + 1]:
            problems.append(f"gap after {dates[i]}")
            break
    return problems
