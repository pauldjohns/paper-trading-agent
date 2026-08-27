# Robinhood Agentic MCP — Capabilities Reference

_Captured 2026-06-16 by direct introspection of the live connected MCP (server tools + read-only calls). **No orders were placed, reviewed, or cancelled.** Order behavior marked “untested” below needs one deliberate, authorized test trade after the account is funded._

This file is the on-file record of what the MCP actually exposes, what it does not, and how each fact shapes the build. It supersedes the public-docs guesses in `FINDINGS_AND_PROPOSAL.md` §2 wherever they conflict.

---

## 1. Account setup (confirmed live)

| Account | Type | Agent can trade? | Options | Funded | Role |
|---|---|---|---|---|---|
| ••••XXXX (default) | **margin** | **No** (`agentic_allowed: false`) | off | — | Your main account. The agent is hard-walled out of it. |
| ••••YYYY “Agentic” | **cash** | **Yes** (`agentic_allowed: true`) | off (`option_level` empty) | **$0 — not funded yet** | The only account the agent can touch. |

What this settles:
- **The Agentic account is a CASH account.** No margin, no borrowing, zero Pattern-Day-Trader exposure. Settlement (T+1) is the only pacing rule, and only for re-deploying the *same* dollars — selling is unconstrained. This matches our scoped strategy exactly.
- **Options are disabled** on it. The MCP exposes options *tools*, but this account cannot place options until upgraded. Equities-only in practice.
- **It is empty.** Backtest and paper-monitor need no money. Going live later needs a deposit into ••••YYYY.
- The agent must be handed an `account_number` explicitly on every call; it cannot default to one. Trading the main account is impossible by design.

---

## 2. Full tool inventory (35 tools)

**Account & portfolio:** `get_accounts`, `get_portfolio` (value + real-time buying power), `get_equity_positions`, `get_option_positions`
**Equity market data:** `get_equity_quotes`, `get_equity_historicals`, `get_equity_fundamentals`, `get_equity_tradability`
**Index data:** `get_index_quotes`, `get_indexes`
**Options data:** `get_option_chains`, `get_option_instruments`, `get_option_quotes`
**Search:** `search` (resolves names → instruments; also crypto pairs and indexes)
**Watchlists:** `get_watchlists`, `get_watchlist_items`, `get_popular_watchlists`, `create_watchlist`, `update_watchlist`, `add_to_watchlist`, `remove_from_watchlist`, `follow_watchlist`, `unfollow_watchlist`, plus option-watchlist variants
**Equity orders:** `review_equity_order`, `place_equity_order`, `cancel_equity_order`, `get_equity_orders`
**Options orders:** `review_option_order`, `place_option_order`, `cancel_option_order`, `get_option_orders`

No tool for: streaming/websocket quotes (polling only), crypto orders (read-only crypto via search), corporate-action calendars, news/sentiment, or analyst ratings.

---

## 3. Order capabilities — `place_equity_order` / `review_equity_order`

The most decision-relevant surface. Confirmed from the live schema:

| Parameter | Values | Notes |
|---|---|---|
| `type` | **`market`, `limit`, `stop_market`, `stop_limit`** | **Native stop orders confirmed.** No native *trailing* stop (emulate by moving a stop). |
| `side` | `buy`, `sell` | |
| `quantity` | shares; **fractional** (≤6 dp) only for `market` + `regular_hours` | |
| `dollar_amount` | USD notional | **`market` only.** Lets you buy “$100 of X.” |
| `limit_price` | required for `limit` / `stop_limit` | |
| `stop_price` | required for `stop_market` / `stop_limit` | |
| `time_in_force` | **`gfd`** (day) or **`gtc`** (good-till-cancelled) | GTC ⇒ a resting stop persists across sessions. |
| `market_hours` | `regular_hours` (default), `extended_hours`, `all_day_hours` | Fractional/dollar orders are regular-hours only. |
| `ref_id` | UUID idempotency key | **Re-send the same UUID on a retry; upstream deduplicates.** Solves double-submit. |
| `account_number` | required, must be `agentic_allowed=true` | Non-agentic accounts are rejected outright. |

- **`review_equity_order`** simulates without placing and returns the live quote plus **pre-trade alerts** (buying power, PDT, instrument halt). Intended as the pre-flight check before `place_equity_order`.
- **Autonomy:** the “get explicit user confirmation before placing” instruction lives in the *tool description* (client-side guidance to the agent), **not** as a Robinhood-server-enforced gate. So fully-autonomous placement is possible at the protocol level; whether the Robinhood app *also* surfaces an approval step is the one thing schema-inspection can’t tell us (see §6).
- **`cancel_equity_order`** by `order_id` (from `get_equity_orders`); may be rejected if already filled/cancelled.
- **`get_equity_orders`** filters by `state`, `symbol`, `created_at_gte`, and `placed_agent` (`user` / `agentic` / `recurring` / …) — so agent-placed orders are tagged and auditable.

