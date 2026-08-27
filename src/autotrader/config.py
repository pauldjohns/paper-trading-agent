"""Static configuration: universes, liquidity tiers, fee constants, strategy cost floors."""

# Survivorship-clean, gating-grade momentum universe (9 original Select Sector SPDRs).
SECTOR_SPDRS = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU"]
INDEX_ETFS = ["SPY", "QQQ", "DIA", "IWM"]
BOND_ETFS = ["IEF", "AGG"]

# Liquidity tiers (axis A: instrument). Calm value = round-trip fraction of price.
TIER_INDEX_ETF = "index_etf"
TIER_SECTOR_SPDR = "sector_spdr"
TIER_MEGA_CAP = "mega_cap"
TIER_OTHER = "other"

TIER_CALM_ROUNDTRIP = {
    TIER_INDEX_ETF: 0.0010,    # 0.10%
    TIER_SECTOR_SPDR: 0.0015,  # 0.15%
    TIER_MEGA_CAP: 0.0020,     # 0.20%
    TIER_OTHER: 0.0050,        # 0.50%
}
TIER_MAX_STRESS = {
    TIER_INDEX_ETF: 5.0, TIER_SECTOR_SPDR: 5.0, TIER_MEGA_CAP: 5.0, TIER_OTHER: 5.0,
}

# Axis B: strategy-level punitive cost floor (COST_MODEL.md section 4). S3 dip-buys when
# spreads are widest, so it is charged a floor REGARDLESS of its (cheap) ETF instrument tier.
# Cost = max(instrument_tier_calm, strategy_floor) x stress.
S3_COST_FLOOR = 0.0045   # 0.45% round-trip floor for the mean-reversion null-test.

# Strategy -> cost-floor registry (COST_MODEL.md section 4). Only S3 carries a punitive
# floor; every other strategy is priced on its instrument tier alone (no entry => None).
# Plan 02's strategy dispatch prices trades via costs.roundtrip_cost_for_strategy, which
# reads this map, so the S3 floor cannot be silently omitted at the call site.
STRATEGY_COST_FLOORS = {"S3": S3_COST_FLOOR}

# Regulatory pass-throughs (SELL side only). Verified 2026-06-16 (COST_MODEL.md section 6).
SEC_SECTION31_RATE = 0.0000206   # $20.60 per $1,000,000 of sell proceeds.
FINRA_TAF_PER_SHARE = 0.000166   # per share sold.
FINRA_TAF_MAX = 8.30             # per-trade cap.
