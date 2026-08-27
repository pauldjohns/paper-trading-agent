# src/autotrader/engine.py
"""Offline walk-forward backtest engine: drives a strategy's causal target-weight frame through
the locked Simulator (next-open fills, stop-first daily-bar stops, shared T+1 ledger, per-trade
cost tiers + S3 floor) and emits a daily mark-to-market equity curve + a trade log.

The Simulator is ALL-OR-NOTHING per symbol, so this engine is event-driven on holdings-set
changes (D1): enter/exit whole positions, tolerate drift between membership changes. Strategy =
intent; engine = execution. Offline only — never calls the MCP, never places a real order.
"""
import datetime as dt
from dataclasses import dataclass, field
from typing import Optional
import pandas as pd
from autotrader import config
from autotrader.calendar_nyse import TradingCalendar
from autotrader.simulator import Simulator

_RUNWAY_DAYS = 2   # synthetic trailing trading days so a last-real-bar sale can settle (D5)

_MIN_TRADE = 1.0   # dollars; a buy fundable for less than this is a T+1 throttle, logged not executed

_INDEX_TIER_SYMBOLS = set(config.INDEX_ETFS) | set(config.BOND_ETFS)   # SPY/QQQ/DIA/IWM + IEF/AGG
_SECTOR_TIER_SYMBOLS = set(config.SECTOR_SPDRS)


def tier_for_symbol(symbol: str) -> str:
    """Instrument liquidity tier for a symbol in the Plan-04 (survivorship-clean) universe.
    Bond ETFs (IEF/AGG) are very liquid -> index-ETF tier. Unknown symbols raise: single names
    are non-gating and out of scope for the engine (spec §3.7)."""
    if symbol in _INDEX_TIER_SYMBOLS:
        return config.TIER_INDEX_ETF
    if symbol in _SECTOR_TIER_SYMBOLS:
        return config.TIER_SECTOR_SPDR
    raise ValueError(f"no tier for {symbol!r}: not in the Plan-04 ETF/sector universe "
                     f"(single names are non-gating, spec §3.7)")


def cost_floor_for_strategy(cost_strategy: Optional[str]) -> Optional[float]:
    """Resolve a punitive cost floor (config.STRATEGY_COST_FLOORS) for a cost-strategy LABEL. Only
    S3 carries one; everything else returns None (instrument tier alone). The engine calls this
    PER SYMBOL (Task 5) via a `cost_strategy_for(symbol)` map, so a multi-sleeve strategy (the
    blend, Task 14) can charge the S3 floor on its MR sleeve only — the floor cannot be dropped."""
    if cost_strategy is None:
        return None
    return config.STRATEGY_COST_FLOORS.get(cost_strategy)


def build_engine_inputs(raw_bars: dict, universe: list):
    """From a raw {symbol: position-indexed OHLCV DataFrame} dict, build the two views the run
    needs, restricted to `universe` (Engine contract #2 load-set):
      aligned: {symbol: DataFrame} all sharing the anchor symbol's date axis (validated);
      nested:  {symbol: {date: {open,high,low,close}}} for the Simulator;
      calendar: TradingCalendar over the real dates + a 2-trading-day synthetic settlement runway.
    Raises if any requested symbol's date axis differs from the anchor (no silent shift)."""
    aligned = {s: raw_bars[s].reset_index(drop=True) for s in universe}
    anchor = universe[0]
    axis = list(aligned[anchor]["date"])
    for s in universe:
        if list(aligned[s]["date"]) != axis:
            raise ValueError(f"{s} date axis differs from {anchor}; the engine requires one shared "
                             "calendar-aligned axis (Engine contract #1)")
    nested = {}
    for s in universe:
        df = aligned[s]
        nested[s] = {row.date: {"open": row.open, "high": row.high,
                                "low": row.low, "close": row.close}
                     for row in df.itertuples(index=False)}
    days = list(axis)
    cur = days[-1]
    for _ in range(_RUNWAY_DAYS):                 # append calendar-only runway dates (never given bars)
        cur = cur + dt.timedelta(days=1)
        while cur.weekday() >= 5:                  # skip Sat/Sun so runway dates look like trading days
            cur = cur + dt.timedelta(days=1)
        days.append(cur)
    return aligned, nested, TradingCalendar(days)


def plan_rebalance(held: set, weights: dict, equity: float):
    """All-or-nothing reconciliation of held positions against a target weight row (D1).
    Returns (sells, buys): `sells` = sorted list of held symbols whose target weight is ~0;
    `buys` = list of (symbol, dollars) for target>0 symbols NOT currently held, sized at
    weight*equity, ordered by descending target weight then symbol (deterministic). Held names
    that remain in the target set are left untouched (drift tolerated). Zero/absent weights never
    trade (Engine contract #2)."""
    target = {s: w for s, w in weights.items() if w > 1e-12}
    sells = sorted(s for s in held if s not in target)
    fresh = [(s, w) for s, w in target.items() if s not in held]
    fresh.sort(key=lambda sw: (-sw[1], sw[0]))
    buys = [(s, w * equity) for s, w in fresh]
    return sells, buys


