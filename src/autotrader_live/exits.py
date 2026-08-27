# src/autotrader_live/exits.py
"""Catastrophe floor + chandelier ratchet exit logic.

Pure math — no pandas or external dependencies.  The stop is monotonic-up:
it can only rise, never fall.
"""


def initial_catastrophe_stop(entry_price: float, atr_at_entry: float, m: float = 2.0) -> float:
    """Compute the initial catastrophe stop price.

    stop = entry_price - m * atr_at_entry

    Parameters
    ----------
    entry_price:
        Price at which the position was entered.  Must be > 0.
    atr_at_entry:
        ATR at the time of entry.  Must be > 0.
    m:
        ATR multiplier (default 2.0).  Must be > 0.

    Returns
    -------
    float — stop price.

    Raises
    ------
    ValueError
        If any input is non-positive, or if the resulting stop would be <= 0
        (ATR too wide relative to entry price).
    """
    if entry_price <= 0:
        raise ValueError(f"entry_price must be > 0, got {entry_price!r}")
    if atr_at_entry <= 0:
        raise ValueError(f"atr_at_entry must be > 0, got {atr_at_entry!r}")
    if m <= 0:
        raise ValueError(f"m must be > 0, got {m!r}")

    stop = entry_price - m * atr_at_entry
    if stop <= 0:
        raise ValueError(
            f"Computed stop ({stop}) is not > 0 — ATR too wide for the price. "
            f"entry_price={entry_price}, atr_at_entry={atr_at_entry}, m={m}"
        )
    return stop


def chandelier_level(
    highest_high_since_entry: float, atr_current: float, k: float = 3.0
) -> float:
    """Compute the chandelier exit level.

    level = highest_high_since_entry - k * atr_current

    Parameters
    ----------
    highest_high_since_entry:
        Highest high recorded since position entry.  Must be > 0.
    atr_current:
        Current ATR.  Must be > 0.
    k:
        ATR multiplier (default 3.0).  Must be > 0.

    Returns
    -------
    float — chandelier level (may be negative if k*ATR > highest high).
    """
    if highest_high_since_entry <= 0:
        raise ValueError(
            f"highest_high_since_entry must be > 0, got {highest_high_since_entry!r}"
        )
    if atr_current <= 0:
        raise ValueError(f"atr_current must be > 0, got {atr_current!r}")
    if k <= 0:
        raise ValueError(f"k must be > 0, got {k!r}")

    return highest_high_since_entry - k * atr_current


def ratchet_stop(prev_stop: float, new_level: float) -> float:
    """Enforce the monotonic-up invariant: the stop can never decrease.

    Returns max(prev_stop, new_level).  No input guards — pure max of two floats.
    NaN is order-dependent in Python's max() (max(nan, x)->nan, max(x, nan)->x);
    callers must supply valid floats. In the live path prev_stop is always seeded
    by initial_catastrophe_stop() (validated), so NaN cannot enter normally.
    """
    return max(prev_stop, new_level)


def update_trailing_stop(
    prev_stop: float,
    highest_high_since_entry: float,
    atr_current: float,
    k: float = 3.0,
) -> float:
    """Daily-close ratchet update: compute chandelier then apply monotonic-up guard.

    Equivalent to ratchet_stop(prev_stop, chandelier_level(highest_high_since_entry,
    atr_current, k)).

    Parameters
    ----------
    prev_stop:
        Current stop price.
    highest_high_since_entry:
        Highest high recorded since position entry.
    atr_current:
        Current ATR.
    k:
        ATR multiplier for the chandelier (default 3.0).

    Returns
    -------
    float — updated (potentially higher) stop price.
    """
    return ratchet_stop(prev_stop, chandelier_level(highest_high_since_entry, atr_current, k))
