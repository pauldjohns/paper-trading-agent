# src/autotrader/ingest.py
"""Normalize raw get_equity_historicals JSON into a clean OHLCV DataFrame."""
import pandas as pd


def parse_historicals(raw: dict) -> pd.DataFrame:
    """One get_equity_historicals response -> sorted OHLCV DataFrame.

    Columns: date (datetime.date), open/high/low/close (float), volume (int).
    Drops interpolated=true gap-fill bars (per MCP guidance). Raises ValueError if empty.
    """
    results = raw.get("data", {}).get("results", [])
    if not results or not results[0].get("bars"):
        raise ValueError("no bars in historicals response")
    rows = []
    required = ("begins_at", "open_price", "high_price", "low_price", "close_price", "volume")
    for b in results[0]["bars"]:
        if b.get("interpolated"):
            continue
        missing = [k for k in required if k not in b]
        if missing:
            raise ValueError(f"bar missing required keys: {missing}")
        rows.append({
            "date": pd.to_datetime(b["begins_at"]).date(),
            "open": float(b["open_price"]), "high": float(b["high_price"]),
            "low": float(b["low_price"]), "close": float(b["close_price"]),
            "volume": int(b["volume"]),
        })
    if not rows:
        raise ValueError("no non-interpolated bars")
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    return df.sort_values("date").reset_index(drop=True)
