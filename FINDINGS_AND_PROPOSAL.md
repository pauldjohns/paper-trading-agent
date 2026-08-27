# Robinhood Agentic Trading — Verified Findings & Proposal (v2)

_Research date: 2026-06-16. Load-bearing claims checked against primary sources (Robinhood support pages, FINRA, SEC filings, law-firm alerts) and pressure-tested by three independent reviewers (regulatory, technical, financial-realism). Corrections from that review are folded in below; v1 errors are noted where they mattered._

---

## TL;DR verdict

**The infrastructure is real and more capable than the Gemini file claimed. The return goal is the thing that makes or breaks this — and on the evidence, the goal as stated is fiction.**

- Robinhood’s official **Agentic Trading MCP is real** (launched 2026-05-27, beta, US, equities only). Endpoint: `https://agent.robinhood.com/mcp/trading`.
- **Fully autonomous, no-per-trade-approval mode exists.** The Gemini file’s central claim (“you cannot bypass manual approvals”) is **false**. Hands-off within a budget is a setting.
- **Near-real-time *reactive exits* are available; near-real-time *capital recycling* is not — and recycling was never the goal.** You can sell held positions as fast and as often as you like (no settlement or PDT limit on *selling*). What settlement throttles is reusing the *same* dollars for repeated intraday round trips. **Margin is not required** — use a Cash or Instant account. Tick-level HFT is off the table; reactive monitor-and-exit is not.
- **You are 100% liable for whatever the agent does, with no recourse and forced FINRA arbitration.** If the bot malfunctions, you eat the loss.
- **1%/week = 67.7%/year compounded.** Nobody has publicly sustained that. The best controlled evidence (StockBench, Oct 2025) shows frontier models — including Claude-4 and GPT-5 — trading autonomously did **not** beat buy-and-hold over a real 4-month window, *before* trading costs. On a $1k account, costs alone (~0.2%/round trip) bleed ~10%/year — the entire stock-market premium.

**Recommendation: do it as capped-downside R&D to learn agentic/market engineering — not as an income engine — and do it cheaper and slower than the perfect-world plan.** Backtest → paper-trade for months → risk **a fraction of the account, not all of it**, with the rest in an index fund as the benchmark you’re trying to beat. Judge success on “did I build something durable that didn’t blow up,” never on P&L. If the appeal is the 1%/week money, the honest answer is: don’t build this.

---

## 1. Scorecard on the Gemini blueprint

| Gemini claim | Verdict | Reality |
|---|---|---|
| “Requires manual push-notification approval for every trade” | **False** | Autonomous mode exists: _“if you’ve asked your agent to take action without asking your approval, it can place trades without your confirmation.”_ Notifications ≠ approval gates. |
| “You cannot bypass manual approvals” | **False** | Per-trade approval is optional; you may require it on high-risk orders, but are not forced to. |
| “Native MCP requires an isolated ‘Agentic Account’” | **True** | A walled-off Agentic account is real. Agent only touches funds deposited there; main account is read-only to it. |
| “T+1 settlement bites a small account doing frequent trades” | **True (cash path)** | Real for a cash account. The June 4 2026 PDT change affects the *margin* path — but see §3 for why that path may not even be available here. |
| Polygon.io for data + MCP for execution | **Optional, not required** | The MCP serves quotes (`get_equity_quotes`). A paid feed is a later add-on, *if* MCP quote quality proves inadequate (unverified — see §6). |
| Fama-French / Jegadeesh-Titman as the strategy frame | **Real strategies, wrong fit** | Legitimate, referenced anomalies — but multi-month, diversified-portfolio edges. The disqualifier for $1k is **horizon and diversification**, not cost (momentum actually survives costs to multi-billion fund size). See §5. |

**Net:** Gemini invented the biggest constraint (mandatory approvals) and missed the biggest recent fact (PDT rule replaced 12 days ago). That is why it read as misleading.

---

## 2. What Robinhood Agentic Trading actually is (verified)

