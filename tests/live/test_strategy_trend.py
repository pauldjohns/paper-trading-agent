# tests/live/test_strategy_trend.py
"""Tests for autotrader_live.strategy_trend — the today-decision function.

TDD: tests written FIRST (RED), implementation follows (GREEN).

Anti-tautology approach: cross-check sub-signals by independently calling the same
trusted indicator functions that the implementation must call, then assert the
decide() output matches their [t] values (proving wiring, not reimplementation).
Synthetic bar frames are constructed programmatically with known shapes.
"""
import dataclasses
import datetime
import math

import pandas as pd
import pytest

from autotrader.indicators import sma, trailing_return, nearness_to_high
from autotrader_live.indicators_ohlc import atr, donchian
from autotrader_live.strategy_trend import decide, TrendDecision


# ---------------------------------------------------------------------------
# Helpers to build synthetic bar frames
# ---------------------------------------------------------------------------

def _make_bars(n: int, *, start_price: float = 100.0, step: float = 0.5,
               high_margin: float = 1.0, low_margin: float = 1.0,
               start_date: datetime.date | None = None) -> pd.DataFrame:
    """Return a DataFrame[date, high, low, close] with `n` rows.

    close[i] = start_price + i * step  (strictly increasing if step > 0,
    decreasing if step < 0).
    high[i] = close[i] + high_margin
    low[i]  = close[i] - low_margin
    """
    if start_date is None:
        start_date = datetime.date(2022, 1, 3)
    dates = [start_date + datetime.timedelta(days=i) for i in range(n)]
    closes = [start_price + i * step for i in range(n)]
    highs = [c + high_margin for c in closes]
    lows = [c - low_margin for c in closes]
    return pd.DataFrame({
        "date": dates,
        "high": highs,
        "low": lows,
        "close": closes,
    })


def _make_bars_full(n: int, *, start_price: float = 100.0, step: float = 0.5,
                    high_margin: float = 1.0, low_margin: float = 1.0,
                    start_date: datetime.date | None = None) -> pd.DataFrame:
    """Like _make_bars but includes open and volume columns (DataStore schema)."""
    bars = _make_bars(n, start_price=start_price, step=step,
                      high_margin=high_margin, low_margin=low_margin,
                      start_date=start_date)
    bars.insert(1, "open", bars["close"] - 0.25)
    bars["volume"] = 1_000_000
    return bars


# ---------------------------------------------------------------------------
# 1. Strong uptrend: all signals True → entry True, reason "ok"
# ---------------------------------------------------------------------------

class TestStrongUptrend:
    """290 bars, close strictly increasing — after burn-in all sub-signals should be True."""

    def setup_method(self):
        self.bars = _make_bars(290, start_price=50.0, step=0.3,
                               high_margin=0.5, low_margin=0.5)
        self.d = decide(self.bars, "FAKE")

    def test_reason_ok(self):
        assert self.d.reason == "ok"

    def test_trend_ok(self):
        assert self.d.trend_ok is True

    def test_momentum_ok(self):
        assert self.d.momentum_ok is True

    def test_near_high(self):
        # strictly increasing → last close IS the 252-day high → nearness == 1.0
        assert self.d.nearness == pytest.approx(1.0, abs=1e-9)
        assert self.d.near_high is True

    def test_entry_true(self):
        assert self.d.entry is True

    def test_atr14_positive(self):
        assert self.d.atr14 > 0

    def test_symbol_field(self):
        assert self.d.symbol == "FAKE"


# ---------------------------------------------------------------------------
# 2. Downtrend: trend_ok False → entry False
# ---------------------------------------------------------------------------

class TestDowntrend:
    """290 bars strictly decreasing — close < SMA200 → trend_ok False → entry False."""

    def setup_method(self):
        # start high so prices stay positive throughout
        self.bars = _make_bars(290, start_price=200.0, step=-0.3,
                               high_margin=0.5, low_margin=0.5)
        self.d = decide(self.bars, "DOWN")

    def test_reason_ok(self):
        assert self.d.reason == "ok"

    def test_trend_ok_false(self):
        # in a downtrend the last close is below the 200-bar SMA
        assert self.d.trend_ok is False

    def test_entry_false(self):
        assert self.d.entry is False


# ---------------------------------------------------------------------------
# 3. Cross-check wiring: sub-signals derived from same trusted indicators
# ---------------------------------------------------------------------------

