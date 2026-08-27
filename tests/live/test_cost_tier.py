"""Tests for src/autotrader_live/cost_tier.py (Task T2.2, Commit 1).

All tests are offline/deterministic — no MCP calls.

Contract under test
-------------------
- ``TIER_MEGA_CAP.roundtrip_bps == 20.0``  (0.20%)
- ``TIER_OTHER.roundtrip_bps == 50.0``     (0.50%)
- ``cost_tier_for`` is FAIL-CLOSED:
    - None, zero, negative, non-finite market_cap → TIER_OTHER
    - None, zero  average_volume                  → TIER_OTHER
    - Both above threshold (inclusive)            → TIER_MEGA_CAP
    - Either threshold strictly below             → TIER_OTHER
    - Boundary (exactly 50e9 & 5e6)              → TIER_MEGA_CAP
"""
import math

import pytest

from autotrader_live.cost_tier import (
    CostTier,
    TIER_MEGA_CAP,
    TIER_OTHER,
    cost_tier_for,
)


# ── Constant sanity ────────────────────────────────────────────────────────────

def test_tier_mega_cap_roundtrip_bps():
    assert TIER_MEGA_CAP.roundtrip_bps == 20.0


def test_tier_other_roundtrip_bps():
    assert TIER_OTHER.roundtrip_bps == 50.0


def test_tier_is_frozen_dataclass():
    """CostTier instances must be immutable (frozen=True)."""
    with pytest.raises((AttributeError, TypeError)):
        TIER_MEGA_CAP.name = "mutated"  # type: ignore[misc]


def test_tier_instances_are_the_module_constants():
    """cost_tier_for should return the exact module-level constants."""
    result = cost_tier_for(100e9, 10e6)
    assert result is TIER_MEGA_CAP


# ── Happy-path: mega-cap ───────────────────────────────────────────────────────

def test_both_above_threshold_returns_mega_cap():
    """Large market cap and high volume → TIER_MEGA_CAP."""
    tier = cost_tier_for(100e9, 10e6)
    assert tier == TIER_MEGA_CAP
    assert tier.roundtrip_bps == 20.0


def test_boundary_exact_threshold_returns_mega_cap():
    """Exactly at the boundary (market_cap=50e9, avg_vol=5e6) → TIER_MEGA_CAP."""
    tier = cost_tier_for(50e9, 5e6)
    assert tier == TIER_MEGA_CAP


def test_large_values_returns_mega_cap():
    """Very large values (trillion-dollar name) → TIER_MEGA_CAP."""
    tier = cost_tier_for(3e12, 50e6)
    assert tier == TIER_MEGA_CAP


# ── Just-below-threshold cases → TIER_OTHER ───────────────────────────────────

def test_market_cap_just_below_threshold():
    """market_cap one cent below $50B → TIER_OTHER even with high volume."""
    tier = cost_tier_for(50e9 - 0.01, 10e6)
    assert tier == TIER_OTHER


def test_avg_volume_just_below_threshold():
    """average_volume one share below 5M → TIER_OTHER even with huge market cap."""
    tier = cost_tier_for(200e9, 5e6 - 1)
    assert tier == TIER_OTHER


def test_both_just_below_threshold():
    """Both thresholds one unit below → TIER_OTHER."""
    tier = cost_tier_for(50e9 - 1, 5e6 - 1)
    assert tier == TIER_OTHER


def test_market_cap_ok_but_volume_below():
    """market_cap above $50B but average_volume below 5M → TIER_OTHER."""
    tier = cost_tier_for(100e9, 1e6)
    assert tier == TIER_OTHER


def test_volume_ok_but_market_cap_below():
    """average_volume above 5M but market_cap below $50B → TIER_OTHER."""
    tier = cost_tier_for(10e9, 20e6)
    assert tier == TIER_OTHER


# ── Fail-closed: bad market_cap inputs ────────────────────────────────────────

def test_none_market_cap_returns_tier_other():
    tier = cost_tier_for(None, 10e6)
    assert tier == TIER_OTHER


def test_zero_market_cap_returns_tier_other():
    tier = cost_tier_for(0, 10e6)
    assert tier == TIER_OTHER


def test_negative_market_cap_returns_tier_other():
    tier = cost_tier_for(-1e9, 10e6)
    assert tier == TIER_OTHER


def test_nan_market_cap_returns_tier_other():
    tier = cost_tier_for(float("nan"), 10e6)
    assert tier == TIER_OTHER


def test_inf_market_cap_returns_tier_other():
    """Infinite market_cap is non-finite → treated as bad data → TIER_OTHER."""
    tier = cost_tier_for(math.inf, 10e6)
    assert tier == TIER_OTHER


def test_neg_inf_market_cap_returns_tier_other():
    tier = cost_tier_for(-math.inf, 10e6)
    assert tier == TIER_OTHER


# ── Fail-closed: bad average_volume inputs ────────────────────────────────────

def test_none_avg_volume_returns_tier_other():
    tier = cost_tier_for(100e9, None)
    assert tier == TIER_OTHER


def test_zero_avg_volume_returns_tier_other():
    tier = cost_tier_for(100e9, 0)
    assert tier == TIER_OTHER


def test_negative_avg_volume_returns_tier_other():
    tier = cost_tier_for(100e9, -5e6)
    assert tier == TIER_OTHER


def test_nan_avg_volume_returns_tier_other():
    tier = cost_tier_for(100e9, float("nan"))
    assert tier == TIER_OTHER


# ── Fail-closed: both bad ─────────────────────────────────────────────────────

def test_both_none_returns_tier_other():
    tier = cost_tier_for(None, None)
    assert tier == TIER_OTHER


def test_both_zero_returns_tier_other():
    tier = cost_tier_for(0, 0)
    assert tier == TIER_OTHER


# ── Never raises ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("mc,av", [
    (None, None),
    (0, 0),
    (-1, -1),
    (float("nan"), float("nan")),
    (float("inf"), float("inf")),
    (50e9, 5e6),
    (100e9, 10e6),
])
def test_never_raises(mc, av):
    """cost_tier_for must never raise regardless of inputs."""
    result = cost_tier_for(mc, av)
    assert isinstance(result, CostTier)
