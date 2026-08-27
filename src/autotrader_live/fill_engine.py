# src/autotrader_live/fill_engine.py
"""Pure virtual-fill logic for LIVE-02. NO MCP calls; NO order placement.

Fills cross the real observed spread on both sides (buy@ask, sell@bid) plus a
slippage_bps adverse-drift haircut. The cost_tier estimate is comparison-only
metadata (never deducted). All functions are pure given (book/position/quote).
"""
from __future__ import annotations

import dataclasses

from autotrader_live import exits
from autotrader_live.mcp_live import Quote
from autotrader_live.paper_book import ArmedEntry, Fill, PaperPosition
from autotrader_live.strategy_trend import TrendDecision

# Observed live quote states that count as a tradeable regular session.
# VERIFY the exact live string(s) in the P0 spike and extend if needed.
_ALLOWED_STATES = {"active"}
MIN_NOTIONAL: float = 50.0
_MAX_MOVE_FRAC: float = 0.50  # reject a last more than 50% from previous_close


def quote_is_fillable(quote: Quote) -> bool:
    if quote.last_trade_price <= 0:
        return False
    if quote.bid is None or quote.ask is None:
        return False
    if quote.bid > quote.ask:
        return False
    if not quote.has_traded:
        return False
    if quote.state not in _ALLOWED_STATES:
        return False
    if quote.previous_close > 0 and abs(quote.last_trade_price / quote.previous_close - 1.0) > _MAX_MOVE_FRAC:
        return False
    return True


def entry_reference(decision: TrendDecision) -> tuple[float, str]:
    """Intraday entry reference level + its basis.

    Breakout-qualifiers must clear the prior 55-day HIGH intraday (a real
    breakout confirmation); near-high-qualifiers need only clear the prior
    settled close. `prior_donch_upper` and `close` are frozen on the decision —
    never recomputed here.
    """
    if decision.breakout_55:
        return (decision.prior_donch_upper, "breakout")
    return (decision.close, "near_high")


def should_trigger_entry(last_trade_price: float, entry_ref: float) -> bool:
    return last_trade_price > entry_ref  # strict, matches decide()'s close_t > prior_donch_upper


def entry_fill(quote: Quote, armed: ArmedEntry, available_cash: float, ts: str,
               *, slippage_bps: float = 3.0) -> Fill | None:
    """Price a virtual BUY for an armed name. Caller has already confirmed the
    quote is fillable and the trigger fired. Returns None on a dust/affordability
    skip."""
    base = quote.ask if quote.ask is not None else quote.last_trade_price
    fill_price = base * (1.0 + slippage_bps / 1e4)
    notional = min(armed.target_notional, available_cash)
    if notional < MIN_NOTIONAL or available_cash < MIN_NOTIONAL:
        return None
    shares = notional / fill_price
    return Fill(
        fill_id=f"{armed.symbol}:entry:{armed.arm_date}", ts=ts, symbol=armed.symbol,
        side="buy", intent_type="entry", price=fill_price, shares=shares,
        notional=fill_price * shares, entry_ref=armed.entry_ref, ref_basis=armed.ref_basis,
        bid=quote.bid, ask=quote.ask, last_trade_price=quote.last_trade_price,
        previous_close=quote.previous_close, spread=quote.spread,
        cost_tier_bps=armed.cost_tier_bps, realized_pnl_delta=0.0)


def stop_fill(position: PaperPosition, quote: Quote, ts: str,
              *, slippage_bps: float = 3.0) -> Fill | None:
    """Price a virtual SELL when the NBBO bid is at/below the stop.

    Trigger uses the BID (not last_trade) to avoid a stale-print wick. Fill at the
    bid minus slippage, so a gentle touch crosses the sell-side spread (fills below
    the stop) and a gap-through fills at the low bid. Returns None if bid is missing
    (never force-sell on missing data) or the stop is not breached."""
    if quote.bid is None or quote.bid > position.current_stop:
        return None
    fill_price = quote.bid * (1.0 - slippage_bps / 1e4)
    shares = position.shares
    realized = (fill_price - position.entry_price) * shares
    return Fill(
        # DATE-NAMESPACED by the entry date (entry_ts[:10]) so a name that opens and
        # stops out flat (ratchet_seq=0) on two DIFFERENT days yields distinct fill_ids
        # — otherwise the second exit dedups against the first in applied_fill_ids and
        # the stop-out is silently dropped (cash never credited, position never closed).
        fill_id=f"{position.symbol}:stop:{position.entry_ts[:10]}:{position.ratchet_seq}", ts=ts,
        symbol=position.symbol, side="sell", intent_type="stop", price=fill_price,
        shares=shares, notional=fill_price * shares, entry_ref=None, ref_basis=None,
        bid=quote.bid, ask=quote.ask, last_trade_price=quote.last_trade_price,
        previous_close=quote.previous_close, spread=quote.spread,
        cost_tier_bps=position.cost_tier_bps, realized_pnl_delta=realized)


def ratchet(position: PaperPosition, last_trade_price: float, *, k: float = 3.0) -> PaperPosition:
    """Monotonic-up chandelier ratchet on the highest SAMPLED poll price.

    `highest_high_since_entry` updates to include this poll's last_trade_price; the
    new stop = max(current_stop, hh - k*atr_at_entry). ratchet_seq increments only
    when the stop actually rises (so each stop_fill fill_id is unique)."""
    hh = max(position.highest_high_since_entry, last_trade_price)
    new_stop = exits.update_trailing_stop(position.current_stop, hh, position.atr_at_entry, k=k)
    seq = position.ratchet_seq + (1 if new_stop > position.current_stop else 0)
    return dataclasses.replace(position, highest_high_since_entry=hh,
                               current_stop=new_stop, ratchet_seq=seq)
