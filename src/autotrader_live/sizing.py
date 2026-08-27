# src/autotrader_live/sizing.py
"""Volatility-targeted fixed-fractional position sizing.

Pure math — no pandas or external dependencies beyond stdlib math.
"""
import math


def size(
    equity: float,
    atr: float,
    price: float,
    f: float = 0.01,
    k: float = 3.0,
    per_name_cap_frac: float = 0.15,
    fractional: bool = True,
) -> dict:
    """Compute position size using volatility-targeted fixed-fractional risk sizing.

    Parameters
    ----------
    equity:
        Total account equity in dollars.  Must be > 0.
    atr:
        Average True Range for the instrument.  Must be > 0.
    price:
        Current instrument price.  Must be > 0.
    f:
        Fraction of equity to risk per trade (default 1 %).  Must be > 0.
    k:
        ATR multiplier for stop-distance (default 3.0).  Must be > 0.
    per_name_cap_frac:
        Maximum fraction of equity that may be allocated to a single name
        (default 15 %).  Must be in (0, 1].
    fractional:
        If True, allow fractional shares.  If False, floor to whole shares
        and recompute notional from the floored share count.

    Returns
    -------
    dict with keys:
        shares          — number of shares (float or int when fractional=False)
        notional        — dollar value of the position
        risk_per_share  — k * atr
        dollars_at_risk — f * equity
        capped          — True when the per-name cap constrained the size
    """
    # --- input guards ---
    if equity <= 0:
        raise ValueError(f"equity must be > 0, got {equity!r}")
    if atr <= 0:
        raise ValueError(f"atr must be > 0, got {atr!r}")
    if price <= 0:
        raise ValueError(f"price must be > 0, got {price!r}")
    if f <= 0:
        raise ValueError(f"f must be > 0, got {f!r}")
    if k <= 0:
        raise ValueError(f"k must be > 0, got {k!r}")
    if not (0 < per_name_cap_frac <= 1):
        raise ValueError(
            f"per_name_cap_frac must be in (0, 1], got {per_name_cap_frac!r}"
        )

    # --- core computation ---
    risk_per_share = k * atr
    dollars_at_risk = f * equity

    raw_shares = dollars_at_risk / risk_per_share
    raw_notional = raw_shares * price

    cap_notional = per_name_cap_frac * equity

    if raw_notional > cap_notional:
        notional = cap_notional
        shares = cap_notional / price
        capped = True
    else:
        notional = raw_notional
        shares = raw_shares
        capped = False

    if not fractional:
        shares = math.floor(shares)
        notional = shares * price

    return {
        "shares": shares,
        "notional": notional,
        "risk_per_share": risk_per_share,
        "dollars_at_risk": dollars_at_risk,
        "capped": capped,
    }
