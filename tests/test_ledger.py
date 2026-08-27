# tests/test_ledger.py
import datetime as dt, pytest
from autotrader.calendar_nyse import TradingCalendar
from autotrader.ledger import SettledCashLedger

DAYS = [dt.date(2026, 1, 2), dt.date(2026, 1, 5), dt.date(2026, 1, 6), dt.date(2026, 1, 7)]
def make(): return SettledCashLedger(calendar=TradingCalendar(DAYS))

def test_deposit_is_immediately_settled():
    led = make(); led.deposit(1000.0, on=dt.date(2026, 1, 2))
    assert led.settled_cash(dt.date(2026, 1, 2)) == 1000.0

def test_sale_proceeds_settle_next_trading_day():
    led = make(); led.deposit(1000.0, on=dt.date(2026, 1, 2))
    led.execute_buy(1000.0, on=dt.date(2026, 1, 2))
    led.record_sale(1050.0, trade_date=dt.date(2026, 1, 5))
    assert led.settled_cash(dt.date(2026, 1, 5)) == 0.0
    assert led.settled_cash(dt.date(2026, 1, 6)) == 1050.0

def test_buy_with_unsettled_funds_is_refused_gfv_guard():
    led = make(); led.deposit(1000.0, on=dt.date(2026, 1, 2))
    led.execute_buy(1000.0, on=dt.date(2026, 1, 2))
    led.record_sale(1000.0, trade_date=dt.date(2026, 1, 5))
    with pytest.raises(ValueError):
        led.execute_buy(500.0, on=dt.date(2026, 1, 5))
    led.execute_buy(500.0, on=dt.date(2026, 1, 6))
    assert led.settled_cash(dt.date(2026, 1, 6)) == 500.0

def test_can_buy_reflects_settled_only():
    led = make(); led.deposit(300.0, on=dt.date(2026, 1, 2))
    assert led.can_buy(300.0, on=dt.date(2026, 1, 2)) is True
    assert led.can_buy(300.01, on=dt.date(2026, 1, 2)) is False
