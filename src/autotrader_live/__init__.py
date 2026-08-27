# src/autotrader_live/__init__.py
"""autotrader_live — the forward live/paper track (LIVE-01 paper-monitor).

FIREWALL (see IMPLEMENTATION_PLAN_LIVE_01_paper_monitor.md §2): this package imports
`autotrader` as a LIBRARY. It NEVER edits the offline harness — the Task-8 golden
Simulator / stops / ledger / engine and the STRATEGY_COST_FLOORS registry are read-only
from here. New OHLC-consuming indicators live in this package's `indicators_ohlc.py`,
NOT appended to the protected close-series `autotrader.indicators`.

NO-PLACE INVARIANT (§2.5): nothing in this package's import graph may reach
any MCP broker-mutation call (the place/cancel/review order family).
For the current offline package this is enforced by a source-scan test:
``tests/live/test_no_place_invariant.py``.  The runtime monkeypatch tripwire —
patching the order-placement MCP tools to fail-on-call across the PaperBroker
import graph — lands with the broker seam at T2.3.
"""
