"""tests/live/test_broker.py — TDD tests for autotrader_live.broker (T2.3).

Tests cover:
- normalize_review_response against both fixture captures
- PaperBroker.review records into .reviews and returns ReviewResult
- PaperBroker mutating methods raise NoPlaceInPaperError (runtime tripwire)
- LiveBroker every method raises NotImplementedError
- ref_id determinism and uniqueness across intents
- OrderIntent validation (bad side, both/neither qty+amount, missing prices,
  empty account)
- Source-level no-place invariant (broker.py must contain none of the
  forbidden tokens — belt-and-suspenders alongside the parametrized scanner)
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from autotrader_live.broker import (
    Broker,
    LiveBroker,
    NoPlaceInPaperError,
    OrderIntent,
    PaperBroker,
    ReviewResult,
    normalize_review_response,
)

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

_FIXTURE_PATH = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "mcp_samples"
    / "review_equity_order.json"
)


def _load_fixture() -> dict:
    return json.loads(_FIXTURE_PATH.read_text(encoding="utf-8"))


def _buy_raw() -> dict:
    """Returns the ``buy_market_unfunded`` capture as a bare data-wrapped dict."""
    return _load_fixture()["buy_market_unfunded"]


def _sell_raw() -> dict:
    """Returns the ``sell_stop_market`` capture as a bare data-wrapped dict."""
    return _load_fixture()["sell_stop_market"]


def _make_buy_intent() -> OrderIntent:
    return OrderIntent(
        signal_date=date(2026, 6, 22),
        symbol="AMD",
        side="buy",
        intent_type="entry",
        order_type="market",
        account_number="123456789",
        quantity="1",
        time_in_force="gfd",
    )


def _make_sell_intent() -> OrderIntent:
    return OrderIntent(
        signal_date=date(2026, 6, 22),
        symbol="AMD",
        side="sell",
        intent_type="catastrophe_stop",
        order_type="stop_market",
        account_number="123456789",
        quantity="1",
        stop_price="500.00",
        time_in_force="gtc",
    )


# ---------------------------------------------------------------------------
# normalize_review_response — buy_market_unfunded
# ---------------------------------------------------------------------------

class TestNormalizeBuyMarketUnfunded:
    """Fixture: buy_market_unfunded — order_checks has alertType + depositAmount."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.intent = _make_buy_intent()
        self.raw = _buy_raw()
        self.result = normalize_review_response(self.raw, self.intent)

    def test_returns_review_result_instance(self):
        assert isinstance(self.result, ReviewResult)

    def test_alert_type_extracted(self):
        assert self.result.alert_type == "EQUITY_NOT_ENOUGH_BP"

    def test_alerts_raw_contains_deposit_detail(self):
        assert "equityNotEnoughBpAlertDetails" in self.result.alerts_raw
        detail = self.result.alerts_raw["equityNotEnoughBpAlertDetails"]
        assert detail["depositAmount"]["amount"] == "562.0650"
        assert detail["depositAmount"]["currency"] == "USD"

    def test_last_trade_price_parsed_as_float(self):
        assert abs(self.result.last_trade_price - 551.67) < 0.001

    def test_bid_parsed_as_float(self):
        assert self.result.bid is not None
        assert abs(self.result.bid - 546.31) < 0.001

    def test_ask_parsed_as_float(self):
        assert self.result.ask is not None
        assert abs(self.result.ask - 575.0) < 0.001

    def test_previous_close_parsed(self):
        assert abs(self.result.previous_close - 537.37) < 0.001

    def test_previous_close_date_parsed(self):
        assert self.result.previous_close_date == date(2026, 6, 18)

    def test_has_traded_true(self):
        assert self.result.has_traded is True

    def test_state_active(self):
        assert self.result.state == "active"

    def test_disclosure_preserved_verbatim(self):
        expected = (
            "Bid $546.11 × 100 Q · Ask $547.26 × 200 Q · Last $547.26 × 100. "
            "Updated 7:59 PM ET."
        )
        assert self.result.market_data_disclosure == expected

    def test_spread_computed(self):
        assert self.result.spread is not None
        assert abs(self.result.spread - (575.0 - 546.31)) < 0.001

    def test_ref_id_matches_intent(self):
        assert self.result.ref_id == self.intent.ref_id

    def test_symbol_captured(self):
        assert self.result.symbol == "AMD"

    def test_side_captured(self):
        assert self.result.side == "buy"

    def test_order_type_captured(self):
        assert self.result.order_type == "market"

    def test_stop_price_none_for_market(self):
        # buy_market_unfunded has no stop_price in fixture
        assert self.result.stop_price is None


