# tests/test_simulator.py
import datetime as dt, json
from pathlib import Path
from autotrader.calendar_nyse import TradingCalendar
from autotrader.simulator import Simulator
from autotrader import config

_FIX = Path(__file__).resolve().parent / "fixtures"

DAYS = [dt.date(2026,1,2), dt.date(2026,1,5), dt.date(2026,1,6),
        dt.date(2026,1,7), dt.date(2026,1,8), dt.date(2026,1,9)]
BARS = {"XLK": {
    dt.date(2026,1,2): {"open":100.0,"high":101.0,"low":99.0,"close":100.0},
    dt.date(2026,1,5): {"open":100.0,"high":102.0,"low":100.0,"close":101.0},  # buy fills here
    dt.date(2026,1,6): {"open":101.0,"high":103.0,"low":100.0,"close":102.0},  # stop low 100 > 95: no fill
    dt.date(2026,1,7): {"open":102.0,"high":103.0,"low":101.0,"close":102.5},  # no fill
    dt.date(2026,1,8): {"open":90.0,"high":91.0,"low":88.0,"close":89.0},      # GAP-THROUGH stop@95 -> fill @90
    dt.date(2026,1,9): {"open":89.0,"high":90.0,"low":88.0,"close":89.0},      # settlement day
}}

def test_buy_next_open_with_cost():
    sim = Simulator(calendar=TradingCalendar(DAYS), bars=BARS, slippage_frac=0.0)
    sim.deposit(1000.0, on=dt.date(2026,1,2))
    buy = sim.submit_buy("XLK", signal_date=dt.date(2026,1,2),
                         dollar_amount=1000.0, tier=config.TIER_SECTOR_SPDR)
    assert buy.date == dt.date(2026,1,5)
    assert buy.price == 100.0
    half = config.TIER_CALM_ROUNDTRIP[config.TIER_SECTOR_SPDR] / 2
    assert abs(buy.cost - 1000.0 * half) < 1e-9
    assert abs(buy.shares - (1000.0 - buy.cost) / 100.0) < 1e-9

def test_full_sequence_with_stop_and_gfv_matches_golden():
    sim = Simulator(calendar=TradingCalendar(DAYS), bars=BARS, slippage_frac=0.0)
    sim.deposit(1000.0, on=dt.date(2026,1,2))
    buy = sim.submit_buy("XLK", signal_date=dt.date(2026,1,2),
                         dollar_amount=1000.0, tier=config.TIER_SECTOR_SPDR)
    sim.place_stop("XLK", stop_price=95.0)
    assert sim.evaluate_stops(dt.date(2026,1,6)) == []   # not triggered
    assert sim.evaluate_stops(dt.date(2026,1,7)) == []
    fills = sim.evaluate_stops(dt.date(2026,1,8))        # gap-through trigger
    assert len(fills) == 1
    stop_exit = fills[0]
    assert stop_exit.price == 90.0                       # filled at gapped-down open

    # GFV guard in the integrated sim: stop proceeds (sold 1/8) are unsettled until 1/9.
    assert sim.ledger.can_buy(100.0, on=dt.date(2026,1,8)) is False
    assert sim.ledger.can_buy(100.0, on=dt.date(2026,1,9)) is True

    result = {
        "buy": {"date": str(buy.date), "price": buy.price,
                "shares": round(buy.shares,6), "cost": round(buy.cost,6)},
        "stop_exit": {"date": str(stop_exit.date), "price": stop_exit.price,
                      "shares": round(stop_exit.shares,6),
                      "proceeds": round(stop_exit.proceeds,6), "cost": round(stop_exit.cost,6)},
        "settled_on_exit_day": round(sim.ledger.settled_cash(dt.date(2026,1,8)),6),
        "settled_next_trading_day": round(sim.ledger.settled_cash(dt.date(2026,1,9)),6),
    }
    with open(_FIX / "golden_simulator_sequence.json") as f:
        assert result == json.load(f)
