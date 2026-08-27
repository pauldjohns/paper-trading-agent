# tests/live/test_sizing.py
"""Tests for autotrader_live.sizing — volatility-targeted fixed-fractional position sizing.

Oracle values were computed by hand (not from the implementation) to avoid tautological tests.
See task T1.3 for derivations.
"""
import math
import pytest
from autotrader_live.sizing import size


# ---------------------------------------------------------------------------
# Oracle A — uncapped, fractional shares
# ---------------------------------------------------------------------------
class TestOracleA:
    """equity=100000, atr=2.0, price=50.0, defaults (f=0.01, k=3.0, cap=0.15, fractional=True)."""

    def setup_method(self):
        self.result = size(equity=100000, atr=2.0, price=50.0)

    def test_risk_per_share(self):
        # k * atr = 3.0 * 2.0 = 6.0
        assert abs(self.result["risk_per_share"] - 6.0) < 1e-6

    def test_dollars_at_risk(self):
        # f * equity = 0.01 * 100000 = 1000.0
        assert abs(self.result["dollars_at_risk"] - 1000.0) < 1e-6

    def test_shares(self):
        # 1000.0 / 6.0 = 166.6666...
        assert abs(self.result["shares"] - 166.6666666667) < 1e-6

    def test_notional(self):
        # 166.6666... * 50.0 = 8333.333...
        assert abs(self.result["notional"] - 8333.3333333) < 1e-4

    def test_not_capped(self):
        assert self.result["capped"] is False


# ---------------------------------------------------------------------------
# Oracle B — capped, fractional shares
# ---------------------------------------------------------------------------
class TestOracleB:
    """equity=100000, atr=0.5, price=50.0 — raw_notional > cap triggers cap."""

    def setup_method(self):
        # raw: risk_per_share = 3*0.5 = 1.5; dollars_at_risk = 1000; raw_shares = 666.67
        # raw_notional = 666.67 * 50 = 33333 > cap=15000 → capped
        self.result = size(equity=100000, atr=0.5, price=50.0)

    def test_capped(self):
        assert self.result["capped"] is True

    def test_notional(self):
        assert abs(self.result["notional"] - 15000.0) < 1e-6

    def test_shares(self):
        # 15000 / 50 = 300.0
        assert abs(self.result["shares"] - 300.0) < 1e-6


# ---------------------------------------------------------------------------
# Oracle C — uncapped, fractional=False (integer shares)
# ---------------------------------------------------------------------------
class TestOracleC:
    """equity=100000, atr=2.0, price=50.0, fractional=False."""

    def setup_method(self):
        self.result = size(equity=100000, atr=2.0, price=50.0, fractional=False)

    def test_shares_floored(self):
        # floor(166.6666...) = 166
        assert self.result["shares"] == 166

    def test_notional_recomputed(self):
        # 166 * 50.0 = 8300.0
        assert abs(self.result["notional"] - 8300.0) < 1e-6

    def test_not_capped(self):
        assert self.result["capped"] is False


# ---------------------------------------------------------------------------
# Oracle D — capped, fractional=False (integer shares)
# ---------------------------------------------------------------------------
class TestOracleD:
    """equity=100000, atr=0.5, price=50.0, fractional=False — cap gives exact 300."""

    def setup_method(self):
        self.result = size(equity=100000, atr=0.5, price=50.0, fractional=False)

    def test_shares_floored(self):
        # cap: 15000/50 = 300.0 exactly; floor(300.0) = 300
        assert self.result["shares"] == 300

    def test_notional(self):
        assert abs(self.result["notional"] - 15000.0) < 1e-6

    def test_capped(self):
        assert self.result["capped"] is True


# ---------------------------------------------------------------------------
# Return dict keys
# ---------------------------------------------------------------------------
class TestReturnKeys:
    def test_all_keys_present(self):
        r = size(equity=100000, atr=2.0, price=50.0)
        assert set(r.keys()) == {"shares", "notional", "risk_per_share", "dollars_at_risk", "capped"}


# ---------------------------------------------------------------------------
# Input guards — every bad input raises ValueError
# ---------------------------------------------------------------------------
class TestGuards:
    def test_equity_zero_raises(self):
        with pytest.raises(ValueError):
            size(equity=0, atr=2.0, price=50.0)

    def test_equity_negative_raises(self):
        with pytest.raises(ValueError):
            size(equity=-1, atr=2.0, price=50.0)

    def test_atr_zero_raises(self):
        with pytest.raises(ValueError):
            size(equity=100000, atr=0, price=50.0)

    def test_atr_negative_raises(self):
        with pytest.raises(ValueError):
            size(equity=100000, atr=-1.0, price=50.0)

    def test_price_zero_raises(self):
        with pytest.raises(ValueError):
            size(equity=100000, atr=2.0, price=0)

    def test_price_negative_raises(self):
        with pytest.raises(ValueError):
            size(equity=100000, atr=2.0, price=-50.0)

    def test_f_zero_raises(self):
        with pytest.raises(ValueError):
            size(equity=100000, atr=2.0, price=50.0, f=0)

    def test_f_negative_raises(self):
        with pytest.raises(ValueError):
            size(equity=100000, atr=2.0, price=50.0, f=-0.01)

    def test_k_zero_raises(self):
        with pytest.raises(ValueError):
            size(equity=100000, atr=2.0, price=50.0, k=0)

    def test_k_negative_raises(self):
        with pytest.raises(ValueError):
            size(equity=100000, atr=2.0, price=50.0, k=-1.0)

    def test_per_name_cap_frac_zero_raises(self):
        with pytest.raises(ValueError):
            size(equity=100000, atr=2.0, price=50.0, per_name_cap_frac=0)

    def test_per_name_cap_frac_above_one_raises(self):
        with pytest.raises(ValueError):
            size(equity=100000, atr=2.0, price=50.0, per_name_cap_frac=1.5)


class TestZeroSharesEdge:
    """fractional=False with equity too small to afford one risk-sized share
    floors to 0 shares / 0 notional. Pins this as the defined result (a 0-share
    order) so a downstream caller that checks shares > 0 has a locked contract."""

    def test_fractional_false_small_equity_yields_zero_shares(self):
        # raw_shares = (0.01*100)/(3*2) = 0.1/6 ≈ 0.0167 → floor → 0
        r = size(equity=100, atr=2.0, price=50.0, fractional=False)
        assert r["shares"] == 0
        assert r["notional"] == 0.0
        assert r["capped"] is False
