#!/usr/bin/env python3
# SUPERSEDED — LIVE-01 supervised review-monitor. NOT an autonomous task; dead under
# account-lockdown (agentic off on 123456789). Quarantined to scripts/legacy/ so the
# fail-closed no-place scan (scripts/*.py) excludes it. Kept for reference; do not schedule.
"""LIVE-01 paper-monitor driver — central normalize + plan/record (review-only).

The AGENT fetches raw MCP responses into data/live/raw/ (see RUNBOOK_LIVE_PAPER_RUN.md);
this script normalizes them CENTRALLY (never a subagent) and runs the pure pipeline.

Two modes:
  plan    : raw -> normalize -> StaticMarketData -> plan_day -> write plan.json + summary.
            (Then the agent reviews each order_intent via review_equity_order and writes
             data/live/reviews.json = {ref_id: raw_review_response}.)
  record  : re-plan deterministically + PaperBroker(responder from reviews.json) -> run_day
            -> atomic state record + telemetry. Prints the record summary.

Usage:  python scripts/run_paper_monitor_live.py plan
        python scripts/run_paper_monitor_live.py record <run_timestamp_iso>
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # scripts/legacy/ -> repo root
sys.path.insert(0, str(REPO / "src"))

from autotrader.calendar_nyse import TradingCalendar
from autotrader_live import mcp_live, paper_monitor
from autotrader_live.broker import PaperBroker

RAW = REPO / "data" / "live" / "raw"
LIVE = REPO / "data" / "live"
STATE_DIR = LIVE / "state"
TELEMETRY = LIVE / "telemetry.jsonl"
PLAN_OUT = LIVE / "plan.json"
REVIEWS_IN = LIVE / "reviews.json"

ACCOUNT = "123456789"
EQUITY = 1000.0
TODAY = dt.date(2026, 6, 23)          # the run date (last settled session = 2026-06-22)
SYMBOLS = ["TSM", "MU", "AMD", "JPM", "ASML", "INTC", "LRCX", "AMAT", "CAT", "ARM",
           "BAC", "ABBV", "GE", "UNH", "MS", "SNDK", "GEV", "TXN", "DELL", "MRVL"]


def _load(name: str) -> dict:
    return json.loads((RAW / name).read_text())


def build_market_data() -> tuple[mcp_live.StaticMarketData, dt.date]:
    """Normalize all raw files CENTRALLY into a StaticMarketData + resolve signal_date."""
    # Calendar from SPY (drops interpolated; last settled bar is the calendar end)
    spy = mcp_live.normalize_bars(_load("spy_hist.json"))
    calendar = TradingCalendar(spy["date"].tolist())
    signal_date = paper_monitor.resolve_signal_date(calendar, TODAY)

    # Per-symbol historicals from the two batch files (normalize each symbol's result)
    historicals: dict = {}
    for batch in ("hist_A.json", "hist_B.json"):
        raw = _load(batch)
        for result in raw["data"]["results"]:
            sym = result["symbol"]
            historicals[sym] = mcp_live.normalize_bars({"data": {"results": [result]}})

    # Scan rows, filtered to the enriched candidate set
    scan_rows = [r for r in mcp_live.normalize_scan(_load("scan.json")) if r.symbol in SYMBOLS]

    quotes = mcp_live.normalize_quotes(_load("quotes.json"))
    tradability = {**mcp_live.normalize_tradability(_load("tradability_A.json")),
                   **mcp_live.normalize_tradability(_load("tradability_B.json"))}
    fundamentals = {**mcp_live.normalize_fundamentals(_load("fundamentals_A.json")),
                    **mcp_live.normalize_fundamentals(_load("fundamentals_B.json"))}
    earnings = mcp_live.normalize_earnings(_load("earnings.json"))

    md = mcp_live.StaticMarketData(
        scan_rows=scan_rows, historicals=historicals, quotes=quotes,
        tradability=tradability, fundamentals=fundamentals, earnings=earnings,
    )
    return md, signal_date


def _intent_dict(it) -> dict:
    d = dataclasses.asdict(it)
    d["signal_date"] = it.signal_date.isoformat()
    d["ref_id"] = it.ref_id
    return d


def cmd_plan() -> None:
    md, signal_date = build_market_data()
    plan = paper_monitor.plan_day(
        md, positions={}, signal_date=signal_date,
        account_number=ACCOUNT, equity=EQUITY,
    )
    out = {
        "signal_date": signal_date.isoformat(),
        "account_number": ACCOUNT,
        "equity": EQUITY,
        "reconciled": plan.reconciled,
        "selected": [
            {"symbol": c.symbol, "entry": c.decision.entry,
             "close": c.decision.close, "atr14": c.decision.atr14,
             "mom_252": c.decision.mom_252, "nearness": c.decision.nearness,
             "near_high": c.decision.near_high, "breakout_55": c.decision.breakout_55,
             "cost_tier": c.cost_tier.name, "cost_bps": c.cost_tier.roundtrip_bps,
             "earnings_blackout": c.earnings_blackout}
            for c in plan.selected
        ],
        "order_intents": [_intent_dict(it) for it in plan.order_intents],
        "skipped": plan.skipped,
        "held_halted": plan.held_halted,
        "earnings_flags": plan.earnings_flags,
        "notes": plan.notes,
    }
    PLAN_OUT.write_text(json.dumps(out, indent=2, sort_keys=True) + "\n")

    print(f"signal_date={signal_date}  reconciled={plan.reconciled}  equity=${EQUITY:.0f}")
    print(f"selected={len(plan.selected)}  order_intents={len(plan.order_intents)}  "
          f"skipped={len(plan.skipped)}  held_halted={len(plan.held_halted)}")
    print("\nSELECTED (entry-eligible, ranked by mom_252):")
    for c in plan.selected:
        flag = " [EARNINGS-BLACKOUT]" if c.earnings_blackout else ""
        print(f"  {c.symbol:5s} close={c.decision.close:9.2f} atr14={c.decision.atr14:7.2f} "
              f"mom252={c.decision.mom_252:+.3f} near={c.decision.nearness:.3f} "
              f"brk55={c.decision.breakout_55} tier={c.cost_tier.name}{flag}")
    print("\nWOULD-BE ORDER INTENTS:")
    for it in plan.order_intents:
        sz = it.dollar_amount and f"${it.dollar_amount}" or f"{it.quantity} sh"
        stop = f" stop={it.stop_price}" if it.stop_price else ""
        print(f"  {it.intent_type:16s} {it.symbol:5s} {it.side:4s} {it.order_type:11s} "
              f"{sz:>10s}{stop} tif={it.time_in_force}  ref={it.ref_id[:12]}")
    if plan.skipped:
        print("\nSKIPPED:")
        for sym, reason in plan.skipped:
            print(f"  {sym}: {reason}")
    print(f"\nwrote {PLAN_OUT}")


def cmd_record(run_timestamp: str) -> None:
    md, signal_date = build_market_data()
    reviews_by_ref = json.loads(REVIEWS_IN.read_text())

    def responder(intent):
        return reviews_by_ref[intent.ref_id]

    broker = PaperBroker(responder=responder)
    record = paper_monitor.run_day(
        md, broker, positions={}, signal_date=signal_date,
        account_number=ACCOUNT, equity=EQUITY,
        state_dir=str(STATE_DIR), telemetry_path=str(TELEMETRY),
        run_timestamp=run_timestamp,
    )
    print(f"status={record.status}  signal_date={record.signal_date}  "
          f"data_source={record.data_source}")
    print(f"selected={len(record.selected)}  order_intents={len(record.order_intents)}  "
          f"reviews={len(record.reviews)}  reconciled={record.reconciled}")
    print(f"state -> {STATE_DIR}/{record.signal_date}.json")
    print(f"telemetry -> {TELEMETRY}")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "plan"
    if mode == "plan":
        cmd_plan()
    elif mode == "record":
        cmd_record(sys.argv[2])
    else:
        raise SystemExit(f"unknown mode {mode!r}; use 'plan' or 'record <iso_ts>'")
