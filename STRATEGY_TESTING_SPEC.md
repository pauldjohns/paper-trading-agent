# Strategy Testing Spec — Backtest + Paper-Monitor Harness (v2)

_Draft 2026-06-16, revised after three independent adversarial reviews (methodology, finance-fidelity, MCP/implementation). Rules are locked to the published literature; the methodology is built to **correctly kill** a weak strategy, not to flatter one. Reads constraints from [`MCP_CAPABILITIES.md`](MCP_CAPABILITIES.md) and [`FINDINGS_AND_PROPOSAL.md`](FINDINGS_AND_PROPOSAL.md). v1→v2 changes summarized in Appendix B._

---

## 0. Purpose & frame

Test whether documented, fully-deterministic strategies beat a **dividend-adjusted SPY** benchmark **net of realistic costs, out-of-sample, after correcting for the number of strategies tried** — using only data and orders the Robinhood MCP actually provides. The backtest is the cheap kill-switch. **A strategy that fails its corrected, out-of-sample, net-of-cost gate never reaches paper-trading.** Per direction, 1%/week is an aspirational north star, not a gate.

The single biggest risk in a project like this is **fooling ourselves** — look-ahead, survivorship, and testing many variants until one looks good. The methodology (§3-§4) is mostly defenses against exactly that.

---

## 1. Shared constraints (from the live MCP)

Cash account, long-only, US equities/ETFs, fractional shares (market + regular hours). No shorting/options/margin. Data and orders per `MCP_CAPABILITIES.md`. **Settlement is T+1 on a single shared cash pool** — selling is unconstrained; re-deploying the *same* dollars waits one business day.

---

## 2. The four test objects (rules locked to the literature)

### Strategy 1 — Trend-Following (the risk-control sleeve)
**Plain English:** stay invested while the market trends up; step to cash/short-bonds when it doesn't. A risk dial, not a stock picker.

```
NAME:      Faber MA-cross + (optional) Antonacci absolute-momentum filter — a COMPOSITE
UNIVERSE:  SPY (+ a short/intermediate bond ETF as the defensive parking asset)
SIGNAL:    monthly close > 10-month SMA            (Faber 2007 — the core rule)
           [optional, tested separately] AND 12-month total return > 0  (Antonacci abs-mom)
BAND:      act only when price is ≥1% beyond the MA  (Siegel whipsaw filter, cited by Faber)
DATA:      TOTAL-RETURN series (Faber's rule is defined on total return — dividends included)
EXIT:      (a) the rule itself, monthly;  (b) wide stop_market GTC intramonth-crash backstop
```
**Honest expectation (corrected):** the documented benefit is **drawdown reduction, not higher CAGR**; on a single equity ETF it tends to *lag* SPY's CAGR in bull markets and only wins in sustained bears. Century-long evidence supports trend-following as a *diversified* strategy (Hurst-Ooi-Pedersen 2017), but it **notably underperformed in the 2010s** ("lost decade for trend" — AQR's own *You Can't Always Trend When You Want*; Cambridge Associates). A single-asset SPY timer historically cut max drawdown versus ~−50% buy-and-hold but is **whipsaw-prone** (its own drawdown lands roughly −20% to −30%, not the −23% of Antonacci's *diversified* GEM system — don't borrow that number). **Judged on drawdown and risk-adjusted return vs SPY *and* vs 60/40 — explicitly not on beating SPY's CAGR.**
_Sources: Faber 2007 (SSRN 962461); Antonacci 2014 (SSRN 2244633); Hurst-Ooi-Pedersen 2017 (AQR); AQR "You Can't Always Trend…"; Cambridge Associates._
_Correction (review): v1 called this "Faber's rule" — it is Faber + Antonacci welded together; relabeled. v1's "decade Sharpe 1.04→0.61 (HOP)" was a misattribution (HOP argues decade-to-decade consistency) — removed; the 2010s-underperformance claim is re-sourced correctly above._

### Strategy 2 — Cross-Sectional Momentum, run on SECTOR ETFs (the offense sleeve)
**Plain English:** hold the few market sectors showing the strongest relative strength; rotate monthly; only when the broad market is healthy.

