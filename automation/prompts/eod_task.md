# LIVE-03 EOD (recurring, weekdays ~16:45 ET, single fire — after the FINAL poll)

MCP-FREE. 1. Run `eod_audit.py`. 2. Push ONE EOD summary: total_equity,
realized_pnl_cum, slots expected/actual/missing, and the honesty label
"simulator-only, orders forbidden; neg-to-breakeven after costs". The audit also
snapshots book.json + equity_curve.jsonl + fills.jsonl to the PRIVATE GitHub
`paper-book-data` branch (off-machine record). Scheduled at 16:45 ET so it always
runs AFTER the 15:55 ET close-bell FINAL poll (no ordering race); re-running is
harmless (idempotent — a no-change EOD commits nothing).