# ---------------------------------------------------------------------------
# normalize_review_response — sell_stop_market
# ---------------------------------------------------------------------------

class TestNormalizeSellStopMarket:
    """Fixture: sell_stop_market — order_checks is empty ({})."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.intent = _make_sell_intent()
        self.raw = _sell_raw()
        self.result = normalize_review_response(self.raw, self.intent)

    def test_alert_type_is_none_for_empty_order_checks(self):
        """Empty order_checks → alert_type must be None."""
        assert self.result.alert_type is None

    def test_alerts_raw_is_empty_dict(self):
        assert self.result.alerts_raw == {}

    def test_stop_price_captured(self):
        """stop_price from the fixture data should be captured."""
        assert self.result.stop_price == "500.00"

    def test_side_sell(self):
        assert self.result.side == "sell"

    def test_order_type_stop_market(self):
        assert self.result.order_type == "stop_market"

    def test_last_trade_price_parsed(self):
        assert abs(self.result.last_trade_price - 551.67) < 0.001

    def test_disclosure_verbatim(self):
        expected = (
            "Bid $546.11 × 100 Q · Ask $547.26 × 200 Q · Last $547.26 × 100. "
            "Updated 7:59 PM ET."
        )
        assert self.result.market_data_disclosure == expected

    def test_spread_computed(self):
        assert self.result.spread is not None

    def test_ref_id_matches_intent(self):
        assert self.result.ref_id == self.intent.ref_id


# ---------------------------------------------------------------------------
# normalize_review_response — bare data dict (no outer wrapper)
# ---------------------------------------------------------------------------

class TestNormalizeBareDataDict:
    """normalize_review_response must accept a bare data dict (no 'data' key)."""

    def test_bare_dict_accepted(self):
        fixture = _load_fixture()
        bare = fixture["buy_market_unfunded"]["data"]   # inner data object
        intent = _make_buy_intent()
        result = normalize_review_response(bare, intent)
        assert result.alert_type == "EQUITY_NOT_ENOUGH_BP"
        assert abs(result.last_trade_price - 551.67) < 0.001


# ---------------------------------------------------------------------------
# PaperBroker — review records into .reviews
# ---------------------------------------------------------------------------

class TestPaperBrokerReview:
    """PaperBroker.review must call the responder, normalize, record, and return."""

    @pytest.fixture(autouse=True)
    def setup(self):
        buy_raw = _buy_raw()
        sell_raw = _sell_raw()

        def fake_responder(intent: OrderIntent) -> dict:
            if intent.side == "buy":
                return buy_raw
            return sell_raw

        self.broker = PaperBroker(responder=fake_responder)
        self.buy_intent = _make_buy_intent()
        self.sell_intent = _make_sell_intent()

    def test_review_returns_review_result(self):
        result = self.broker.review(self.buy_intent)
        assert isinstance(result, ReviewResult)

    def test_review_appends_to_reviews_list(self):
        assert len(self.broker.reviews) == 0
        self.broker.review(self.buy_intent)
        assert len(self.broker.reviews) == 1

    def test_review_multiple_intents_recorded(self):
        self.broker.review(self.buy_intent)
        self.broker.review(self.sell_intent)
        assert len(self.broker.reviews) == 2

    def test_review_result_in_reviews_list(self):
        result = self.broker.review(self.buy_intent)
        assert self.broker.reviews[0] is result

    def test_review_buy_alert_type(self):
        result = self.broker.review(self.buy_intent)
        assert result.alert_type == "EQUITY_NOT_ENOUGH_BP"

    def test_review_sell_no_alert(self):
        result = self.broker.review(self.sell_intent)
        assert result.alert_type is None

    def test_broker_starts_with_empty_reviews(self):
        fresh = PaperBroker(responder=lambda i: _buy_raw())
        assert fresh.reviews == []


# ---------------------------------------------------------------------------
# PaperBroker — mutating methods raise NoPlaceInPaperError (runtime tripwire)
# ---------------------------------------------------------------------------

class TestPaperBrokerPlaceTripwire:
    """All four mutating methods on PaperBroker must raise NoPlaceInPaperError."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.broker = PaperBroker(responder=lambda i: _buy_raw())
        self.intent = _make_buy_intent()

    def test_place_entry_raises(self):
        with pytest.raises(NoPlaceInPaperError):
            self.broker.place_entry(self.intent)

    def test_place_stop_raises(self):
        with pytest.raises(NoPlaceInPaperError):
            self.broker.place_stop(self.intent)

    def test_cancel_raises(self):
        with pytest.raises(NoPlaceInPaperError):
            self.broker.cancel("some-order-id")

    def test_ratchet_replace_raises(self):
        with pytest.raises(NoPlaceInPaperError):
            self.broker.ratchet_replace("old-order-id", self.intent)

    def test_no_place_error_is_runtime_error(self):
        """NoPlaceInPaperError must be a RuntimeError subclass."""
        with pytest.raises(RuntimeError):
            self.broker.place_entry(self.intent)

    def test_error_message_mentions_paper_broker(self):
        with pytest.raises(NoPlaceInPaperError, match="PaperBroker"):
            self.broker.place_entry(self.intent)


