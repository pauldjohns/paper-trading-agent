# tests/test_costs.py
import math
import pytest
from autotrader.costs import (corwin_schultz_spread, average_cs_spread,
                              regulatory_sell_fees, effective_roundtrip_cost,
                              roundtrip_cost_for_strategy)
from autotrader import config

def test_corwin_schultz_positive_case_real_value():
    # Wide ranges -> a genuinely POSITIVE spread (NOT the clamp-to-zero path).
    s = corwin_schultz_spread(high_t=110, low_t=90, high_t1=112, low_t1=92)
    assert s > 0.0                      # kills the 0==0 tautology
    assert 0.13 < s < 0.17              # band catches a sign-flip/swap in alpha or beta/gamma

def test_corwin_schultz_zero_clamp_on_negative_estimate():
    # Narrow, non-trending ranges produce a negative raw estimate -> clamped to 0.
    s = corwin_schultz_spread(high_t=101, low_t=99, high_t1=102, low_t1=100)
    assert s == 0.0

def test_corwin_schultz_zero_when_no_range():
    assert corwin_schultz_spread(100, 100, 100, 100) == 0.0

def test_average_cs_spread_over_window():
    bars = [{"high": 110, "low": 90}, {"high": 112, "low": 92}, {"high": 111, "low": 91}]
    avg = average_cs_spread(bars)
    assert avg > 0.0  # mean of the per-pair estimates

def test_regulatory_sell_fees_small():
    fees = regulatory_sell_fees(proceeds=1000.0, shares=4.0)
    assert abs(fees - (1000.0 * config.SEC_SECTION31_RATE + 4.0 * config.FINRA_TAF_PER_SHARE)) < 1e-9
    assert fees < 0.05

def test_taf_cap_applied():
    fees = regulatory_sell_fees(proceeds=10_000.0, shares=10_000_000.0)
    assert fees == 10_000.0 * config.SEC_SECTION31_RATE + config.FINRA_TAF_MAX

def test_effective_roundtrip_tier_only():
    assert effective_roundtrip_cost(config.TIER_INDEX_ETF) == config.TIER_CALM_ROUNDTRIP[config.TIER_INDEX_ETF]

def test_effective_roundtrip_strategy_floor_overrides_cheap_tier():
    # S3 on an index ETF: cheap 0.10% tier must be lifted to the 0.45% floor.
    c = effective_roundtrip_cost(config.TIER_INDEX_ETF, floor=config.S3_COST_FLOOR)
    assert c == config.S3_COST_FLOOR

def test_effective_roundtrip_stress_scales_and_clamps():
    base = effective_roundtrip_cost(config.TIER_INDEX_ETF)
    assert effective_roundtrip_cost(config.TIER_INDEX_ETF, stress=3.0) == base * 3.0
    assert effective_roundtrip_cost(config.TIER_INDEX_ETF, stress=99.0) == base * config.TIER_MAX_STRESS[config.TIER_INDEX_ETF]

def test_corwin_schultz_rejects_inverted_bar():
    # high < low is corrupt OHLC; the squared-log math would fabricate a spurious positive
    # spread instead of clamping. Reject it rather than silently return garbage.
    with pytest.raises(ValueError):
        corwin_schultz_spread(high_t=90, low_t=110, high_t1=112, low_t1=92)

def test_effective_roundtrip_unknown_tier_raises():
    # A mistyped tier string should give a clear error, not a bare KeyError.
    with pytest.raises(ValueError):
        effective_roundtrip_cost("not_a_real_tier")

def test_strategy_floor_registry_binds_s3():
    assert config.STRATEGY_COST_FLOORS["S3"] == config.S3_COST_FLOOR

def test_roundtrip_for_s3_applies_floor_on_cheap_tier():
    # S3 on a cheap index ETF must be lifted to the 0.45% floor automatically — the caller
    # cannot silently omit it (the whole point of the registry).
    assert roundtrip_cost_for_strategy("S3", config.TIER_INDEX_ETF) == config.S3_COST_FLOOR

def test_roundtrip_for_unfloored_strategy_uses_tier():
    # A strategy with no registered floor falls through to its instrument tier.
    assert (roundtrip_cost_for_strategy("S2", config.TIER_SECTOR_SPDR)
            == config.TIER_CALM_ROUNDTRIP[config.TIER_SECTOR_SPDR])

def test_roundtrip_for_strategy_still_stress_scales():
    base = roundtrip_cost_for_strategy("S3", config.TIER_INDEX_ETF)
    assert roundtrip_cost_for_strategy("S3", config.TIER_INDEX_ETF, stress=2.0) == base * 2.0