@dataclass
class Trade:
    symbol: str
    entry_date: dt.date; entry_price: float; shares: float; dollars: float; entry_cost: float
    exit_date: dt.date; exit_price: float; proceeds: float; exit_cost: float
    exit_reason: str          # "signal" | "stop" | "terminal"
    pnl: float; ret: float


@dataclass
class BacktestResult:
    equity: pd.Series                       # date-indexed daily mark-to-market total equity
    returns: pd.Series                      # daily simple returns of equity
    trades: list                            # list[Trade] (incl. terminal mark-to-close positions)
    weights: pd.DataFrame                   # date x symbol realized (held) weights
    skipped_buys: list                      # [{date, symbol, target_dollars, reason}] — genuine T+1 throttles
    dates: list


class BacktestEngine:
    """Drive one strategy's causal weight frame through the locked Simulator. Offline, deterministic.
    `stress`: None -> the default causal CS-ratio model per symbol (D2); a float -> constant (goldens);
    a callable(symbol, t)->float -> custom. The strategy may expose `cost_strategy_for(symbol)` for
    per-symbol cost routing (the blend); otherwise every symbol maps to its single `.cost_strategy`."""
    def __init__(self, strategy, raw_bars: dict, initial_cash: float = 1000.0,
                 slippage_frac: float = 0.0, stress=None):
        self.strategy = strategy
        self.initial_cash = initial_cash
        self.slippage_frac = slippage_frac
        self.aligned, self.nested, self.calendar = build_engine_inputs(raw_bars, strategy.universe)
        if stress is None:                                   # default D2 model, per symbol at its tier
            from autotrader.stress import causal_stress_series
            ss = {s: causal_stress_series(self.aligned[s], tier_for_symbol(s)) for s in strategy.universe}
            self.stress = lambda s, t, _ss=ss: float(_ss[s].iloc[t])
        else:
            self.stress = stress
        cs_for = getattr(strategy, "cost_strategy_for", None)        # per-symbol cost-strategy map
        self.cost_strategy_for = cs_for or (lambda s, _c=getattr(strategy, "cost_strategy", None): _c)
        self._held_for_stress = []

    def _stress(self, symbol, t):
        return self.stress(symbol, t) if callable(self.stress) else float(self.stress)

    def _stress_max(self, t):
        """A resting stop fires intrabar; the Simulator applies ONE stress to every stop fill that
        bar, so the engine passes the MAX stress across currently-held names — a deliberate
        conservative (worst-sleeve cost) choice, pinned here (review finding, not hand-waved)."""
        if not callable(self.stress):
            return float(self.stress)
        return max((self._stress(s, t) for s in self._held_for_stress), default=1.0)

    def run(self) -> BacktestResult:
        strat = self.strategy
        dates = list(self.aligned[strat.universe[0]]["date"])
        n = len(dates)
        close = {s: list(self.aligned[s]["close"]) for s in strat.universe}
        W = strat.target_weights({s: self.aligned[s] for s in strat.universe}).reset_index(drop=True)

        sim = Simulator(calendar=self.calendar, bars=self.nested, slippage_frac=self.slippage_frac)
        sim.deposit(self.initial_cash, on=dates[0])

        cash = self.initial_cash
        holdings = {}    # symbol -> dict(shares, dollars, entry_date, entry_price, entry_cost)
        trades, skipped = [], []
        equity_rows, weight_rows = [], []

        def mark_equity(t):
            return cash + sum(h["shares"] * close[s][t] for s, h in holdings.items())

        for t in range(n):
            date = dates[t]
            is_last = (t == n - 1)
            self._held_for_stress = list(holdings)               # for _stress_max, before evaluate_stops
            # 1) STOP-FIRST (Engine contract #3). Needs a next day to settle -> skip on the last bar.
            if not is_last:
                for fill in sim.evaluate_stops(date, stress=self._stress_max(t)):
                    cash += fill.proceeds
                    h = holdings.pop(fill.symbol)
                    trades.append(self._close_trade(h, fill.date, fill.price, fill.proceeds,
                                                    fill.cost, "stop"))
            # 2) mark-to-market at this bar's close (after stops)
            eq = mark_equity(t)
            equity_rows.append((date, eq))
            weight_rows.append({s: (holdings[s]["shares"] * close[s][t] / eq if s in holdings else 0.0)
                                for s in strat.universe})
            if is_last:
                break
            # 3) reconcile weights -> next-open execution (signal exits first, then capped entries)
            row = {s: float(W[s].iloc[t]) for s in W.columns}
            sells, buys = plan_rebalance(set(holdings), row, eq)
            for s in sells:
                fill = sim.submit_sell(s, signal_date=date, stress=self._stress(s, t))
                cash += fill.proceeds
                h = holdings.pop(s)
                trades.append(self._close_trade(h, fill.date, fill.price, fill.proceeds,
                                                fill.cost, "signal"))
            fill_date = self.calendar.next_trading_day(date)
            for s, target_dollars in buys:
                # D1 settled-cash cap: deploy what's actually settled for the fill date; a near-zero
                # fundable amount is a genuine T+1 rotation throttle -> log it (spec §3.6), don't carry.
                dollars = min(target_dollars, sim.ledger.settled_cash(fill_date))
                if dollars < _MIN_TRADE:
                    skipped.append({"date": str(date), "symbol": s,
                                    "target_dollars": round(target_dollars, 6),
                                    "reason": "insufficient settled cash (T+1 throttle)"})
                    continue
                floor = cost_floor_for_strategy(self.cost_strategy_for(s))   # per-symbol (S3 floor on MR)
                buy = sim.submit_buy(s, signal_date=date, dollar_amount=dollars,
                                     tier=tier_for_symbol(s), cost_floor=floor, stress=self._stress(s, t))
                cash -= dollars
                holdings[s] = {"symbol": s, "shares": buy.shares, "dollars": dollars,
                               "entry_date": buy.date, "entry_price": buy.price, "entry_cost": buy.cost}
                if getattr(strat, "stop_loss_pct", None) is not None:
                    sim.place_stop(s, buy.price * (1 - strat.stop_loss_pct))

        # cash-book reconciliation invariant: the engine's marking scalar must equal the ledger total
        # (settled + unsettled). Two books, one truth — assert they never silently diverge (review B1).
        ledger_total = sum(tr.amount for tr in sim.ledger._tranches)
        assert abs(cash - ledger_total) < 1e-6, f"cash {cash} != ledger {ledger_total}"

        # terminal mark-to-close (D5): value any still-open position at the last close, emit as a
        # "terminal" trade (no exit cost) so trade-based metrics + turnover include the final bet.
        last = n - 1
        for s in list(holdings):
            h = holdings.pop(s)
            px = close[s][last]
            trades.append(self._close_trade(h, dates[last], px, h["shares"] * px, 0.0, "terminal"))

        idx = pd.Index([d for d, _ in equity_rows], name="date")
        equity = pd.Series([v for _, v in equity_rows], index=idx, dtype="float64")
        weights = pd.DataFrame(weight_rows, index=idx)
        return BacktestResult(equity=equity, returns=equity.pct_change(), trades=trades,
                              weights=weights, skipped_buys=skipped, dates=dates)

    def _close_trade(self, h, exit_date, exit_price, proceeds, exit_cost, reason) -> Trade:
        pnl = proceeds - h["dollars"]
        return Trade(symbol=h["symbol"], entry_date=h["entry_date"], entry_price=h["entry_price"],
                     shares=h["shares"], dollars=h["dollars"], entry_cost=h["entry_cost"],
                     exit_date=exit_date, exit_price=exit_price, proceeds=proceeds,
                     exit_cost=exit_cost, exit_reason=reason, pnl=pnl, ret=pnl / h["dollars"])


