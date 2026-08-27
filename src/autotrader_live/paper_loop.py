# src/autotrader_live/paper_loop.py
"""Orchestrator for the LIVE-02 paper book: once-daily arm + per-poll advance.

PURE over a MarketData provider + quotes dict; the agent does all MCP I/O.
Writes book.json (atomic, via PaperBook.save) FIRST, then appends the equity row.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from autotrader_live import exits, fill_engine, paper_monitor, schedule_state
from autotrader_live.mcp_live import MarketData, Quote
from autotrader_live.paper_book import ArmedEntry, PaperBook, PaperPosition
from autotrader_live.paper_monitor import LookAheadError, StaleDataError
from autotrader_live.sizing import size
from autotrader_live.strategy_trend import TrendDecision
from autotrader_live.universe import build_universe

# Ratified knobs (single source).
NEAR_THRESHOLD = 0.90
F = 0.01
K = 3.0
PER_NAME_CAP_FRAC = 0.15
TOP_N = 10
M = 2.0
BLACKOUT_DAYS = 5
SLIPPAGE_BPS = 3.0


def arm_day(book: PaperBook, market_data: MarketData, *,
            signal_date: dt.date, today: dt.date) -> None:
    """Build the day's armed entry set (once per calendar day). Idempotent: a
    second call on the same `today` is a no-op."""
    today_iso = today.isoformat()
    if book.last_arm_date == today_iso:
        return

    uni = build_universe(market_data, signal_date=signal_date,
                         near_threshold=NEAR_THRESHOLD, blackout_days=BLACKOUT_DAYS, top_n=TOP_N)

    equity_basis = book.equity_at_cost()
    armed: dict[str, ArmedEntry] = {}
    for cand in uni.selected:
        sym = cand.symbol
        if sym in book.positions:
            continue
        # LOOK-AHEAD GUARD (spec §1.5/§7): never arm off an unsettled bar. decide()
        # trusts the caller to pass settled bars; enforce it here, mirroring
        # paper_monitor.py's per-name guard. A today-dated (in-progress) bar -> skip.
        try:
            paper_monitor.completed_bar_guard(market_data.historicals(sym), signal_date)
        except (LookAheadError, StaleDataError, KeyError, ValueError):
            continue
        dec: TrendDecision = cand.decision  # non-None for selected
        ref, basis = fill_engine.entry_reference(dec)
        sz = size(equity_basis, dec.atr14, dec.close, f=F, k=K, per_name_cap_frac=PER_NAME_CAP_FRAC)
        armed[sym] = ArmedEntry(symbol=sym, entry_ref=ref, ref_basis=basis,
                                target_notional=sz["notional"], atr_at_arm=dec.atr14,
                                cost_tier_bps=cand.cost_tier.roundtrip_bps, arm_date=today_iso)

    book.armed = armed
    book.filled_today = set()
    book.last_arm_date = today_iso


def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def advance_poll(book: PaperBook, quotes: dict[str, Quote], market_data: MarketData, *,
                 ts: str, state_dir: str | Path, slippage_bps: float = SLIPPAGE_BPS) -> None:
    """One poll: fill triggered entries, fill/ratchet open positions, mark-to-market,
    checkpoint (book.json atomic FIRST), then append the equity row."""
    state_dir = Path(state_dir)

    # ── Arm-ordering guard ─────────────────────────────────────────────────────
    # Only take entries if TODAY's arm ran. If a poll fires before arm_day on a new
    # day (overnight wake / restart), armed[] + filled_today are STALE — manage open
    # positions only. The stale-arm skip is visible in the equity row (entries_taken).
    poll_day = schedule_state.poll_day_et(ts)
    take_entries = (book.last_arm_date == poll_day)

    # ── Entries: armed names with a fillable, triggered quote ──────────────────
    for sym in (sorted(book.armed.keys()) if take_entries else []):
        if sym in book.filled_today or sym in book.positions:
            continue
        q = quotes.get(sym)
        if q is None or not fill_engine.quote_is_fillable(q):
            continue
        armed = book.armed[sym]
        if not fill_engine.should_trigger_entry(q.last_trade_price, armed.entry_ref):
            continue
        fill = fill_engine.entry_fill(q, armed, book.cash, ts, slippage_bps=slippage_bps)
        if fill is None:
            continue
        try:
            stop_px = exits.initial_catastrophe_stop(fill.price, armed.atr_at_arm, m=M)
        except ValueError:
            continue  # ATR too wide for a valid stop — skip this entry
        position = PaperPosition(symbol=sym, shares=fill.shares, entry_price=fill.price,
                                 entry_ts=ts, atr_at_entry=armed.atr_at_arm, current_stop=stop_px,
                                 highest_high_since_entry=fill.price, ratchet_seq=0,
                                 cost_tier_bps=armed.cost_tier_bps)
        book.apply_entry(position, fill)

    # ── Open positions: stop-fill else ratchet ────────────────────────────────
    for sym in sorted(book.positions.keys()):
        q = quotes.get(sym)
        if q is None or not fill_engine.quote_is_fillable(q):
            continue  # never act on missing/bad data; resting stop stands
        pos = book.positions[sym]
        stop = fill_engine.stop_fill(pos, q, ts, slippage_bps=slippage_bps)
        if stop is not None:
            book.apply_exit(stop)
        else:
            book.replace_position(fill_engine.ratchet(pos, q.last_trade_price, k=K))

    # ── Checkpoint (book.json FIRST) + equity row ─────────────────────────────
    book.last_poll_ts = ts
    book.save(state_dir)  # atomic book.json + fills.jsonl reconcile
    snap = book.mark_to_market(quotes, ts=ts)
    _append_jsonl(state_dir / "equity_curve.jsonl", {
        "ts": snap.ts, "cash": round(snap.cash, 10), "positions_mv": round(snap.positions_mv, 10),
        "total_equity": round(snap.total_equity, 10), "n_positions": snap.n_positions,
        "realized_pnl_cum": round(snap.realized_pnl_cum, 10),
        "unrealized_pnl": round(snap.unrealized_pnl, 10),
        "n_marked_at_cost": snap.n_marked_at_cost,   # >0 => provisional equity (data outage)
        "entries_taken": take_entries})              # False => poll ran before today's arm (stale skip)