class TestWiringCrossCheck:
    """Independently compute sma/trailing_return/nearness_to_high and assert
    decide() used their [t] values. Proves wiring, not reimplementation."""

    def setup_method(self):
        self.bars = _make_bars(290, start_price=50.0, step=0.3,
                               high_margin=0.5, low_margin=0.5)
        self.d = decide(self.bars, "CHK")
        close = self.bars["close"]
        t = len(self.bars) - 1
        self.expected_sma200 = sma(close, 200).iloc[t]
        self.expected_mom_252 = trailing_return(close, 252, skip=0).iloc[t]
        self.expected_nearness = nearness_to_high(close, 252).iloc[t]

    def test_sma200_matches_indicator(self):
        assert abs(self.d.sma200 - self.expected_sma200) < 1e-9

    def test_mom_252_matches_indicator(self):
        assert abs(self.d.mom_252 - self.expected_mom_252) < 1e-9

    def test_nearness_matches_indicator(self):
        assert abs(self.d.nearness - self.expected_nearness) < 1e-9

    def test_atr14_matches_indicator(self):
        """atr14 from decide() must equal atr(high, low, close, 14)[t]."""
        h = self.bars["high"]
        l = self.bars["low"]
        c = self.bars["close"]
        t = len(self.bars) - 1
        expected_atr = atr(h, l, c, 14).iloc[t]
        assert abs(self.d.atr14 - expected_atr) < 1e-9

    def test_prior_donch_upper_uses_shift1(self):
        """prior_donch_upper = donchian(high, low, 55)['upper'].shift(1).iloc[t].
        Cross-check against the same expression."""
        h = self.bars["high"]
        l = self.bars["low"]
        t = len(self.bars) - 1
        expected = donchian(h, l, 55)["upper"].shift(1).iloc[t]
        assert abs(self.d.prior_donch_upper - expected) < 1e-9


# ---------------------------------------------------------------------------
# 4. Breakout_55 semantics: excludes today; close-based not high-based
# ---------------------------------------------------------------------------

class TestBreakout55:
    """Validate that breakout_55 uses close vs prior-55-high-of-highs (shift(1))."""

    def _make_breakout_bars(self, *, final_close_above: bool,
                             final_high_above: bool) -> pd.DataFrame:
        """260 flat bars at 100.0, then a final bar where:
        - high > 100.0 + epsilon always (to separate high-based from close-based)
        - close is either above or at/below 100.0 per the flag.
        The prior 55 high-of-highs is 100.5 (high_margin=0.5 on the flat segment).
        """
        n_flat = 260
        start = datetime.date(2022, 1, 3)
        dates = [start + datetime.timedelta(days=i) for i in range(n_flat + 1)]
        closes = [100.0] * n_flat + ([101.0] if final_close_above else [100.0])
        highs = [100.5] * n_flat + [101.5]   # final high always above prior-55-high
        lows = [99.5] * n_flat + [99.5]
        return pd.DataFrame({"date": dates, "high": highs, "low": lows, "close": closes})

    def test_breakout_true_when_close_exceeds_prior55_high(self):
        """close[t] > donchian(55)['upper'].shift(1).iloc[t] → breakout_55 True."""
        bars = self._make_breakout_bars(final_close_above=True, final_high_above=True)
        d = decide(bars, "BRK")
        assert d.reason == "ok"
        assert d.breakout_55 is True

    def test_breakout_false_when_close_does_not_exceed_prior55_high(self):
        """close[t] == 100.0, prior-55-upper == 100.5 → breakout_55 False.
        Even though final high (101.5) > prior-55-upper, the signal is CLOSE-based."""
        bars = self._make_breakout_bars(final_close_above=False, final_high_above=True)
        d = decide(bars, "NOB")
        assert d.reason == "ok"
        assert d.breakout_55 is False

    def test_breakout_excludes_todays_high_from_donchian(self):
        """If today's high were included in the Donchian window, the prior_donch_upper
        would be at least 101.5 (today's high), so close=101.0 could NOT exceed it.
        But with shift(1) today is excluded, so prior_donch_upper remains 100.5
        (the flat-segment high) and close=101.0 > 100.5 → breakout True.
        This test fails if shift(1) is missing or wrong."""
        bars = self._make_breakout_bars(final_close_above=True, final_high_above=True)
        d = decide(bars, "EXC")
        # If today's high were included, prior_donch_upper >= 101.5 > close 101.0 → False.
        # With correct shift(1): prior_donch_upper = 100.5, close 101.0 > 100.5 → True.
        assert d.breakout_55 is True


# ---------------------------------------------------------------------------
# 5. near_threshold parameter: near_high flips at different thresholds
# ---------------------------------------------------------------------------

