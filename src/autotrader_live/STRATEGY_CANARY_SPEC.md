# LIVE-01 canary spec — pinned math & strategy semantics

_The written oracle for the Phase-1 canary (T1.0 correctness gate + T1.5 golden). Every
formula here is deterministic and hand-workable. The plan (`IMPLEMENTATION_PLAN_LIVE_01_paper_monitor.md`
§1) gives the strategy contract; this doc pins the exact definitions the plan delegated to T1.0._

**Shared conventions (inherited from `autotrader.indicators`):** all series outputs are
**position-indexed** (`RangeIndex 0..n-1`), aligned 1:1 to input order; **NaN during warm-up**;
**no look-ahead** (output at `t` uses only inputs at positions `≤ t`); bad params raise
`ValueError`. Multi-series functions require equal-length inputs (else `ValueError`). Inputs are
coerced to `float64` with a fresh `RangeIndex` (a local `_as_series`, mirroring
`autotrader.indicators._as_series` — do NOT import the private helper across the firewall).
All bars are split-adjusted (price-return) daily OHLCV from `DataStore.load(sym,"day","split")`,
columns `[date, open, high, low, close, volume]`, `date` = `datetime.date`, ascending.

---

## 1. `indicators_ohlc.py` (T1.1) — unambiguous, no strategy judgment

### `true_range(high, low, close) -> pd.Series`
Wilder (1978) True Range.
- `TR[0] = high[0] - low[0]`  (no prior close on bar 0).
- `TR[t] = max( high[t]-low[t], abs(high[t]-close[t-1]), abs(low[t]-close[t-1]) )` for `t≥1`.
- No NaN (TR is defined for every bar given ≥1 bar).

### `atr(high, low, close, period=14) -> pd.Series`  (Wilder ATR)
- `tr = true_range(high, low, close)`.
- NaN for index `< period-1`.
- **Seed:** `ATR[period-1] = mean(tr[0 : period])`  (simple average of the first `period` TRs).
- **Smooth:** `ATR[t] = (ATR[t-1]*(period-1) + tr[t]) / period` for `t ≥ period`.
- `period < 1 → ValueError`. `n < period → all NaN`. (Note: first valid index is `period-1`,
  NOT `period` as in `wilder_rsi`, because `TR[0]` needs no prior close — documented divergence.)

### `donchian(high, low, window) -> pd.DataFrame[upper, lower]`
- `upper[t] = max(high[t-window+1 .. t])`, `lower[t] = min(low[t-window+1 .. t])` (inclusive of `t`).
- NaN until `window` bars are available. `window < 1 → ValueError`.
- The channel **includes** bar `t`. A breakout that must EXCLUDE today shifts by 1 (see §2).

### `ema(prices, period) -> pd.Series`  (SMA-seeded EMA)
- `alpha = 2/(period+1)`.
- NaN for index `< period-1`. **Seed:** `EMA[period-1] = mean(prices[0 : period])`.
- `EMA[t] = alpha*prices[t] + (1-alpha)*EMA[t-1]` for `t ≥ period`. `period < 1 → ValueError`.
- _Provided per the plan's module list; NOT consumed by the current `strategy_trend` contract
  (which uses SMA200). Available for future trend variants. Built + tested for completeness._

---

## 2. `strategy_trend.py` (T1.2) — the today-decision (one decision row for the settled bar `t = n-1`)

Reuses `autotrader.indicators.{sma, rolling_high, nearness_to_high, trailing_return}` (close-series,
already trusted) + `indicators_ohlc.{atr, donchian}`. Computes on the LAST row `t`, which the caller
guarantees is the completed/settled signal bar.

- `trend_ok   = close[t] > sma(close, 200)[t]`
- `momentum_ok= trailing_return(close, 252, skip=0)[t] > 0`
- `nearness   = nearness_to_high(close, 252)[t]`  (close / trailing-252 **closing** high, in (0,1])
- `near_high  = nearness ≥ near_threshold`            ⟵ **[FLAG A] near_threshold = 0.90 (within 10%) — RATIFIED the operator 2026-06-22**
- `prior_donch_upper = donchian(high, low, 55).upper.shift(1)[t]`  (max high over the 55 bars
  **before** `t`, excluding `t`)
- `breakout_55 = close[t] > prior_donch_upper`        ⟵ **[FLAG B] close-based, excludes today**
- **`entry = trend_ok AND momentum_ok AND (near_high OR breakout_55)`**
- Also emit `atr14 = atr(high, low, close, 14)[t]` for sizing/exits.

**Burn-in:** needs `max(200, 252, 55+1) = 253` bars for all sub-signals non-NaN. If any sub-signal at
`t` is NaN → decision is `reason="insufficient_history"`, `entry=False`.

Returns a decision record (dataclass or dict):
`{signal_date, symbol, close, sma200, trend_ok, mom_252, momentum_ok, nearness, near_high,
prior_donch_upper, breakout_55, atr14, entry, reason}`.