class CappedBudgetBlend:
    """3-way add-on test (spec §2 S4 / §3.6): primary blend at (1-mr_cap) risk + the S3 dip-buyer
    at a capped mr_cap budget, funded from the SHARED settled-cash pool (so the MR sleeve is
    settlement-throttled below standalone). target_weights = (1-mr_cap)*primary ⊕ mr_cap*mr,
    column-unioned; cash = 1 - row.sum. Per-symbol cost routing via `cost_strategy_for` (built into
    the engine in Task 1/5): MR-sleeve symbols are charged the S3 floor; the momentum sleeve is not."""
    def __init__(self, primary, mr, mr_cap=0.15):
        self.primary, self.mr, self.mr_cap = primary, mr, mr_cap
        self.universe = list(dict.fromkeys(list(primary.universe) + list(mr.universe)))
        self.stop_loss_pct = primary.stop_loss_pct
        self.cost_strategy = None                     # not used — see cost_strategy_for
        self._mr_symbols = set(mr.universe)

    def cost_strategy_for(self, symbol):
        """Per-symbol cost-strategy label: the S3 floor applies to the MR sleeve ONLY (spec §3.6 /
        COST_MODEL §4); the momentum sleeve keeps its instrument tier. The engine calls this per buy."""
        return "S3" if symbol in self._mr_symbols else None

    def target_weights(self, bars):
        wp = self.primary.target_weights({s: bars[s] for s in self.primary.universe})
        wm = self.mr.target_weights({s: bars[s] for s in self.mr.universe})
        n = len(wp)
        w = pd.DataFrame(0.0, index=range(n), columns=self.universe)
        for c in self.primary.universe:
            if c in wp.columns:
                w[c] = w[c].values + (1.0 - self.mr_cap) * wp[c].values
        for c in self.mr.universe:
            if c in wm.columns:
                w[c] = w[c].values + self.mr_cap * wm[c].values
        return w
