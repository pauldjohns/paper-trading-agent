# src/autotrader/datastore.py
"""Local parquet cache for normalized OHLCV bars."""
import datetime as dt
from pathlib import Path
import pandas as pd

_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


class DataStore:
    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol, interval, adjustment):
        return self.cache_dir / f"{symbol}_{interval}_{adjustment}.parquet"

    def write(self, symbol, interval, adjustment, df):
        if list(df.columns) != _COLUMNS:
            raise ValueError(f"unexpected columns: {list(df.columns)}")
        dates = df["date"].tolist()
        # Date dtype guard: the column must hold plain datetime.date objects. A datetime64
        # column yields pd.Timestamp (a datetime.datetime subclass) on .tolist(), which
        # silently breaks TradingCalendar (Timestamp != datetime.date in a set; unorderable
        # against date in bisect). Reject it here so a broken calendar can't reach the cache.
        if any(isinstance(d, dt.datetime) for d in dates):
            raise ValueError("date column must hold datetime.date, not Timestamp/datetime")
        if not all(isinstance(d, dt.date) for d in dates):
            raise ValueError("date column must hold datetime.date objects")
        if dates != sorted(dates):
            raise ValueError("dates must be sorted ascending")
        if len(set(dates)) != len(dates):
            raise ValueError("duplicate dates not allowed")
        df.to_parquet(self._path(symbol, interval, adjustment), index=False)

    def load(self, symbol, interval, adjustment):
        path = self._path(symbol, interval, adjustment)
        if not path.exists():
            raise FileNotFoundError(str(path))
        return pd.read_parquet(path)[_COLUMNS].reset_index(drop=True)