---

## 3. `sizing.py` (T1.3) — volatility-targeted fixed-fractional risk

`size(equity, atr, price, f=0.01, k=3.0, per_name_cap_frac=0.15, fractional=True) -> dict`
- `risk_per_share = k * atr`
- `dollars_at_risk = f * equity`
- `raw_shares = dollars_at_risk / risk_per_share`;  `raw_notional = raw_shares * price`
- `cap_notional = per_name_cap_frac * equity`
- if `raw_notional > cap_notional`: `notional = cap_notional`, `shares = cap_notional/price`, `capped=True`
  else `notional = raw_notional`, `shares = raw_shares`, `capped=False`
- if `not fractional`: `shares = floor(shares)`, `notional = shares*price`
- Guards (`ValueError`): `equity>0`, `atr>0`, `price>0`, `f>0`, `k>0`, `0 < per_name_cap_frac ≤ 1`.
- Returns `{shares, notional, risk_per_share, dollars_at_risk, capped}`.
- **[FLAG C]** defaults `f=1%`, `k=3.0`, `cap=15%` — point picks inside the plan §5 ranges
  (`f`=0.5–1%, `k`=2.5–3×ATR). With `N`=8–12 names a 15% cap is slightly loose vs `1/N`.

---

## 4. `exits.py` (T1.4) — catastrophe floor + chandelier ratchet (monotonic-up)

- `initial_catastrophe_stop(entry_price, atr_at_entry, m=2.0) -> float`
  = `entry_price - m*atr_at_entry`.  Guards: `entry_price>0`, `atr_at_entry>0`, `m>0`, result `>0`
  (else `ValueError` — ATR too wide for the price). **[FLAG D] m=2.0** (plan §5: 1.5–2.0×ATR).
- `chandelier_level(highest_high_since_entry, atr_current, k=3.0) -> float`
  = `highest_high_since_entry - k*atr_current`.
- `ratchet_stop(prev_stop, new_level) -> float` = `max(prev_stop, new_level)`  — **monotonic-up
  guard**: the stop NEVER decreases. The daily-close update is
  `current_stop[t] = ratchet_stop(prev_stop, chandelier_level(max_high_since_entry, atr[t], k))`;
  the initial `prev_stop` at entry is the catastrophe floor. A gap-through fills below the stop →
  **sizing bounds the loss, not the stop** (plan §1).

---

---

## Risk-bound coupling (sizing k, catastrophe m, chandelier k)

**Why these three constants must be jointly calibrated:**

The per-trade expected-case loss bound is:

    loss-at-stop = m * ATR * shares
                 = m * ATR * (f * equity) / (k * ATR)
                 = (m / k) * f * equity

So `sizing k >= catastrophe m` keeps `loss-at-stop <= f * equity` (within the
risk budget).  When `k < m`, the stop is placed WIDER than sizing assumed and even
a clean-fill at the stop exceeds the budget.

Note: a gap-through fills BELOW the stop, so `sizing.size` bounds the *expected-case*
loss, not the worst-case.  The per-name cap (15% of equity, currently loose vs 1/N
for N=8–12 — see §C) is the TRUE gap/wipeout bound.  **Tighten the per-name cap
in the funded config.**

**`sizing k == chandelier k` (spec FLAG D):** the chandelier ratchet uses the same
ATR multiplier as the initial stop-distance so the trail calibration is consistent
with the initial risk-per-share.  A divergence makes the ratchet semantics
inconsistent with what sizing assumed.

Both invariants are pinned by `tests/live/test_risk_invariants.py`, which reads the
actual function-signature defaults via `inspect.signature` so silent drift is caught.

---

## Strategy semantics — RATIFIED by the operator 2026-06-22 (all are function params, cheap to re-pin)
- **[A]** `near_threshold = 0.90` (within 10% of the trailing-252 high) — RATIFIED (was 0.95). At
  0.95 XLK was excluded on 2026-06-16 (nearness 0.941); at 0.90 it qualifies on the near-high leg.
- **[B]** 55-day breakout is **close-based and excludes today** (close > prior-55 high-of-highs),
  consistent with the close-based `nearness_to_high` rationale (no intraday-high look-ahead).
- **[C]** sizing `f=1%`, `k=3.0`, `per_name_cap=15%`.
- **[D]** catastrophe stop `m=2.0×ATR`; chandelier `k=3.0×ATR` (same `k` as sizing by default).
- **ATR is Wilder-smoothed** (mirrors the codebase's Wilder RSI), seeded by SMA of the first
  `period` TRs.

_None of these change the plan's contract; they pin the precise numbers the golden needs. T1.0
hand-works ≥2 names against these definitions and cross-checks the close-series sub-signals against
the already-trusted `autotrader.indicators` before the golden is frozen (T1.5)._
