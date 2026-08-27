# src/autotrader/simulator.py
"""Deterministic execution simulator: next-open fills, per-tier+floor costs, T+1 ledger,
integrated daily-bar stops.

Entry/exit each pay HALF the effective round-trip cost. Signal-based buys/sells fill at the
NEXT trading day's open; resting stops fill SAME-DAY at the stop-fill price. The ledger
enforces T+1 settlement. Position tracking here is minimal — only what's needed to evaluate
stops; full holdings/oversell accounting lives in the backtest engine (Plan 03). Frozen
behind the Task-8 golden fixture before any strategy is built.

Caller contract (enforced with clear ValueErrors, never silent corruption):
- One tranche per symbol: re-buying a held symbol is refused; sell before re-entry.
- place_stop / submit_sell require a currently-held symbol.
- A sell needs a next trading day to settle into, so submit_sell on the last calendar
  date — and evaluate_stops that would trigger on it — raises (Plan 03's loop must stop one
  bar before the calendar end, or extend the calendar by a trailing settlement day).
"""
import datetime as dt
from dataclasses import dataclass
from typing import Optional
from autotrader.calendar_nyse import TradingCalendar
from autotrader.ledger import SettledCashLedger
from autotrader.costs import effective_roundtrip_cost, regulatory_sell_fees
from autotrader.stops import stop_fill_price


@dataclass(frozen=True)
class BuyFill:
    symbol: str; date: dt.date; price: float; shares: float; cost: float


@dataclass(frozen=True)
class SellFill:
    symbol: str; date: dt.date; price: float; shares: float; proceeds: float; cost: float


@dataclass
class _Position:
    symbol: str; shares: float; tier: str; cost_floor: Optional[float]; stop_price: Optional[float] = None


class Simulator:
    def __init__(self, calendar: TradingCalendar, bars: dict, slippage_frac: float = 0.0):
        self.calendar = calendar
        self.bars = bars
        self.slippage_frac = slippage_frac
        self.ledger = SettledCashLedger(calendar=calendar)
        self.positions = {}

    def deposit(self, amount, on): self.ledger.deposit(amount, on)

    def submit_buy(self, symbol, signal_date, dollar_amount, tier,
                   cost_floor=None, stress=1.0) -> BuyFill:
        if symbol in self.positions:
            raise ValueError(f"position already held for {symbol}; sell before re-entry "
                             "(this minimal model holds one tranche per symbol)")
        fill_date = self.calendar.next_trading_day(signal_date)
        price = self.bars[symbol][fill_date]["open"]
        if not self.ledger.can_buy(dollar_amount, fill_date):
            raise ValueError(f"buy refused: insufficient settled cash on {fill_date}")
        cost = dollar_amount * effective_roundtrip_cost(tier, cost_floor, stress) / 2
        shares = (dollar_amount - cost) / price
        self.ledger.execute_buy(dollar_amount, on=fill_date)
        self.positions[symbol] = _Position(symbol, shares, tier, cost_floor)
        return BuyFill(symbol, fill_date, price, shares, cost)

    def place_stop(self, symbol, stop_price):
        if symbol not in self.positions:
            raise ValueError(f"no position held for {symbol}; cannot place a stop")
        self.positions[symbol].stop_price = stop_price

    def _book_sell(self, pos, trade_date, price, stress=1.0) -> SellFill:
        gross = pos.shares * price
        spread_cost = gross * effective_roundtrip_cost(pos.tier, pos.cost_floor, stress) / 2
        cost = spread_cost + regulatory_sell_fees(proceeds=gross, shares=pos.shares)
        proceeds = gross - cost
        self.ledger.record_sale(proceeds, trade_date=trade_date)
        return SellFill(pos.symbol, trade_date, price, pos.shares, proceeds, cost)

    def submit_sell(self, symbol, signal_date, stress=1.0) -> SellFill:
        if symbol not in self.positions:
            raise ValueError(f"no position held for {symbol}; nothing to sell")
        fill_date = self.calendar.next_trading_day(signal_date)
        if fill_date not in self.bars.get(symbol, {}):
            raise ValueError(f"no bar for {symbol} on fill date {fill_date}; cannot price the sell")
        pos = self.positions.pop(symbol)  # pop only AFTER validation, to avoid half-committed state
        return self._book_sell(pos, fill_date, self.bars[symbol][fill_date]["open"], stress)

    def evaluate_stops(self, date, stress=1.0):
        fills = []
        for symbol in list(self.positions):
            pos = self.positions[symbol]
            if pos.stop_price is None:
                continue
            bar = self.bars[symbol].get(date)
            if bar is None:
                continue
            fp = stop_fill_price(bar, pos.stop_price, self.slippage_frac)
            if fp is None:
                continue
            fills.append(self._book_sell(pos, date, fp, stress))
            del self.positions[symbol]
        return fills