```
PRIMARY UNIVERSE (survivorship-clean, gating-eligible):
           the 9 original Select Sector SPDRs (XLK XLF XLE XLV XLY XLP XLI XLB XLU,
           continuous since 1998; add XLRE/XLC/sector-style ETFs only from their inception)
SIGNAL:    52-week-high nearness = price / (rolling 252-trading-day high from HISTORICALS)
           [optional cross-check: 12-1 momentum = return over t-12..t-2 months]
RANK:      descending; HOLD top 3–4 sectors equal-weight (fractional → exact weights)
REBALANCE: monthly, fixed business day; rank hysteresis (sell a held name only when it
           falls below rank N+2) to suppress boundary churn
TREND GATE: hold the basket only when SPY close > its trend signal (same basis as S1); else cash
EXIT:      (a) drops out of top N at rebalance;  (b) wide stop_market GTC (−15% to −20%)
SECONDARY (NON-GATING, optional): the same rules on single large-caps — see §3.7, results are
           survivorship/look-ahead-biased and are DIRECTIONAL SANITY ONLY, never a PASS basis.
```
**Honest expectation (corrected):** this is **George-Hwang-*inspired*, not George-Hwang** — it deviates in breadth (top 3-4 vs the paper's ~30% of a universe) and holding period (monthly vs 6-month), which raises turnover above where the paper measured the edge. Long-only large-cap/sector captures roughly **half** the (post-2000-decayed) momentum premium (Israel-Moskowitz 2013), and long-only sidesteps the worst **momentum-crash** tail, which is a short-leg phenomenon (Daniel-Moskowitz 2016) — **but** long-only momentum is just a **high-beta long-equity book**: expect it to draw down *more* than SPY in any bear the gate doesn't catch early. **Why sectors, not single stocks:** ranking 2026's hand-picked large-caps back to 2005 is survivorship + look-ahead-membership bias that can *fabricate* the entire edge (review finding); sector SPDRs are continuously listed, so the backtest is clean and can actually gate a decision.
_Sources: Jegadeesh-Titman 1993; George-Hwang 2004; Daniel-Moskowitz 2016 (NBER w20439); Israel-Moskowitz 2013; AQR 2014._

### Strategy 3 — Short-Term Mean Reversion (the NULL-CONFIRMATION test)
**Plain English:** buy a sharp short dip inside an uptrend, expecting a bounce. Included mainly to prove the harness can correctly *reject* a strategy whose edge dies after costs.

```
UNIVERSE:  liquid ETFs {SPY, QQQ, DIA, IWM} (survivorship-clean)
REGIME:    Close > SMA200
ENTRY:     CumRSI(2,2) < 35   [ = RSI(2)_today + RSI(2)_yesterday; RSI uses WILDER smoothing ]
EXIT:      RSI(2) > 65  OR  Close > SMA5   (whichever first)
TIME STOP: force-exit after 5–10 trading days
CATASTROPHE STOP: stop_market GTC −8% to −10% (wide; a tight stop self-destructs on a dip-buy)
COSTS:     charged at ≥0.4–0.5% round-trip (NOT 0.2%) + stress-scaling — see §3.3
```
**Role & honest expectation:** the academic reversal edge is an **illiquidity premium that largely dies after costs in liquid large-caps** (Avramov-Chordia-Goyal 2006: "profits are smaller than likely transaction costs"; Frazzini-Israel-Moskowitz: net ~1.52%/yr even for an institution). Connors' 88%-win in-sample ETF results predate decimalization decay and use no stops. **Expectation: ≈ break-even-to-negative after honest costs.** Its job is to (1) prove the harness can *kill* an ≈-cost edge and (2) calibrate the cost model against a known-negative. **It is NOT part of the primary blend** (review finding); it is run standalone, and reported with *and* without the stop so the cost of safety is explicit.
_Sources: Connors-Alvarez 2008; Avramov-Chordia-Goyal 2006; de Groot-Huij-Zhou 2012; Frazzini-Israel-Moskowitz 2014; Nagel 2012._

### Strategy 4 — The Blend(s) (the cross-plan test)
v1's three-sleeve blend counted the SPY-200SMA signal three times (it is S1's whole signal, S2's gate, and the master switch) and combined two highly-correlated long-equity books — the "drawdown reduction" was mostly the one regime filter, not diversification (review finding). Redesigned into a **principled primary blend plus one explicit add-on test** — which is the cleaner way to answer your "how do cross-plan strategies perform" question:

```
PRIMARY BLEND — "Trend-Gated Momentum" (= Strategy 1 ⊕ Strategy 2):
   regime switch (S1's trend signal) decides risk-on/off; in risk-on hold the S2
   sector-momentum basket; in risk-off hold cash/bonds.  This is exactly Antonacci's
   dual-momentum thesis — ONE coherent, literature-backed object, not a 3-way composite.
   PRIMARY benchmark = gated-SPY (so the trend filter's credit isn't mistaken for alpha).

ADD-ON TEST — does mean-reversion add anything?
   Take the primary blend and allocate a SEPARATE, fixed, capped risk budget (≤15%) to the
   Strategy 3 dip-buyer, funded only from the SHARED settled-cash pool.  Then MEASURE the
   3-way's net risk-adjusted return and drawdown vs the 2-way.  The cross-plan insight is
   precisely this comparison: if the MR sleeve doesn't beat the 2-way after costs, it's cut.
   Allocation is principled (equal-risk-contribution or 100% momentum), NOT a hand-set 70/30.
```
**Reported for the blend:** sleeve-return correlation (prior: high in risk-on, since both sleeves are long US equity), and the 3-way-vs-2-way delta. **Expectation:** the 2-way ≈ Antonacci dual momentum (drawdown-aware equity); the MR add-on most likely adds cost/noise, not return — which is itself the answer to the cross-plan question.

---

## 3. Backtest methodology (the anti-self-deception layer)

**3.1 Data — CORRECTED 2026-06-17 (price-return basis; total-return is not available from this MCP).** MCP `get_equity_historicals`, daily, **`adjustment_type='split'`** (the split-adjusted, **non-negative, full-history** price series) for every return and benchmark figure. Backfill with explicit `interval='day'`, paged in ≤~1,600-bar windows; **do not** omit interval (auto-select downsamples to ~3-day bars). Cache keyed by (symbol, interval, adjustment); refresh incrementally. A build-time **discontinuity guard** corrects the finite set of un-back-adjusted old splits (IWM 2:1, 2005-06-09) and flags anything else >40% overnight before caching.

> **Why not total return (the v2 mandate, now retracted):** empirical capability discovery (2026-06-17) found the MCP exposes **no** dividend-inclusive history. `adjustment_type='all'` is a **subtractive** adjustment (`all = price − Σ future dividends`) that goes **negative** in the 2009 crash and, decisively, carries **no per-event dividends before ~2013** — verified by `D = split − all` being a frozen constant ($69.5883) across every 2008 SPY ex-date. There is also no dividend-history tool (`get_equity_fundamentals` is a today-only snapshot, banned in the backtest path) and no total-return index (`get_indexes` is price-only). So total return for the deep history (incl. the 2008-09 fold) is **impossible from MCP data** — not a method we can build. **Decision (the operator):** use price-return (ex-dividend). Impact: equity strategy-vs-benchmark comparisons are largely **unbiased** (both sides ex-dividend); the **bond sleeve (IEF/AGG) and 60/40 benchmark** understate by the coupon — addressed, if needed, by an *optional* later coupon-accrual model for the two bond ETFs (their distribution is a predictable yield, unlike equity dividends). Every "dividend-adjusted SPY"/"total-return" reference below (§2 S1 DATA, §3.10, §6, Appendix B) is superseded by this note.

> **OPEN TICKET (logged 2026-06-22, deferred — separate from the LIVE-01 work):** the residual total-return / dividend-adjusted **definitions** in §2 (S1 DATA), §3.10, §6, and Appendix B still read as if dividends are included. They are already superseded by the §3.1 note above (price-return basis), but should be rewritten inline to price-return in a dedicated spec-hygiene pass so the spec is internally consistent end-to-end. Held out of T0.4 to keep that ticket scoped to the stale `adjustment_type='all'` *capability* claims.

**3.2 Trading calendar.** Derive the NYSE calendar from the set of SPY bar dates returned by historicals (no external dependency). All T+1 settlement, next-open execution, and the paper scheduler key off it.

**3.3 Cost model.** Per [`COST_MODEL.md`](COST_MODEL.md). Costs are **NOT** a flat constant. **Backtest:** estimate per-bar spread via Corwin-Schultz high-low (auto stress-widening) + Layer-2 fees + slippage cushion; or, as a baseline, the per-tier round-trip × volatility stress multiplier in COST_MODEL §3. **Live/paper:** charge the actual real-time bid/ask, assume ~no price improvement. Per-strategy assignment per COST_MODEL §4 — note **S3 is charged ≥0.4–0.5% + stress** (it dip-buys when spreads are widest), and single-name results carry single-name-tier costs and remain non-gating.

**3.4 No look-ahead.** Signals on **completed daily bars**; execution at **next open** (`bars[t+1].open`); a signal on the final available bar is not executable. Indicator burn-in enforced (no signal until the full lookback — 200/252 bars — is satisfied). **`get_equity_fundamentals` (52-wk high, avg vol, market cap) is FORBIDDEN in the backtest path** — it returns *today's* snapshot; all historical signals derive solely from historicals bars ≤ signal date. (It may be used only in live/paper mode, where "today" is correct.)

**3.5 Stop-fill modeling on daily bars** (critical — a wrong rule here fabricates downside protection and flips FAIL→PASS):
- `open ≤ stop` (gap-through): fill at the **open** — worse than the stop, never at the stop.
- `low ≤ stop < open` (intrabar pierce): fill at **stop price + a slippage charge** (a stop_market becomes a market order on trigger).
- bar where both entry and stop are in range: assume **stop-first** (conservative).

**3.6 T+1 settled-cash ledger — one shared pool, dated tranches.** Proceeds recorded as `(amount, settle_date=next trading day)`; a buy may draw only settled tranches; the sim refuses a buy lacking settled funds. This is a **deliberate conservative proxy** for the live Good-Faith-Violation rule (under-counts trades vs live — not a paper-reconciliation failure). The pool is **shared across all sleeves**, so in the blend's add-on test the momentum-rebalance proceeds and the MR exits compete for the same settled cash — the MR sleeve is settlement-throttled below its standalone cadence; report its realized trade count in the blend vs standalone.

**3.7 Survivorship & universe-selection bias.** MCP returns only *currently-listed* tickers, and a hand-picked 2026 large-cap list is additionally **forward-selected** (you wouldn't have chosen today's winners in 2005) — together these can *fabricate* a single-name momentum edge (generic survivorship alone is +1-4%/yr CAGR, ~14% DD understatement; forward-selection is worse). Therefore **all gating decisions use survivorship-clean ETFs/sector SPDRs only.** Single-name results are **directional sanity only**, labeled biased, never a PASS basis.

**3.8 Out-of-sample design.** Parameters are literature-locked, so in principle the whole 2005-present series is out-of-sample. To guard the residual soft knobs (top-N, stop width, blend budget): **anchored walk-forward** (expanding window, step yearly; parameters re-derived from the literature each fold, never re-optimized) **plus regime-stratified reporting** that forces the named stress folds — **2008-09 (the only deep bear in MCP history), 2020, 2022** — into the evaluation as weighted, separately-reported periods. **Stated plainly: the harness cannot validate deep-bear behavior beyond 2008/2020/2022; conclusions about Strategy 1's drawdown benefit and Strategy 2's crash behavior are extrapolations, not measurements.**

**3.9 Multiple-testing control** (new — the headline v1 gap). Across all strategies × signal variants × stop/gate on-off × parameter-plateau cells × sub-periods we will run dozens of configurations; the best Sharpe is inflated even on pure noise. Therefore: enumerate the total trial count; compute and report the **Deflated Sharpe Ratio** (Bailey-López de Prado, SSRN 2460551) and **Probability of Backtest Overfitting via CSCV** (Bailey-Borwein-LdP-Zhu, SSRN 2326253) for every strategy. **Auto-kill any strategy with PBO ≥ 0.5 or deflated-Sharpe p ≥ 0.05**, regardless of headline numbers. Robustness is a **plateau, not a peak**: a setting must work across its neighborhood, not be the best cell.

**3.10 Benchmarks.** (a) **dividend-adjusted SPY** buy-and-hold net of costs; (b) **gated-SPY** (primary for S2 and the blend — isolates selection from the trend filter); (c) **60/40 SPY/bond** (the honest competitor to "risk-controlled equity" — S1/blend must beat *risk-adjusted* 60/40, not just SPY); (d) equal-weight buy-hold of the momentum universe (separates sector-selection skill from universe drift).

---

## 4. Metrics & success gates (de-gamed)

**Report (full sample + walk-forward + named stress folds):** CAGR, vol, Sharpe (+ **deflated** Sharpe & its p-value), Sortino, max drawdown, Calmar, win rate, avg win/loss, profit factor, turnover, time-in-market, trades/yr, **PBO**, and **bootstrap confidence intervals** on (strategy − benchmark) Sharpe and max-DD — plus the **number of independent bets** (regime calls / rebalances), not just trade count.

**Pass/fail (out-of-sample, net of costs, after trial correction):**
- **Return-seekers (S2, blend):** PASS only if deflated-Sharpe p < 0.05 **AND** Sharpe CI lower-bound ≥ the relevant benchmark's Sharpe (gated-SPY for these) **AND** max-DD upper-CI < SPY's **AND** PBO < 0.5 **AND** it beats the **95th percentile of a random-selection placebo** (1,000 random picks from the same universe under the same gate). No undefined "tolerance" escape hatch.
- **S1 (risk sleeve):** judged on drawdown and risk-adjusted return vs SPY *and* 60/40 — explicitly **not** on beating SPY CAGR. PASS if it materially cuts max-DD at acceptable CAGR cost with PBO < 0.5.
- **S3 (null test):** there is no "PASS" to chase — success is the harness correctly showing it ≈ break-even-to-negative after honest costs. If it somehow clears the §4 bar, treat that as a **red flag to audit the cost model**, not a win.
- **Hard kill:** fail the corrected OOS gate → dropped before paper-trading. "It works if we retune it" is not an exception.

---

## 5. Paper-monitor phase (only for strategies that pass §4)

Same deterministic logic, run forward on live MCP data, **placing no orders but calling `review_equity_order`** on every would-be order (it simulates without placing) to capture buying-power / PDT / GFV / halt alerts — that telemetry is the point of the dry run. Decisions are computed on the **official close (after 16:00 ET)**, never on a stale off-hours quote. Honors the NYSE calendar; watches for corporate actions (no CA-calendar tool exists — detect splits by price discontinuity) and quote staleness.
- **Duration:** months, not weeks (at ~1 trade/week, 4 weeks ≈ 4 trades = indistinguishable from luck). Target dozens of decisions per strategy.
- **Reconciliation:** compare the **signal boolean and would-be order** against the backtest's decision for the same date — **not** fill prices (live NBBO vs historical adjusted bars cannot match by construction).
- **Measured-spread log:** record the real bid/ask spread (from `get_equity_quotes`) at each decision, so the backtest cost tiers (COST_MODEL §3) can be tightened with real per-name data over time.
- **Success = process integrity** (correct, stable, no operational faults), not P&L — the live sample is far too small for P&L to mean anything.

---

## 6. Known limitations (stated up front)
- **Deep-bear behavior is unvalidatable** beyond 2008/2020/2022 (MCP history starts 2005); the strategies' headline reason to exist (bear protection / crash behavior) is largely an extrapolation.
- **Single-name momentum is not gating-grade** on MCP-only data (survivorship + forward-selection); only ETF/sector results gate.
- **No total-return basis is available** — `adjustment_type='all'` is broken for the deep history (subtractive, negative 2009 prices, no per-event dividends before ~2013; verified 2026-06-17, §3.1), so every return and benchmark is **price-return (split-adjusted, ex-dividend)**. Equity strategy-vs-benchmark comparisons are largely unbiased (both sides ex-dividend); the bond sleeve / 60-40 benchmark understate by the coupon (optional later coupon-accrual refinement).
- **Decayed edges:** every strategy here has documented post-publication decay; expectations are set on that, not headline in-sample numbers.
- **Small-sample noise:** live P&L is noise for a long time; signal lives in the backtest + paper phases.

## 7. Build order (canary-first)
1. **Data layer** (MCP historicals → cached store) **+ NYSE trading calendar** derived from bar dates. Use **`adjustment_type='split'`** (the price-return basis — `'all'` is broken for the deep history; verified 2026-06-17, §3.1).
2. **Cost + T+1 settled-cash ledger simulator** (shared pool, dated tranches, **stop-fill-on-daily-bar incl. gap-through**) — **unit-tested against hand-worked sequences (a gap-through stop; a GFV-blocked dip-buy) and frozen as a golden fixture before any strategy result is trusted** (per PROJECT_CONTEXT.md: pin behavior with a canary first).
3. **Indicator library** (SMA, RSI(2)-Wilder, CumRSI, rolling-252 high, returns) — pure functions, unit-tested.
4. **Strategies** as deterministic rule modules.
5. **Backtest engine** + metrics (incl. deflated Sharpe, PBO, bootstrap CIs) + the four benchmarks + report (summary + equity curve + trades table).
6. **Robustness runner** (walk-forward, plateau scan, stress folds, with/without stop & gate, **rebalance-day dispersion** — run on day 1/8/15/22 to expose timing luck).
7. **Paper-monitor** (calls review, same state store).

## Appendix A — Open questions for the operator
- **Momentum universe:** primary is the 9 Select Sector SPDRs (clean, gating-grade). Want me to also run the single-large-cap version as a labeled, non-gating side experiment, or skip it?
- **Mean reversion:** keep it as the standalone null-confirmation test (recommended — it validates the kill-switch), or drop it entirely?
- **Blend:** primary is the 2-way trend-gated momentum; the 3-way (adding the MR sleeve) is run as the explicit "does it help?" test. Good, or do you want the MR sleeve in the blend by default?

## Appendix B — What changed v1→v2 (from the three reviews)
- **Killed a look-ahead landmine:** 52-wk-high now computed from historical bars; fundamentals snapshot banned from the backtest path.
- **Added stop-fill-on-daily-bar modeling** (gap-throughs fill at the gapped open) — v1 would have hidden real drawdowns.
- **Added multiple-testing correction** (Deflated Sharpe + PBO, auto-kill thresholds) — v1 had none.
- **Fixed survivorship/look-ahead:** momentum now runs on survivorship-clean **sector ETFs** for any gating decision; single names demoted to non-gating.
- **Dividend adjustment** (`'all'`) mandated for all returns/benchmarks — v1 split-only understated SPY ~30-40% of its premium.
- **Redesigned the blend:** primary = trend-gated momentum (Antonacci dual momentum, one object); mean-reversion moved to a separate capped add-on *test*, not baked in at an arbitrary 70/30; shared T+1 ledger.
- **Fixed two citation/labeling errors** in the "honest expectations" blocks (S1 relabeled Faber+Antonacci; HOP decade-Sharpe misattribution removed).
- **De-gamed the success gate** (CIs, placebo test, defined thresholds, no undefined "tolerance"); per-strategy stress-scaled costs; added NYSE calendar, total-return series, rebalance-timing check, golden-fixture-first build order, and the paper-monitor `review_equity_order` telemetry.
- **(v2.1) Finalized the cost model** in [`COST_MODEL.md`](COST_MODEL.md): three-layer stack (zero commission; negligible SEC §31 + FINRA TAF pass-throughs; spread-dominated implicit cost with weak PFOF price improvement). Backtest estimates spread via Corwin-Schultz high-low; live/paper charges real bid/ask; per-tier × stress, S3 charged ≥0.4–0.5%. §3.3 rewritten to point to it; §5 adds a measured-spread log.