- **Status:** Beta, 2026-05-27, US, **equities only**. Options/crypto/futures/event contracts “coming soon.”
- **Endpoint:** `https://agent.robinhood.com/mcp/trading`. Any MCP client (Claude, ChatGPT, Cursor, custom).
- **Account model:** Dedicated **Agentic account**, separate from your portfolio; up to 10 self-directed individual accounts total; primary account in good standing required first; desktop-only setup.
- **MCP tool surface** (community-reconstructed — Robinhood’s own page lists no tool names): `get_accounts`, `get_portfolio`, `get_equity_positions`, `get_equity_orders`, `get_equity_quotes`, `get_equity_tradability`, `search`, `get_popular_lists`, watchlist verbs, `review_equity_order`, `place_equity_order`, `cancel_equity_order`. Options tools exist in-schema, not live.
- **Order types:** Public evidence says **“market or limit orders for equities.”** **Stop / stop-limit / bracket via the MCP is unconfirmed and may not exist yet.** This is decision-critical (see §6 R1).
- **Autonomy:** Configurable hands-off-within-budget OR require approval. Push notification per trade, real-time activity + P&L feed, one-tap disconnect.
- **Auth (now known, was “open item” in v1):** OAuth, PKCE, **access token ~95.5h (~4 days) + refresh token** — so a 24/7 refresh loop can stay alive without daily human re-auth. **Caveat:** a documented bug (anthropics/claude-code #65895) makes Claude Code’s native MCP OAuth silently fail against Robinhood; workaround is hand-writing the token. The “paste one URL and go” path is not clean today.
- **Rate limits:** Undocumented. Design the loop to be polite.

Sources: robinhood.com/us/en/agentic-trading, /support/articles/agentic-trading-overview, /newsroom/robinhood-is-now-open-to-agents; TechCrunch + CNBC 2026-05-27; sherwoodcraft.com/robinhood-mcp; github.com/anthropics/claude-code/issues/65895.

---

## 3. Regulatory reality — corrected

**You do not need a margin account, and the strategy described avoids the hard constraints entirely.** Robinhood has three account types ([source](https://robinhood.com/us/en/support/articles/robinhood-accounts/)): **Cash** (no borrowing; unlimited day trades; never subject to PDT; proceeds settle T+1), **Instant** (the default; no borrowing, but instant access to sale proceeds, erasing the T+1 wait), and **Margin** (actual borrowing — not wanted here). Use Cash or Instant.

The goal is hold-a-position-and-exit-fast-on-weakness, not rapid capital recycling. That distinction removes the constraints v1 over-weighted:
- **Selling is never constrained.** You can sell held positions as many times a day as you want, in any account type — no PDT, no good-faith violation, no settlement wait. Near-real-time *reactive exits* are fully available.
- **“Day trading” means buying *and* selling the same security the same day.** A buy-hold-sell-later pattern usually isn’t a day trade; and Cash accounts have unlimited day trades regardless ([source](https://www.fidelity.com/learning-center/trading-investing/trading/avoiding-cash-trading-violations)).
- The only thing settlement throttles is reusing the *same* dollars for another round trip before they settle. That is the rapid-recycling case you explicitly do not want. Hold some settled dry powder, or use Instant, and it never bites.

Net shape: a Cash or Instant agentic account, a few held positions, a tight monitoring loop that sells on a threshold breach, buying new candidates from the fixed strategy library when settled cash is available. No margin, no PDT, no HFT. _(The PDT/margin detail below is kept as reference, but with no margin account it is no longer load-bearing.)_

**PDT rule replaced 2026-06-04.** SEC approved (2026-04-14, Release 34-105226) amendments to FINRA Rule 4210 (Notice 26-10) that strike the day-trading margin provisions, including the $25,000 PDT minimum and the “4 day-trades in 5 days” designation, replacing them with an **intraday-margin standard**.

**Three corrections to v1 (two reviewers independently caught the first):**

1. **The $2,000 figure is NOT created by this amendment and “unlocks” nothing new.** $2,000 is the long-standing Rule 4210(b)(4) **margin-account minimum**, unchanged by Notice 26-10 (WilmerHale: the amendment has “no impact on… paragraph (c)”). It is also **inapplicable to cash accounts**. v1’s “fund $2,000 to unlock the new intraday rules” was a category error. The PDT removal already benefits any margin account; $2,000 only lets you open a margin account at all (true since 2019).
2. **The new standard is not pure deregulation.** It replaces a bright line with an affirmative broker duty: real-time monitoring that **blocks trades creating an intraday margin deficit**, or end-of-day margin calls; habitual offenders face a **90-day credit freeze**. For a small margin account churned by a bot, this can be *more* constraining and less predictable than the old line. Brokers have an **18-month phase-in to 2027-10-20**, so Robinhood may not have fully implemented it yet. “Robinhood removed PDT flags” is **unconfirmed**.
3. **Whether the Agentic account can even be a *margin* account is unverified** — and the entire near-real-time/margin path depends on it. Robinhood describes it only as a “self-directed individual investing account” with a “pre-loaded balance / dedicated wallet,” language consistent with **cash-only** in beta. **So the default working assumption is: Agentic account = cash, $1,000, T+1-throttled.**

**What T+1 means for a small cash account:** proceeds from a sale are unsettled until the next business day. With $1,000 fully deployed you get **≈ one buy-sell round trip per day, then you’re done until settlement**. Re-deploying unsettled proceeds and selling again = a **Good Faith Violation**; 3 GFVs in 12 months → 90-day settled-cash-only restriction. T+1 is a hard ceiling on frequency, not a speed bump.

**Liability (omitted in v1 — HIGH):** Robinhood’s agentic terms put **100% of the risk on you**: _“You are ultimately responsible for the trades your AI agent places”_; Robinhood _“is not responsible for losses resulting from agent-generated decisions.”_ A runaway loop, fat-finger size, or misparsed quote is your loss — no chargeback, no “the model did it” defense. Disputes go to **mandatory FINRA arbitration** with a **class-action waiver**. Your downside model must assume the worst case is messier than “lose the $1k,” especially on margin.

**Tax (under-weighted in v1):** all gains short-term/ordinary. The **wash-sale rule** is worse than “messy” for a re-entering bot: late-December losses re-bought in January **defer into the next tax year**, and you can owe ordinary-income tax on **phantom gains** even on a flat/losing account. If you also hold the same tickers in a Robinhood IRA, repurchase there **permanently disallows** the loss — keep the bot’s universe disjoint from any IRA. The proper fix for an active trader is a **§475(f) mark-to-market election** (kills wash-sale tracking), which needs Trader-Tax-Status and a timely election. On $1k the dollars are small but the bookkeeping is real.

Sources: sec.gov/files/rules/sro/finra/2026/34-105226.pdf; finra.org/rules-guidance/notices/26-10; finra.org/rules-guidance/rulebooks/finra-rules/4210; wilmerhale.com (2026-04-23 alert); kslaw.com margin alert; robinhood.com Customer Agreement + agentic-trading-overview; Fidelity cash-account-violations; greentradertax.com wash-sale guide.

---

## 4. What others have actually built

- Official MCP is ~3 weeks old; almost all prior art uses the **unofficial, reverse-engineered `robin_stocks` API** (not supported; breaks without notice — see robin_stocks issues #521/#530/#537 on auth/MFA breakage).
- Repos: `siropkin/robinhood-ai-trading-bot` (LLM + RSI/VWAP/MA, auto-executes), `2018kguo/RobinhoodBot`, `abghorba/Robinhood-Trading-Bot` (MA crossover), `Open-Agent-Tools/open-stocks-mcp` (community MCP, **live-tested market/limit/stop-loss**, headless), `kevin1chun/robinhood-for-agents` (headless TS client, encrypted token store, Playwright OAuth intercept).
- “I gave an AI my account” writeups are anecdotal, mostly break-even-to-loss.
- **Honest read:** no reproducible evidence an LLM autonomously beats the market net of costs. Where systematic retail bots make money, it is a *disciplined documented strategy*, not LLM “judgment.” The LLM’s value is orchestration and rule-following, not alpha.

---

## 5. The return goal — hard evidence, not vibes

_Per direction, 1%/week is treated as an aspirational north star, not a success gate — the build proceeds regardless. The evidence below stays in the record so the target is set with eyes open, not to block the work._

- 1%/week compounded = **67.77%/year**. Renaissance Medallion (best public track record) did ~66% gross / **~39% net** — with PhD staff, co-located execution, proprietary data. Sustained 1%/week net at retail is not “hard,” it is **undemonstrated by anyone publicly.** Treat it as fiction, not a stretch ceiling.
- **StockBench (arXiv 2510.02209, Oct 2025):** over a real ~4-month window, buy-and-hold returned +0.4%; the best LLM (+2.5% cumulative ≈ 7–8%/yr); **GPT-5 (+0.3%) and Claude-4 at or below the passive baseline** — before trading costs.
- **Agent Market Arena (arXiv 2510.11695):** the headline +40% result is a single asset (Tesla) over a short window; the real finding is _“no single LLM or agent consistently outperformed across all assets”_ and _“architecture matters more than the model”_ — the signature of overfit, not generalizable edge.
- **Costs dominate a $1k account.** At ~0.2% round-trip (spread + slippage), **one round trip per week ≈ 10.4%/yr of drag** — the whole long-run equity premium consumed by friction before any pick is made. Robinhood’s regulatory fees are rounding error; the real tax is **spread + PFOF execution quality** (independent studies: only ~25% of Robinhood orders at midpoint-or-better).
- **Documented strategies, corrected:** momentum (Jegadeesh-Titman) survives costs to multi-billion fund size — its disqualifier for you is **horizon (3–12 mo) and needing a diversified cross-section you can’t hold with $1k**, not cost. Short-term reversal is right-horizon but its gross edge (~30 bps/wk in large-caps) ≈ the round-trip cost, so net ≈ 0. The takeaway: keep the documented-strategy guardrail (no free-form guessing, no 100×$10 spam — your exact instinct), but don’t expect these to clear 1%/week.
- **Base rates for active small-account trading:** Brazil futures day-traders persisting ≥300 days — **97% lost money**; Taiwan — **~84% lose**; Barber-Odean — most active retail quintile **underperforms by ~6.5pp/yr** from turnover; copy-trading abnormal returns **−4%/yr**. Modal outcome for this bet over 12 months: **slow bleed of −5% to −20%**, a fat left tail if a guardrail or token fails, a thin lucky right tail, and ~0 probability of 67%.

Sources: ofdollarsanddata.com/medallion-fund; arxiv.org/abs/2510.02209 (StockBench); arxiv.org/abs/2510.11695 (AMA); Korajczyk-Sadka JF 2004; AQR Frazzini et al.; Avramov-Chordia-Goyal; Barber-Odean SSRN 219228; Chague et al. SSRN 3423101; Wharton WIFPR + SEC DERA on PFOF.

---

## 6. Recommended architecture (if we proceed)

A localized, **stateful** controller — the skeleton, with the reviewer-mandated safety changes:

1. **Topology (decide up front):** a **headless Python controller is the MCP client**, holding the OAuth refresh token; the **LLM is a stateless function call**, not a hosted Claude/ChatGPT session (those hit the #65895 OAuth bug and can’t be supervised headless). One manual browser login to bootstrap the token; refresh is headless thereafter.
2. **Refresh-token subsystem (first-class):** secure at-rest storage (keychain / AES-GCM), renew before the ~95.5h expiry, **alert loudly on refresh failure** (a silent token death mid-position = unmanaged risk).
3. **State store** (SQLite): positions, open orders, per-strategy P&L, decision log, drawdown tracker. Mandatory — the agent is stateless between polls.
4. **Strategy library** (deterministic Python): a small *fixed* set of documented, backtested rules (one momentum, one mean-reversion). Signals (SMA/RSI/Z-score) in pandas. This is the guardrail that stops improvisation.
5. **LLM layer:** given pre-screened candidates + rules + state, select and justify a sanctioned order sized by a fixed risk budget. The LLM **never invents strategies**; it picks among sanctioned ones and logs rationale.
6. **Guardrails — hard-coded, AFTER the LLM, treating LLM output as untrusted** (prompt-injection blast-radius, not just P&L): reject any order whose symbol/side/size didn’t come from the sanctioned library; max position, max trades/day, daily-loss auto-halt, no-trade list, no-spam check. (Robinhood’s own injection defense is self-reported at ~99.4%, i.e., ~0.6% gets through.)
7. **Protective stops — precondition for autonomy, not an open item.** Public evidence points to **market/limit only**. A “synthetic stop” run by the controller fails exactly when needed (process asleep/crashed, MCP down, token-refresh failed, Mac slept) — on a margin account that can cost more than the position. **Do not run hands-off until either** native stops are confirmed in the live `place_equity_order` schema, **or** you size positions so max loss = full position is acceptable (no stop needed), **or** you move the protective-order leg to `open-stocks-mcp` and accept its unofficial-auth fragility.
8. **Idempotent writes:** `review_equity_order` → log → `place_equity_order`; before any retry, reconcile against `get_equity_orders`/positions. **Never blind-retry a write** (timeout-after-fill double-submits real money).
9. **Market calendar / halt handling:** half-days, holidays, LULD halts, no pre/post-market submits.
10. **Reporting:** daily email/Slack recap from your own state store.

---

## 7. Go / no-go and the path

**Go — as capped R&D, on this ladder (cheapest kill-switch first):**

1. **Backtest the strategy library** on 5–10 yrs of free daily data, modeling 0.2% round-trip cost. **If it doesn’t beat SPY net-of-cost in backtest, stop here** — $0, a weekend.
2. **Paper-trade the full agent for ~3 months** ($0 at risk): MCP plumbing, state, guardrails, token longevity, failure modes. (v1’s “2–4 weeks” is too short — at ~1 trade/week that’s ~4 trades, indistinguishable from luck.)
3. **Then risk a fraction of the account, not all of it.** Keep the remainder in a broad index fund as the literal benchmark you feel every week. The stake size changes only how much you lose, not how much you learn.
4. **Flip to hands-off only after** the log shows good behavior **and** the protective-stop precondition (§6.7) is met.
5. **Success metric = process integrity** (built something durable, no operational blow-up), **not P&L** — the sample is far too small for P&L to mean anything.

**Waste of time?** Yes, if the goal stays “reliably earn 1%/week as income” — that’s undemonstrated by anyone and the modal outcome is a bleed. No, if the goal is to learn agentic/market engineering on real infrastructure that is genuinely new, with downside capped to a few hundred dollars. Those are two different projects; pick the second.

---

## Build-time confirmations — RESOLVED 2026-06-16 by live MCP inspection
_Full detail in [`MCP_CAPABILITIES.md`](MCP_CAPABILITIES.md)._
- ✅ **Order types:** `place_equity_order` supports **market, limit, stop_market, stop_limit** with **gfd/gtc**. Native stops confirmed (no native trailing). Built-in `ref_id` idempotency + `review_equity_order` pre-trade alerts.
- ✅ **Account type:** the Agentic account is a **cash** account (`agentic_allowed=true`, options off, currently **unfunded**). The main account is `agentic_allowed=false` — walled off from the agent.
- ✅ **Quote/data semantics:** `get_equity_quotes` is **real-time with NBBO bid/ask** + official prior close. `get_equity_historicals` reaches back to **2005**, split-adjusted ⇒ the MCP is our data source; no external feed needed.
- ⏳ **Still needs a live action:** does autonomous placement fill or trigger an in-app approval (one authorized tiny test trade after funding); rate limits; unattended-loop token lifetime; ToS for headless loops. See `MCP_CAPABILITIES.md` §6.

---

## Appendix A: Fabrication & hallucination log — the Gemini blueprint

_Purpose: catalog what `trading_agent_blueprint.md` invented vs. got right, typed by error pattern, so the same tells are catchable next time. The original file has been annotated inline with matching flags._

**Error-type legend:**
- `[INVENTED-CONSTRAINT]` — a specific rule/prohibition stated as fact that does not exist
- `[FABRICATED-AUTHORITY]` — language implying verification/testing that never happened
- `[STALE]` — outdated fact stated as current
- `[INCOMPLETE-MISLEADING]` — true in part, but omits a material exception that flips the conclusion
- `[FILLER-TELL]` — generated placeholder/artifact signaling un-grounded output
- `[HELD-UP]` — checked against primary sources and correct (kept so we don’t overcorrect)

| # | Claim in the blueprint | Type | Reality |
|---|---|---|---|
| 1 | “Official Robinhood MCP implementation requires manual push-notification approval for trades… preventing a ‘set and forget’ loop” | `INVENTED-CONSTRAINT` | False. A fully autonomous, no-approval mode exists; approval is an optional setting. |
| 2 | “You cannot bypass manual trade approvals; the system requires a batch-approval workflow rather than full ‘hands-off’ automation” | `INVENTED-CONSTRAINT` | False. No “batch-approval workflow” exists. Hands-off within a budget is supported. |
| 3 | “Execution: Automated via Robinhood MCP server, with manual review hooks **required by regulatory compliance**” | `INVENTED-CONSTRAINT` | No such regulatory requirement exists. |
| 4 | Section header “**Subagent Pressure Test Results**” presenting claims 1–2 as validated findings | `FABRICATED-AUTHORITY` | Implies a multi-agent verification that never ran. This framing is what made the invented constraints sound authoritative — the most dangerous tell in the document. |
| 5 | “Claude 3.5 Sonnet / Gemini 1.5 Pro to evaluate candidates” | `STALE` | Outdated model generation — a signature of training-data recall instead of current research. |
| 6 | “T+1 Settlement… will lead to ‘Insufficient Settled Funds’ issues” framed as a blocker | `INCOMPLETE-MISLEADING` | Real only for rapid capital recycling. Omits that **selling held positions is unconstrained** and that the default **Instant account gives instant settlement** — which flips the conclusion. |
| 7 | “[Image of autonomous trading system architecture]” | `FILLER-TELL` | Placeholder for a non-existent image; generated filler with no content behind it. |
| 8 | “Native Robinhood MCP requires an ‘Agentic Account’ setup, isolating your capital” | `HELD-UP` (but lucky) | The isolated Agentic account is real — but it was presented via the same fabricated “pressure test,” so it was a correct guess dressed as a verified finding, not a sourced fact. |
| 9 | “The agent is state-free… a local `state_tracker.json` is required” | `HELD-UP` | Sound. |
| 10 | “`yfinance` is a scraper and unstable for production” | `HELD-UP` | Fair. |
| 11 | “No native daily recap exists; custom SMTP/email integration is necessary” | `HELD-UP` | Fair — no native programmatic recap. |

**The dominant pattern (catch rule):** the blueprint’s worst errors were not random — they were confident, official-sounding prohibitions (“requires,” “you cannot,” “required by compliance”) about a system the author never actually inspected, with **zero primary-source links**, and dressed in verification language (“Subagent Pressure Test Results”). _Any claim of the form “X requires / you cannot / compliance mandates,” stated about a system without a primary-source link, is suspect until independently verified._ Two of three such claims here were fabricated.

**My own v1 error, logged for symmetry:** v1 of this doc claimed “fund $2,000 to unlock the new intraday rules.” That was a `[INVENTED-CONSTRAINT]`-adjacent category error — $2,000 is the pre-existing Rule 4210(b)(4) margin-account minimum, unchanged by the June 4 amendment and inapplicable to cash accounts. Two independent reviewers caught it. Same discipline applies to me.
