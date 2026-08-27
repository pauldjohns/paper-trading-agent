# src/autotrader_live/paper_monitor.py
"""Daily-loop planning + recording for the LIVE-01 paper-monitor (Tasks T3a/T3b).

Roles
-----
- ``Position``                : frozen record of a held position.
- ``DayPlan``                 : frozen output of one ``plan_day`` call.
- ``DayRecord``               : fully-serializable per-date record (T3b).
- ``LookAheadError``          : raised when a bar is dated AFTER signal_date.
- ``StaleDataError``          : raised when the last settled bar is BEFORE signal_date.
- ``resolve_signal_date``     : returns the last completed session date.
- ``completed_bar_guard``     : validates bars up to and including signal_date.
- ``plan_day``                : builds the day's would-be order envelope (planning only).
- ``record_day``              : merges reviews into a DayRecord, atomically persists (T3b).
- ``record_skip``             : writes a loud attributed-skip DayRecord (T3b).
- ``load_day_record``         : reloads a DayRecord from disk (T3b).
- ``is_day_complete``         : True iff a 'complete' record exists on disk (T3b).
- ``append_telemetry``        : appends one JSONL row to the telemetry log (T3b).
- ``run_day``                 : idempotent orchestrator: plan → review → record (T3b).

Architecture notes
------------------
This module is OFFLINE/pure — no MCP calls, no I/O beyond the state_dir writes
that ``record_day``/``record_skip`` perform.  The agent loop fetches data via MCP
and passes a pre-populated ``MarketData`` + ``positions`` dict in.

``record_day`` and ``record_skip`` write atomically via a .tmp → os.replace pair
so a crash mid-write never leaves a corrupt state file.

``run_day`` is the happy-path orchestrator. Auth/fetch faults that occur BEFORE
``run_day`` is called must be caught by the caller, which then calls
``record_skip`` to write an attributed skip record.  ``run_day`` itself stays the
happy path and does not call ``record_skip`` internally.

NO-PLACE INVARIANT (§2.5): this module contains NONE of the forbidden broker
mutation tokens.  The source-scan enforcer in
``tests/live/test_no_place_invariant.py`` will catch any violation.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from autotrader_live.order_types import OrderIntent, ReviewResult
from autotrader_live.exits import initial_catastrophe_stop, update_trailing_stop
from autotrader_live.mcp_live import MarketData
from autotrader_live.sizing import size
from autotrader_live.strategy_trend import TrendDecision, decide
from autotrader_live.universe import Candidate, build_universe

_LOG = logging.getLogger(__name__)

# ── custom exceptions ─────────────────────────────────────────────────────────


class LookAheadError(ValueError):
    """Raised when a bar's date is strictly after ``signal_date``.

    This indicates a look-ahead violation: data not yet settled was consumed.
    """


class StaleDataError(ValueError):
    """Raised when the last settled bar is strictly before ``signal_date``.

    This indicates that the feed hasn't caught up — the data is stale.
    """


# ── frozen shapes ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Position:
    """Record of one held position.

    Attributes
    ----------
    symbol:
        Ticker string.
    shares:
        Number of shares held (fractional allowed).
    entry_price:
        Price at which the position was entered.
    entry_date:
        Settlement date of the entry bar.
    current_stop:
        The current resting GTC catastrophe/ratchet stop price.
    highest_high_since_entry:
        The maximum high bar since entry (used for chandelier ratchet).
    ratchet_seq:
        Monotonically incrementing counter; incremented each time the stop
        is ratcheted upward.  The initial catastrophe stop has ratchet_seq=0.
    """

    symbol: str
    shares: float
    entry_price: float
    entry_date: dt.date
    current_stop: float
    highest_high_since_entry: float
    ratchet_seq: int

    def __post_init__(self) -> None:
        if self.shares <= 0:
            raise ValueError(
                f"Position.shares must be > 0, got {self.shares!r} for {self.symbol!r}"
            )
        if self.entry_price <= 0:
            raise ValueError(
                f"Position.entry_price must be > 0, got {self.entry_price!r} for {self.symbol!r}"
            )
        if self.current_stop <= 0:
            raise ValueError(
                f"Position.current_stop must be > 0, got {self.current_stop!r} for {self.symbol!r} "
                f"(a non-positive stop assembled from get_equity_positions must fail loudly, "
                f"not flow into a ratchet)"
            )


@dataclass(frozen=True)
class DayPlan:
    """Full planning output for one daily run.

    Attributes
    ----------
    signal_date:
        The last completed session date used for all signals.
    selected:
        Entry-eligible top-N candidates from ``build_universe``.
    order_intents:
        Ordered list of ``OrderIntent`` objects (entry + catastrophe_stop for
        new names; ratchet for held names that require a stop update).
    held_halted:
        Symbols of held positions whose ratchet update was halted due to
        missing/stale bar data.  Their GTC stop persists at the broker;
        no exit intent is emitted.
    skipped:
        List of (symbol, reason) tuples for names that were skipped during
        intent construction.  Reasons: "infeasible_catastrophe_stop", etc.
    earnings_flags:
        Mapping of symbol → earnings_blackout flag from the universe result.
    loss_halt:
        Detection-only summary dict for book-level loss-halt logic.
        Never causes exit intents to be emitted.
    reconciled:
        True iff all selected candidates passed the in-run determinism check
        (re-running ``decide`` matched the ``build_universe`` decision).
    notes:
        Free-text list of audit notes accumulated during planning.
    """

    signal_date: dt.date
    selected: list[Candidate]
    order_intents: list[OrderIntent]
    held_halted: list[str]
    skipped: list[tuple[str, str]]
    earnings_flags: dict[str, bool]
    loss_halt: dict[str, Any]
    reconciled: bool
    notes: list[str]


# ── functions ─────────────────────────────────────────────────────────────────


def resolve_signal_date(calendar: Any, today: dt.date) -> dt.date:
    """Return the last completed session date strictly before ``today``.

    A morning RTH run (e.g. 09:45 ET) decides on the PRIOR settled bar, so
    ``signal_date`` is always the most recent trading day that is STRICTLY
    BEFORE ``today``.

    Parameters
    ----------
    calendar:
        A ``TradingCalendar`` instance (has ``_days`` attribute — sorted list
        of trading dates).
    today:
        The current calendar date (must have at least one trading day before
        it within the calendar's range).

    Returns
    -------
    dt.date — the last trading day strictly before ``today``.

    Raises
    ------
    ValueError
        If no trading day before ``today`` exists in the calendar.
    """
    days = calendar._days  # sorted list of dt.date
    # Filter to days strictly before today
    prior_days = [d for d in days if d < today]
    if not prior_days:
        raise ValueError(
            f"resolve_signal_date: no trading day before {today!r} in calendar range "
            f"({days[0] if days else 'empty'} .. {days[-1] if days else 'empty'})"
        )
    return prior_days[-1]


def completed_bar_guard(bars: pd.DataFrame, signal_date: dt.date) -> pd.DataFrame:
    """Validate that ``bars`` represent completed data up to ``signal_date``.

    Parameters
    ----------
    bars:
        OHLCV DataFrame with a ``date`` column (``datetime.date`` values),
        already with interpolated bars dropped (``normalize_bars`` handles
        that upstream).  Must have at least ``["date"]`` column.
    signal_date:
        The expected date of the last settled bar.

    Returns
    -------
    pd.DataFrame — ``bars`` unchanged on success.

    Raises
    ------
    ValueError
        If ``bars`` is empty, or dates are not strictly ascending.
    LookAheadError
        If any bar has a date strictly after ``signal_date`` (look-ahead).
    StaleDataError
        If the last bar's date is strictly before ``signal_date`` (stale feed).
    """
    if bars is None or len(bars) == 0:
        raise ValueError("completed_bar_guard: bars must be non-empty")

    dates = bars["date"].tolist()

    # Strictly ascending check
    for i in range(1, len(dates)):
        if dates[i] <= dates[i - 1]:
            raise ValueError(
                f"completed_bar_guard: dates are not strictly ascending at index {i}: "
                f"{dates[i - 1]} -> {dates[i]}"
            )

    # Look-ahead check: no bar dated after signal_date
    last_date = dates[-1]
    max_date = max(dates)
    if max_date > signal_date:
        raise LookAheadError(
            f"completed_bar_guard: LookAheadError — bar dated {max_date!r} is strictly "
            f"after signal_date={signal_date!r}. Bars must not include unsettled data."
        )

    # Stale check: last bar must equal signal_date
    if last_date < signal_date:
        raise StaleDataError(
            f"completed_bar_guard: StaleDataError — last bar date {last_date!r} is "
            f"strictly before signal_date={signal_date!r}. Feed has not caught up."
        )

    return bars


def plan_day(
    market_data: MarketData,
    positions: dict[str, Position],
    *,
    signal_date: dt.date,
    account_number: str,
    equity: float,
    config: dict | None = None,
) -> DayPlan:
    """Build the day's would-be order envelope.

    Calls ``build_universe`` for entry-eligible names, then:
    - For NEW entries (selected names not already in ``positions``): emits an
      ``entry`` + ``catastrophe_stop`` ``OrderIntent`` pair.
    - For HELD positions (names in ``positions``): emits a ``ratchet`` intent if
      the chandelier stop has risen; emits NOTHING if it hasn't (monotonic-up).
      If bars are unavailable, halts the ratchet (the resting GTC stop persists;
      no exit intent is ever emitted for a held name).
    - Runs an in-run determinism reconcile: re-runs ``decide`` for each selected
      name and asserts it byte-matches the ``build_universe`` decision.

    Parameters
    ----------
    market_data:
        Pre-populated ``MarketData`` provider.
    positions:
        Current held positions keyed by symbol.  Empty dict for day-1.
    signal_date:
        Last completed session date (from ``resolve_signal_date``).
    account_number:
        Robinhood account number for all ``OrderIntent`` objects.
    equity:
        Total account equity in dollars (for sizing).
    config:
        Optional override dict for tunable parameters.  Keys accepted:
        ``near_threshold``, ``blackout_days``, ``top_n``, ``f``, ``k``,
        ``per_name_cap_frac``, ``m``.

    Returns
    -------
    DayPlan
        Frozen planning output for this day.
    """
    if equity <= 0:
        raise ValueError(
            "plan_day requires equity > 0 (nominal sizing capital; "
            "the $0 account is separate — review returns EQUITY_NOT_ENOUGH_BP, "
            "but sizing uses this nominal equity param)"
        )

    cfg = config or {}

    # Tunable parameters with defaults
    near_threshold: float = cfg.get("near_threshold", 0.90)
    blackout_days: int = cfg.get("blackout_days", 5)
    top_n: int = cfg.get("top_n", 10)
    f: float = cfg.get("f", 0.01)
    k: float = cfg.get("k", 3.0)
    per_name_cap_frac: float = cfg.get("per_name_cap_frac", 0.15)
    m: float = cfg.get("m", 2.0)

    notes: list[str] = []
    order_intents: list[OrderIntent] = []
    held_halted: list[str] = []
    skipped: list[tuple[str, str]] = []

    # ── Step 1: build universe ────────────────────────────────────────────────
    uni = build_universe(
        market_data,
        signal_date=signal_date,
        near_threshold=near_threshold,
        blackout_days=blackout_days,
        top_n=top_n,
    )

    # ── Step 2: earnings flags (all candidates) ───────────────────────────────
    earnings_flags: dict[str, bool] = {
        c.symbol: c.earnings_blackout for c in uni.candidates
    }

    # ── Step 3: process selected (new entries) in universe order ──────────────
    for candidate in uni.selected:
        sym = candidate.symbol
        dec: TrendDecision = candidate.decision  # guaranteed non-None in selected

        if sym in positions:
            # Held — handled in Step 4
            continue

        # Validate bars
        try:
            bars = market_data.historicals(sym)
            completed_bar_guard(bars, signal_date)
        except (KeyError, LookAheadError, StaleDataError, ValueError) as exc:
            reason = f"bar_guard_failed:{type(exc).__name__}"
            skipped.append((sym, reason))
            notes.append(f"{sym}: skipped entry — {reason}: {exc}")
            continue

        # Size the position
        sz = size(equity, dec.atr14, dec.close, f=f, k=k, per_name_cap_frac=per_name_cap_frac)

        # Compute initial catastrophe stop
        try:
            stop_px = initial_catastrophe_stop(dec.close, dec.atr14, m=m)
        except ValueError as exc:
            skipped.append((sym, "infeasible_catastrophe_stop"))
            notes.append(f"{sym}: skipped — infeasible_catastrophe_stop: {exc}")
            continue

        # Entry intent (market buy, dollar_amount, gfd)
        entry_intent = OrderIntent(
            signal_date=signal_date,
            symbol=sym,
            side="buy",
            intent_type="entry",
            order_type="market",
            account_number=account_number,
            dollar_amount=f"{round(sz['notional'], 2):.2f}",
            time_in_force="gfd",
        )
        # Catastrophe stop intent (stop_market sell, quantity, gtc)
        stop_intent = OrderIntent(
            signal_date=signal_date,
            symbol=sym,
            side="sell",
            intent_type="catastrophe_stop",
            order_type="stop_market",
            account_number=account_number,
            quantity=f"{sz['shares']:.6f}",
            stop_price=f"{round(stop_px, 2):.2f}",
            time_in_force="gtc",
        )
        order_intents.append(entry_intent)
        order_intents.append(stop_intent)

    # ── Step 4: process held positions (sorted by symbol for determinism) ─────
    for sym in sorted(positions.keys()):
        pos = positions[sym]

        # Attempt to fetch and validate bars for ratchet update
        try:
            bars = market_data.historicals(sym)
            completed_bar_guard(bars, signal_date)
        except (KeyError, LookAheadError, StaleDataError, ValueError) as exc:
            # Cannot ratchet — halt the ratchet; the GTC stop at the broker persists
            held_halted.append(sym)
            notes.append(
                f"resting GTC catastrophe stop persists for {sym}; ratchet halted "
                f"(bar fetch/guard failed: {type(exc).__name__}: {exc})"
            )
            continue

        # Re-run decide for current bar to get fresh ATR
        try:
            dec = decide(bars, sym, near_threshold=near_threshold)
        except Exception as exc:  # noqa: BLE001
            held_halted.append(sym)
            notes.append(
                f"resting GTC catastrophe stop persists for {sym}; ratchet halted "
                f"(decide failed: {exc})"
            )
            continue

        # Update the highest high (use the latest bar's high)
        latest_high = float(bars["high"].iloc[-1])
        new_high = max(pos.highest_high_since_entry, latest_high)

        # Compute updated chandelier stop (monotonic-up)
        new_stop = update_trailing_stop(
            pos.current_stop, new_high, dec.atr14, k=k
        )

        if new_stop > pos.current_stop:
            # Stop has risen — emit a ratchet intent
            ratchet_intent = OrderIntent(
                signal_date=signal_date,
                symbol=sym,
                side="sell",
                intent_type="ratchet",
                order_type="stop_market",
                account_number=account_number,
                quantity=f"{pos.shares:.6f}",
                stop_price=f"{round(new_stop, 2):.2f}",
                time_in_force="gtc",
                ratchet_seq=pos.ratchet_seq + 1,
            )
            order_intents.append(ratchet_intent)
        # Otherwise: stop unchanged — monotonic-up; no intent emitted

    # ── Step 5: in-run determinism reconcile ──────────────────────────────────
    # Re-run decide for each selected name; assert byte-match with universe result.
    for candidate in uni.selected:
        sym = candidate.symbol
        try:
            bars = market_data.historicals(sym)
        except KeyError:
            # If bars aren't available (shouldn't happen for selected), skip
            notes.append(f"reconcile: {sym} bars unavailable, skipping check")
            continue
        re_dec = decide(bars, sym, near_threshold=near_threshold)
        if dataclasses.asdict(re_dec) != dataclasses.asdict(candidate.decision):
            raise RuntimeError(
                f"plan_day: DETERMINISM FAILURE for {sym} — "
                f"decide() re-run produced a different TrendDecision than "
                f"build_universe recorded.\n"
                f"  build_universe: {dataclasses.asdict(candidate.decision)}\n"
                f"  re-run        : {dataclasses.asdict(re_dec)}"
            )

    # ── Step 6: loss-halt detection (detection-only; no exit intents) ─────────
    if not positions:
        loss_halt: dict[str, Any] = {
            "triggered": False,
            "reason": "no positions / $0 book",
        }
    else:
        # Simple detection: compute unrealized book value vs entry value.
        # The account is $0 so review returns EQUITY_NOT_ENOUGH_BP, but
        # plan_day uses a nominal equity param (e.g. 1000.0 from the runbook)
        # for sizing — equity=0 is never passed here (guarded at entry above).
        loss_halt = {
            "triggered": False,
            "reason": "detection_only",
            "position_count": len(positions),
            "note": (
                "loss-halt thresholds not yet configured; "
                "no exit intents emitted"
            ),
        }

    return DayPlan(
        signal_date=signal_date,
        selected=list(uni.selected),
        order_intents=order_intents,
        held_halted=held_halted,
        skipped=skipped,
        earnings_flags=earnings_flags,
        loss_halt=loss_halt,
        reconciled=True,
        notes=notes,
    )


# ── T3b: DayRecord + persistence helpers ──────────────────────────────────────


@dataclass(frozen=True)
class DayRecord:
    """Fully-serializable per-date record written by ``record_day`` / ``record_skip``.

    Attributes
    ----------
    signal_date:
        The last completed session date this record covers.
    status:
        ``'complete'`` after a successful run; ``'skipped'`` after an attributed
        missed-fire (auth/token/fetch fault or deliberate no-run).
    skip_reason:
        Human-readable attribution for a skipped day.  ``None`` for 'complete'.
    run_timestamp:
        ISO-8601 UTC string INJECTED by the caller; never computed inside this
        module.  Ensures records are reproducible in tests.
    account_number:
        The Robinhood account number used for this run.
    equity:
        Total account equity in dollars at run time.  0.0 in the paper-at-$0 phase.
    selected:
        Per-name dicts: symbol, decision (asdict), cost_tier (name + bps),
        earnings_blackout.
    order_intents:
        Per-intent dicts (dataclasses.asdict of each OrderIntent, plus ref_id).
    reviews:
        Per-review dicts (dataclasses.asdict of each ReviewResult).
    held_halted:
        Symbols whose ratchet update was halted (resting GTC stop persists).
    skipped:
        List of [symbol, reason] pairs for names skipped during intent construction.
    loss_halt:
        Detection-only loss-halt summary dict from ``plan_day``.
    reconciled:
        True iff plan_day's determinism check passed.
    notes:
        Free-text audit notes from ``plan_day``.
    """

    signal_date: dt.date
    status: str                       # 'complete' | 'skipped'
    skip_reason: str | None
    run_timestamp: str                # ISO-8601 UTC; injected by caller
    account_number: str
    equity: float
    selected: list[dict]
    order_intents: list[dict]
    reviews: list[dict]
    held_halted: list[str]
    skipped: list[list]               # list of [symbol, reason]
    loss_halt: dict
    reconciled: bool
    notes: list[str]
    data_source: str = "robinhood_mcp_live"  # provenance (official Robinhood MCP, real live feed)
    earnings_flags: dict = field(default_factory=dict)  # symbol -> earnings_blackout (audit: why excluded)

    def to_dict(self) -> dict:
        """Return a JSON-serializable dict (dates → ISO strings, floats rounded to 10dp)."""
        def _coerce(obj: Any) -> Any:  # noqa: ANN401
            if isinstance(obj, dt.date):
                return obj.isoformat()
            if isinstance(obj, float):
                return round(obj, 10)
            if isinstance(obj, dict):
                return {k: _coerce(v) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [_coerce(x) for x in obj]
            return obj

        return {
            "signal_date": self.signal_date.isoformat(),
            "status": self.status,
            "skip_reason": self.skip_reason,
            "run_timestamp": self.run_timestamp,
            "account_number": self.account_number,
            "equity": round(self.equity, 10),
            "selected": _coerce(self.selected),
            "order_intents": _coerce(self.order_intents),
            "reviews": _coerce(self.reviews),
            "held_halted": list(self.held_halted),
            "skipped": _coerce(self.skipped),
            "loss_halt": _coerce(self.loss_halt),
            "reconciled": self.reconciled,
            "notes": list(self.notes),
            "data_source": self.data_source,
            "earnings_flags": _coerce(self.earnings_flags),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DayRecord":
        """Reconstruct a ``DayRecord`` from the dict produced by ``to_dict``."""
        return cls(
            signal_date=dt.date.fromisoformat(d["signal_date"]),
            status=d["status"],
            skip_reason=d.get("skip_reason"),
            run_timestamp=d["run_timestamp"],
            account_number=d["account_number"],
            equity=float(d["equity"]),
            selected=d.get("selected", []),
            order_intents=d.get("order_intents", []),
            reviews=d.get("reviews", []),
            held_halted=d.get("held_halted", []),
            skipped=d.get("skipped", []),
            loss_halt=d.get("loss_halt", {}),
            reconciled=bool(d.get("reconciled", False)),
            notes=d.get("notes", []),
            data_source=d.get("data_source", "robinhood_mcp_live"),
            earnings_flags=d.get("earnings_flags", {}),
        )


def _atomic_write_json(path: str | Path, obj: dict) -> None:
    """Write *obj* as pretty-printed JSON to *path* using an atomic tmp→replace.

    Writes to ``path + ".tmp"`` first, then calls ``os.replace`` so that a crash
    mid-write never leaves a corrupt target.  ``sort_keys=True`` for determinism.
    """
    path = Path(path)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(obj, indent=2, sort_keys=True)
    tmp_path.write_text(text, encoding="utf-8")
    os.replace(tmp_path, path)


def _candidate_to_dict(c: "Candidate") -> dict:
    """Serialize a ``Candidate`` to a plain dict suitable for JSON."""
    dec_dict: dict | None = dataclasses.asdict(c.decision) if c.decision is not None else None
    # Coerce date objects inside decision (e.g. any date fields)
    if dec_dict is not None:
        dec_dict = {
            k: (v.isoformat() if isinstance(v, dt.date) else v)
            for k, v in dec_dict.items()
        }
    return {
        "symbol": c.symbol,
        "decision": dec_dict,
        "cost_tier": {
            "name": c.cost_tier.name,
            "roundtrip_bps": c.cost_tier.roundtrip_bps,
        },
        "earnings_blackout": c.earnings_blackout,
    }


def _intent_to_dict(intent: OrderIntent) -> dict:
    """Serialize an ``OrderIntent`` to a plain dict (includes ref_id)."""
    d = dataclasses.asdict(intent)
    # signal_date is a datetime.date — coerce to ISO string
    if isinstance(d.get("signal_date"), dt.date):
        d["signal_date"] = d["signal_date"].isoformat()
    d["ref_id"] = intent.ref_id  # add the computed property
    return d


def _review_to_dict(review: ReviewResult) -> dict:
    """Serialize a ``ReviewResult`` to a plain dict."""
    d = dataclasses.asdict(review)
    # Coerce date fields
    if isinstance(d.get("previous_close_date"), dt.date):
        d["previous_close_date"] = d["previous_close_date"].isoformat()
    return d


def record_day(
    plan: DayPlan,
    reviews: list[ReviewResult],
    *,
    state_dir: str | Path,
    run_timestamp: str,
    account_number: str,
    equity: float,
) -> "DayRecord":
    """Build a 'complete' ``DayRecord``, merge reviews by ref_id, atomically persist.

    Parameters
    ----------
    plan:
        The ``DayPlan`` produced by ``plan_day`` for this signal_date.
    reviews:
        The list of ``ReviewResult`` objects produced by ``broker.review`` for
        each intent in ``plan.order_intents``.  This module does NOT call MCP.
    state_dir:
        Directory where ``{signal_date.isoformat()}.json`` will be written.
        Created if it does not exist.
    run_timestamp:
        ISO-8601 UTC string injected by the caller; never computed here.
    account_number:
        Robinhood account number for this run.
    equity:
        Total account equity at run time.

    Returns
    -------
    DayRecord — the record written to disk.

    Notes
    -----
    Merge logic:
    - Reviews are matched to intents by ``ref_id``.
    - A review with no matching intent is still recorded; a note is added.
    - An intent with no matching review gets a note added.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    notes: list[str] = list(plan.notes)

    # Build ref_id → review map
    review_by_ref: dict[str, ReviewResult] = {r.ref_id: r for r in reviews}

    # Detect intents with no matching review
    intent_refs = {i.ref_id for i in plan.order_intents}
    for intent in plan.order_intents:
        if intent.ref_id not in review_by_ref:
            notes.append(
                f"WARNING: intent ref_id={intent.ref_id!r} ({intent.symbol} "
                f"{intent.intent_type}) has no matching review"
            )

    # Detect orphan reviews (review with no matching intent)
    for review in reviews:
        if review.ref_id not in intent_refs:
            notes.append(
                f"WARNING: review ref_id={review.ref_id!r} ({review.symbol}) "
                f"has no matching intent — recorded verbatim"
            )

    record = DayRecord(
        signal_date=plan.signal_date,
        status="complete",
        skip_reason=None,
        run_timestamp=run_timestamp,
        account_number=account_number,
        equity=equity,
        selected=[_candidate_to_dict(c) for c in plan.selected],
        order_intents=[_intent_to_dict(i) for i in plan.order_intents],
        reviews=[_review_to_dict(r) for r in reviews],
        held_halted=list(plan.held_halted),
        skipped=[list(pair) for pair in plan.skipped],
        loss_halt=plan.loss_halt,
        earnings_flags=plan.earnings_flags,
        reconciled=plan.reconciled,
        notes=notes,
    )

    out_path = state_dir / f"{plan.signal_date.isoformat()}.json"
    _atomic_write_json(out_path, record.to_dict())

    return record


def record_skip(
    state_dir: str | Path,
    signal_date: dt.date,
    reason: str,
    *,
    run_timestamp: str,
    account_number: str,
) -> "DayRecord":
    """Write a loud attributed-skip record for ``signal_date``.

    A missed-fire (auth/token/fetch fault) calls this so a missed day is NEVER
    recorded as a clean absence.  The skip_reason is always attributed.

    Parameters
    ----------
    state_dir:
        Directory where ``{signal_date.isoformat()}.json`` will be written.
    signal_date:
        The date for which the run was skipped.
    reason:
        Human-readable attribution for the skip (e.g. 'auth_token_expired',
        'mcp_fetch_failed:AAPL').
    run_timestamp:
        ISO-8601 UTC string injected by the caller.
    account_number:
        Robinhood account number (may be empty string if auth failed before
        account was resolved).

    Returns
    -------
    DayRecord — the 'skipped' record written to disk.
    """
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)

    record = DayRecord(
        signal_date=signal_date,
        status="skipped",
        skip_reason=reason,
        run_timestamp=run_timestamp,
        account_number=account_number,
        equity=0.0,
        selected=[],
        order_intents=[],
        reviews=[],
        held_halted=[],
        skipped=[],
        loss_halt={},
        reconciled=False,
        notes=[f"SKIP ATTRIBUTED: {reason}"],
    )

    out_path = state_dir / f"{signal_date.isoformat()}.json"
    _atomic_write_json(out_path, record.to_dict())

    return record


