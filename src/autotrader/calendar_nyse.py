# src/autotrader/calendar_nyse.py
"""Trading calendar derived from the dates present in cached SPY bars."""
import bisect
import datetime as dt


class TradingCalendar:
    def __init__(self, trading_days):
        self._days = sorted(set(trading_days))
        self._set = set(self._days)

    @classmethod
    def from_datastore(cls, store, symbol="SPY", interval="day", adjustment="split"):
        return cls(store.load(symbol, interval, adjustment)["date"].tolist())

    def is_trading_day(self, d: dt.date) -> bool:
        return d in self._set

    def next_trading_day(self, d: dt.date) -> dt.date:
        i = bisect.bisect_right(self._days, d)
        if i >= len(self._days):
            raise ValueError(f"no trading day after {d} within calendar range")
        return self._days[i]

    def add_trading_days(self, d: dt.date, n: int) -> dt.date:
        """Advance n trading days from d (each hop is next_trading_day, so d itself need not
        be a trading day). NOTE: n=0 returns d UNCHANGED — it does NOT snap to a trading day;
        pass n>=1 if you need to land on one. Raises if it would run past the calendar end."""
        cur = d
        for _ in range(n):
            cur = self.next_trading_day(cur)
        return cur
