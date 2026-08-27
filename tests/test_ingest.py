# tests/test_ingest.py
import json, pytest, pandas as pd
from pathlib import Path
from autotrader.ingest import parse_historicals

_FIX = Path(__file__).resolve().parent / "fixtures"

def test_parse_historicals_from_real_fixture():
    with open(_FIX / "historicals_aapl_2026ytd.json") as f:
        df = parse_historicals(json.load(f))
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(df) == 3
    assert str(df.iloc[0]["date"]) == "2026-01-02"
    assert str(df.iloc[-1]["date"]) == "2026-01-06"
    assert df.iloc[0]["open"] == 272.255
    assert df.iloc[0]["close"] == 271.010
    assert df.iloc[0]["volume"] == 37838054

def test_parse_drops_interpolated_bars():
    raw = {"data": {"results": [{"bars": [
        {"begins_at": "2026-01-02T00:00:00Z", "open_price": "1", "close_price": "1",
         "high_price": "1", "low_price": "1", "volume": 10, "session": "reg"},
        {"begins_at": "2026-01-03T00:00:00Z", "open_price": "1", "close_price": "1",
         "high_price": "1", "low_price": "1", "volume": 0, "session": "reg", "interpolated": True},
    ]}]}}
    df = parse_historicals(raw)
    assert len(df) == 1  # interpolated gap-fill bar dropped

def test_parse_historicals_rejects_empty():
    with pytest.raises(ValueError):
        parse_historicals({"data": {"results": []}})

def test_parse_rejects_bar_missing_keys():
    # A non-interpolated bar missing a required OHLCV key should give a clear ValueError,
    # not a bare KeyError, so malformed MCP responses are debuggable.
    raw = {"data": {"results": [{"bars": [
        {"begins_at": "2026-01-02T00:00:00Z", "open_price": "1", "close_price": "1",
         "high_price": "1", "volume": 10, "session": "reg"},  # missing low_price
    ]}]}}
    with pytest.raises(ValueError):
        parse_historicals(raw)
