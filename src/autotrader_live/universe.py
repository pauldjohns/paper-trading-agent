"""Universe-discovery pipeline for the LIVE-01 paper-monitor (Task T2.2).

Pipeline: scan → tradability + EQUITY gate → historicals → decide → cost-tier
          → earnings-blackout → rank by momentum → select top-N.

This module is a PURE function over a ``MarketData`` provider (pre-fetched;
no MCP calls here).  All MCP I/O happens in the agent loop before this
module is called.

NO-PLACE INVARIANT (§2.5): this module contains NONE of the forbidden broker
mutation tokens.  The source-scan enforcer in
``tests/live/test_no_place_invariant.py`` will catch any violation.

Tuneable defaults (surfaced here for visibility):
    near_threshold = 0.90   -- nearness fraction for FLAG A in decide()
    blackout_days  = 5      -- calendar days pre-earnings to exclude
    top_n          = 10     -- maximum number of entry-eligible candidates
    rank_key       = "mom_252"  -- TrendDecision field used for ranking
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from autotrader_live.cost_tier import CostTier, cost_tier_for
from autotrader_live.mcp_live import EarningsEvent, MarketData, ScanRow
from autotrader_live.strategy_trend import TrendDecision, decide

_LOG = logging.getLogger(__name__)

# Minimum settled bars required by the full indicator burn-in (253 = max of
# the 200-day SMA, 252-day trailing return, and 56-day Donchian look-back).
_MIN_BARS: int = 253


# =============================================================================
# Result shapes
# =============================================================================


@dataclass(frozen=True)
class Candidate:
    """One scanned name, fully annotated.

    ALL scanned names appear in ``UniverseResult.candidates`` — including
    those that were dropped.  Excluded names carry a non-None ``drop_reason``.

    Attributes
    ----------
    symbol:
        Ticker string.
    decision:
        ``TrendDecision`` (or ``None`` if the name was dropped before the
        decide step, e.g. due to missing historicals).
    cost_tier:
        The assigned ``CostTier`` (always set; defaults to ``TIER_OTHER``
        via fail-closed logic if fundamentals are absent).
    tradeable:
        ``True`` iff the tradability gate passed.
    earnings_blackout:
        ``True`` iff an upcoming unreported earnings event falls within
        ``blackout_days`` of ``signal_date``.
    drop_reason:
        Human-readable reason this name was excluded from ``selected``, or
        ``None`` if the name is entry-eligible.  Examples:
        ``"not_equity"``, ``"not_tradeable"``, ``"no_historicals"``,
        ``"insufficient_history"``, ``"earnings_blackout"``,
        ``"decision_no_entry"``, ``"error:<short_message>"``.
    """

    symbol: str
    decision: TrendDecision | None
    cost_tier: CostTier
    tradeable: bool
    earnings_blackout: bool
    drop_reason: str | None


@dataclass(frozen=True)
class UniverseResult:
    """Full output of one ``build_universe`` call.

    Attributes
    ----------
    signal_date:
        The date for which signals were computed (last settled session bar).
    candidates:
        Every name from the scan, with ``drop_reason`` set for those excluded.
    selected:
        The top-N entry-eligible candidates, ranked by ``rank_key`` descending
        (ties broken by symbol ascending for determinism).
    params:
        The build parameters used (near_threshold, blackout_days, top_n,
        rank_key).  Pinned into the record so the output is self-describing.
    """

    signal_date: dt.date
    candidates: list[Candidate]
    selected: list[Candidate]
    params: dict[str, Any]


# =============================================================================
# Pipeline
# =============================================================================


def build_universe(
    market_data: MarketData,
    *,
    signal_date: dt.date,
    near_threshold: float = 0.90,
    blackout_days: int = 5,
    top_n: int = 10,
    rank_key: str = "mom_252",
) -> UniverseResult:
    """Run the full universe-discovery pipeline over pre-fetched market data.

    Parameters
    ----------
    market_data:
        Pre-populated ``MarketData`` provider (``StaticMarketData`` in tests;
        the agent-populated live provider in production).  No MCP calls are
        made here.
    signal_date:
        The last settled session date.  Used for the earnings-blackout window
        and stored in the result for auditability.
    near_threshold:
        Passed to ``decide()`` as the nearness fraction for FLAG A.
        Default 0.90 (within 10% of the trailing-252 high).
        **Tunable for the operator.**
    blackout_days:
        Number of calendar days on/after ``signal_date`` within which an
        upcoming earnings event triggers a blackout.
        Default 5.  **Tunable for the operator.**
    top_n:
        Maximum number of entry-eligible candidates in ``selected``.
        Default 10.  **Tunable for the operator.**
    rank_key:
        ``TrendDecision`` field used to rank entry-eligible candidates
        (descending).  Default ``"mom_252"``.  **Tunable for the operator.**

    Returns
    -------
    UniverseResult
        Frozen result with all candidates and the selected top-N.

    Notes
    -----
    Robustness contract: a single bad name (bad data, ``decide`` raises, etc.)
    does NOT crash the build.  The name is caught, assigned
    ``drop_reason="error:<short_message>"``, and excluded from ``selected``.
    """
    params: dict[str, Any] = {
        "near_threshold": near_threshold,
        "blackout_days": blackout_days,
        "top_n": top_n,
        "rank_key": rank_key,
    }

    # Pre-fetch shared data once (earnings covers all symbols in the scan)
    earnings_map: dict[str, EarningsEvent] = market_data.earnings()

    # Blackout deadline: signal_date + blackout_days (inclusive on both ends)
    blackout_deadline: dt.date = signal_date + dt.timedelta(days=blackout_days)

    scan_rows: list[ScanRow] = market_data.scan()

    candidates: list[Candidate] = []
    entry_eligible: list[tuple[float, str, Candidate]] = []  # (rank_val, symbol, candidate)

    for row in scan_rows:
        symbol = row.symbol
        candidate = _process_row(
            row=row,
            symbol=symbol,
            signal_date=signal_date,
            near_threshold=near_threshold,
            market_data=market_data,
            earnings_map=earnings_map,
            blackout_deadline=blackout_deadline,
            rank_key=rank_key,
        )
        candidates.append(candidate)

        if candidate.drop_reason is None and candidate.decision is not None and candidate.decision.entry:
            rank_val = getattr(candidate.decision, rank_key)
            entry_eligible.append((rank_val, symbol, candidate))

    # Rank entry-eligible: descending by rank_key, ties broken by symbol ascending.
    # Python's sort is stable; sorting by (symbol asc) first then by (-rank_val)
    # ensures that ties on rank_val break alphabetically.
    entry_eligible.sort(key=lambda t: (-t[0], t[1]))

    selected: list[Candidate] = [c for _, _, c in entry_eligible[:top_n]]

    return UniverseResult(
        signal_date=signal_date,
        candidates=candidates,
        selected=selected,
        params=params,
    )


def _process_row(
    *,
    row: ScanRow,
    symbol: str,
    signal_date: dt.date,
    near_threshold: float,
    market_data: MarketData,
    earnings_map: dict[str, EarningsEvent],
    blackout_deadline: dt.date,
    rank_key: str,
) -> Candidate:
    """Process one scan row; return a fully annotated ``Candidate``.

    Any exception during processing is caught and converts the row to a
    ``drop_reason="error:<short_message>"`` candidate so one bad name cannot
    crash the whole build.
    """
    try:
        return _process_row_unsafe(
            row=row,
            symbol=symbol,
            signal_date=signal_date,
            near_threshold=near_threshold,
            market_data=market_data,
            earnings_map=earnings_map,
            blackout_deadline=blackout_deadline,
            rank_key=rank_key,
        )
    except Exception as exc:  # noqa: BLE001
        short_msg = str(exc)[:80].replace("\n", " ")
        _LOG.warning("universe: error processing %s: %s", symbol, exc)
        return Candidate(
            symbol=symbol,
            decision=None,
            cost_tier=_safe_cost_tier(symbol, market_data),
            tradeable=False,
            earnings_blackout=False,
            drop_reason=f"error:{short_msg}",
        )


def _process_row_unsafe(
    *,
    row: ScanRow,
    symbol: str,
    signal_date: dt.date,
    near_threshold: float,
    market_data: MarketData,
    earnings_map: dict[str, EarningsEvent],
    blackout_deadline: dt.date,
    rank_key: str,
) -> Candidate:
    """Core per-row logic (may raise; caller wraps in try/except)."""

    # ── Step 1: EQUITY gate ────────────────────────────────────────────────────
    if row.instrument_type != "EQUITY":
        return Candidate(
            symbol=symbol,
            decision=None,
            cost_tier=_safe_cost_tier(symbol, market_data),
            tradeable=False,
            earnings_blackout=False,
            drop_reason="not_equity",
        )

    # ── Step 2: tradability gate ───────────────────────────────────────────────
    trad_map = market_data.tradability([symbol])
    trad = trad_map.get(symbol)
    tradeable: bool = trad.tradeable if trad is not None else False

    if not tradeable:
        return Candidate(
            symbol=symbol,
            decision=None,
            cost_tier=_safe_cost_tier(symbol, market_data),
            tradeable=False,
            earnings_blackout=False,
            drop_reason="not_tradeable",
        )

    # ── Step 3: historicals gate ───────────────────────────────────────────────
    try:
        bars: pd.DataFrame = market_data.historicals(symbol)
    except KeyError:
        return Candidate(
            symbol=symbol,
            decision=None,
            cost_tier=_safe_cost_tier(symbol, market_data),
            tradeable=tradeable,
            earnings_blackout=False,
            drop_reason="no_historicals",
        )

    if bars is None or len(bars) < _MIN_BARS:
        return Candidate(
            symbol=symbol,
            decision=None,
            cost_tier=_safe_cost_tier(symbol, market_data),
            tradeable=tradeable,
            earnings_blackout=False,
            drop_reason="insufficient_history",
        )

    # ── Step 4: decide ────────────────────────────────────────────────────────
    dec: TrendDecision = decide(bars, symbol, near_threshold=near_threshold)

    # decide() returns reason="insufficient_history" when burn-in is incomplete
    if dec.reason == "insufficient_history":
        return Candidate(
            symbol=symbol,
            decision=dec,
            cost_tier=_safe_cost_tier(symbol, market_data),
            tradeable=tradeable,
            earnings_blackout=False,
            drop_reason="insufficient_history",
        )

    # ── Step 5: cost tier (fail-closed) ───────────────────────────────────────
    fund_map = market_data.fundamentals([symbol])
    fund = fund_map.get(symbol)
    mc = fund.market_cap if fund is not None else None
    av = fund.average_volume if fund is not None else None
    cost_tier = cost_tier_for(mc, av)

    # ── Step 6: earnings blackout ──────────────────────────────────────────────
    event = earnings_map.get(symbol)
    earnings_blackout: bool = (
        event is not None
        and not event.reported
        and signal_date <= event.report_date <= blackout_deadline
    )

    # ── Step 7: entry eligibility ──────────────────────────────────────────────
    # A name IS eligible iff: entry signal, tradeable, not in blackout, no drop_reason.
    # drop_reason is None only for eligible names.
    if not dec.entry:
        drop_reason: str | None = "decision_no_entry"
    elif earnings_blackout:
        drop_reason = "earnings_blackout"
    else:
        drop_reason = None  # ENTRY ELIGIBLE

    return Candidate(
        symbol=symbol,
        decision=dec,
        cost_tier=cost_tier,
        tradeable=tradeable,
        earnings_blackout=earnings_blackout,
        drop_reason=drop_reason,
    )


def _safe_cost_tier(symbol: str, market_data: MarketData) -> CostTier:
    """Return the cost tier for *symbol*, defaulting to TIER_OTHER on any error."""
    try:
        fund_map = market_data.fundamentals([symbol])
        fund = fund_map.get(symbol)
        mc = fund.market_cap if fund is not None else None
        av = fund.average_volume if fund is not None else None
        return cost_tier_for(mc, av)
    except Exception:  # noqa: BLE001
        from autotrader_live.cost_tier import TIER_OTHER as _TIER_OTHER
        return _TIER_OTHER
