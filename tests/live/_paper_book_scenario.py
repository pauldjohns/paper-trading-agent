# tests/live/_paper_book_scenario.py
"""Deterministic, MCP-free arm + multi-poll replay used by the golden lock (P3.5).

`replay(state_dir)` runs a FIXED scripted sequence (no wall-clock, no randomness,
no network) against the pure `paper_loop` orchestrator and leaves `book.json`,
`fills.jsonl`, and `equity_curve.jsonl` in `state_dir`. The frozen golden under
`fixtures/golden_paper_book/` is regenerated from this exact scenario by
`scripts/regen_paper_book_golden.py`.

It exercises every behavior the golden tests assert:
  (a) a triggered ENTRY (poll-1 last > entry_ref),
  (b) a RATCHET that RAISES the stop on a new high above entry (poll-2),
  (c) a WINNING EXIT on a ratchet-stop touch — bid pulls back to the raised stop,
      booking a positive realized_pnl_delta with a non-zero ratchet_seq (poll-4),
  (d) a MISSING-QUOTE poll so n_marked_at_cost>0 in the equity curve (poll-3).

Scenario math (single name AMD; ratified knobs: slippage_bps=3, m=2.0, k=3.0):
  Strictly-rising daily bars => breakout qualifier:
    entry_ref         = 159.2  (prior 55d high, "breakout" basis)
    atr_at_arm        = 0.7
    target_notional   = 300.0  (per_name_cap 0.15 * 2000 binds)
  Poll 1 (entry):  ask=160.00, last=160.50 (>159.2 => trigger)
    fill_price  = 160.00 * (1 + 3/1e4)        = 160.048
    shares      = 300.0 / 160.048             ~= 1.8744...
    init stop   = 160.048 - 2.0*0.7           = 158.648
    same-poll ratchet: hh=max(160.048,160.50)=160.50; level=160.50-2.1=158.40
      => stop stays 158.648 (no raise), ratchet_seq=0
  Poll 2 (ratchet raises): last=165.00, bid=164.90 (>stop, no fill)
    hh=165.00; level=165.00-3.0*0.7=162.90 > 158.648 => stop=162.90, ratchet_seq=1
    (162.90 > entry 160.048 => a later stop-touch is a WINNER)
  Poll 3 (data outage): quotes={} => position marked at cost, n_marked_at_cost=1,
    stop unchanged at 162.90
  Poll 4 (winning stop-out): bid=162.50 (<=162.90 => stop fires)
    fill_price = 162.50 * (1 - 3/1e4)         = 162.45125
    realized   = (162.45125 - 160.048) * shares > 0  (winner)
    fill_id    = "AMD:stop:2026-06-23:1"  (entry-date namespaced, ratchet_seq=1)
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd

from autotrader_live import paper_loop
from autotrader_live.mcp_live import (Fundamentals, Quote, ScanRow,
                                      StaticMarketData, Tradability)
from autotrader_live.paper_book import PaperBook

SIGNAL_DATE = dt.date(2026, 6, 22)
TODAY = dt.date(2026, 6, 23)
START_CAPITAL = 2000.0
SLIPPAGE_BPS = 3.0


def _bars_ending(signal_date: dt.date, n: int = 300, start: float = 10.0,
                 step: float = 0.5) -> pd.DataFrame:
    """Strictly-rising daily OHLCV whose LAST bar is dated EXACTLY signal_date
    (so completed_bar_guard passes). Same shape as the unit-test fixture."""
    rows = []
    px = start
    for i in range(n):
        d = signal_date - dt.timedelta(days=(n - 1 - i))
        rows.append((d, px, px + 0.2, px - 0.2, px, 1_000_000))
        px += step
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])


def _market_data() -> StaticMarketData:
    bars = _bars_ending(SIGNAL_DATE)
    last = float(bars["close"].iloc[-1])
    scan = [ScanRow("AMD", "id", "EQUITY", "AMD", last, last, 1e11, 9e6, 1.2, 65.0, 1.0)]
    quotes = {"AMD": Quote("AMD", last, SIGNAL_DATE, False, "x", last - 0.05, last + 0.05,
                           last, last, True, "active")}
    trad = {"AMD": Tradability("AMD", True, "active", True, False)}
    fund = {"AMD": Fundamentals("AMD", 1e11, 9e6, 9e6, last * 1.1, "Tech", "Semis")}
    return StaticMarketData(scan_rows=scan, historicals={"AMD": bars}, quotes=quotes,
                            tradability=trad, fundamentals=fund, earnings={})


def _q(bid: float, ask: float, last: float) -> dict[str, Quote]:
    # previous_close fixed at 159.5 (the settled close) so the <=50% move gate passes.
    return {"AMD": Quote("AMD", 159.5, SIGNAL_DATE, False, "x", bid, ask, last, 159.5, True, "active")}


# FIXED scripted polls: (ts, quotes-dict). ts is hand-written, never wall-clock.
_POLLS: list[tuple[str, dict[str, Quote]]] = [
    ("2026-06-23T14:00:00Z", _q(159.95, 160.00, 160.50)),  # entry trigger
    ("2026-06-23T15:00:00Z", _q(164.90, 165.10, 165.00)),  # ratchet raises stop to 162.90
    ("2026-06-23T16:00:00Z", {}),                          # data outage -> marked at cost
    ("2026-06-23T17:00:00Z", _q(162.50, 162.70, 162.55)),  # winning stop-out (bid<=162.90)
]


def replay(state_dir: str | Path) -> None:
    """Run the scripted scenario into `state_dir` from a CLEAN slate.

    MUST clear the append-only logs first: equity_curve.jsonl is never rewritten,
    so a re-run in a dirty dir would double-append and false-pass the golden.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    for name in ("book.json", "fills.jsonl", "equity_curve.jsonl"):
        (state_dir / name).unlink(missing_ok=True)

    md = _market_data()
    book = PaperBook.new(start_capital=START_CAPITAL, start_ts="2026-06-23T13:30:00Z",
                         token_issue_ts="2026-06-23T13:30:00Z")
    paper_loop.arm_day(book, md, signal_date=SIGNAL_DATE, today=TODAY)
    for ts, quotes in _POLLS:
        paper_loop.advance_poll(book, quotes, md, ts=ts, state_dir=state_dir,
                                slippage_bps=SLIPPAGE_BPS)