---

## 4. Market-data capabilities (confirmed by live calls)

- **`get_equity_historicals` — backtest-grade and deep.** SPY returned clean monthly bars **back to January 2005** (21 years). **Split-adjusted by default** (the right default for backtesting). **⚠ Correction (2026-06-17): `adjustment_type='all'` does NOT supply usable dividends for the deep history** — contrary to the original capture, `'all'` is a broken *subtractive* series (negative 2009 prices; **no per-event dividends before ~2013**, the `split−all` offset is frozen across every 2008 ex-date), and there is no dividend-history tool or total-return index. The backtest basis is therefore **price-return (split-adjusted)**, not total return. Use `adjustment_type='split'` everywhere; see `STRATEGY_TESTING_SPEC.md` §3.1 and the `PROJECT_CONTEXT.md` data-limitation note. Intervals from `15second` to `50year` (daily = `day`). Up to **10 symbols/call**. Soft cap ~2,500 bars when an explicit interval is given (page the range for full daily history; the cap doesn’t apply when interval is auto-selected). Daily AAPL bars came back clean for 2026 YTD. **⇒ No external data vendor (Polygon/yfinance) is needed.**
- **`get_equity_quotes` — real-time with NBBO.** Returns `last_trade_price` + **bid/ask** + venue timestamps + the official prior-session close. Observed spreads were tiny on liquid names (SPY ≈ $0.06 / 0.008%, AAPL ≈ $0.26 / 0.087%). **⇒ Good enough for reactive exits and for measuring real trading cost.** Polling, not streaming.
- **`get_equity_fundamentals`** — PE, P/B, market cap, shares/float, 52-week range, **average volume** (liquidity screen), dividend schedule, sector/industry, profile. Usable for the candidate screen.
- **`get_equity_tradability`** — per-symbol, per-session, fractional, short-selling, and per-account-type eligibility. Call before ordering. (AAPL & SPY: tradeable, fractional yes.)

---

## 5. How this changes our build decisions

1. **Native stops simplify the exit and cut the worst risk.** The “catch a falling price” exit can be a real `stop_market`/`stop_limit` **GTC** order resting at Robinhood, not just bot-side polling. It reacts at the exchange and works even when the controller is asleep or offline — which neutralizes the reviewer’s biggest objection (“a synthetic stop fails exactly when you need it”). We still poll for trailing logic (move the stop up) and for non-price exits.
2. **Data source = the MCP itself.** Backtest history (2005+), live quotes, fundamentals, and tradability all come from one place. Drop the planned external feed. One fewer dependency, and the “yfinance is unreliable” gotcha disappears. Only caveat: page daily history in ≤2,500-bar chunks.
3. **Idempotency is built in.** A per-order `ref_id` UUID retried safely ⇒ the double-submit failure mode is handled by design, not by us reconciling after the fact.
4. **Pre-trade alerts are built in.** `review_equity_order` surfaces buying-power / PDT / halt before placing ⇒ a free sanity gate in front of every order.
5. **Cash account ⇒ our settlement model stands.** Hold-then-exit fits; selling is unconstrained; re-deploying the same cash waits T+1. No margin mechanics to reason about.
6. **Equities-only is enforced by the account, not just chosen.** Options need an account upgrade; until then the option tools will reject.
7. **Hard wall around the main account.** The agent can only act on ••••YYYY, and only when handed that number. The blast radius of any bug is the Agentic account’s balance.

---

## 6. Not tested / still open (needs a live action, not schema-reading)

- **Does autonomous placement actually fill, or does the Robinhood app inject an approval step?** Resolve with **one deliberate, authorized, tiny test order** after funding (e.g., review → place 1 share of a cheap liquid name, observe whether it fills hands-off and how it’s tagged). Not done here because no orders were authorized.
- **Real contents of pre-trade alerts** on a cash account (PDT/GFV/settlement messaging) — visible only once `review_equity_order` is run against a funded account.
- **Rate limits / throttling** — undocumented; discover empirically and back off politely.
- **Auth/session token lifetime for an unattended loop** — the connector works in this interactive session; a 24/7 headless controller’s re-auth cadence still needs a real run to confirm.
- **ToS for unattended programmatic loops** vs interactive agent sessions — a policy question, not answerable from the schema.

_Account numbers are masked here intentionally; the controller should read the live `account_number` from `get_accounts` at runtime (or an env var), not hardcode it in a doc._
