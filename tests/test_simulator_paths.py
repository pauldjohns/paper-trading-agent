# tests/test_simulator_paths.py
"""Hardening golden: freezes the integrated-simulator exit paths the Task-8 golden did NOT
cover (review finding) — intrabar-pierce stop fill, signal-based submit_sell at next open,
and stress>1 cost scaling on both the buy and the stop exit. Values are hand-verified and
independently re-derived from the COST_MODEL formulas before freezing (see commit message)."""
import datetime as dt, json
from pathlib import Path
from autotrader.calendar_nyse import TradingCalendar
from autotrader.simulator import Simulator
from autotrader import config

_FIX = Path(__file__).resolve().parent / "fixtures"

DAYS = [dt.date(2026, 1, 2), dt.date(2026, 1, 5), dt.date(2026, 1, 6),
        dt.date(2026, 1, 7), dt.date(2026, 1, 8), dt.date(2026, 1, 9)]


def _bars(bar_16):
    return {"XLK": {
        dt.date(2026, 1, 5): {"open": 100.0, "high": 102.0, "low": 99.0, "close": 101.0},
        dt.date(2026, 1, 6): bar_16,
        dt.date(2026, 1, 7): {"open": 102.0, "high": 103.0, "low": 101.0, "close": 102.5},
        dt.date(2026, 1, 8): {"open": 90.0, "high": 91.0, "low": 88.0, "close": 89.0},
    }}


def intrabar_pierce_result():
    # 1/6 opens above the 95 stop but its low (94) pierces it intrabar -> fill stop*(1-slip).
    bars = _bars({"open": 100.0, "high": 101.0, "low": 94.0, "close": 98.0})
    sim = Simulator(calendar=TradingCalendar(DAYS), bars=bars, slippage_frac=0.001)
    sim.deposit(1000.0, on=dt.date(2026, 1, 2))
    sim.submit_buy("XLK", signal_date=dt.date(2026, 1, 2), dollar_amount=1000.0, tier=config.TIER_SECTOR_SPDR)
    sim.place_stop("XLK", stop_price=95.0)
    ex = sim.evaluate_stops(dt.date(2026, 1, 6))[0]
    return {"price": round(ex.price, 6), "shares": round(ex.shares, 6),
            "cost": round(ex.cost, 6), "proceeds": round(ex.proceeds, 6),
            "trade_date": str(ex.date),
            "settled_same_day": round(sim.ledger.settled_cash(dt.date(2026, 1, 6)), 6),
            "settled_next_day": round(sim.ledger.settled_cash(dt.date(2026, 1, 7)), 6)}


def signal_sell_result():
    # submit_sell on a 1/6 signal fills at the 1/7 open (102), settles 1/8.
    bars = _bars({"open": 101.0, "high": 103.0, "low": 100.0, "close": 102.0})
    sim = Simulator(calendar=TradingCalendar(DAYS), bars=bars, slippage_frac=0.0)
    sim.deposit(1000.0, on=dt.date(2026, 1, 2))
    sim.submit_buy("XLK", signal_date=dt.date(2026, 1, 2), dollar_amount=1000.0, tier=config.TIER_SECTOR_SPDR)
    ex = sim.submit_sell("XLK", signal_date=dt.date(2026, 1, 6))
    return {"price": round(ex.price, 6), "shares": round(ex.shares, 6),
            "cost": round(ex.cost, 6), "proceeds": round(ex.proceeds, 6),
            "trade_date": str(ex.date),
            "settled_same_day": round(sim.ledger.settled_cash(dt.date(2026, 1, 7)), 6),
            "settled_next_day": round(sim.ledger.settled_cash(dt.date(2026, 1, 8)), 6)}


def stress_result():
    # stress=2.0 doubles the round-trip on both legs: buy cost 0.75 -> 1.5; stressed gap exit.
    bars = _bars({"open": 101.0, "high": 103.0, "low": 100.0, "close": 102.0})
    sim = Simulator(calendar=TradingCalendar(DAYS), bars=bars, slippage_frac=0.0)
    sim.deposit(1000.0, on=dt.date(2026, 1, 2))
    buy = sim.submit_buy("XLK", signal_date=dt.date(2026, 1, 2), dollar_amount=1000.0,
                         tier=config.TIER_SECTOR_SPDR, stress=2.0)
    sim.place_stop("XLK", stop_price=95.0)
    ex = sim.evaluate_stops(dt.date(2026, 1, 8), stress=2.0)[0]
    return {"buy_cost": round(buy.cost, 6), "buy_shares": round(buy.shares, 6),
            "exit_price": round(ex.price, 6), "exit_cost": round(ex.cost, 6),
            "exit_proceeds": round(ex.proceeds, 6), "trade_date": str(ex.date)}


with open(_FIX / "golden_simulator_extra_paths.json") as f:
    GOLDEN = json.load(f)


def test_intrabar_pierce_path_matches_golden():
    assert intrabar_pierce_result() == GOLDEN["intrabar_pierce"]


def test_signal_sell_path_matches_golden():
    assert signal_sell_result() == GOLDEN["signal_sell"]


def test_stress_scaling_path_matches_golden():
    assert stress_result() == GOLDEN["stress_buy_stop"]
