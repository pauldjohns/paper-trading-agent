# src/autotrader/strategies.py
"""Deterministic strategy rule modules (spec §2). Each strategy emits a CAUSAL target-weight
DataFrame (date x symbols, weights in [0,1]; cash = 1 - row.sum) that the Plan 04 engine
executes at the NEXT open. Stateful rules are computed by a causal forward scan inside
target_weights, so the frame never looks ahead. Strategy = intent; engine = execution.
"""
import pandas as pd
from autotrader.indicators import (sma, nearness_to_high, wilder_rsi, cumulative_rsi,
                                    trailing_return, monthly_closes, align_monthly_to_daily)


def _ref_dates(bars, anchor):
    """Reference daily date axis = the anchor symbol's bar dates. The Plan 04 engine MUST pass a
    bars dict whose every symbol is aligned to one shared NYSE-calendar date axis; this validates
    that contract and raises loudly on any misalignment, so a shorter-history symbol can never
    silently shift the position-indexed weight frame."""
    dates = list(bars[anchor]["date"])
    for s, df in bars.items():
        if list(df["date"]) != dates:
            raise ValueError(f"{s} bars are not aligned to the {anchor} date axis "
                             "(the engine must pass a calendar-aligned bars dict)")
    return dates


def select_with_hysteresis(order, held, n_hold, buffer):
    """Pick the N-name portfolio with rank hysteresis. `order` = symbols best-ranked first.
    Keep currently-held names still within rank <= N+buffer (priority, best-ranked first),
    then fill remaining slots with the best-ranked fresh names. A held name is sold only once
    it falls below rank N+buffer. Returns a set."""
    rank_of = {s: i + 1 for i, s in enumerate(order)}
    keep = [s for s in order if s in held and rank_of[s] <= n_hold + buffer]
    if len(keep) >= n_hold:
        return set(keep[:n_hold])
    fill = [s for s in order if s not in keep][:n_hold - len(keep)]
    return set(keep) | set(fill)


def trend_regime(dates, closes, sma_months=10, band=0.01, use_abs_momentum=False, abs_lookback=12):
    """Shared Faber trend signal as a daily boolean Series (risk-on/off), forward-filled from
    monthly decisions. Monthly close vs the `sma_months`-month SMA of monthly closes, with a
    +/-band no-action zone (Siegel whipsaw filter -> hysteresis: hold the prior state inside
    the band). Optional Antonacci absolute-momentum AND-filter (trailing `abs_lookback`-month
    total return > 0). Causal: a month-end decision uses only that month's completed close,
    aligned to the daily axis by date <= d. Warm-up / pre-first-signal = OFF (out of market)."""
    mc = monthly_closes(dates, closes)
    m_sma = sma(mc["close"], sma_months)
    m_mom = trailing_return(mc["close"], abs_lookback, skip=0) if use_abs_momentum else None
    state = False
    monthly_state = []
    for i in range(len(mc)):
        s = m_sma.iloc[i]
        c = mc["close"].iloc[i]
        if pd.isna(s):
            monthly_state.append(False)
            continue
        if c >= s * (1 + band):
            state = True
        elif c <= s * (1 - band):
            state = False
        if use_abs_momentum:
            mom = m_mom.iloc[i]
            on = state and (not pd.isna(mom)) and (mom > 0)
        else:
            on = state
        monthly_state.append(on)
    daily = align_monthly_to_daily(dates, list(mc["date"]),
                                   [1.0 if s else 0.0 for s in monthly_state])
    return (daily == 1.0).reset_index(drop=True)


class S1Trend:
    """Faber MA-cross (+ optional Antonacci filter). Hold the equity when risk-on, the bond
    ETF when risk-off (spec §2 S1)."""
    def __init__(self, equity="SPY", bond="IEF", sma_months=10, band=0.01,
                 use_abs_momentum=False, stop_loss_pct=0.20):
        self.equity, self.bond = equity, bond
        self.sma_months, self.band = sma_months, band
        self.use_abs_momentum = use_abs_momentum
        self.stop_loss_pct = stop_loss_pct
        self.cost_strategy = None
        self.universe = [equity, bond]

    def target_weights(self, bars):
        dates = _ref_dates(bars, self.equity)
        on = trend_regime(dates, bars[self.equity]["close"], self.sma_months, self.band,
                          self.use_abs_momentum)
        w = pd.DataFrame(0.0, index=range(len(dates)), columns=self.universe)
        w[self.equity] = on.astype(float).values
        w[self.bond] = (~on).astype(float).values
        return w