class TestNearThreshold:
    """Build bars where the last close is 92% of the 252-day high."""

    def _make_partial_recovery_bars(self) -> pd.DataFrame:
        """260 bars: first 252 bars climb to 100.0, then retreat to ~92.0 at bar 259.
        252-day high of closes = 100.0; last close = 92.0; nearness = 0.92.
        """
        n = 260
        start = datetime.date(2022, 1, 3)
        dates = [start + datetime.timedelta(days=i) for i in range(n)]
        # First 252 bars climb from ~85 to 100
        closes_up = [85.0 + i * (15.0 / 251) for i in range(252)]
        # Remaining 8 bars drop from 100 to 92
        closes_down = [100.0 - (i + 1) * 1.0 for i in range(8)]
        closes = closes_up + closes_down
        highs = [c + 1.0 for c in closes]
        lows = [c - 1.0 for c in closes]
        return pd.DataFrame({"date": dates, "high": highs, "low": lows, "close": closes})

    def setup_method(self):
        self.bars = self._make_partial_recovery_bars()

    def test_near_high_true_at_low_threshold(self):
        """With near_threshold=0.90, nearness ~0.92 > 0.90 → near_high True."""
        d = decide(self.bars, "NEAR", near_threshold=0.90)
        assert d.near_high is True

    def test_near_high_false_at_high_threshold(self):
        """With near_threshold=0.99, nearness ~0.92 < 0.99 → near_high False."""
        d = decide(self.bars, "NEAR", near_threshold=0.99)
        assert d.near_high is False

    def test_nearness_value_is_consistent_across_thresholds(self):
        """The nearness value itself doesn't change with near_threshold."""
        d90 = decide(self.bars, "NEAR", near_threshold=0.90)
        d99 = decide(self.bars, "NEAR", near_threshold=0.99)
        assert abs(d90.nearness - d99.nearness) < 1e-9


# ---------------------------------------------------------------------------
# 6. Insufficient history: <253 bars → reason="insufficient_history", entry=False
# ---------------------------------------------------------------------------

class TestInsufficientHistory:
    """100-bar frame triggers the burn-in guard."""

    def setup_method(self):
        self.bars = _make_bars(100, start_price=50.0, step=0.3)
        self.d = decide(self.bars, "SHORT")

    def test_reason_insufficient_history(self):
        assert self.d.reason == "insufficient_history"

    def test_entry_false(self):
        assert self.d.entry is False

    def test_trend_ok_false_or_irrelevant(self):
        # Under insufficient history, entry must be False regardless of trend_ok
        assert self.d.entry is False

    def test_symbol_still_set(self):
        assert self.d.symbol == "SHORT"

    def test_signal_date_still_set(self):
        assert self.d.signal_date == self.bars["date"].iloc[-1]

    def test_exactly_253_bars_is_sufficient(self):
        """The burn-in threshold is 253 bars (max(200, 252, 56)). With exactly
        253 bars, every indicator has its first valid value — reason must be "ok"."""
        bars = _make_bars(253, start_price=50.0, step=0.3,
                          high_margin=0.5, low_margin=0.5)
        d = decide(bars, "EXACT")
        assert d.reason == "ok"

    def test_252_bars_insufficient(self):
        """252 bars: sma(200) has 53 valid values, but trailing_return(252, skip=0)
        needs 253 bars (its first valid value is at position 252). The binding
        constraint is trailing_return, so 252 rows → NaN → insufficient_history."""
        bars = _make_bars(252, start_price=50.0, step=0.3,
                          high_margin=0.5, low_margin=0.5)
        d = decide(bars, "INSUF252")
        assert d.reason == "insufficient_history"


# ---------------------------------------------------------------------------
# 7. Validation: missing columns / empty frame → ValueError
# ---------------------------------------------------------------------------