# ---------------------------------------------------------------------------
# LiveBroker — every method raises NotImplementedError
# ---------------------------------------------------------------------------

class TestLiveBrokerStub:
    """LiveBroker is a stub — every method raises NotImplementedError."""

    @pytest.fixture(autouse=True)
    def setup(self):
        self.broker = LiveBroker()
        self.intent = _make_buy_intent()

    def test_review_raises(self):
        with pytest.raises(NotImplementedError):
            self.broker.review(self.intent)

    def test_place_entry_raises(self):
        with pytest.raises(NotImplementedError):
            self.broker.place_entry(self.intent)

    def test_place_stop_raises(self):
        with pytest.raises(NotImplementedError):
            self.broker.place_stop(self.intent)

    def test_cancel_raises(self):
        with pytest.raises(NotImplementedError):
            self.broker.cancel("order-id")

    def test_ratchet_replace_raises(self):
        with pytest.raises(NotImplementedError):
            self.broker.ratchet_replace("old-id", self.intent)

    def test_error_message_mentions_stub(self):
        with pytest.raises(NotImplementedError, match="stub"):
            self.broker.review(self.intent)


# ---------------------------------------------------------------------------
# ref_id — determinism and uniqueness
# ---------------------------------------------------------------------------

class TestRefId:
    """ref_id must be deterministic and differ across distinct intents."""

    def _entry_intent(self) -> OrderIntent:
        return OrderIntent(
            signal_date=date(2026, 6, 22),
            symbol="NVDA",
            side="buy",
            intent_type="entry",
            order_type="market",
            account_number="123456789",
            quantity="10",
        )

    def _cstop_intent(self) -> OrderIntent:
        return OrderIntent(
            signal_date=date(2026, 6, 22),
            symbol="NVDA",
            side="sell",
            intent_type="catastrophe_stop",
            order_type="stop_market",
            account_number="123456789",
            quantity="10",
            stop_price="90.00",
        )

    def _ratchet_seq1_intent(self) -> OrderIntent:
        return OrderIntent(
            signal_date=date(2026, 6, 22),
            symbol="NVDA",
            side="sell",
            intent_type="ratchet",
            order_type="stop_market",
            account_number="123456789",
            quantity="10",
            stop_price="95.00",
            ratchet_seq=1,
        )

    def _ratchet_seq2_repriced_intent(self) -> OrderIntent:
        """Same ratchet_seq=1 but different stop_price → different ref_id."""
        return OrderIntent(
            signal_date=date(2026, 6, 22),
            symbol="NVDA",
            side="sell",
            intent_type="ratchet",
            order_type="stop_market",
            account_number="123456789",
            quantity="10",
            stop_price="98.00",   # different price
            ratchet_seq=1,
        )

    def test_same_intent_same_ref_id(self):
        a = self._entry_intent()
        b = self._entry_intent()
        assert a.ref_id == b.ref_id

    def test_entry_vs_cstop_differ(self):
        assert self._entry_intent().ref_id != self._cstop_intent().ref_id

    def test_cstop_vs_ratchet_seq1_differ(self):
        assert self._cstop_intent().ref_id != self._ratchet_seq1_intent().ref_id

    def test_entry_vs_ratchet_differ(self):
        assert self._entry_intent().ref_id != self._ratchet_seq1_intent().ref_id

    def test_ratchet_repriced_differs_from_prior(self):
        """A re-priced ratchet (different stop_price, same ratchet_seq) must differ."""
        seq1 = self._ratchet_seq1_intent()
        repriced = self._ratchet_seq2_repriced_intent()
        assert seq1.ref_id != repriced.ref_id

    def test_ref_id_is_hex_string(self):
        """ref_id should be a 64-char hex string (SHA-256)."""
        ref = self._entry_intent().ref_id
        assert len(ref) == 64
        assert all(c in "0123456789abcdef" for c in ref)

    def test_different_symbol_differs(self):
        a = self._entry_intent()
        b = OrderIntent(
            signal_date=date(2026, 6, 22),
            symbol="AMD",
            side="buy",
            intent_type="entry",
            order_type="market",
            account_number="123456789",
            quantity="10",
        )
        assert a.ref_id != b.ref_id

    def test_different_date_differs(self):
        a = self._entry_intent()
        b = OrderIntent(
            signal_date=date(2026, 6, 23),
            symbol="NVDA",
            side="buy",
            intent_type="entry",
            order_type="market",
            account_number="123456789",
            quantity="10",
        )
        assert a.ref_id != b.ref_id

    def test_different_quantity_differs(self):
        """Two intents differing only in quantity must have distinct ref_ids."""
        a = OrderIntent(
            signal_date=date(2026, 6, 22),
            symbol="NVDA",
            side="buy",
            intent_type="entry",
            order_type="market",
            account_number="123456789",
            quantity="10",
        )
        b = OrderIntent(
            signal_date=date(2026, 6, 22),
            symbol="NVDA",
            side="buy",
            intent_type="entry",
            order_type="market",
            account_number="123456789",
            quantity="20",    # different quantity
        )
        assert a.ref_id != b.ref_id

    def test_different_dollar_amount_differs(self):
        """Two intents differing only in dollar_amount must have distinct ref_ids."""
        a = OrderIntent(
            signal_date=date(2026, 6, 22),
            symbol="NVDA",
            side="buy",
            intent_type="entry",
            order_type="market",
            account_number="123456789",
            dollar_amount="1000.00",
        )
        b = OrderIntent(
            signal_date=date(2026, 6, 22),
            symbol="NVDA",
            side="buy",
            intent_type="entry",
            order_type="market",
            account_number="123456789",
            dollar_amount="2000.00",   # different dollar_amount
        )
        assert a.ref_id != b.ref_id


