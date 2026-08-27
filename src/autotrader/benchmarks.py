# src/autotrader/benchmarks.py
"""Price-return benchmarks (spec §3.10). Three are weight-frame strategies run through the SAME
engine for identical cost treatment: BuyHold (a), GatedSPY (b, the primary for S2/blend),
EqualWeightUniverse (d). 60/40 (c) is a constant-mix object the all-or-nothing engine cannot
express, so sixty_forty_returns computes it analytically (D4) — but charges its monthly rebalance
with the SAME effective_roundtrip_cost(tier, stress) model every other path uses, so it is not an
optimistically-cheap bar in the stress folds that decide the §3.10c comparison."""
import pandas as pd
from autotrader import config
from autotrader.strategies import trend_regime
from autotrader.indicators import monthly_closes
from autotrader.costs import effective_roundtrip_cost
from autotrader.stress import causal_stress_series


class BuyHold:
    """Buy-and-hold one symbol (price-return, net of the single entry cost via the engine)."""
    def __init__(self, symbol="SPY"):
        self.symbol = symbol
        self.universe = [symbol]
        self.stop_loss_pct = None
        self.cost_strategy = None

    def target_weights(self, bars):
        n = len(bars[self.symbol])
        return pd.DataFrame({self.symbol: [1.0] * n})


class GatedSPY:
    """SPY when the broad trend is on, cash when off (spec §3.10b — isolates selection from the
    trend filter). Same trend basis as S1/S2/S4."""
    def __init__(self, symbol="SPY", sma_months=10, band=0.01):
        self.symbol = symbol
        self.sma_months, self.band = sma_months, band
        self.universe = [symbol]
        self.stop_loss_pct = None
        self.cost_strategy = None

    def target_weights(self, bars):
        df = bars[self.symbol]
        on = trend_regime(list(df["date"]), df["close"], self.sma_months, self.band)
        return pd.DataFrame({self.symbol: on.astype(float).values})


class EqualWeightUniverse:
    """Equal-weight buy-hold of a symbol set (spec §3.10d — separates selection skill from universe
    drift). Each name holds 1/k every row; the engine sizes entries at 1/k * equity."""
    def __init__(self, symbols):
        self.symbols = list(symbols)
        self.universe = list(symbols)
        self.stop_loss_pct = None
        self.cost_strategy = None

    def target_weights(self, bars):
        n = len(bars[self.symbols[0]])
        k = len(self.symbols)
        return pd.DataFrame({s: [1.0 / k] * n for s in self.symbols})


def sixty_forty_returns(bars, equity="SPY", bond="IEF", w_equity=0.60, w_bond=0.40,
                        rebalance="ME", charge_rebalance_cost=True) -> pd.Series:
    """Analytic constant-mix 60/40 daily return series, rebalanced on the last trading day of each
    period (`rebalance="ME"` = month-end). The rebalance cost is the **same per-tier×stress model**
    the engine uses: each leg's drift back to target crosses a half round-trip at the index-ETF tier
    scaled by that day's causal CS-ratio stress (not a flat bps — review S4). Price-return; the bond
    coupon is omitted (spec §3.1 documented limitation). Returns a date-indexed daily-return Series."""
    e_df, b_df = bars[equity], bars[bond]
    dates = list(e_df["date"])
    ce = e_df["close"].reset_index(drop=True)
    cb = b_df["close"].reset_index(drop=True)
    re = ce.pct_change().fillna(0.0).to_numpy()
    rb = cb.pct_change().fillna(0.0).to_numpy()
    se = causal_stress_series(e_df, config.TIER_INDEX_ETF).to_numpy()   # SPY/IEF are index-ETF tier
    sb = causal_stress_series(b_df, config.TIER_INDEX_ETF).to_numpy()
    me = set(monthly_closes(dates, list(ce))["date"])
    we, wb = w_equity, w_bond
    out = []
    for i, d in enumerate(dates):
        port_r = we * re[i] + wb * rb[i]
        ve, vb = we * (1 + re[i]), wb * (1 + rb[i])    # drift the weights with realized sleeve returns
        we, wb = ve / (ve + vb), vb / (ve + vb)
        if d in me:                                    # rebalance back to target, charge tier×stress cost
            if charge_rebalance_cost:
                traded = abs(we - w_equity)            # == abs(wb - w_bond); sell one leg, buy the other
                cost = traded * (effective_roundtrip_cost(config.TIER_INDEX_ETF, None, se[i]) / 2
                                 + effective_roundtrip_cost(config.TIER_INDEX_ETF, None, sb[i]) / 2)
                port_r -= cost
            we, wb = w_equity, w_bond
        out.append(port_r)
    return pd.Series(out, index=pd.Index(dates, name="date"), dtype="float64")
