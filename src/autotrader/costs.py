# src/autotrader/costs.py
"""Cost model per COST_MODEL.md.

Default cost path = effective_roundtrip_cost (per-tier x stress, with an optional
strategy-level floor). Corwin-Schultz is the spread-estimation REFINEMENT (used later to
calibrate/replace the tier baseline). NOTE: corwin_schultz_spread is the raw 2-day
estimator; the overnight-gap adjustment (COST_MODEL.md section 6) is deferred to Plan 03 —
do not use the single-pair value directly for per-bar cost; use average_cs_spread.
"""
import math
from autotrader import config

_K = 3 - 2 * math.sqrt(2)


def corwin_schultz_spread(high_t, low_t, high_t1, low_t1) -> float:
    """Raw two-day Corwin-Schultz (2012) proportional spread. Negative -> clamped to 0."""
    if min(low_t, low_t1) <= 0:
        raise ValueError("prices must be positive")
    if high_t < low_t or high_t1 < low_t1:
        raise ValueError("high must be >= low for each bar")
    beta = math.log(high_t / low_t) ** 2 + math.log(high_t1 / low_t1) ** 2
    gamma = math.log(max(high_t, high_t1) / min(low_t, low_t1)) ** 2
    alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / _K - math.sqrt(gamma / _K)
    return max(2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha)), 0.0)


def average_cs_spread(bars) -> float:
    """Mean of per-consecutive-pair Corwin-Schultz estimates over a list of OHLC bars.

    bars: list of dicts with 'high' and 'low'. Requires >= 2 bars.
    (Overnight-gap adjustment deferred to Plan 03 per COST_MODEL.md section 6.)
    """
    if len(bars) < 2:
        raise ValueError("need >= 2 bars")
    vals = [corwin_schultz_spread(bars[i]["high"], bars[i]["low"],
                                  bars[i + 1]["high"], bars[i + 1]["low"])
            for i in range(len(bars) - 1)]
    return sum(vals) / len(vals)


def regulatory_sell_fees(proceeds: float, shares: float) -> float:
    """SEC Section 31 (on gross proceeds) + FINRA TAF (per share, capped). Sell side only."""
    return proceeds * config.SEC_SECTION31_RATE + min(
        shares * config.FINRA_TAF_PER_SHARE, config.FINRA_TAF_MAX)


def effective_roundtrip_cost(tier: str, floor: float = None, stress: float = 1.0) -> float:
    """Round-trip cost fraction = max(instrument-tier calm, strategy floor) x clamped stress."""
    if tier not in config.TIER_CALM_ROUNDTRIP:
        raise ValueError(f"unknown tier: {tier!r} (expected one of {sorted(config.TIER_CALM_ROUNDTRIP)})")
    base = config.TIER_CALM_ROUNDTRIP[tier]
    if floor is not None:
        base = max(base, floor)
    stress = max(1.0, min(stress, config.TIER_MAX_STRESS[tier]))
    return base * stress


def roundtrip_cost_for_strategy(strategy: str, tier: str, stress: float = 1.0) -> float:
    """Round-trip cost for a NAMED strategy on an instrument tier, applying that strategy's
    cost floor (config.STRATEGY_COST_FLOORS, per COST_MODEL.md section 4) automatically.

    This is the entry point Plan 02 strategy dispatch should use so the S3 floor cannot be
    silently omitted at the call site. Strategies with no registered floor fall through to
    their instrument tier. Unknown tiers still raise (via effective_roundtrip_cost).
    """
    return effective_roundtrip_cost(tier, config.STRATEGY_COST_FLOORS.get(strategy), stress)