# ---------------------------------------------------------------------------
# OrderIntent validation
# ---------------------------------------------------------------------------

class TestOrderIntentValidation:
    """OrderIntent must raise ValueError on invalid inputs."""

    def test_bad_side_raises(self):
        with pytest.raises(ValueError, match="side"):
            OrderIntent(
                signal_date=date(2026, 6, 22),
                symbol="AAPL",
                side="long",  # invalid
                intent_type="entry",
                order_type="market",
                account_number="12345",
                quantity="1",
            )

    def test_both_quantity_and_dollar_amount_raises(self):
        with pytest.raises(ValueError, match="Exactly one"):
            OrderIntent(
                signal_date=date(2026, 6, 22),
                symbol="AAPL",
                side="buy",
                intent_type="entry",
                order_type="market",
                account_number="12345",
                quantity="5",
                dollar_amount="500",  # both set
            )

    def test_neither_quantity_nor_dollar_amount_raises(self):
        with pytest.raises(ValueError, match="Exactly one"):
            OrderIntent(
                signal_date=date(2026, 6, 22),
                symbol="AAPL",
                side="buy",
                intent_type="entry",
                order_type="market",
                account_number="12345",
                # neither quantity nor dollar_amount
            )

    def test_stop_market_without_stop_price_raises(self):
        with pytest.raises(ValueError, match="stop_price"):
            OrderIntent(
                signal_date=date(2026, 6, 22),
                symbol="AAPL",
                side="sell",
                intent_type="catastrophe_stop",
                order_type="stop_market",
                account_number="12345",
                quantity="1",
                # no stop_price
            )

    def test_stop_limit_without_stop_price_raises(self):
        with pytest.raises(ValueError, match="stop_price"):
            OrderIntent(
                signal_date=date(2026, 6, 22),
                symbol="AAPL",
                side="sell",
                intent_type="catastrophe_stop",
                order_type="stop_limit",
                account_number="12345",
                quantity="1",
                limit_price="490.00",
                # no stop_price
            )

    def test_limit_without_limit_price_raises(self):
        with pytest.raises(ValueError, match="limit_price"):
            OrderIntent(
                signal_date=date(2026, 6, 22),
                symbol="AAPL",
                side="buy",
                intent_type="entry",
                order_type="limit",
                account_number="12345",
                quantity="1",
                # no limit_price
            )

    def test_stop_limit_without_limit_price_raises(self):
        with pytest.raises(ValueError, match="limit_price"):
            OrderIntent(
                signal_date=date(2026, 6, 22),
                symbol="AAPL",
                side="sell",
                intent_type="catastrophe_stop",
                order_type="stop_limit",
                account_number="12345",
                quantity="1",
                stop_price="490.00",
                # no limit_price
            )

    def test_empty_account_number_raises(self):
        with pytest.raises(ValueError, match="account_number"):
            OrderIntent(
                signal_date=date(2026, 6, 22),
                symbol="AAPL",
                side="buy",
                intent_type="entry",
                order_type="market",
                account_number="",  # empty
                quantity="1",
            )

    def test_valid_market_buy_succeeds(self):
        """Smoke test — a valid market buy should not raise."""
        intent = OrderIntent(
            signal_date=date(2026, 6, 22),
            symbol="AAPL",
            side="buy",
            intent_type="entry",
            order_type="market",
            account_number="12345",
            quantity="5",
        )
        assert intent.symbol == "AAPL"

    def test_valid_dollar_amount_buy_succeeds(self):
        """dollar_amount path should work when quantity is absent."""
        intent = OrderIntent(
            signal_date=date(2026, 6, 22),
            symbol="AAPL",
            side="buy",
            intent_type="entry",
            order_type="market",
            account_number="12345",
            dollar_amount="1000.00",
        )
        assert intent.dollar_amount == "1000.00"

    def test_valid_stop_market_sell_succeeds(self):
        """stop_market with stop_price should not raise."""
        intent = OrderIntent(
            signal_date=date(2026, 6, 22),
            symbol="AAPL",
            side="sell",
            intent_type="catastrophe_stop",
            order_type="stop_market",
            account_number="12345",
            quantity="5",
            stop_price="150.00",
        )
        assert intent.stop_price == "150.00"

    def test_valid_stop_limit_succeeds(self):
        """stop_limit with both prices should not raise."""
        intent = OrderIntent(
            signal_date=date(2026, 6, 22),
            symbol="AAPL",
            side="sell",
            intent_type="catastrophe_stop",
            order_type="stop_limit",
            account_number="12345",
            quantity="5",
            stop_price="150.00",
            limit_price="149.00",
        )
        assert intent.limit_price == "149.00"

    def test_stop_price_zero_raises(self):
        """stop_price of '0.00' must raise ValueError (zero stop is never valid)."""
        with pytest.raises(ValueError, match="stop_price must be > 0"):
            OrderIntent(
                signal_date=date(2026, 6, 22),
                symbol="AAPL",
                side="sell",
                intent_type="catastrophe_stop",
                order_type="stop_market",
                account_number="12345",
                quantity="5",
                stop_price="0.00",
            )

    def test_stop_price_negative_raises(self):
        """A negative stop_price must also raise ValueError."""
        with pytest.raises(ValueError, match="stop_price must be > 0"):
            OrderIntent(
                signal_date=date(2026, 6, 22),
                symbol="AAPL",
                side="sell",
                intent_type="catastrophe_stop",
                order_type="stop_market",
                account_number="12345",
                quantity="5",
                stop_price="-10.00",
            )

    def test_limit_price_zero_raises(self):
        """limit_price of '0.00' must raise ValueError."""
        with pytest.raises(ValueError, match="limit_price must be > 0"):
            OrderIntent(
                signal_date=date(2026, 6, 22),
                symbol="AAPL",
                side="sell",
                intent_type="catastrophe_stop",
                order_type="stop_limit",
                account_number="12345",
                quantity="5",
                stop_price="150.00",
                limit_price="0.00",
            )

    def test_positive_stop_price_succeeds(self):
        """A positive stop_price must succeed."""
        intent = OrderIntent(
            signal_date=date(2026, 6, 22),
            symbol="AAPL",
            side="sell",
            intent_type="catastrophe_stop",
            order_type="stop_market",
            account_number="12345",
            quantity="5",
            stop_price="0.01",
        )
        assert intent.stop_price == "0.01"


