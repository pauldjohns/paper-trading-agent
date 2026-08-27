"""Inert order dataclasses — OrderIntent / ReviewResult / normalize_review_response.

Pure data + a pure parser. NO MCP calls, NO LiveBroker, NO placement. Split out of
broker.py so the autonomous loop graph (paper_loop -> paper_monitor) can reference
the order *types* without importing broker.py (LiveBroker / PaperBroker placement
surface). The no-place source-scan still applies to this file.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date

# ---------------------------------------------------------------------------
# OrderIntent
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OrderIntent:
    """Immutable descriptor of a desired order action.

    ``ref_id`` is a deterministic SHA-256 hex digest of the fields that uniquely
    identify the intent.  Same inputs → same ref_id; a ratchet replacement
    (different ``ratchet_seq`` or ``stop_price``) → different ref_id, so a
    cancel+replace is NOT deduped against the prior stop.

    Exactly one of ``quantity`` / ``dollar_amount`` must be provided.
    ``stop_price`` is required for stop_market / stop_limit orders.
    ``limit_price`` is required for limit / stop_limit orders.
    """

    signal_date: date
    symbol: str
    side: str                        # 'buy' | 'sell'
    intent_type: str                 # 'entry' | 'catastrophe_stop' | 'ratchet' | 'exit'
    order_type: str                  # 'market' | 'stop_market' | 'limit' | 'stop_limit'
    account_number: str
    quantity: str | None = None
    dollar_amount: str | None = None
    stop_price: str | None = None
    limit_price: str | None = None
    time_in_force: str = "gfd"       # 'gfd' | 'gtc'
    ratchet_seq: int = 0

    def __post_init__(self) -> None:
        # side validation
        if self.side not in {"buy", "sell"}:
            raise ValueError(f"side must be 'buy' or 'sell', got {self.side!r}")

        # exactly one of quantity/dollar_amount
        qty_set = self.quantity is not None
        amt_set = self.dollar_amount is not None
        if qty_set == amt_set:  # both set or neither set
            raise ValueError(
                "Exactly one of 'quantity' or 'dollar_amount' must be set; "
                f"got quantity={self.quantity!r}, dollar_amount={self.dollar_amount!r}"
            )

        # stop_price required for stop orders
        if self.order_type in {"stop_market", "stop_limit"} and self.stop_price is None:
            raise ValueError(
                f"stop_price is required for order_type={self.order_type!r}"
            )

        # stop_price must be > 0 when provided (a zero or negative stop must
        # never become a reviewable/placeable order — closes the chandelier-≤0
        # carry-forward risk)
        if self.stop_price is not None:
            try:
                stop_px_f = float(self.stop_price)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"stop_price must be a numeric string, got {self.stop_price!r}"
                ) from exc
            if stop_px_f <= 0:
                raise ValueError(
                    f"stop_price must be > 0, got {stop_px_f!r} "
                    f"(a non-positive stop must never become a reviewable order)"
                )

        # limit_price required for limit orders
        if self.order_type in {"limit", "stop_limit"} and self.limit_price is None:
            raise ValueError(
                f"limit_price is required for order_type={self.order_type!r}"
            )

        # limit_price must be > 0 when provided
        if self.limit_price is not None:
            try:
                limit_px_f = float(self.limit_price)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"limit_price must be a numeric string, got {self.limit_price!r}"
                ) from exc
            if limit_px_f <= 0:
                raise ValueError(
                    f"limit_price must be > 0, got {limit_px_f!r}"
                )

        # account_number must be non-empty
        if not self.account_number:
            raise ValueError("account_number must be non-empty")

    @property
    def ref_id(self) -> str:
        """Deterministic SHA-256 hex digest of the intent's identifying tuple.

        ``quantity`` and ``dollar_amount`` are included so two intents that
        differ only in size (e.g. a re-sized entry or a partial close) get
        distinct ref_ids — preventing a silent review-merge collision when
        size varies between runs.
        """
        key_tuple = (
            self.signal_date.isoformat(),
            self.symbol,
            self.side,
            self.intent_type,
            self.order_type,
            self.quantity or "",
            self.dollar_amount or "",
            self.stop_price or "",
            self.limit_price or "",
            str(self.ratchet_seq),
        )
        raw = "\x00".join(key_tuple).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# ReviewResult
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReviewResult:
    """Normalized result from a broker order-review call.

    ``alerts_raw`` is the opaque ``order_checks`` dict passed through verbatim.
    ``alert_type`` is None when ``order_checks`` is empty (``{}``), or the
    ``alertType`` string when present.

    ``market_data_disclosure`` is a COMPLIANCE string preserved verbatim.
    ``spread`` is ``ask - bid`` when both are non-None, else None.
    """

    ref_id: str
    symbol: str
    side: str
    order_type: str
    quantity: str | None
    stop_price: str | None
    alert_type: str | None           # None when order_checks == {}
    alerts_raw: dict                 # opaque order_checks passthrough
    last_trade_price: float
    bid: float | None
    ask: float | None
    previous_close: float
    previous_close_date: date | None
    has_traded: bool
    state: str
    market_data_disclosure: str      # verbatim compliance string
    spread: float | None             # ask - bid when both present


# ---------------------------------------------------------------------------
# normalize_review_response
# ---------------------------------------------------------------------------

def normalize_review_response(raw: dict, intent: OrderIntent) -> ReviewResult:
    """Parse a raw MCP review-response dict into a ``ReviewResult``.

    Accepts two shapes:
    - The fixture's top-level structure: ``{"data": {...}}``
    - A bare data dict (already the inner ``data`` object)

    ``order_checks`` is stored as-is in ``alerts_raw``; ``alert_type`` is
    extracted as ``order_checks["alertType"]`` when present, else None.

    All float fields are parsed from their string representations.
    ``bid`` and ``ask`` are None when absent or 0 in the response.
    """
    # Accept both shapes: {"data": {...}} or bare {...}
    data = raw.get("data", raw)

    # --- quote data ---------------------------------------------------------
    quote = data.get("quote_data", {})

    def _to_float(val: str | float | None, *, allow_zero: bool = True) -> float | None:
        if val is None:
            return None
        try:
            f = float(val)
        except (TypeError, ValueError):
            return None
        if not allow_zero and f == 0.0:
            return None
        return f

    last_trade_price = float(quote.get("last_trade_price", 0))

    raw_bid = quote.get("bid_price")
    raw_ask = quote.get("ask_price")
    bid = _to_float(raw_bid, allow_zero=False)   # 0 → None (not a valid bid)
    ask = _to_float(raw_ask, allow_zero=False)   # 0 → None

    previous_close = float(quote.get("previous_close", 0))

    # previous_close_date: parse "YYYY-MM-DD" string if present
    raw_pcd = quote.get("previous_close_date")
    if raw_pcd and isinstance(raw_pcd, str):
        try:
            prev_close_date: date | None = date.fromisoformat(raw_pcd)
        except ValueError:
            prev_close_date = None
    elif isinstance(raw_pcd, date):
        prev_close_date = raw_pcd
    else:
        prev_close_date = None

    has_traded = bool(quote.get("has_traded", False))
    state = str(quote.get("state", ""))

    # --- order_checks -------------------------------------------------------
    order_checks: dict = data.get("order_checks", {})
    alert_type: str | None = order_checks.get("alertType") if order_checks else None

    # --- disclosure ---------------------------------------------------------
    market_data_disclosure = str(data.get("market_data_disclosure", ""))

    # --- spread -------------------------------------------------------------
    spread: float | None = (ask - bid) if (bid is not None and ask is not None) else None

    return ReviewResult(
        ref_id=intent.ref_id,
        symbol=str(data.get("symbol", intent.symbol)),
        side=str(data.get("side", intent.side)),
        order_type=str(data.get("type", intent.order_type)),
        quantity=data.get("quantity") or intent.quantity,
        stop_price=data.get("stop_price") or intent.stop_price,
        alert_type=alert_type,
        alerts_raw=order_checks,
        last_trade_price=last_trade_price,
        bid=bid,
        ask=ask,
        previous_close=previous_close,
        previous_close_date=prev_close_date,
        has_traded=has_traded,
        state=state,
        market_data_disclosure=market_data_disclosure,
        spread=spread,
    )