def load_day_record(
    state_dir: str | Path,
    signal_date: dt.date,
) -> "DayRecord | None":
    """Load a ``DayRecord`` from disk, or return ``None`` if no file exists.

    Parameters
    ----------
    state_dir:
        Directory containing ``{signal_date.isoformat()}.json`` state files.
    signal_date:
        The date to load.

    Returns
    -------
    DayRecord if the file exists and is valid JSON; None otherwise.
    """
    path = Path(state_dir) / f"{signal_date.isoformat()}.json"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8")
    d = json.loads(text)
    return DayRecord.from_dict(d)


def is_day_complete(state_dir: str | Path, signal_date: dt.date) -> bool:
    """Return True iff a 'complete' record exists for ``signal_date`` on disk.

    A 'skipped' record returns False — a skip is NOT a complete run.

    Parameters
    ----------
    state_dir:
        Directory containing state files.
    signal_date:
        The date to check.

    Returns
    -------
    bool
    """
    rec = load_day_record(state_dir, signal_date)
    return rec is not None and rec.status == "complete"


def append_telemetry(telemetry_path: str | Path, record: "DayRecord") -> None:
    """Append one JSONL row for *record* to *telemetry_path*.

    Creates the file if absent.  The row is a compact summary:
    ``{signal_date, status, n_selected, n_intents, n_reviews,
      n_held_halted, n_skipped, reconciled, run_timestamp}``.

    Parameters
    ----------
    telemetry_path:
        Path to the JSONL telemetry log.
    record:
        The ``DayRecord`` to summarise.
    """
    telemetry_path = Path(telemetry_path)
    telemetry_path.parent.mkdir(parents=True, exist_ok=True)

    row = {
        "signal_date": record.signal_date.isoformat(),
        "status": record.status,
        "n_selected": len(record.selected),
        "n_intents": len(record.order_intents),
        "n_reviews": len(record.reviews),
        "n_held_halted": len(record.held_halted),
        "n_skipped": len(record.skipped),
        "reconciled": record.reconciled,
        "run_timestamp": record.run_timestamp,
        "data_source": record.data_source,
    }
    with telemetry_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def run_day(
    market_data: MarketData,
    broker: Any,
    positions: dict[str, Position],
    *,
    signal_date: dt.date,
    account_number: str,
    equity: float,
    state_dir: str | Path,
    telemetry_path: str | Path,
    run_timestamp: str,
    config: dict | None = None,
) -> "DayRecord":
    """Idempotent daily-loop orchestrator: plan → review → record → telemetry.

    Idempotency
    -----------
    If a 'complete' record already exists for ``signal_date``, the record is
    returned immediately — no re-planning, no re-reviewing, no re-writing.

    Happy path
    ----------
    1. ``plan_day`` — builds the order envelope (pure, no I/O).
    2. ``broker.review(intent)`` for each intent in ``plan.order_intents``.
    3. ``record_day`` — merges reviews, atomically writes the state file.
    4. ``append_telemetry`` — appends one JSONL row.
    5. Returns the ``DayRecord``.

    Fault handling
    --------------
    Auth/fetch/plan faults that occur BEFORE calling ``run_day`` must be caught
    by the caller, which should then call ``record_skip`` to write an attributed
    skip.  ``run_day`` itself is the happy path and does not call ``record_skip``.

    Parameters
    ----------
    market_data:
        Pre-populated ``MarketData`` provider.
    broker:
        A ``PaperBroker`` (or compatible) with a ``review(intent) -> ReviewResult``
        method.  In tests, a ``PaperBroker`` with a dict-lookup responder.
    positions:
        Current held positions keyed by symbol.  Empty dict for day-1.
    signal_date:
        Last completed session date.
    account_number:
        Robinhood account number.
    equity:
        Total account equity in dollars.
    state_dir:
        Directory for state JSON files.
    telemetry_path:
        Path to the JSONL telemetry log.
    run_timestamp:
        ISO-8601 UTC string injected by the caller.  Never computed here.
    config:
        Optional override dict for ``plan_day`` tunable parameters.

    Returns
    -------
    DayRecord
    """
    # ── Idempotency check ──────────────────────────────────────────────────────
    if is_day_complete(state_dir, signal_date):
        existing = load_day_record(state_dir, signal_date)
        assert existing is not None  # is_day_complete guarantees this
        return existing

    # ── Happy path ─────────────────────────────────────────────────────────────
    plan = plan_day(
        market_data,
        positions,
        signal_date=signal_date,
        account_number=account_number,
        equity=equity,
        config=config,
    )

    reviews: list[ReviewResult] = [broker.review(intent) for intent in plan.order_intents]

    rec = record_day(
        plan,
        reviews,
        state_dir=state_dir,
        run_timestamp=run_timestamp,
        account_number=account_number,
        equity=equity,
    )

    append_telemetry(telemetry_path, rec)

    return rec
