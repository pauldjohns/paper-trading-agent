#!/usr/bin/env python3
"""Compute signal_date + today for the LIVE-02 daily ARM from raw SPY history.

The driver (run_paper_book.py) does NOT consult the calendar; per the RUNBOOK
the AGENT computes signal_date upstream and passes it as argv. This helper reads
the agent-saved raw/hist_SPY.json, builds the trading calendar from the SETTLED
SPY bars (equivalent to TradingCalendar.from_datastore, but without touching the
locked cache parquet), and prints signal_date (the last settled session strictly
before `today`) and today. MCP-free.

Usage: compute_signal_date.py [today_iso]   (today defaults to ET today)
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from autotrader.calendar_nyse import TradingCalendar  # noqa: E402
from autotrader_live import mcp_live, paper_monitor    # noqa: E402

ET = ZoneInfo("America/New_York")
RAW = REPO / "data" / "live" / "paper_book" / "raw"


def main(argv: list[str]) -> None:
    today = (dt.date.fromisoformat(argv[1]) if len(argv) > 1
             else dt.datetime.now(ET).date())
    bars = mcp_live.normalize_bars(json.loads((RAW / "hist_SPY.json").read_text()))
    cal = TradingCalendar(bars["date"].tolist())
    signal_date = paper_monitor.resolve_signal_date(cal, today)
    last_bar = bars["date"].iloc[-1]
    print(f"today={today.isoformat()}")
    print(f"signal_date={signal_date.isoformat()}")
    print(f"spy_bars={len(bars)}  first={bars['date'].iloc[0].isoformat()}  "
          f"last={last_bar.isoformat()}")
    # The last settled SPY bar must equal signal_date, else the candidate
    # completed_bar_guard (which requires last_bar == signal_date) would reject
    # every name. Surface it loudly.
    print(f"GUARD_OK={signal_date == last_bar}")


if __name__ == "__main__":
    main(sys.argv)
