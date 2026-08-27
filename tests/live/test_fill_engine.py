# tests/live/test_fill_engine.py
import datetime as dt
import pytest
from autotrader_live.mcp_live import Quote
from autotrader_live import fill_engine as fe

def _q(**kw):
    base = dict(symbol="AMD", settled_close=100.0, settled_close_date=dt.date(2026,6,22),
                settled_close_interpolated=False, settled_close_source="x",
                bid=99.9, ask=100.1, last_trade_price=100.0, previous_close=100.0,
                has_traded=True, state="active")
    base.update(kw)
    return Quote(**base)

def test_fillable_happy():
    assert fe.quote_is_fillable(_q()) is True

@pytest.mark.parametrize("kw", [
    dict(last_trade_price=0.0),
    dict(bid=None),
    dict(ask=None),
    dict(bid=100.2, ask=100.1),          # crossed
    dict(has_traded=False),
    dict(state="closed"),
    dict(last_trade_price=200.0),        # >50% vs previous_close=100
])
def test_not_fillable(kw):
    assert fe.quote_is_fillable(_q(**kw)) is False


from autotrader_live.strategy_trend import TrendDecision

def _dec(**kw):
    base = dict(signal_date=dt.date(2026,6,22), symbol="AMD", close=100.0, sma200=80.0,
                trend_ok=True, mom_252=0.3, momentum_ok=True, nearness=0.97, near_high=True,
                prior_donch_upper=105.0, breakout_55=False, atr14=5.0, entry=True, reason="ok")
    base.update(kw)
    return TrendDecision(**base)

def test_entry_reference_near_high():
    ref, basis = fe.entry_reference(_dec(breakout_55=False))
    assert ref == 100.0 and basis == "near_high"          # uses close

def test_entry_reference_breakout():
    ref, basis = fe.entry_reference(_dec(breakout_55=True))
    assert ref == 105.0 and basis == "breakout"           # uses prior_donch_upper

def test_should_trigger_strict_gt():
    assert fe.should_trigger_entry(105.01, 105.0) is True
    assert fe.should_trigger_entry(105.0, 105.0) is False  # strict >


from autotrader_live.paper_book import ArmedEntry

def _armed(**kw):
    base = dict(symbol="AMD", entry_ref=99.0, ref_basis="near_high", target_notional=200.0,
                atr_at_arm=5.0, cost_tier_bps=20.0, arm_date="2026-06-23")
    base.update(kw)
    return ArmedEntry(**base)

def test_entry_fill_at_ask_plus_slippage():
    q = _q(ask=100.10, bid=99.90, last_trade_price=100.0)
    f = fe.entry_fill(q, _armed(target_notional=200.0), available_cash=2000.0,
                      ts="2026-06-23T14:00:00Z", slippage_bps=3.0)
    assert f is not None
    assert f.price == pytest.approx(100.10 * (1 + 3/1e4))   # ask + 3bps
    assert f.notional == pytest.approx(200.0)               # capped by target
    assert f.shares == pytest.approx(200.0 / f.price)
    assert f.fill_id == "AMD:entry:2026-06-23" and f.intent_type == "entry"
    assert f.realized_pnl_delta == 0.0

def test_entry_fill_capped_by_cash():
    f = fe.entry_fill(_q(), _armed(target_notional=200.0), available_cash=120.0,
                      ts="t", slippage_bps=3.0)
    assert f.notional == pytest.approx(120.0)

def test_entry_fill_skips_dust():
    assert fe.entry_fill(_q(), _armed(target_notional=200.0), available_cash=40.0,
                         ts="t", slippage_bps=3.0) is None   # below MIN_NOTIONAL

def test_entry_fill_ask_none_falls_back_to_last():
    q = _q(ask=None, bid=99.9, last_trade_price=100.0)
    # quote_is_fillable would reject ask=None, but entry_fill defends anyway:
    f = fe.entry_fill(q, _armed(), available_cash=2000.0, ts="t", slippage_bps=0.0)
    assert f.price == pytest.approx(100.0)


def _pos(**kw):
    base = dict(symbol="AMD", shares=2.0, entry_price=100.0, entry_ts="t", atr_at_entry=5.0,
                current_stop=95.0, highest_high_since_entry=100.0, ratchet_seq=0, cost_tier_bps=20.0)
    base.update(kw)
    return PaperPosition(**base)

from autotrader_live.paper_book import PaperPosition

def test_stop_no_trigger_when_bid_above_stop():
    q = _q(bid=96.0, ask=96.1, last_trade_price=96.05)   # bid 96 > stop 95
    assert fe.stop_fill(_pos(current_stop=95.0), q, ts="t", slippage_bps=3.0) is None

def test_stop_touch_fills_at_bid_below_stop():
    # bid touches the stop: a non-gap touch still crosses the spread (fills < stop)
    q = _q(bid=95.0, ask=95.2, last_trade_price=95.1)
    f = fe.stop_fill(_pos(current_stop=95.0), q, ts="t", slippage_bps=3.0)
    assert f is not None and f.intent_type == "stop" and f.side == "sell"
    assert f.price == pytest.approx(95.0 * (1 - 3/1e4))
    assert f.price < 95.0                                  # realizes LESS than the stop
    assert f.realized_pnl_delta == pytest.approx((f.price - 100.0) * 2.0)
    assert f.fill_id == "AMD:stop:t:0"   # date-namespaced by entry_ts[:10] ("t" in this fixture)

def test_stop_gap_through_fills_at_low_bid():
    q = _q(bid=80.0, ask=80.3, last_trade_price=80.1)      # gapped well below stop
    f = fe.stop_fill(_pos(current_stop=95.0), q, ts="t", slippage_bps=3.0)
    assert f.price == pytest.approx(80.0 * (1 - 3/1e4))    # loss bounded by sizing, not stop

def test_stop_bid_none_skips():
    q = _q(bid=None, ask=95.0, last_trade_price=94.0)
    assert fe.stop_fill(_pos(current_stop=95.0), q, ts="t", slippage_bps=3.0) is None


def test_ratchet_raises_stop_on_new_high():
    pos = _pos(current_stop=95.0, highest_high_since_entry=110.0, atr_at_entry=5.0, ratchet_seq=0)
    # new sampled high 120 -> chandelier 120 - 3*5 = 105 > 95 -> stop rises, seq++
    out = fe.ratchet(pos, last_trade_price=120.0, k=3.0)
    assert out.highest_high_since_entry == 120.0
    assert out.current_stop == pytest.approx(105.0)
    assert out.ratchet_seq == 1

def test_ratchet_monotonic_no_lower():
    pos = _pos(current_stop=105.0, highest_high_since_entry=120.0, atr_at_entry=5.0, ratchet_seq=1)
    out = fe.ratchet(pos, last_trade_price=100.0, k=3.0)   # lower price
    assert out.current_stop == 105.0                       # never falls
    assert out.highest_high_since_entry == 120.0           # high unchanged
    assert out.ratchet_seq == 1                            # no increment
