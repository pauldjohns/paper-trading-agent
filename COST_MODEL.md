# Robinhood Trading Cost Model

Date 2026-06-16. The authoritative cost basis for the backtest and live engine.
Commissions are zero and regulatory fees are negligible at this account size — the
entire real cost is the bid-ask spread crossed with little price improvement.
(WARNING) = confirm against a primary source before hardcoding (rates drift; immaterial here).

## 1. The three-layer cost stack

### Layer 1 — Robinhood direct fees: $0
- $0 commission on US stocks/ETFs, every order type (market, limit, stop).
- No account, inactivity, or maintenance fees.
- Robinhood Gold (~$5/mo) is optional and irrelevant to a cash equities strategy.
- ADR custody fees apply only to foreign depositary receipts — N/A (US universe).

### Layer 2 — Regulatory pass-throughs (SELL side only): negligible
- (WARNING) SEC Section 31 fee: ~0.002–0.003% of sell proceeds (~$20–30 per $1M; SEC resets periodically; Robinhood waives amounts under ~$0.01). On a $1,000 sell ≈ $0.02.
- (WARNING) FINRA TAF: ~$0.0002/share sold (recent rate ~$0.000166–0.000195), small per-trade cap (~$8–9). On a fractional/large-cap sell ≈ fractions of a cent.
- Combined: < 0.01% per round trip. Real but rounding-error at this size.

### Layer 3 — Implicit costs: THIS is the cost (10–50x the fees)
- Bid-ask spread (measured live from the account): SPY ~0.008%, AAPL ~0.087%. You pay ~half the spread per side when you cross it.
- Weak price improvement (PFOF): independent studies (Wharton, SEC DERA) found only ~25% of Robinhood orders fill at midpoint-or-better, so model paying close to the FULL spread, not the midpoint. (WARNING) (Central to Robinhood's 2020 SEC settlement.)
- Slippage / market impact: minimal at retail size on liquid names with marketable limit orders; larger on thin names or in volatile markets.
- Stress widening: spreads blow out x2–5 in panics (2008, Mar 2020). Today's tight spread understates a stressed-period fill.

## 2. How cost is measured vs estimated
- LIVE / PAPER: read the real-time bid/ask from get_equity_quotes at decision time; charge the actual half-spread per side; assume ~no price improvement. No guessing.
- BACKTEST (bars have no bid/ask): estimate per-bar spread from the daily high/low via the Corwin-Schultz (2012) estimator — it auto-widens in volatile periods — then add Layer-2 fees + a small slippage cushion. (WARNING) verify formula on implementation. Simpler baseline if needed: fixed per-tier spread (below) × a volatility stress multiplier.

## 3. Backtest cost model — round-trip, by liquidity tier × stress
| Tier                                   | Calm round-trip | Stress x   |
|----------------------------------------|-----------------|------------|
| Index ETFs (SPY/QQQ/DIA/IWM)           | 0.05–0.10%      | x2–5       |
| Sector SPDRs (XLK, XLF, ...)           | 0.08–0.15%      | x2–5       |
| Mega-cap single names (AAPL/MSFT)      | 0.15–0.20%      | x2–5       |
| Other large/mid-caps                   | 0.25–0.50%      | x3–5       |

## 4. Per-strategy cost assignment (matches the spec)
- S1 (SPY + bond ETF): index-ETF tier (~0.08%), <1–4 trades/yr → immaterial annually.
- S2 (sector SPDRs): SPDR tier (~0.10–0.15%), ~1–3 trades/month.
- S3 (mean reversion, liquid ETFs): charge >=0.4–0.5% + stress scaling — NOT the calm ETF spread. Rationale: it trades frequently AND buys dips precisely when spreads are widest, so the effective per-trade spread is far worse than the calm average. This is why S3 is the null-confirmation test: honest costs should push it to ~break-even/negative.
- Single-name momentum (non-gating/directional only): single-name tier 0.15–0.50%.

## 5. Bottom line
No commissions; regulatory fees are pennies. The whole real cost is the spread plus Robinhood's weak price improvement — measurable live, estimable from high/low historically. A single flat 0.2% is wrong both ways: too high for calm liquid ETFs, too low for single names and for any strategy trading in stress.

---

## 6. Verification log (added 2026-06-16 — web-confirmed against primary sources; none change the model)

The (WARNING) rate items above, confirmed current:
- **SEC Section 31 fee:** **$20.60 per $1,000,000** of sells (= **0.00206%** of sell proceeds), effective **2026-04-04**. (The rate was $0.00/million through 2026-04-03, then reset to $20.60/million — it resets on a published schedule.) On a $1,000 sell ≈ **$0.02**. Sources: [FINRA Information Notice 2026-03-17](https://www.finra.org/rules-guidance/notices/information-notice-20260317); SEC FY2026 Fee Rate Advisory.
- **FINRA TAF (covered-equity sells):** **$0.000166 per share**, **max $8.30 per trade**. On a fractional / large-cap sell ≈ fractions of a cent. Source: [FINRA Trading Activity Fee guidance](https://www.finra.org/rules-guidance/guidance/trading-activity-fee).
- Combined Layer-2 stays **well under 0.01% per round trip** — confirmed rounding-error at this size. Re-confirm before hardcoding (both drift on a schedule), but they never alter the spread-dominated structure.

**Corwin-Schultz (2012) high-low spread estimator — canonical formula** (WARNING: re-verify against the primary source at implementation):
For two consecutive days *t* and *t+1*:
- β = [ln(Hₜ / Lₜ)]² + [ln(Hₜ₊₁ / Lₜ₊₁)]²   — sum of the two single-day squared log high-low ranges
- γ = [ln(H₂ / L₂)]²   where H₂ = max(Hₜ, Hₜ₊₁) and L₂ = min(Lₜ, Lₜ₊₁)   — the two-day high and low
- α = (√(2β) − √β) / (3 − 2√2) − √( γ / (3 − 2√2) )
- **Ŝ = 2(e^α − 1) / (1 + e^α)**   → the proportional (fraction-of-price) bid-ask spread

Notes for implementation: set negative two-day Ŝ estimates to zero; apply the overnight-gap adjustment to highs/lows when day *t+1* gaps clear of day *t*; average the daily estimates over a window (e.g., monthly) for stability. Primary source: Corwin & Schultz (2012), "A Simple Way to Estimate Bid-Ask Spreads from Daily High and Low Prices," *Journal of Finance* ([SSRN 1106193](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=1106193)); a clean reference implementation is B. A. Ødegaard's high-low estimator notes.
