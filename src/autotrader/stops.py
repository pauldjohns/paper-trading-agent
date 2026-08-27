# src/autotrader/stops.py
"""Daily-bar stop-fill modeling for protective SELL stops (spec section 3.5).

- Gap-through (open <= stop): fill at the OPEN (worse than stop; the gapped open already
  embeds the adverse move, so no extra slippage is layered on).
- Intrabar pierce (low <= stop < open): fill at stop_price minus slippage (a stop_market
  becomes a market sell on trigger).
- No touch (low > stop): no fill (None).
A SELL stop never fills ABOVE the stop, so this never fabricates downside protection.
"""


def stop_fill_price(bar: dict, stop_price: float, slippage_frac: float):
    if bar["open"] <= stop_price:
        return bar["open"]
    if bar["low"] <= stop_price:
        return stop_price * (1 - slippage_frac)
    return None
