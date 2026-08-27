# src/autotrader/placebo.py
"""Random-selection placebo (spec §4): the 'is the selection skill real?' control. RandomSelection
mirrors the gated sector-momentum strategies EXACTLY except that, each rebalance month, it picks
n_hold sectors at RANDOM (seeded) instead of the momentum top-N — same gate, same warm-up, same
off-asset. 1,000 seeded draws give the null distribution; a real strategy must beat its 95th pct.
Offline + deterministic. Reuses the locked trend_regime; never modifies a locked module."""
import numpy as np
import pandas as pd
from autotrader.strategies import trend_regime, select_with_hysteresis


class RandomSelection:
    """Gated random-N sector basket — a FAIR placebo: same interface, same trend gate, same warm-up,
    same off-asset, AND the same rank-hysteresis as S2/S4 (so its turnover/cost match — review fix:
    a no-hysteresis placebo churns far more, so beating it would conflate selection skill with the
    real strategy's lower churn). Each rebalance MONTH it draws a seeded random permutation of the
    sectors as the 'rank', then runs the locked `select_with_hysteresis` daily over it. Gate path
    mirrors S4: gate off -> off_asset (bonds; cash if off_asset=None); gate-on-but-warming -> cash;
    warmed -> the hysteresis-selected random basket."""
    def __init__(self, sectors, gate_symbol="SPY", n_hold=3, buffer=2, seed=0, gate_sma_months=10,
                 gate_band=0.01, warmup=252, off_asset=None):
        self.sectors = list(sectors)
        self.gate_symbol = gate_symbol
        self.n_hold, self.buffer, self.seed = n_hold, buffer, seed
        self.gate_sma_months, self.gate_band = gate_sma_months, gate_band
        self.warmup, self.off_asset = warmup, off_asset
        self.stop_loss_pct = None
        self.cost_strategy = None
        self.universe = self.sectors + [gate_symbol] + ([off_asset] if off_asset else [])

    def target_weights(self, bars):
        dates = list(bars[self.gate_symbol]["date"])
        n = len(dates)
        gate_on = trend_regime(dates, bars[self.gate_symbol]["close"],
                               self.gate_sma_months, self.gate_band).values
        ym = [d.year * 12 + d.month for d in dates]
        rng = np.random.default_rng(self.seed)
        held, cur_month, order = set(), None, None
        w = pd.DataFrame(0.0, index=range(n), columns=self.universe)
        for t in range(n):
            if not gate_on[t]:                              # gate off -> off-asset (bonds) or cash; flat basket
                held = set()
                if self.off_asset:
                    w.iloc[t, w.columns.get_loc(self.off_asset)] = 1.0
                continue
            if t < self.warmup:                             # gate on but warming -> cash (mirrors empty basket)
                held = set()
                continue
            if ym[t] != cur_month:                          # new rebalance month -> fresh random 'rank'
                cur_month = ym[t]
                order = list(rng.permutation(self.sectors))
            held = select_with_hysteresis(order, held, self.n_hold, self.buffer)   # daily, like S2/S4
            wt = 1.0 / len(held)
            for s in held:
                w.iloc[t, w.columns.get_loc(s)] = wt
        return w


from autotrader.engine import BacktestEngine, build_engine_inputs, tier_for_symbol
from autotrader.stress import causal_stress_series
from autotrader import report


def precompute_stress(raw_bars, universe):
    """Build the engine's per-symbol causal CS-ratio stress ONCE for a universe and return it as a
    `stress(symbol, t)` callable. ~74% of an engine run is this recompute (review E6); all placebo
    seeds share one raw_bars/universe, so computing it once and passing it via the engine's `stress=`
    param is a ~3.8x speedup (equity identical to 1e-6 vs the default per-run recompute)."""
    aligned, _, _ = build_engine_inputs(raw_bars, universe)
    series = {s: causal_stress_series(aligned[s], tier_for_symbol(s)) for s in universe}
    return lambda sym, t, _ss=series: float(_ss[sym].iloc[t])


def placebo_distribution(sectors, gate_symbol, n_hold, raw_bars, buffer=2, n_placebo=1000,
                         gate_sma_months=10, gate_band=0.01, warmup=252, off_asset=None,
                         seed_base=0, initial_cash=1000.0) -> np.ndarray:
    """Net Sharpe of `n_placebo` seeded RandomSelection runs through the engine (the runner's bulk).
    Precomputes the shared stress ONCE (E6). Uses `summarize_run(..., trim_warmup=False)` so an
    all-cash placebo (gate off all window) yields Sharpe 0.0 instead of crashing (review B2).
    Seeds seed_base..seed_base+n_placebo-1 -> reproducible. n_placebo is a parameter (default 1000)."""
    universe = list(sectors) + [gate_symbol] + ([off_asset] if off_asset else [])
    stress_fn = precompute_stress(raw_bars, universe)
    out = np.empty(n_placebo)
    for i in range(n_placebo):
        rs = RandomSelection(sectors, gate_symbol=gate_symbol, n_hold=n_hold, buffer=buffer,
                             seed=seed_base + i, gate_sma_months=gate_sma_months, gate_band=gate_band,
                             warmup=warmup, off_asset=off_asset)
        res = BacktestEngine(rs, raw_bars, initial_cash=initial_cash, stress=stress_fn).run()
        out[i] = report.summarize_run(res, trim_warmup=False)["sharpe"]
    return out


def placebo_95th(distribution) -> float:
    return float(np.quantile(np.asarray(distribution, dtype="float64"), 0.95))


def beats_placebo(strategy_sharpe: float, distribution) -> bool:
    """The §4 placebo condition: the strategy's net Sharpe beats the 95th percentile of the null."""
    return bool(strategy_sharpe > placebo_95th(distribution))
