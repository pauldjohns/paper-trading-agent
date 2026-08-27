# tests/live/test_indicators_ohlc.py
"""Tests for autotrader_live.indicators_ohlc — OHLC-based indicators (ATR, TR, Donchian, EMA).

Oracle values were computed by hand (not from the implementation) to avoid tautological tests.
See task T1.1 for derivations.
"""
import math
import pytest
from autotrader_live.indicators_ohlc import true_range, atr, donchian, ema

# ---------------------------------------------------------------------------
# Shared oracle inputs
# ---------------------------------------------------------------------------
_HIGH  = [10.0, 11.0, 10.5, 12.0, 11.0]
_LOW   = [8.0,   9.0,  9.5, 10.0, 10.5]
_CLOSE = [9.0,  10.5, 10.0, 11.5, 10.8]

# ---------------------------------------------------------------------------
# true_range
# ---------------------------------------------------------------------------

class TestTrueRange:
    def test_oracle_values(self):
        """Hand-computed TR for the 5-bar reference series."""
        tr = true_range(_HIGH, _LOW, _CLOSE)
        expected = [2.0, 2.0, 1.0, 2.0, 1.0]
        assert len(tr) == 5
        for i, exp in enumerate(expected):
            assert abs(tr.iloc[i] - exp) < 1e-9, f"TR[{i}] = {tr.iloc[i]}, expected {exp}"

    def test_bar0_is_high_minus_low(self):
        """Bar 0 has no prior close, so TR[0] = high[0] - low[0]."""
        tr = true_range([15.0], [10.0], [12.0])
        assert abs(tr.iloc[0] - 5.0) < 1e-9

    def test_no_nans_any_bar(self):
        """TR is defined for every bar — no warm-up NaNs."""
        tr = true_range(_HIGH, _LOW, _CLOSE)
        assert not tr.isna().any()

    def test_intrabar_gap_prevclose_dominates(self):
        """Gap case: |high - prev_close| dominates over (high - low) of current bar."""
        # bar 0: high=10, low=9, close=9.5
        # bar 1: opens gap up: high=12, low=11, close=11
        #   high-low = 1.0, |high-prevclose| = |12-9.5| = 2.5 -> TR[1] = 2.5
        tr = true_range([10.0, 12.0], [9.0, 11.0], [9.5, 11.0])
        assert abs(tr.iloc[0] - 1.0) < 1e-9   # 10-9
        assert abs(tr.iloc[1] - 2.5) < 1e-9   # |12-9.5|

    def test_low_prevclose_dominates(self):
        """Gap-down case: |low - prev_close| dominates."""
        # bar 0: high=10, low=9, close=9.5
        # bar 1: gap down: high=8, low=7, close=7.5
        #   high-low=1, |high-prevclose|=|8-9.5|=1.5, |low-prevclose|=|7-9.5|=2.5
        tr = true_range([10.0, 8.0], [9.0, 7.0], [9.5, 7.5])
        assert abs(tr.iloc[1] - 2.5) < 1e-9

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            true_range([10, 11], [8, 9], [9])        # close is shorter

    def test_high_low_close_mismatch_raises(self):
        with pytest.raises(ValueError):
            true_range([10, 11, 12], [8, 9], [9, 10])  # high is longer

    def test_accepts_list_input_and_preserves_length(self):
        tr = true_range(_HIGH, _LOW, _CLOSE)
        assert len(tr) == len(_HIGH)

    def test_position_indexed_rangeindex(self):
        """Output must carry a RangeIndex regardless of incoming index type."""
        import pandas as pd
        h = pd.Series(_HIGH, index=[10, 20, 30, 40, 50])
        l = pd.Series(_LOW,  index=[10, 20, 30, 40, 50])
        c = pd.Series(_CLOSE, index=[10, 20, 30, 40, 50])
        tr = true_range(h, l, c)
        assert list(tr.index) == list(range(5))


# ---------------------------------------------------------------------------
# atr
# ---------------------------------------------------------------------------

