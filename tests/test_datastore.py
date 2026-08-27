# tests/test_datastore.py
import json, pytest, pandas as pd
from pathlib import Path
from autotrader.ingest import parse_historicals
from autotrader.datastore import DataStore

_FIX = Path(__file__).resolve().parent / "fixtures"

def test_write_then_load_roundtrip(tmp_path):
    with open(_FIX / "historicals_aapl_2026ytd.json") as f:
        df = parse_historicals(json.load(f))
    store = DataStore(cache_dir=tmp_path)
    store.write("AAPL", "day", "all", df)
    pd.testing.assert_frame_equal(store.load("AAPL", "day", "all"), df)

def test_load_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        DataStore(cache_dir=tmp_path).load("ZZZZ", "day", "all")

def test_write_rejects_unsorted_or_dupe_dates(tmp_path):
    store = DataStore(cache_dir=tmp_path)
    bad = pd.DataFrame({
        "date": [pd.Timestamp("2026-01-02").date(), pd.Timestamp("2026-01-02").date()],
        "open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
        "close": [1.0, 1.0], "volume": [1, 1]})
    with pytest.raises(ValueError):
        store.write("AAPL", "day", "all", bad)

def test_write_rejects_timestamp_date_column(tmp_path):
    # A datetime64 date column yields pd.Timestamp on .tolist(), which silently breaks
    # TradingCalendar (Timestamp != datetime.date in a set / unorderable vs date in bisect).
    # write() must reject it so the broken-calendar foot-gun cannot reach the cache.
    store = DataStore(cache_dir=tmp_path)
    bad = pd.DataFrame({
        "date": pd.to_datetime(["2026-01-02", "2026-01-05"]),  # datetime64[ns] -> Timestamps
        "open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
        "close": [1.0, 1.0], "volume": [1, 1]})
    with pytest.raises(ValueError):
        store.write("AAPL", "day", "all", bad)
