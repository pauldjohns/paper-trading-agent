# tests/test_simulator_guards.py
"""Position-safety guards added during foundation hardening (review findings):
double-buy overwrite, KeyError-vs-ValueError on unheld symbols, half-committed
submit_sell, and fill immutability. The pristine Task-8 golden lives in test_simulator.py."""
import datetime as dt, dataclasses
import pytest
from autotrader.calendar_nyse import TradingCalendar
from autotrader.simulator import Simulator
from autotrader import config

DAYS = [dt.date(2026, 1, 2), dt.date(2026, 1, 5), dt.date(2026, 1, 6),
        dt.date(2026, 1, 7), dt.date(2026, 1, 8), dt.date(2026, 1, 9)]
BARS = {"XLK": {
    dt.date(2026, 1, 2): {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0},
    dt.date(2026, 1, 5): {"open": 100.0, "high": 102.0, "low": 100.0, "close": 101.0},
    dt.date(2026, 1, 6): {"open": 101.0, "high": 103.0, "low": 100.0, "close": 102.0},
    dt.date(2026, 1, 7): {"open": 102.0, "high": 103.0, "low": 101.0, "close": 102.5},
    dt.date(2026, 1, 8): {"open": 90.0, "high": 91.0, "low": 88.0, "close": 89.0},
    dt.date(2026, 1, 9): {"open": 89.0, "high": 90.0, "low": 88.0, "close": 89.0},
}}


def _sim():
    s = Simulator(calendar=TradingCalendar(DAYS), bars=BARS, slippage_frac=0.0)
    s.deposit(2000.0, on=dt.date(2026, 1, 2))
    return s


def test_double_buy_same_symbol_raises():
    # Re-buying a held symbol must be refused, not silently overwrite the position
    # (which would burn ledger cash while dropping the prior shares).
    sim = _sim()
    sim.submit_buy("XLK", signal_date=dt.date(2026, 1, 2), dollar_amount=500.0, tier=config.TIER_SECTOR_SPDR)
    with pytest.raises(ValueError):
        sim.submit_buy("XLK", signal_date=dt.date(2026, 1, 5), dollar_amount=500.0, tier=config.TIER_SECTOR_SPDR)


def test_place_stop_on_unheld_symbol_raises():
    sim = _sim()
    with pytest.raises(ValueError):
        sim.place_stop("XLK", stop_price=95.0)


def test_submit_sell_unheld_symbol_raises():
    sim = _sim()
    with pytest.raises(ValueError):
        sim.submit_sell("XLK", signal_date=dt.date(2026, 1, 2))


def test_submit_sell_missing_bar_preserves_position():
    # If the fill-date bar is absent, submit_sell must raise BEFORE removing the position
    # (no half-committed state where the position is gone but no sale was booked).
    bars = {"XLK": {dt.date(2026, 1, 5): {"open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0}}}
    sim = Simulator(calendar=TradingCalendar(DAYS), bars=bars, slippage_frac=0.0)
    sim.deposit(1000.0, on=dt.date(2026, 1, 2))
    sim.submit_buy("XLK", signal_date=dt.date(2026, 1, 2), dollar_amount=500.0, tier=config.TIER_SECTOR_SPDR)  # fills 1/5
    with pytest.raises(ValueError):
        sim.submit_sell("XLK", signal_date=dt.date(2026, 1, 5))  # fill 1/6 has no bar
    assert "XLK" in sim.positions  # position intact, not half-committed away


def test_fills_are_immutable():
    sim = _sim()
    buy = sim.submit_buy("XLK", signal_date=dt.date(2026, 1, 2), dollar_amount=1000.0, tier=config.TIER_SECTOR_SPDR)
    with pytest.raises(dataclasses.FrozenInstanceError):
        buy.price = 999.0


def test_integrated_buy_refused_when_proceeds_unsettled_gfv():
    # End-to-end GFV proxy through the simulator (not just ledger.can_buy): after a stop-out
    # on 1/8 the proceeds settle 1/9, so a same-day (1/8) redeploy must be REFUSED by submit_buy.
    sim = Simulator(calendar=TradingCalendar(DAYS), bars=BARS, slippage_frac=0.0)
    sim.deposit(1000.0, on=dt.date(2026, 1, 2))
    sim.submit_buy("XLK", signal_date=dt.date(2026, 1, 2), dollar_amount=1000.0, tier=config.TIER_SECTOR_SPDR)  # fills 1/5
    sim.place_stop("XLK", stop_price=95.0)
    sim.evaluate_stops(dt.date(2026, 1, 8))  # gap-through sell on 1/8; proceeds settle 1/9
    with pytest.raises(ValueError):
        # signal 1/7 -> fill 1/8, but 1/8 has no settled cash yet -> integrated refusal
        sim.submit_buy("XLK", signal_date=dt.date(2026, 1, 7), dollar_amount=100.0, tier=config.TIER_SECTOR_SPDR)