class TestAtr:
    def test_oracle_values_period3(self):
        """Hand-computed ATR(3) for the 5-bar reference series."""
        result = atr(_HIGH, _LOW, _CLOSE, period=3)
        assert len(result) == 5
        assert math.isnan(result.iloc[0])
        assert math.isnan(result.iloc[1])
        assert abs(result.iloc[2] - 1.6666666667) < 1e-9
        assert abs(result.iloc[3] - 1.7777777778) < 1e-9
        assert abs(result.iloc[4] - 1.5185185185) < 1e-9

    def test_warmup_nans_index_less_than_period_minus_1(self):
        """NaN at every index < period-1."""
        result = atr(_HIGH, _LOW, _CLOSE, period=3)
        assert math.isnan(result.iloc[0])
        assert math.isnan(result.iloc[1])
        assert not math.isnan(result.iloc[2])

    def test_n_less_than_period_all_nan(self):
        """When the series has fewer bars than period, all outputs are NaN."""
        result = atr([10, 11], [8, 9], [9, 10], period=5)
        assert result.isna().all()

    def test_period_less_than_1_raises(self):
        with pytest.raises(ValueError):
            atr(_HIGH, _LOW, _CLOSE, period=0)

    def test_period_1_no_nans(self):
        """period=1 seeds at bar 0 (mean of 1 TR) and smooths immediately."""
        result = atr(_HIGH, _LOW, _CLOSE, period=1)
        assert not result.isna().any()

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            atr([10, 11], [8, 9], [9], period=2)

    def test_accepts_list_input_and_preserves_length(self):
        result = atr(_HIGH, _LOW, _CLOSE, period=3)
        assert len(result) == len(_HIGH)

    def test_first_valid_index_is_period_minus_1(self):
        """First valid (non-NaN) index is period-1, NOT period."""
        result = atr(_HIGH, _LOW, _CLOSE, period=3)
        assert not math.isnan(result.iloc[2])  # index 2 = period-1

    def test_seed_is_simple_mean_of_first_period_trs(self):
        """ATR[period-1] == mean(TR[0:period]) — validates seeding, not Wilder smoothing."""
        result = atr(_HIGH, _LOW, _CLOSE, period=3)
        tr = true_range(_HIGH, _LOW, _CLOSE)
        seed_expected = (tr.iloc[0] + tr.iloc[1] + tr.iloc[2]) / 3
        assert abs(result.iloc[2] - seed_expected) < 1e-9


# ---------------------------------------------------------------------------
# donchian
# ---------------------------------------------------------------------------

class TestDonchian:
    def test_oracle_values_window2(self):
        """Hand-computed Donchian(2) for the 5-bar reference series."""
        dc = donchian(_HIGH, _LOW, window=2)
        upper_expected = [float("nan"), 11.0, 11.0, 12.0, 12.0]
        lower_expected = [float("nan"),  8.0,  9.0,  9.5, 10.0]
        assert list(dc.columns) == ["upper", "lower"]
        assert len(dc) == 5
        assert math.isnan(dc["upper"].iloc[0])
        assert math.isnan(dc["lower"].iloc[0])
        for i in range(1, 5):
            assert abs(dc["upper"].iloc[i] - upper_expected[i]) < 1e-9, \
                f"upper[{i}] = {dc['upper'].iloc[i]}, expected {upper_expected[i]}"
            assert abs(dc["lower"].iloc[i] - lower_expected[i]) < 1e-9, \
                f"lower[{i}] = {dc['lower'].iloc[i]}, expected {lower_expected[i]}"

    def test_returns_dataframe_with_correct_columns(self):
        """Output is a DataFrame with exactly columns ['upper', 'lower']."""
        import pandas as pd
        dc = donchian(_HIGH, _LOW, window=2)
        assert isinstance(dc, pd.DataFrame)
        assert list(dc.columns) == ["upper", "lower"]

    def test_warmup_nans(self):
        """NaN until `window` bars are available (window-1 NaNs at the start)."""
        dc = donchian(_HIGH, _LOW, window=3)
        assert math.isnan(dc["upper"].iloc[0])
        assert math.isnan(dc["upper"].iloc[1])
        assert not math.isnan(dc["upper"].iloc[2])

    def test_window_1_no_nans(self):
        """window=1: every bar is its own window — no NaNs."""
        dc = donchian(_HIGH, _LOW, window=1)
        assert not dc["upper"].isna().any()
        assert not dc["lower"].isna().any()
        # upper[t] == high[t], lower[t] == low[t]
        for i, (h, l) in enumerate(zip(_HIGH, _LOW)):
            assert abs(dc["upper"].iloc[i] - h) < 1e-9
            assert abs(dc["lower"].iloc[i] - l) < 1e-9

    def test_window_less_than_1_raises(self):
        with pytest.raises(ValueError):
            donchian(_HIGH, _LOW, window=0)

    def test_length_mismatch_raises(self):
        with pytest.raises(ValueError):
            donchian([10, 11], [8], window=2)

    def test_position_indexed_rangeindex(self):
        """Output index must be RangeIndex regardless of incoming index."""
        import pandas as pd
        h = pd.Series(_HIGH, index=[10, 20, 30, 40, 50])
        l = pd.Series(_LOW,  index=[10, 20, 30, 40, 50])
        dc = donchian(h, l, window=2)
        assert list(dc.index) == list(range(5))

    def test_accepts_list_input_and_preserves_length(self):
        dc = donchian(_HIGH, _LOW, window=2)
        assert len(dc) == len(_HIGH)


