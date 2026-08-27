# LIVE-03 POLL (headless, recurring ~every 15 min, RTH window starting AT/after ARM)

Same hard guardrails as ARM: PAPER ONLY, broker READ-ONLY, NEVER call the six
order-surface tools, account 987654321, venv python, absolute paths.

1. DECIDE: action = `run_paper_book.py poll-decide <now_utc_iso>`. Branch:
   - EXIT          -> stop silently.
   - SELF_HEAL_ARM -> run the full ARM prompt now (a failed ARM must not kill the
                      trading day), then continue to a NORMAL poll. (The ARM prompt's
                      step-0 .arm_inprogress lock makes this safe if the scheduled
                      ARM is still mid-fetch — SELF_HEAL exits rather than double-clear.)
   - NORMAL        -> proceed to step 2.
   - FINAL         -> proceed to step 2, then step 4.
2. QUOTES: load held+armed symbols from `run_paper_book.py status`; fetch their
   get_equity_quotes (<=20/batch) -> data/live/paper_book/raw/quotes.json verbatim.
3. POLL: run_paper_book.py poll <now_utc_iso>. (poll_day is derived in ET.)
4. FINAL only: run_paper_book.py mark-eod <today>. (The EOD task pushes the summary.)
5. NOTIFY ON EVENTS ONLY: if this poll produced fills or stop-outs, push a one-line
   summary with the honesty label. Otherwise stay silent (no "nothing happened").

The schedule includes a dedicated 15:55 ET fire (schedule_state.POLL_SLOTS_ET) so a
close-bell breakout (the AMAT-15:59 case) is captured by a NORMAL in-session poll,
not only the FINAL boundary fire. DEATH SIGNAL: broker auth/unreachable -> push
"<your re-auth message>" and stop. Missing/bad quote for a name -> the driver marks
it at cost and the resting stop stands; NEVER force-sell on bad data. (Half-day
13:00 ET closes have no dedicated close-bell fire — covered by FINAL; known limit.)