class S2SectorMomentum:
    """Top-N sector SPDRs by 52-week-high nearness, equal-weight, with rank-buffer hysteresis,
    held only while the broad (gate_symbol) trend is on; else cash (spec §2 S2)."""
    def __init__(self, sectors, gate_symbol="SPY", n_hold=3, buffer=2, nearness_window=252,
                 gate_sma_months=10, gate_band=0.01, stop_loss_pct=0.20):
        self.sectors = list(sectors)
        self.gate_symbol = gate_symbol
        self.n_hold, self.buffer = n_hold, buffer
        self.nearness_window = nearness_window
        self.gate_sma_months, self.gate_band = gate_sma_months, gate_band
        self.stop_loss_pct = stop_loss_pct
        self.cost_strategy = None
        self.universe = self.sectors + [gate_symbol]

    def target_weights(self, bars):
        dates = _ref_dates(bars, self.gate_symbol)
        n = len(dates)
        near = pd.DataFrame({s: nearness_to_high(bars[s]["close"], self.nearness_window).values
                             for s in self.sectors})
        gate_on = trend_regime(dates, bars[self.gate_symbol]["close"],
                               self.gate_sma_months, self.gate_band).values
        held = set()
        w = pd.DataFrame(0.0, index=range(n), columns=self.universe)
        for t in range(n):
            row_valid = near.iloc[t].dropna()
            if not gate_on[t] or row_valid.empty:
                held = set()
                continue
            order = list(row_valid.sort_values(ascending=False).index)   # best-ranked first
            held = select_with_hysteresis(order, held, self.n_hold, self.buffer)
            if held:
                wt = 1.0 / len(held)
                for s in held:
                    w.iloc[t, w.columns.get_loc(s)] = wt
        return w


class S3MeanReversion:
    """Short-term mean reversion null-test (spec §2 S3). Per-ETF: regime Close>SMA(regime_sma);
    enter on CumRSI(2,2)<cumrsi_entry; exit on RSI(2)>rsi_exit OR Close>SMA(exit_sma) OR after
    time_stop_days. Equal-weight across names in a position. Routed to the punitive S3 cost floor."""
    def __init__(self, etfs, regime_sma=200, exit_sma=5, cumrsi_entry=35.0, rsi_exit=65.0,
                 time_stop_days=10, stop_loss_pct=0.10):
        self.etfs = list(etfs)
        self.regime_sma, self.exit_sma = regime_sma, exit_sma
        self.cumrsi_entry, self.rsi_exit = cumrsi_entry, rsi_exit
        self.time_stop_days = time_stop_days
        self.stop_loss_pct = stop_loss_pct
        self.cost_strategy = "S3"
        self.universe = list(etfs)

    def target_weights(self, bars):
        dates = _ref_dates(bars, self.etfs[0])
        n = len(dates)
        sig = {}
        for s in self.etfs:
            c = bars[s]["close"]
            sig[s] = dict(close=c.reset_index(drop=True), sma_regime=sma(c, self.regime_sma),
                          sma_exit=sma(c, self.exit_sma), rsi=wilder_rsi(c), cumrsi=cumulative_rsi(c))
        in_pos = {s: None for s in self.etfs}
        flags = pd.DataFrame(0.0, index=range(n), columns=self.etfs)
        for t in range(n):
            for s in self.etfs:
                d = sig[s]
                close, sreg, sx = d["close"].iloc[t], d["sma_regime"].iloc[t], d["sma_exit"].iloc[t]
                rsi, cum = d["rsi"].iloc[t], d["cumrsi"].iloc[t]
                if in_pos[s] is None:
                    if (not pd.isna(sreg) and not pd.isna(cum) and close > sreg
                            and cum < self.cumrsi_entry):
                        in_pos[s] = t
                        flags.iloc[t, flags.columns.get_loc(s)] = 1.0
                else:
                    # regime (Close > SMA) is an ENTRY filter only (Connors); there is no
                    # regime-off exit — a held position leaves only on RSI/SMA5/time-stop.
                    held_days = t - in_pos[s]
                    exit_now = ((not pd.isna(rsi) and rsi > self.rsi_exit)
                                or (not pd.isna(sx) and close > sx)
                                or held_days >= self.time_stop_days)
                    if exit_now:
                        in_pos[s] = None
                    else:
                        flags.iloc[t, flags.columns.get_loc(s)] = 1.0
        counts = flags.sum(axis=1)
        return flags.div(counts.where(counts > 0, 1.0), axis=0)


class S4TrendGatedMomentum:
    """Trend-gated momentum (spec §2 S4 primary blend): S1's trend regime decides risk-on/off;
    risk-on hold the S2 sector basket, risk-off hold the bond ETF. The S2 basket is gated by the
    same trend on `equity`, so the sleeves share one regime signal."""
    def __init__(self, sectors, equity="SPY", bond="IEF", n_hold=3, buffer=2, nearness_window=252,
                 sma_months=10, band=0.01, stop_loss_pct=0.20):
        self.s2 = S2SectorMomentum(sectors, gate_symbol=equity, n_hold=n_hold, buffer=buffer,
                                   nearness_window=nearness_window, gate_sma_months=sma_months,
                                   gate_band=band, stop_loss_pct=stop_loss_pct)
        self.equity, self.bond = equity, bond
        self.sma_months, self.band = sma_months, band
        self.stop_loss_pct = stop_loss_pct
        self.cost_strategy = None
        self.universe = list(sectors) + [equity, bond]

    def target_weights(self, bars):
        dates = _ref_dates(bars, self.equity)
        on = trend_regime(dates, bars[self.equity]["close"], self.sma_months, self.band)
        basket = self.s2.target_weights(bars)
        w = pd.DataFrame(0.0, index=range(len(dates)), columns=self.universe)
        for s in self.s2.sectors:
            w[s] = basket[s].values
        w[self.bond] = (~on).astype(float).values
        return w
