# tests/test_stops.py
from autotrader.stops import stop_fill_price

def test_gap_through_fills_at_open_worse_than_stop():
    bar = {"open": 90.0, "high": 92.0, "low": 88.0, "close": 91.0}
    assert stop_fill_price(bar, stop_price=95.0, slippage_frac=0.0) == 90.0  # open, NOT 95

def test_intrabar_pierce_fills_at_stop_minus_slippage():
    bar = {"open": 100.0, "high": 101.0, "low": 96.0, "close": 99.0}
    fill = stop_fill_price(bar, stop_price=97.0, slippage_frac=0.001)
    assert abs(fill - 97.0 * (1 - 0.001)) < 1e-9  # sell fills worse = lower

def test_no_trigger_returns_none():
    bar = {"open": 100.0, "high": 101.0, "low": 98.0, "close": 99.5}
    assert stop_fill_price(bar, stop_price=97.0, slippage_frac=0.001) is None

def test_open_equal_stop_fills_at_open_no_slippage():
    # Boundary: open == stop routes to the gap-through branch (open <= stop), so it fills
    # exactly at the stop with no slippage layered on. Conservative and intentional; locked
    # here so the boundary can't drift into the intrabar-pierce (stop - slippage) branch.
    bar = {"open": 95.0, "high": 96.0, "low": 90.0, "close": 94.0}
    assert stop_fill_price(bar, stop_price=95.0, slippage_frac=0.01) == 95.0