class TestValidation:
    def test_missing_close_column_raises_value_error(self):
        bars = pd.DataFrame({"date": [datetime.date(2024, 1, 2)],
                             "high": [10.0], "low": [9.0]})
        with pytest.raises(ValueError, match="close"):
            decide(bars, "X")

    def test_missing_high_column_raises_value_error(self):
        bars = pd.DataFrame({"date": [datetime.date(2024, 1, 2)],
                             "close": [10.0], "low": [9.0]})
        with pytest.raises(ValueError, match="high"):
            decide(bars, "X")

    def test_missing_low_column_raises_value_error(self):
        bars = pd.DataFrame({"date": [datetime.date(2024, 1, 2)],
                             "high": [10.0], "close": [9.5]})
        with pytest.raises(ValueError, match="low"):
            decide(bars, "X")

    def test_missing_date_column_raises_value_error(self):
        bars = pd.DataFrame({"high": [10.0], "low": [9.0], "close": [9.5]})
        with pytest.raises(ValueError, match="date"):
            decide(bars, "X")

    def test_empty_frame_raises_value_error(self):
        bars = pd.DataFrame({"date": [], "high": [], "low": [], "close": []})
        with pytest.raises(ValueError):
            decide(bars, "X")

    def test_near_threshold_out_of_range_raises(self):
        bars = _make_bars(260, start_price=50.0, step=0.3,
                          high_margin=0.5, low_margin=0.5)
        for bad in (0.0, -0.1, 1.5):
            with pytest.raises(ValueError, match="near_threshold"):
                decide(bars, "X", near_threshold=bad)

    def test_full_schema_accepted(self):
        """DataStore schema [date, open, high, low, close, volume] must be accepted."""
        bars = _make_bars_full(260, start_price=50.0, step=0.3)
        # Should not raise; just check it runs
        d = decide(bars, "FULL")
        assert d.symbol == "FULL"


# ---------------------------------------------------------------------------
# 8. Record integrity: field types, signal_date, to_dict / asdict
# ---------------------------------------------------------------------------

class TestRecordIntegrity:
    def setup_method(self):
        self.bars = _make_bars(290, start_price=50.0, step=0.3,
                               high_margin=0.5, low_margin=0.5)
        self.d = decide(self.bars, "REC")

    def test_signal_date_equals_last_row_date(self):
        assert self.d.signal_date == self.bars["date"].iloc[-1]

    def test_entry_is_python_bool(self):
        assert type(self.d.entry) is bool

    def test_trend_ok_is_python_bool(self):
        assert type(self.d.trend_ok) is bool

    def test_momentum_ok_is_python_bool(self):
        assert type(self.d.momentum_ok) is bool

    def test_near_high_is_python_bool(self):
        assert type(self.d.near_high) is bool

    def test_breakout_55_is_python_bool(self):
        assert type(self.d.breakout_55) is bool

    def test_close_is_float(self):
        assert isinstance(self.d.close, float)

    def test_sma200_is_float(self):
        assert isinstance(self.d.sma200, float)

    def test_symbol_is_str(self):
        assert isinstance(self.d.symbol, str)

    def test_reason_is_str(self):
        assert isinstance(self.d.reason, str)

    def test_dataclass_is_frozen(self):
        with pytest.raises(dataclasses.FrozenInstanceError):
            self.d.entry = not self.d.entry  # type: ignore[misc]

    def test_asdict_roundtrip(self):
        d_dict = dataclasses.asdict(self.d)
        assert "signal_date" in d_dict
        assert "entry" in d_dict
        assert "symbol" in d_dict
        assert d_dict["symbol"] == "REC"
        assert d_dict["entry"] == self.d.entry
        # All bool fields survive asdict as Python bool
        for field in ("trend_ok", "momentum_ok", "near_high", "breakout_55", "entry"):
            assert type(d_dict[field]) is bool, f"{field} should be bool in asdict output"

    def test_to_dict_available(self):
        """TrendDecision must expose a to_dict() method (may delegate to asdict)."""
        d_dict = self.d.to_dict()
        assert isinstance(d_dict, dict)
        assert "entry" in d_dict

    def test_close_value_matches_last_bar_close(self):
        expected = float(self.bars["close"].iloc[-1])
        assert abs(self.d.close - expected) < 1e-9

    def test_all_required_fields_present(self):
        """Every field in the spec is present on the dataclass."""
        required = {
            "signal_date", "symbol", "close", "sma200", "trend_ok",
            "mom_252", "momentum_ok", "nearness", "near_high",
            "prior_donch_upper", "breakout_55", "atr14", "entry", "reason",
        }
        actual = {f.name for f in dataclasses.fields(self.d)}
        assert required <= actual

    def test_insufficient_history_nan_floats_are_float(self):
        """Under insufficient history, NaN sub-signal values must be float('nan'),
        not the string 'nan' or numpy.nan via weird typing."""
        bars = _make_bars(100)
        d = decide(bars, "NAN")
        # sma200 must be a float (NaN is a float)
        assert isinstance(d.sma200, float)
        # Most will be NaN; just confirm they're float
        assert isinstance(d.mom_252, float)
        assert isinstance(d.nearness, float)
