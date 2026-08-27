"""Single-name cost-tier model for the LIVE-01 paper-monitor (Task T2.2).

Design notes
------------
(a) FIREWALL: this module deliberately does NOT touch the protected
    ``autotrader.costs`` module or the ``STRATEGY_COST_FLOORS`` registry.
    The live track owns its cost model independently so the offline
    strategy registry is never mutated.

(b) FAIL-CLOSED: the function ``cost_tier_for`` always returns the
    conservative ``TIER_OTHER`` (0.50%) on missing, zero, negative, or
    non-finite inputs — it never returns ``None`` and never escalates to a
    cheaper tier on bad data.  This is a deliberate contrast with the
    offline ``roundtrip_cost_for_strategy``, which returns ``None`` for
    unregistered strategies (fail-open); here the default is the
    conservative tier.

(c) The Robinhood MCP serves REAL live market data (verified 2026-06-23: AMD /
    Micron match the live market to the cent). The $50B threshold reflects real
    valuations; in the 2025-26 mega-cap / AI run many liquid names clear it.
    Odd or synthetic instruments (e.g. a tokenized private-company row) are
    dropped by the universe's tradability + EQUITY sanity gate, NOT by the cost
    tier. The cost tier is REVIEW-ONLY METADATA — no P&L is computed from it —
    and the conservative default (TIER_OTHER) protects against under-charging.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class CostTier:
    """Immutable cost-tier descriptor.

    Attributes
    ----------
    name:
        Human-readable label, e.g. ``"TIER_MEGA_CAP"``.
    roundtrip_bps:
        Estimated full roundtrip cost in basis points (entry + exit spread +
        commission).  ``20.0`` ≈ 0.20%; ``50.0`` ≈ 0.50%.
    """

    name: str
    roundtrip_bps: float


# ── Tier constants ─────────────────────────────────────────────────────────────

TIER_MEGA_CAP: CostTier = CostTier("TIER_MEGA_CAP", 20.0)
"""Mega-cap names: market_cap ≥ $50B AND average_volume ≥ 5 M shares/day.

Estimated roundtrip spread + commission ≈ 0.20%.
"""

TIER_OTHER: CostTier = CostTier("TIER_OTHER", 50.0)
"""All other names, and the default when inputs are missing or invalid.

Estimated roundtrip spread + commission ≈ 0.50%.
"""

# Mega-cap thresholds (tuneable constants, easy to find and adjust)
_MEGA_CAP_MARKET_CAP_FLOOR: float = 50e9    # $50 billion
_MEGA_CAP_AVERAGE_VOLUME_FLOOR: float = 5e6  # 5 million shares / day


# ── Public function ────────────────────────────────────────────────────────────


def cost_tier_for(
    market_cap: float | None,
    average_volume: float | None,
) -> CostTier:
    """Return the cost tier for a single name based on its fundamentals.

    Parameters
    ----------
    market_cap:
        Total market capitalisation in USD.  ``None``, zero, negative, or
        non-finite (NaN / Inf) values are treated as unknown and trigger the
        conservative default.
    average_volume:
        Average daily trading volume in shares (typically the 2-week or
        30-day average from ``Fundamentals``).  Same bad-value rules apply.

    Returns
    -------
    CostTier
        ``TIER_MEGA_CAP`` iff BOTH ``market_cap >= 50e9`` AND
        ``average_volume >= 5e6``; ``TIER_OTHER`` in all other cases,
        including bad / missing inputs.

    Notes
    -----
    This function is FAIL-CLOSED: it never raises and never returns ``None``.
    Any uncertain or unclassifiable input yields the conservative tier.
    """
    # Validate market_cap — None / zero / negative / non-finite → conservative
    if market_cap is None:
        return TIER_OTHER
    try:
        mc = float(market_cap)
    except (TypeError, ValueError):
        return TIER_OTHER
    if not math.isfinite(mc) or mc <= 0:
        return TIER_OTHER

    # Validate average_volume — same rules
    if average_volume is None:
        return TIER_OTHER
    try:
        av = float(average_volume)
    except (TypeError, ValueError):
        return TIER_OTHER
    if not math.isfinite(av) or av <= 0:
        return TIER_OTHER

    # Mega-cap only when BOTH thresholds are met
    if mc >= _MEGA_CAP_MARKET_CAP_FLOOR and av >= _MEGA_CAP_AVERAGE_VOLUME_FLOOR:
        return TIER_MEGA_CAP

    return TIER_OTHER
