"""autotrader_live.broker — Broker protocol + PaperBroker (review-only) + LiveBroker stub.

Architecture note (T2.3)
------------------------
Python cannot call MCP tools directly.  The AGENT (main loop) calls the MCP
``review`` tool and obtains a raw response dict, then hands that dict to
``normalize_review_response`` for normalization/recording.  ``PaperBroker`` is
therefore constructed with a ``responder: Callable[[OrderIntent], dict]`` that
returns the raw review-response dict for a given intent.  In tests this is a
simple dict-lookup fake; in the live run the agent pre-collects responses and
passes a dict-lookup responder.  The package stays pure — no MCP calls inside.

NO-PLACE INVARIANT (§2.5)
--------------------------
This module must never reference broker-mutation MCP tool names.  ``PaperBroker``
enforces the invariant at RUNTIME by raising ``NoPlaceInPaperError`` from every
mutating method.  The source-scan test (test_no_place_invariant.py) enforces it at
import time by checking that none of the forbidden tokens appear in any ``.py``
under ``src/autotrader_live/``.

Frozen dataclasses are used throughout so that ``ref_id`` and field values are
immutable after construction — no accidental mutation of an in-flight intent.
"""
from __future__ import annotations

from typing import Callable, Protocol

from autotrader_live.order_types import (  # re-export for back-compat
    OrderIntent,
    ReviewResult,
    normalize_review_response,
)

__all__ = [
    "OrderIntent",
    "ReviewResult",
    "normalize_review_response",
    "Broker",
    "NoPlaceInPaperError",
    "PaperBroker",
    "LiveBroker",
]


# ---------------------------------------------------------------------------
# Broker (Protocol)
# ---------------------------------------------------------------------------

class Broker(Protocol):
    """Full lifecycle broker interface.

    ``review``     — preview an intent; never places an order.
    ``place_entry``  — open a position.
    ``place_stop``   — set a catastrophe or ratchet stop on an open position.
    ``cancel``       — cancel an open order by broker order ID.
    ``ratchet_replace`` — atomic cancel + replace a stop with a new intent.
    """

    def review(self, intent: OrderIntent) -> ReviewResult: ...

    def place_entry(self, intent: OrderIntent) -> None: ...

    def place_stop(self, intent: OrderIntent) -> None: ...

    def cancel(self, order_id: str) -> None: ...

    def ratchet_replace(self, old_order_id: str, new_intent: OrderIntent) -> None: ...


# ---------------------------------------------------------------------------
# NoPlaceInPaperError
# ---------------------------------------------------------------------------

class NoPlaceInPaperError(RuntimeError):
    """Raised by ``PaperBroker`` on any mutating method.

    This is the RUNTIME tripwire for the §2.5 no-place invariant.  Every
    mutating path on ``PaperBroker`` raises this; only ``review`` is allowed.
    """


# ---------------------------------------------------------------------------
# PaperBroker
# ---------------------------------------------------------------------------

class PaperBroker:
    """Review-only broker for the paper-monitor phase.

    ``responder`` is a ``Callable[[OrderIntent], dict]`` that accepts an intent
    and returns a raw MCP review-response dict.  In tests this is a dict-lookup
    fake; in the live agent loop the agent pre-collects review responses and
    passes a closure over them.

    All results are appended to ``self.reviews`` so the monitoring loop can
    inspect what would have been ordered.
    """

    def __init__(self, responder: Callable[[OrderIntent], dict]) -> None:
        self._responder = responder
        self.reviews: list[ReviewResult] = []

    def review(self, intent: OrderIntent) -> ReviewResult:
        """Call the responder, normalize the raw dict, record, and return."""
        raw = self._responder(intent)
        result = normalize_review_response(raw, intent)
        self.reviews.append(result)
        return result

    def place_entry(self, intent: OrderIntent) -> None:  # noqa: ARG002
        raise NoPlaceInPaperError(
            "PaperBroker is review-only; place_entry is blocked — "
            "funded LiveBroker lands at a later phase"
        )

    def place_stop(self, intent: OrderIntent) -> None:  # noqa: ARG002
        raise NoPlaceInPaperError(
            "PaperBroker is review-only; place_stop is blocked — "
            "funded LiveBroker lands at a later phase"
        )

    def cancel(self, order_id: str) -> None:  # noqa: ARG002
        raise NoPlaceInPaperError(
            "PaperBroker is review-only; cancel is blocked — "
            "funded LiveBroker lands at a later phase"
        )

    def ratchet_replace(self, old_order_id: str, new_intent: OrderIntent) -> None:  # noqa: ARG002
        raise NoPlaceInPaperError(
            "PaperBroker is review-only; ratchet_replace is blocked — "
            "funded LiveBroker lands at a later phase"
        )


# ---------------------------------------------------------------------------
# LiveBroker (stub)
# ---------------------------------------------------------------------------

class LiveBroker:
    """Funded-phase stub — not built or authorized yet.

    Every method raises ``NotImplementedError`` to make accidental use
    immediately visible.
    """

    _MSG = "LiveBroker is a funded-phase stub; not built/authorized yet"

    def review(self, intent: OrderIntent) -> ReviewResult:  # noqa: ARG002
        raise NotImplementedError(self._MSG)

    def place_entry(self, intent: OrderIntent) -> None:  # noqa: ARG002
        raise NotImplementedError(self._MSG)

    def place_stop(self, intent: OrderIntent) -> None:  # noqa: ARG002
        raise NotImplementedError(self._MSG)

    def cancel(self, order_id: str) -> None:  # noqa: ARG002
        raise NotImplementedError(self._MSG)

    def ratchet_replace(self, old_order_id: str, new_intent: OrderIntent) -> None:  # noqa: ARG002
        raise NotImplementedError(self._MSG)
