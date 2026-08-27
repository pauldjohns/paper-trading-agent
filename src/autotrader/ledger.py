# src/autotrader/ledger.py
"""Single shared settled-cash ledger with T+1 dated tranches (conservative GFV proxy).

A sale's proceeds become spendable only on the next trading day. A buy may draw ONLY
settled cash; spending unsettled proceeds is refused (conservative proxy for the live
Good-Faith-Violation rule, spec section 3.6). One pool, shared across all sleeves.
"""
import datetime as dt
from dataclasses import dataclass, field


@dataclass
class _Tranche:
    amount: float
    settle_date: dt.date


@dataclass
class SettledCashLedger:
    calendar: object
    _tranches: list = field(default_factory=list)

    def deposit(self, amount: float, on: dt.date) -> None:
        self._tranches.append(_Tranche(amount, on))

    def settled_cash(self, as_of: dt.date) -> float:
        return round(sum(t.amount for t in self._tranches if t.settle_date <= as_of), 10)

    def can_buy(self, amount: float, on: dt.date) -> bool:
        return amount <= self.settled_cash(on) + 1e-9

    def execute_buy(self, amount: float, on: dt.date) -> None:
        if not self.can_buy(amount, on):
            raise ValueError(
                f"insufficient settled cash on {on}: need {amount}, "
                f"have {self.settled_cash(on)} (unsettled proceeds cannot fund a buy)")
        remaining = amount
        for t in sorted(self._tranches, key=lambda x: x.settle_date):
            if t.settle_date > on or remaining <= 0:
                continue
            take = min(t.amount, remaining)
            t.amount -= take
            remaining -= take
        self._tranches = [t for t in self._tranches if t.amount > 1e-12]

    def record_sale(self, proceeds: float, trade_date: dt.date) -> None:
        self._tranches.append(_Tranche(proceeds, self.calendar.next_trading_day(trade_date)))
