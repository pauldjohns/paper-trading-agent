#!/usr/bin/env python3
"""LIVE-02/03 forward paper-book driver — central normalize + arm/poll/status/decide.

The AGENT fetches raw MCP responses into data/live/paper_book/raw/ (see
RUNBOOK_PAPER_BOOK.md); this script normalizes them CENTRALLY (never a subagent)
and drives the pure `autotrader_live` paper-book package. It makes NO MCP calls
and contains NO order-surface tokens: it only reads pre-saved raw JSON and calls
the pure package.

Timestamps and dates are passed in as argv (never computed in-module). The agent
computes `signal_date` (last settled session) and `today` upstream via
TradingCalendar + paper_monitor.resolve_signal_date; this driver does NOT consult
the calendar.

Five modes:
  arm <signal_date_iso> <today_iso>
      raw scan/historicals/quotes/tradability/fundamentals/earnings ->
      normalize -> StaticMarketData -> load-or-create PaperBook ->
      paper_loop.arm_day(...) -> save -> print the armed set.
  poll <ts_iso>
      load PaperBook -> read current quotes raw JSON -> normalize ->
      paper_loop.advance_poll(book, quotes, md, ts=ts, state_dir=...) ->
      print the poll summary (fills this poll, open positions, cash, equity).
  poll-decide <now_et_iso>
      read book.json + eod_done_<date> marker -> compute schedule_state.poll_action
      -> print token (NORMAL/FINAL/SELF_HEAL_ARM/EXIT). No fetch, no mutation.
  mark-eod <date_iso>
      write the eod_done_<date> marker file. Called by the POLL agent after FINAL.
  status
      load PaperBook -> print positions/cash/realized P&L WITHOUT advancing or
      any fetch.

Usage:
  python scripts/run_paper_book.py arm 2026-06-22 2026-06-23
  python scripts/run_paper_book.py poll 2026-06-23T14:00:00Z
  python scripts/run_paper_book.py poll-decide 2026-06-23T15:00:00Z
  python scripts/run_paper_book.py mark-eod 2026-06-23
  python scripts/run_paper_book.py status
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from autotrader_live import mcp_live, paper_loop, schedule_state
from autotrader_live.paper_book import PaperBook

ACCOUNT = "987654321"   # NON-order-capable account: the loop is read-only and a
                        # place_* against this account is broker-rejected. The label
                        # therefore matches reality (paper-only, orders impossible).
START_CAPITAL = 2000.0
STATE_DIR = REPO / "data" / "live" / "paper_book"
RAW = STATE_DIR / "raw"


def _load(name: str) -> dict:
    """Read one agent-saved raw MCP JSON file from RAW."""
    return json.loads((RAW / name).read_text())


def build_market_data() -> mcp_live.StaticMarketData:
    """Normalize all raw files CENTRALLY into a StaticMarketData (no calendar).

    `signal_date`/`today` are computed UPSTREAM by the agent and passed as argv,
    so the driver never reads SPY history or builds a TradingCalendar here.
    Per-symbol historicals are read from `hist_<SYM>.json` files (one batch
    file per symbol, mirroring `get_equity_historicals` responses).
    """
    scan_rows = mcp_live.normalize_scan(_load("scan.json"))

    # Per-symbol historicals: one normalized DataFrame per hist_<SYM>.json file.
    historicals: dict = {}
    for path in sorted(RAW.glob("hist_*.json")):
        sym = path.stem[len("hist_"):]
        historicals[sym] = mcp_live.normalize_bars(json.loads(path.read_text()))

    quotes = mcp_live.normalize_quotes(_load("quotes.json"))
    tradability = mcp_live.normalize_tradability(_load("tradability.json"))
    fundamentals = mcp_live.normalize_fundamentals(_load("fundamentals.json"))
    earnings = mcp_live.normalize_earnings(_load("earnings.json"))

    return mcp_live.StaticMarketData(
        scan_rows=scan_rows, historicals=historicals, quotes=quotes,
        tradability=tradability, fundamentals=fundamentals, earnings=earnings,
    )


def _load_or_create_book() -> PaperBook:
    book = PaperBook.load(STATE_DIR)
    if book is not None:
        return book
    return PaperBook.new(
        start_capital=START_CAPITAL,
        start_ts=dt.datetime.now(dt.timezone.utc).isoformat(),
        token_issue_ts=None,
    )


def cmd_arm(signal_date_iso: str, today_iso: str) -> None:
    signal_date = dt.date.fromisoformat(signal_date_iso)
    today = dt.date.fromisoformat(today_iso)
    md = build_market_data()
    book = _load_or_create_book()

    paper_loop.arm_day(book, md, signal_date=signal_date, today=today)
    book.save(STATE_DIR)

    print(f"ARM  account={ACCOUNT}  signal_date={signal_date}  today={today}")
    print(f"start_capital=${book.start_capital:.2f}  cash=${book.cash:.2f}  "
          f"open_positions={len(book.positions)}  last_arm_date={book.last_arm_date}")
    print(f"\nARMED ({len(book.armed)}):")
    if not book.armed:
        print("  (none — no qualifiers, or all held/guarded)")
    for sym in sorted(book.armed):
        a = book.armed[sym]
        print(f"  {sym:6s} ref={a.entry_ref:10.2f} [{a.ref_basis:9s}] "
              f"target=${a.target_notional:8.2f} atr={a.atr_at_arm:7.3f} "
              f"cost_bps={a.cost_tier_bps:6.1f}")
    if book.positions:
        print(f"\nOPEN POSITIONS ({len(book.positions)}):")
        for sym in sorted(book.positions):
            p = book.positions[sym]
            print(f"  {sym:6s} shares={p.shares:.4f} entry={p.entry_price:.2f} "
                  f"stop={p.current_stop:.2f} seq={p.ratchet_seq}")
    print(f"\nstate -> {STATE_DIR / 'book.json'}")


def cmd_poll(ts_iso: str) -> None:
    book = PaperBook.load(STATE_DIR)
    if book is None:
        raise SystemExit(
            f"no book at {STATE_DIR / 'book.json'}; run `arm` before `poll`")

    quotes = mcp_live.normalize_quotes(_load("quotes.json"))
    # advance_poll consumes only the quotes dict + book; a quotes-only provider
    # satisfies the MarketData seam without re-reading historicals on each poll.
    md = mcp_live.StaticMarketData(
        scan_rows=[], historicals={}, quotes=quotes,
        tradability={}, fundamentals={}, earnings={},
    )

    fills_before = {f.fill_id for f in book.fills}
    paper_loop.advance_poll(book, quotes, md, ts=ts_iso, state_dir=STATE_DIR)
    new_fills = [f for f in book.fills if f.fill_id not in fills_before]

    snap = book.mark_to_market(quotes, ts=ts_iso)
    take_entries = (book.last_arm_date == schedule_state.poll_day_et(ts_iso))

    print(f"POLL account={ACCOUNT}  ts={ts_iso}  entries_taken={take_entries}")
    print(f"fills_this_poll={len(new_fills)}  open_positions={len(book.positions)}  "
          f"armed={len(book.armed)}")
    if new_fills:
        print("\nFILLS THIS POLL:")
        for f in new_fills:
            extra = (f"  realized={f.realized_pnl_delta:+.2f}"
                     if f.intent_type == "stop" else "")
            print(f"  {f.intent_type:5s} {f.side:4s} {f.symbol:6s} "
                  f"price={f.price:.4f} shares={f.shares:.4f} "
                  f"notional=${f.notional:.2f}{extra}")
    if book.positions:
        print("\nOPEN POSITIONS:")
        for sym in sorted(book.positions):
            p = book.positions[sym]
            print(f"  {sym:6s} shares={p.shares:.4f} entry={p.entry_price:.2f} "
                  f"stop={p.current_stop:.2f} seq={p.ratchet_seq}")
    print(f"\ncash=${snap.cash:.2f}  positions_mv=${snap.positions_mv:.2f}  "
          f"total_equity=${snap.total_equity:.2f}")
    print(f"realized_pnl=${snap.realized_pnl_cum:+.2f}  "
          f"unrealized_pnl=${snap.unrealized_pnl:+.2f}  "
          f"n_marked_at_cost={snap.n_marked_at_cost}")
    print(f"\nstate -> {STATE_DIR / 'book.json'}")
    print(f"equity_curve -> {STATE_DIR / 'equity_curve.jsonl'}")


def _eod_marker(date_iso: str) -> Path:
    return STATE_DIR / f"eod_done_{date_iso}"


def cmd_poll_decide(now_et_iso: str) -> None:
    """Print the POLL action token for a headless fire. Reads book.json + the
    eod_done marker; makes NO fetch and NO mutation."""
    now_et = dt.datetime.fromisoformat(now_et_iso.replace("Z", "+00:00")).astimezone(
        schedule_state.ET)
    book = PaperBook.load(STATE_DIR)
    last_arm_date = book.last_arm_date if book else None
    last_poll_ts = book.last_poll_ts if book else None
    today = now_et.date()
    eod_done = _eod_marker(today.isoformat()).exists()
    action = schedule_state.poll_action(
        now_et, last_arm_date=last_arm_date, last_poll_ts=last_poll_ts,
        eod_done=eod_done, today=today)
    print(action.value)


def cmd_mark_eod(date_iso: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _eod_marker(date_iso).write_text(f"eod_done {date_iso}\n")
    print(f"eod_done -> {_eod_marker(date_iso)}")


def cmd_status() -> None:
    book = PaperBook.load(STATE_DIR)
    if book is None:
        print(f"no book at {STATE_DIR / 'book.json'} (not yet armed)")
        return

    print(f"STATUS account={ACCOUNT}  data_source={book.data_source}")
    print(f"start_capital=${book.start_capital:.2f}  cash=${book.cash:.2f}  "
          f"realized_pnl=${book.realized_pnl:+.2f}")
    print(f"last_arm_date={book.last_arm_date}  last_poll_ts={book.last_poll_ts}  "
          f"token_issue_ts={book.token_issue_ts}")
    print(f"\nOPEN POSITIONS ({len(book.positions)}):")
    if not book.positions:
        print("  (flat)")
    for sym in sorted(book.positions):
        p = book.positions[sym]
        print(f"  {sym:6s} shares={p.shares:.4f} entry={p.entry_price:.2f} "
              f"stop={p.current_stop:.2f} hh={p.highest_high_since_entry:.2f} "
              f"seq={p.ratchet_seq}")
    print(f"\nARMED ({len(book.armed)}):")
    if not book.armed:
        print("  (none)")
    for sym in sorted(book.armed):
        a = book.armed[sym]
        print(f"  {sym:6s} ref={a.entry_ref:.2f} [{a.ref_basis}] "
              f"target=${a.target_notional:.2f}")
    print(f"\nfills_total={len(book.fills)}  filled_today={sorted(book.filled_today)}")


def main(argv: list[str]) -> None:
    mode = argv[1] if len(argv) > 1 else "status"
    if mode == "arm":
        if len(argv) != 4:
            raise SystemExit(
                "usage: run_paper_book.py arm <signal_date_iso> <today_iso>")
        cmd_arm(argv[2], argv[3])
    elif mode == "poll":
        if len(argv) != 3:
            raise SystemExit("usage: run_paper_book.py poll <ts_iso>")
        cmd_poll(argv[2])
    elif mode == "poll-decide":
        if len(argv) != 3:
            raise SystemExit("usage: run_paper_book.py poll-decide <now_et_iso>")
        cmd_poll_decide(argv[2])
    elif mode == "mark-eod":
        if len(argv) != 3:
            raise SystemExit("usage: run_paper_book.py mark-eod <date_iso>")
        cmd_mark_eod(argv[2])
    elif mode == "status":
        cmd_status()
    else:
        raise SystemExit(
            f"unknown mode {mode!r}; use 'arm <signal_date> <today>', "
            f"'poll <ts>', 'poll-decide <now_et_iso>', 'mark-eod <date_iso>', "
            f"or 'status'")


if __name__ == "__main__":
    main(sys.argv)
