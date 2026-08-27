# tests/live/test_exits.py
"""Tests for autotrader_live.exits — catastrophe floor + chandelier ratchet.

Oracle values were computed by hand (not from the implementation) to avoid tautological tests.
See task T1.4 for derivations.
"""
import pytest
from autotrader_live.exits import (
    initial_catastrophe_stop,
    chandelier_level,
    ratchet_stop,
    update_trailing_stop,
)


# ---------------------------------------------------------------------------
# initial_catastrophe_stop
# ---------------------------------------------------------------------------
class TestInitialCatastropheStop:
    def test_oracle_default_m(self):
        # 50.0 - 2.0 * 2.0 = 46.0
        assert abs(initial_catastrophe_stop(50.0, 2.0) - 46.0) < 1e-9

    def test_oracle_custom_m(self):
        # 50.0 - 1.5 * 2.0 = 47.0
        assert abs(initial_catastrophe_stop(50.0, 2.0, m=1.5) - 47.0) < 1e-9

    def test_result_below_zero_raises(self):
        # 5.0 - 2.0 * 3.0 = -1.0 — stop must be > 0
        with pytest.raises(ValueError):
            initial_catastrophe_stop(5.0, 3.0, m=2.0)

    def test_entry_price_zero_raises(self):
        with pytest.raises(ValueError):
            initial_catastrophe_stop(0.0, 2.0)

    def test_entry_price_negative_raises(self):
        with pytest.raises(ValueError):
            initial_catastrophe_stop(-10.0, 2.0)

    def test_atr_zero_raises(self):
        with pytest.raises(ValueError):
            initial_catastrophe_stop(50.0, 0.0)

    def test_atr_negative_raises(self):
        with pytest.raises(ValueError):
            initial_catastrophe_stop(50.0, -1.0)

    def test_m_zero_raises(self):
        with pytest.raises(ValueError):
            initial_catastrophe_stop(50.0, 2.0, m=0.0)

    def test_m_negative_raises(self):
        with pytest.raises(ValueError):
            initial_catastrophe_stop(50.0, 2.0, m=-1.0)


# ---------------------------------------------------------------------------
# chandelier_level
# ---------------------------------------------------------------------------
class TestChandelierLevel:
    def test_oracle_default_k(self):
        # 60.0 - 3.0 * 2.0 = 54.0
        assert abs(chandelier_level(60.0, 2.0) - 54.0) < 1e-9

    def test_oracle_custom_k(self):
        # 60.0 - 2.5 * 2.0 = 55.0
        assert abs(chandelier_level(60.0, 2.0, k=2.5) - 55.0) < 1e-9

    def test_highest_high_zero_raises(self):
        with pytest.raises(ValueError):
            chandelier_level(0.0, 2.0)

    def test_highest_high_negative_raises(self):
        with pytest.raises(ValueError):
            chandelier_level(-60.0, 2.0)

    def test_atr_zero_raises(self):
        with pytest.raises(ValueError):
            chandelier_level(60.0, 0.0)

    def test_atr_negative_raises(self):
        with pytest.raises(ValueError):
            chandelier_level(60.0, -1.0)

    def test_k_zero_raises(self):
        with pytest.raises(ValueError):
            chandelier_level(60.0, 2.0, k=0.0)

    def test_k_negative_raises(self):
        with pytest.raises(ValueError):
            chandelier_level(60.0, 2.0, k=-1.0)


# ---------------------------------------------------------------------------
# ratchet_stop — pure max, no guards, but must enforce monotonic-up
# ---------------------------------------------------------------------------
class TestRatchetStop:
    def test_rises_when_new_level_higher(self):
        # 54.0 > 46.0 → 54.0
        assert abs(ratchet_stop(46.0, 54.0) - 54.0) < 1e-9

    def test_refuses_to_lower(self):
        # 50.0 < 54.0 → must stay at 54.0 (monotonic-up invariant)
        assert abs(ratchet_stop(54.0, 50.0) - 54.0) < 1e-9

    def test_equal_values(self):
        assert abs(ratchet_stop(54.0, 54.0) - 54.0) < 1e-9


# ---------------------------------------------------------------------------
# update_trailing_stop — daily-close ratchet entry point
# ---------------------------------------------------------------------------
class TestUpdateTrailingStop:
    def test_oracle_stop_rises(self):
        # chandelier(60, 2, k=3) = 54 > prev_stop=46 → 54
        result = update_trailing_stop(
            prev_stop=46.0, highest_high_since_entry=60.0, atr_current=2.0
        )
        assert abs(result - 54.0) < 1e-9

    def test_monotonic_up_holds_when_chandelier_below_prev(self):
        # chandelier(60, 2, k=3) = 54 < prev_stop=58 → stop held at 58
        result = update_trailing_stop(
            prev_stop=58.0, highest_high_since_entry=60.0, atr_current=2.0
        )
        assert abs(result - 58.0) < 1e-9

    def test_custom_k(self):
        # chandelier(60, 2, k=2) = 56 > prev_stop=46 → 56
        result = update_trailing_stop(
            prev_stop=46.0, highest_high_since_entry=60.0, atr_current=2.0, k=2.0
        )
        assert abs(result - 56.0) < 1e-9