# ---------------------------------------------------------------------------
# Broker Protocol satisfaction
# ---------------------------------------------------------------------------

class TestBrokerProtocol:
    """PaperBroker must satisfy the Broker Protocol structurally."""

    def test_paper_broker_has_review(self):
        broker = PaperBroker(responder=lambda i: _buy_raw())
        assert hasattr(broker, "review")

    def test_paper_broker_has_place_entry(self):
        broker = PaperBroker(responder=lambda i: _buy_raw())
        assert hasattr(broker, "place_entry")

    def test_paper_broker_has_place_stop(self):
        broker = PaperBroker(responder=lambda i: _buy_raw())
        assert hasattr(broker, "place_stop")

    def test_paper_broker_has_cancel(self):
        broker = PaperBroker(responder=lambda i: _buy_raw())
        assert hasattr(broker, "cancel")

    def test_paper_broker_has_ratchet_replace(self):
        broker = PaperBroker(responder=lambda i: _buy_raw())
        assert hasattr(broker, "ratchet_replace")

    def test_live_broker_has_same_interface(self):
        broker = LiveBroker()
        for method in ("review", "place_entry", "place_stop", "cancel", "ratchet_replace"):
            assert hasattr(broker, method)


# ---------------------------------------------------------------------------
# Source-level no-place invariant belt-and-suspenders
# ---------------------------------------------------------------------------

class TestBrokerSourceNoPlaceInvariant:
    """broker.py source must not contain any of the forbidden MCP tokens.

    This is belt-and-suspenders alongside test_no_place_invariant.py's
    parametrized scanner.
    """

    _FORBIDDEN = [
        "place_equity_order",
        "place_option_order",
        "cancel_equity_order",
        "cancel_option_order",
        "review_equity_order",
    ]

    @pytest.fixture(autouse=True)
    def setup(self):
        broker_path = (
            Path(__file__).resolve().parents[2]
            / "src"
            / "autotrader_live"
            / "broker.py"
        )
        self.source = broker_path.read_text(encoding="utf-8")

    @pytest.mark.parametrize("token", _FORBIDDEN)
    def test_no_forbidden_token(self, token: str) -> None:
        assert token not in self.source, (
            f"NO-PLACE INVARIANT VIOLATED in broker.py: token {token!r} found"
        )