# ---------------------------------------------------------------------------
# ema
# ---------------------------------------------------------------------------

class TestEma:
    def test_oracle_values_period3(self):
        """Hand-computed SMA-seeded EMA(3) on the reference close series."""
        result = ema(_CLOSE, period=3)
        assert len(result) == 5
        assert math.isnan(result.iloc[0])
        assert math.isnan(result.iloc[1])
        assert abs(result.iloc[2] - 9.8333333333) < 1e-9
        assert abs(result.iloc[3] - 10.6666666667) < 1e-9
        assert abs(result.iloc[4] - 10.7333333333) < 1e-9

    def test_warmup_nans(self):
        """NaN at every index < period-1."""
        result = ema(_CLOSE, period=3)
        assert math.isnan(result.iloc[0])
        assert math.isnan(result.iloc[1])
        assert not math.isnan(result.iloc[2])

    def test_period_1_no_nans(self):
        """period=1: alpha=1, so EMA[t]=prices[t] for all t."""
        result = ema([5.0, 6.0, 7.0], period=1)
        assert not result.isna().any()
        assert abs(result.iloc[0] - 5.0) < 1e-9
        assert abs(result.iloc[1] - 6.0) < 1e-9
        assert abs(result.iloc[2] - 7.0) < 1e-9

    def test_period_less_than_1_raises(self):
        with pytest.raises(ValueError):
            ema(_CLOSE, period=0)

    def test_accepts_list_input_and_preserves_length(self):
        result = ema(list(_CLOSE), period=3)
        assert len(result) == len(_CLOSE)

    def test_n_less_than_period_all_nan(self):
        """Fewer bars than period yields all NaN."""
        result = ema([10.0, 11.0], period=5)
        assert result.isna().all()

    def test_seed_is_sma_of_first_period_bars(self):
        """EMA[period-1] == mean(prices[0:period])."""
        result = ema(_CLOSE, period=3)
        seed_expected = sum(_CLOSE[:3]) / 3
        assert abs(result.iloc[2] - seed_expected) < 1e-9

    def test_position_indexed_rangeindex(self):
        """Incoming date-indexed Series: output must carry RangeIndex."""
        import pandas as pd
        import datetime as dt
        prices = pd.Series(_CLOSE, index=[dt.date(2026, 1, i+1) for i in range(5)])
        result = ema(prices, period=3)
        assert list(result.index) == list(range(5))

    def test_wilder_smoothing_formula(self):
        """Validate a single smoothing step: EMA[t] = alpha*p[t] + (1-alpha)*EMA[t-1].
        Uses the independently hand-worked seed (mean of _CLOSE[0:3] = 29.5/3), NOT
        result.iloc[2], so a wrong seed/alpha can't make this pass for the wrong reason."""
        result = ema(_CLOSE, period=3)
        alpha = 2 / (3 + 1)
        seed = (9.0 + 10.5 + 10.0) / 3  # = mean(_CLOSE[0:3]), hand-worked, not from `result`
        expected_3 = alpha * _CLOSE[3] + (1 - alpha) * seed
        assert abs(result.iloc[3] - expected_3) < 1e-9
