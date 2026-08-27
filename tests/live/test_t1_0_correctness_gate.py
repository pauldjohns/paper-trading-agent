# tests/live/test_t1_0_correctness_gate.py
"""T1.0 — Independent signal-correctness gate for autotrader_live.

This test file is an INDEPENDENT oracle. It does NOT use the code under test
as its reference. Instead it:
  - Recomputes close-series sub-signals using the already-trusted
    autotrader.indicators functions.
  - Builds its OWN numpy/explicit-loop reference implementations of TR and
    Wilder ATR (per the STRATEGY_CANARY_SPEC.md §1 definitions), and compares
    them against autotrader_live.indicators_ohlc (the module under test) and
    against the strategy_trend.decide() decision values.
  - Hand-works sizing arithmetic and stop arithmetic from spec primitives.
  - Includes a mutation test that proves the gate has teeth — it demonstrates
    that a wrong expected value would cause a real failure, not a silent pass.

The firewall: this file ONLY creates tests. It does NOT modify any file in
src/autotrader/ or src/autotrader_live/.
"""
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# ── path shim (worktree: src/ is on path via pyproject, but confirm) ──────────
_WD = Path(__file__).resolve().parent.parent.parent  # repo root
sys.path.insert(0, str(_WD / "src"))

# ── code under test ────────────────────────────────────────────────────────────
from autotrader_live.strategy_trend import decide
from autotrader_live import sizing, exits
from autotrader_live import indicators_ohlc

# ── trusted oracle (already validated in Plan 04 / Plan 05) ───────────────────
from autotrader.indicators import sma, trailing_return, nearness_to_high, rolling_high
from autotrader.datastore import DataStore

# ── test constants ─────────────────────────────────────────────────────────────
_CACHE = _WD / "data" / "cache"
_SYMBOLS = ["SPY", "XLK"]
_EQUITY = 100_000.0
_TOL_FLOAT = 1e-9   # for sub-signal cross-checks
_TOL_SIZING = 1e-6  # for arithmetic checks


# ══════════════════════════════════════════════════════════════════════════════
# REFERENCE IMPLEMENTATIONS (independent, written from spec, NOT from module)
# ══════════════════════════════════════════════════════════════════════════════

def _ref_true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """Reference TR from STRATEGY_CANARY_SPEC.md §1 (explicit loop, no vectorisation tricks).

    TR[0] = high[0] - low[0]
    TR[t] = max(high[t]-low[t], |high[t]-close[t-1]|, |low[t]-close[t-1]|)  for t >= 1
    """
    n = len(high)
    assert n == len(low) == len(close), "unequal lengths"
    tr = np.empty(n, dtype=np.float64)
    tr[0] = high[0] - low[0]
    for t in range(1, n):
        hl = high[t] - low[t]
        hc = abs(high[t] - close[t - 1])
        lc = abs(low[t] - close[t - 1])
        tr[t] = max(hl, hc, lc)
    return tr


def _ref_wilder_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
                    period: int = 14) -> np.ndarray:
    """Reference Wilder ATR from STRATEGY_CANARY_SPEC.md §1 (explicit seed + smoothing loop).

    NaN before index period-1.
    ATR[period-1] = mean(TR[0:period])
    ATR[t] = (ATR[t-1]*(period-1) + TR[t]) / period  for t >= period
    """
    tr = _ref_true_range(high, low, close)
    n = len(tr)
    out = np.full(n, float("nan"), dtype=np.float64)
    if n < period:
        return out
    out[period - 1] = tr[:period].mean()
    for t in range(period, n):
        out[t] = (out[t - 1] * (period - 1) + tr[t]) / period
    return out


def _ref_rolling_high_shifted(high: pd.Series, window: int) -> pd.Series:
    """Max of high over bars [t-window .. t-1] (excludes bar t).

    This mirrors rolling_high(high, window).shift(1) using trusted indicator + pandas shift.
    Written explicitly here so the cross-check is fully independent of indicators_ohlc.donchian.
    """
    # rolling_high is trusted (used in nearness_to_high etc.)
    return rolling_high(high, window).shift(1)


def _ref_prior_donch_numpy(high: np.ndarray, window: int = 55) -> np.ndarray:
    """Independent numpy/explicit-loop prior-Donchian upper: max(high[t-window .. t-1]).

    Excludes bar t (i.e. the max of the PRIOR `window` bars only).  Returns NaN
    at positions where fewer than `window` prior bars exist.

    This is GENUINELY INDEPENDENT of both rolling_high().shift(1) and
    indicators_ohlc.donchian — it uses a plain Python loop over numpy arrays,
    matching the §2 spec definition directly.
    """
    n = len(high)
    out = np.full(n, float("nan"), dtype=np.float64)
    for t in range(window, n):
        # Indices [t-window .. t-1] inclusive — exactly `window` bars, excludes t
        out[t] = np.max(high[t - window: t])
    return out


# ══════════════════════════════════════════════════════════════════════════════
# FIXTURE: load bars for both symbols
# ══════════════════════════════════════════════════════════════════════════════

@pytest.fixture(scope="module")
def store() -> DataStore:
    return DataStore(str(_CACHE))


@pytest.fixture(scope="module")
def bars_spy(store) -> pd.DataFrame:
    return store.load("SPY", "day", "split")


@pytest.fixture(scope="module")
def bars_xlk(store) -> pd.DataFrame:
    return store.load("XLK", "day", "split")


@pytest.fixture(scope="module")
def decision_spy(bars_spy) -> object:
    return decide(bars_spy, "SPY")


@pytest.fixture(scope="module")
def decision_xlk(bars_xlk) -> object:
    return decide(bars_xlk, "XLK")


# ══════════════════════════════════════════════════════════════════════════════
# PART (a) — Cross-check close-series sub-signals
# ══════════════════════════════════════════════════════════════════════════════

class TestCloseSeriesSubSignals:
    """Each check asserts the module-under-test agrees with an independently computed oracle."""

    @pytest.mark.parametrize("symbol", _SYMBOLS)
    def test_bar_count(self, store, symbol):
        """Data sanity: 5396 bars in the cache for each tested symbol."""
        bars = store.load(symbol, "day", "split")
        assert len(bars) == 5396, f"{symbol}: expected 5396 bars, got {len(bars)}"

    @pytest.mark.parametrize("symbol,bars_fixture,decision_fixture", [
        ("SPY", "bars_spy", "decision_spy"),
        ("XLK", "bars_xlk", "decision_xlk"),
    ])
    def test_sma200(self, symbol, bars_fixture, decision_fixture, request):
        bars = request.getfixturevalue(bars_fixture)
        dec = request.getfixturevalue(decision_fixture)
        t = len(bars) - 1
        close = bars["close"]
        ref_sma200 = sma(close, 200).iloc[t]
        assert not math.isnan(ref_sma200), f"{symbol}: sma200 oracle is NaN at last bar"
        assert abs(dec.sma200 - ref_sma200) < _TOL_FLOAT, (
            f"{symbol}: sma200 mismatch — decide()={dec.sma200!r}, oracle={ref_sma200!r}"
        )
        assert dec.trend_ok == bool(dec.close > ref_sma200), (
            f"{symbol}: trend_ok mismatch — decide()={dec.trend_ok}, "
            f"close={dec.close}, sma200={ref_sma200}"
        )

    @pytest.mark.parametrize("symbol,bars_fixture,decision_fixture", [
        ("SPY", "bars_spy", "decision_spy"),
        ("XLK", "bars_xlk", "decision_xlk"),
    ])
    def test_mom_252(self, symbol, bars_fixture, decision_fixture, request):
        bars = request.getfixturevalue(bars_fixture)
        dec = request.getfixturevalue(decision_fixture)
        t = len(bars) - 1
        close = bars["close"]
        ref_mom = trailing_return(close, 252, skip=0).iloc[t]
        assert not math.isnan(ref_mom), f"{symbol}: mom_252 oracle is NaN at last bar"
        assert abs(dec.mom_252 - ref_mom) < _TOL_FLOAT, (
            f"{symbol}: mom_252 mismatch — decide()={dec.mom_252!r}, oracle={ref_mom!r}"
        )
        assert dec.momentum_ok == bool(ref_mom > 0), (
            f"{symbol}: momentum_ok mismatch — decide()={dec.momentum_ok}, mom_252={ref_mom}"
        )

    @pytest.mark.parametrize("symbol,bars_fixture,decision_fixture", [
        ("SPY", "bars_spy", "decision_spy"),
        ("XLK", "bars_xlk", "decision_xlk"),
    ])
    def test_nearness(self, symbol, bars_fixture, decision_fixture, request):
        bars = request.getfixturevalue(bars_fixture)
        dec = request.getfixturevalue(decision_fixture)
        t = len(bars) - 1
        close = bars["close"]
        ref_nearness = nearness_to_high(close, 252).iloc[t]
        assert not math.isnan(ref_nearness), f"{symbol}: nearness oracle is NaN at last bar"
        assert abs(dec.nearness - ref_nearness) < _TOL_FLOAT, (
            f"{symbol}: nearness mismatch — decide()={dec.nearness!r}, oracle={ref_nearness!r}"
        )
        # 0.90 = the ratified near_threshold (the operator 2026-06-22); pinned independently
        # here so a drift in decide()'s default would correctly FAIL this gate.
        assert dec.near_high == bool(ref_nearness >= 0.90), (
            f"{symbol}: near_high mismatch — decide()={dec.near_high}, nearness={ref_nearness}"
        )

    @pytest.mark.parametrize("symbol,bars_fixture,decision_fixture", [
        ("SPY", "bars_spy", "decision_spy"),
        ("XLK", "bars_xlk", "decision_xlk"),
    ])
    def test_prior_donchian_upper(self, symbol, bars_fixture, decision_fixture, request):
        bars = request.getfixturevalue(bars_fixture)
        dec = request.getfixturevalue(decision_fixture)
        t = len(bars) - 1
        high_np = bars["high"].to_numpy(dtype=np.float64)

        # Oracle 1: rolling_high().shift(1) — same pandas primitive as the module.
        ref_prior_donch_pandas = _ref_rolling_high_shifted(bars["high"], 55).iloc[t]
        assert not math.isnan(ref_prior_donch_pandas), (
            f"{symbol}: prior_donch_upper pandas oracle is NaN at last bar"
        )
        assert abs(dec.prior_donch_upper - ref_prior_donch_pandas) < _TOL_FLOAT, (
            f"{symbol}: prior_donch_upper mismatch (pandas oracle) — "
            f"decide()={dec.prior_donch_upper!r}, oracle={ref_prior_donch_pandas!r}"
        )

        # Oracle 2: GENUINELY INDEPENDENT numpy/explicit-loop reference.
        # Uses max(high[t-55 .. t-1]) directly — no pandas, no indicators module.
        ref_prior_donch_numpy = _ref_prior_donch_numpy(high_np, 55)[t]
        assert not math.isnan(ref_prior_donch_numpy), (
            f"{symbol}: prior_donch_upper numpy oracle is NaN at last bar"
        )
        assert abs(dec.prior_donch_upper - ref_prior_donch_numpy) < _TOL_FLOAT, (
            f"{symbol}: prior_donch_upper mismatch (numpy independent oracle) — "
            f"decide()={dec.prior_donch_upper!r}, numpy oracle={ref_prior_donch_numpy!r}"
        )

        # The two independent oracles must also agree with each other.
        assert abs(ref_prior_donch_pandas - ref_prior_donch_numpy) < _TOL_FLOAT, (
            f"{symbol}: the two prior_donch_upper oracles disagree — test setup error; "
            f"pandas={ref_prior_donch_pandas!r}, numpy={ref_prior_donch_numpy!r}"
        )

        assert dec.breakout_55 == bool(dec.close > ref_prior_donch_pandas), (
            f"{symbol}: breakout_55 mismatch — decide()={dec.breakout_55}, "
            f"close={dec.close}, prior_donch_upper={ref_prior_donch_pandas}"
        )

    @pytest.mark.parametrize("symbol,bars_fixture,decision_fixture", [
        ("SPY", "bars_spy", "decision_spy"),
        ("XLK", "bars_xlk", "decision_xlk"),
    ])
    def test_atr14(self, symbol, bars_fixture, decision_fixture, request):
        """atr14 in the decision must match our reference Wilder ATR at position t."""
        bars = request.getfixturevalue(bars_fixture)
        dec = request.getfixturevalue(decision_fixture)
        t = len(bars) - 1
        high = bars["high"].to_numpy(dtype=np.float64)
        low = bars["low"].to_numpy(dtype=np.float64)
        close = bars["close"].to_numpy(dtype=np.float64)
        ref_atr_series = _ref_wilder_atr(high, low, close, 14)
        ref_atr_t = ref_atr_series[t]
        assert not math.isnan(ref_atr_t), f"{symbol}: reference ATR is NaN at last bar"
        assert abs(dec.atr14 - ref_atr_t) < _TOL_FLOAT, (
            f"{symbol}: atr14 mismatch — decide()={dec.atr14!r}, oracle={ref_atr_t!r}"
        )

    @pytest.mark.parametrize("symbol,bars_fixture,decision_fixture", [
        ("SPY", "bars_spy", "decision_spy"),
        ("XLK", "bars_xlk", "decision_xlk"),
    ])
    def test_entry_logic(self, symbol, bars_fixture, decision_fixture, request):
        """entry must equal trend_ok AND momentum_ok AND (near_high OR breakout_55),
        recomputed independently from the sub-signal oracles."""
        bars = request.getfixturevalue(bars_fixture)
        dec = request.getfixturevalue(decision_fixture)
        t = len(bars) - 1
        close = bars["close"]
        ref_trend_ok = bool(dec.close > sma(close, 200).iloc[t])
        ref_momentum_ok = bool(trailing_return(close, 252, skip=0).iloc[t] > 0)
        ref_near_high = bool(nearness_to_high(close, 252).iloc[t] >= 0.90)  # ratified 0.90
        ref_breakout_55 = bool(dec.close > _ref_rolling_high_shifted(bars["high"], 55).iloc[t])
        ref_entry = ref_trend_ok and ref_momentum_ok and (ref_near_high or ref_breakout_55)
        assert dec.entry == ref_entry, (
            f"{symbol}: entry mismatch — decide()={dec.entry}, oracle={ref_entry} "
            f"[trend_ok={ref_trend_ok}, momentum_ok={ref_momentum_ok}, "
            f"near_high={ref_near_high}, breakout_55={ref_breakout_55}]"
        )

    @pytest.mark.parametrize("symbol,bars_fixture,decision_fixture", [
        ("SPY", "bars_spy", "decision_spy"),
        ("XLK", "bars_xlk", "decision_xlk"),
    ])
    def test_reason_ok(self, symbol, bars_fixture, decision_fixture, request):
        """With 5396 bars, the decision must be reason='ok' (sufficient history)."""
        dec = request.getfixturevalue(decision_fixture)
        assert dec.reason == "ok", (
            f"{symbol}: expected reason='ok', got {dec.reason!r}"
        )


class TestFullSeriesATRandTR:
    """Cross-check the ENTIRE ATR and TR series — not just the last bar.

    This is the strong port check: all 5396 bars must agree between our reference
    and indicators_ohlc (the module under test) to tolerance 1e-9.
    """

    @pytest.mark.parametrize("symbol", _SYMBOLS)
    def test_true_range_full_series(self, store, symbol):
        bars = store.load(symbol, "day", "split")
        high = bars["high"].to_numpy(dtype=np.float64)
        low = bars["low"].to_numpy(dtype=np.float64)
        close = bars["close"].to_numpy(dtype=np.float64)

        ref_tr = _ref_true_range(high, low, close)
        mod_tr = indicators_ohlc.true_range(
            bars["high"], bars["low"], bars["close"]
        ).to_numpy(dtype=np.float64)

        assert ref_tr.shape == mod_tr.shape, (
            f"{symbol}: TR shape mismatch {ref_tr.shape} vs {mod_tr.shape}"
        )
        max_diff = np.nanmax(np.abs(ref_tr - mod_tr))
        assert max_diff < _TOL_FLOAT, (
            f"{symbol}: TR full-series max deviation {max_diff!r} exceeds {_TOL_FLOAT}"
        )

    @pytest.mark.parametrize("symbol", _SYMBOLS)
    def test_atr_full_series(self, store, symbol):
        bars = store.load(symbol, "day", "split")
        high = bars["high"].to_numpy(dtype=np.float64)
        low = bars["low"].to_numpy(dtype=np.float64)
        close = bars["close"].to_numpy(dtype=np.float64)

        ref_atr = _ref_wilder_atr(high, low, close, 14)
        mod_atr = indicators_ohlc.atr(
            bars["high"], bars["low"], bars["close"], 14
        ).to_numpy(dtype=np.float64)

        assert ref_atr.shape == mod_atr.shape, (
            f"{symbol}: ATR shape mismatch {ref_atr.shape} vs {mod_atr.shape}"
        )
        # Compare where BOTH series are non-NaN (warm-up period)
        both_valid = ~np.isnan(ref_atr) & ~np.isnan(mod_atr)
        assert both_valid.sum() == (len(ref_atr) - 13), (
            f"{symbol}: unexpected non-NaN count {both_valid.sum()} "
            f"(expected {len(ref_atr) - 13} non-NaN values)"
        )
        max_diff = np.max(np.abs(ref_atr[both_valid] - mod_atr[both_valid]))
        assert max_diff < _TOL_FLOAT, (
            f"{symbol}: ATR full-series max deviation {max_diff!r} exceeds {_TOL_FLOAT}"
        )
        # First 13 should be NaN in both
        assert np.all(np.isnan(ref_atr[:13])), f"{symbol}: ref ATR not NaN in warm-up"
        assert np.all(np.isnan(mod_atr[:13])), f"{symbol}: mod ATR not NaN in warm-up"


# ══════════════════════════════════════════════════════════════════════════════
# PART (b) — Hand-worked downstream arithmetic: sizing + stops
# ══════════════════════════════════════════════════════════════════════════════

class TestSizingArithmetic:
    """Verify sizing.size() output matches hand-worked arithmetic from spec §3."""

    def test_spy_sizing(self, decision_spy):
        dec = decision_spy
        close = dec.close
        atr14 = dec.atr14
        equity = _EQUITY
        # ── Spec §3 arithmetic ─────────────────────────────────────────────
        f = 0.01
        k = 3.0
        per_name_cap_frac = 0.15
        risk_per_share = k * atr14        # = 3 * ATR
        dollars_at_risk = f * equity      # = 1000.0
        raw_shares = dollars_at_risk / risk_per_share
        raw_notional = raw_shares * close
        cap_notional = per_name_cap_frac * equity   # = 15000.0
        if raw_notional > cap_notional:
            ref_notional = cap_notional
            ref_shares = cap_notional / close
            ref_capped = True
        else:
            ref_notional = raw_notional
            ref_shares = raw_shares
            ref_capped = False
        # fractional=True → no floor
        # ── Module output ──────────────────────────────────────────────────
        result = sizing.size(equity, atr14, close)
        assert abs(result["risk_per_share"] - risk_per_share) < _TOL_SIZING, (
            f"risk_per_share: got {result['risk_per_share']!r}, expected {risk_per_share!r}"
        )
        assert abs(result["dollars_at_risk"] - dollars_at_risk) < _TOL_SIZING, (
            f"dollars_at_risk: got {result['dollars_at_risk']!r}, expected {dollars_at_risk!r}"
        )
        assert abs(result["shares"] - ref_shares) < _TOL_SIZING, (
            f"shares: got {result['shares']!r}, expected {ref_shares!r}"
        )
        assert abs(result["notional"] - ref_notional) < _TOL_SIZING, (
            f"notional: got {result['notional']!r}, expected {ref_notional!r}"
        )
        assert result["capped"] == ref_capped, (
            f"capped: got {result['capped']!r}, expected {ref_capped!r}"
        )

    def test_spy_sizing_notional_constrained(self, decision_spy):
        """Confirm the cap logic fires correctly when we force a small ATR to produce
        a large raw_notional that DEFINITELY exceeds 15% cap."""
        close = decision_spy.close
        # tiny ATR → huge raw_shares
        tiny_atr = close * 0.0001  # ATR so tiny that raw_notional >> 15%
        result = sizing.size(_EQUITY, tiny_atr, close)
        assert result["capped"] is True, "Expected capped=True with tiny ATR"
        assert abs(result["notional"] - 0.15 * _EQUITY) < _TOL_SIZING


class TestStopArithmetic:
    """Verify exits.py stop functions match hand-worked arithmetic from spec §4."""

    def test_initial_catastrophe_stop(self, decision_spy):
        close = decision_spy.close
        atr14 = decision_spy.atr14
        ref_stop = close - 2.0 * atr14
        if ref_stop <= 0:
            # should raise — the spec says result > 0 else ValueError
            with pytest.raises(ValueError):
                exits.initial_catastrophe_stop(close, atr14)
        else:
            computed = exits.initial_catastrophe_stop(close, atr14)
            assert abs(computed - ref_stop) < _TOL_SIZING, (
                f"initial_catastrophe_stop: got {computed!r}, expected {ref_stop!r}"
            )

    def test_chandelier_and_ratchet(self, decision_spy):
        """One chandelier step with a hypothetical highest_high = close * 1.10."""
        close = decision_spy.close
        atr14 = decision_spy.atr14
        # Compute catastrophe stop (safe because real-world prices >> 2*ATR)
        try:
            initial_stop = exits.initial_catastrophe_stop(close, atr14)
        except ValueError:
            pytest.skip("initial_catastrophe_stop would be <= 0 for this price/ATR")

        highest_high = close * 1.10
        # Independent arithmetic per spec §4:
        ref_chandelier = highest_high - 3.0 * atr14
        ref_ratchet = max(initial_stop, ref_chandelier)
        # Module output
        chandelier_val = exits.chandelier_level(highest_high, atr14)
        assert abs(chandelier_val - ref_chandelier) < _TOL_SIZING, (
            f"chandelier_level: got {chandelier_val!r}, expected {ref_chandelier!r}"
        )
        ratchet_val = exits.ratchet_stop(initial_stop, chandelier_val)
        assert abs(ratchet_val - ref_ratchet) < _TOL_SIZING, (
            f"ratchet_stop: got {ratchet_val!r}, expected {ref_ratchet!r}"
        )
        update_val = exits.update_trailing_stop(initial_stop, highest_high, atr14)
        assert abs(update_val - ref_ratchet) < _TOL_SIZING, (
            f"update_trailing_stop: got {update_val!r}, expected {ref_ratchet!r}"
        )

    def test_monotonic_up_invariant(self, decision_spy):
        """A lower chandelier must NOT lower the stop (ratchet is monotonic-up).

        If the new chandelier is lower than prev_stop, the stop stays at prev_stop.
        """
        close = decision_spy.close
        atr14 = decision_spy.atr14
        try:
            initial_stop = exits.initial_catastrophe_stop(close, atr14)
        except ValueError:
            pytest.skip("initial_catastrophe_stop would be <= 0 for this price/ATR")

        # Use a high so small that chandelier < initial_stop
        # chandelier = highest_high - 3*atr14
        # We want chandelier < initial_stop = close - 2*atr14
        # => highest_high - 3*atr < close - 2*atr
        # => highest_high < close + atr
        # Use highest_high = close - 2*atr14 - 0.5 (guaranteed below initial_stop if
        # it's positive; fall back to close * 0.5 if necessary)
        tiny_high = max(initial_stop - atr14, close * 0.5)
        # Make sure chandelier would be < initial_stop
        ref_chandelier = exits.chandelier_level(tiny_high, atr14)
        if ref_chandelier >= initial_stop:
            pytest.skip("Could not construct a low-enough chandelier for this price/ATR")

        updated = exits.update_trailing_stop(initial_stop, tiny_high, atr14)
        assert abs(updated - initial_stop) < _TOL_SIZING, (
            f"Monotonic-up violated: stop dropped from {initial_stop!r} to {updated!r} "
            f"(chandelier={ref_chandelier!r})"
        )


# ══════════════════════════════════════════════════════════════════════════════
# PART (c) — Mutation test: the gate must have TEETH
# ══════════════════════════════════════════════════════════════════════════════

class TestMutationGateHasTEETH:
    """Demonstrate that the gate would CATCH a silent implementation bug.

    We perturb SPY's last close to force trend_ok=False, then confirm:
    1. The oracle AND decide() BOTH change (both detect the mutation).
    2. Using the PRE-mutation expected value against the post-mutation decide()
       output FAILS — proving the test has teeth, not just self-consistency.
    """

    def test_trend_flip_caught(self, bars_spy, decision_spy):
        """Flip close below SMA200 and confirm entry flips off."""
        t = len(bars_spy) - 1
        close = bars_spy["close"]
        sma200_val = sma(close, 200).iloc[t]

        # Determine the direction of current trend_ok
        current_trend_ok = decision_spy.trend_ok

        # Create a perturbed copy with close flipped to opposite side of SMA200
        perturbed = bars_spy.copy()
        if current_trend_ok:
            # close was above SMA200 → set it BELOW
            new_close = sma200_val * 0.90
        else:
            # close was below SMA200 → set it ABOVE
            new_close = sma200_val * 1.10

        perturbed.loc[perturbed.index[t], "close"] = new_close

        # ── Oracle on perturbed data ───────────────────────────────────────
        perturbed_close_series = perturbed["close"]
        oracle_trend_ok_post = bool(new_close > sma(perturbed_close_series, 200).iloc[t])

        # trend_ok must have flipped
        assert oracle_trend_ok_post != current_trend_ok, (
            "Mutation did not flip oracle trend_ok — the test setup is wrong; "
            "the perturbation must flip the oracle value."
        )

        # ── Module under test on perturbed data ───────────────────────────
        dec_post = decide(perturbed, "SPY")
        assert dec_post.trend_ok == oracle_trend_ok_post, (
            f"decide() trend_ok={dec_post.trend_ok!r} != oracle {oracle_trend_ok_post!r} "
            "after mutation — implementation ignored the perturbation!"
        )

        # ── Teeth demonstration ───────────────────────────────────────────
        # Using the PRE-mutation trend_ok value against the post-mutation decision
        # MUST produce a mismatch. This proves the test would catch a silent bug
        # where the implementation ignores the last bar's close.
        # (comment: if this assertion PASSED, it would mean the test is toothless)
        stale_expected_trend_ok = current_trend_ok   # the PRE-mutation value
        assert stale_expected_trend_ok != dec_post.trend_ok, (
            "TEETH FAILURE: the pre-mutation expected value matches the post-mutation "
            "decide() output — the gate cannot detect this class of bug."
        )

    def test_tr_perturbation_caught(self, bars_spy):
        """Perturb one TR input (a high bar) and confirm ATR changes in both oracle and module.

        This proves that a wiring bug — e.g. ATR using the wrong column — would be caught.
        """
        t = len(bars_spy) - 1
        high = bars_spy["high"].to_numpy(dtype=np.float64)
        low = bars_spy["low"].to_numpy(dtype=np.float64)
        close = bars_spy["close"].to_numpy(dtype=np.float64)

        # Pre-mutation ATR at t
        pre_atr_ref = _ref_wilder_atr(high, low, close, 14)[t]

        # Perturb: double the last high bar (makes TR[t] much larger, ripples into ATR[t])
        high_perturbed = high.copy()
        high_perturbed[t] = high[t] * 2.0

        # Post-mutation oracle
        post_atr_ref = _ref_wilder_atr(high_perturbed, low, close, 14)[t]

        # The mutation must change the oracle ATR
        assert abs(post_atr_ref - pre_atr_ref) > 1e-4, (
            "Mutation did not change oracle ATR — the perturbation was too small; test setup error."
        )

        # Build perturbed bars df
        perturbed = bars_spy.copy()
        perturbed.loc[perturbed.index[t], "high"] = high[t] * 2.0
        dec_post = decide(perturbed, "SPY")

        # Module must also change
        assert abs(dec_post.atr14 - pre_atr_ref) > 1e-4, (
            f"decide() atr14 did NOT change after high-perturbation "
            f"(pre={pre_atr_ref!r}, post={dec_post.atr14!r}) — "
            "implementation may be ignoring the high column."
        )

        # NOTE: the duplicate "teeth" assertion that was here (identical to the
        # one above) has been removed — one real assertion is sufficient.


# ══════════════════════════════════════════════════════════════════════════════
# PART (d) — Synthetic breakout-driven entry
# ══════════════════════════════════════════════════════════════════════════════

def _build_synthetic_breakout_frame(n: int = 310) -> pd.DataFrame:
    """Build a synthetic ≥260-bar OHLC DataFrame engineered so that:

      - trend_ok=True   (close > SMA200)
      - momentum_ok=True (252-bar return > 0, i.e. close > close 252 bars ago)
      - near_high=False  (close < 0.90 × trailing-252 CLOSING high)
      - breakout_55=True (close > max high of prior 55 bars)

    Design
    ------
    - Base close = 100.0 for bars 0 .. n-3 (except the peak described below).
    - 180 bars before the last bar (index t-180), inject a temporary peak close
      of 200.  This lands within the 252-bar window for nearness_to_high, making
      the rolling-252 high of closes = 200.  nearness = 100/200 = 0.50 < 0.90.
    - close[0] = 80 (< 100 = close[t]), so momentum_ok=True (252-bar return > 0).
    - high = close + 0.5 everywhere, EXCEPT in the 55-bar prior window where
      high = 99.0 (< close[t] = 100), ensuring prior_donch_upper = 99 < 100.
    - The peak at t-180 does NOT fall in the 55-bar prior window (t-180 < t-55),
      so the injected close-peak does not affect prior_donch_upper.
    - SMA200 is seeded by 200 bars of close ≈ 100 → SMA200 ≈ 100.  To ensure
      close[t] > SMA200[t] we set close[t] = 101.0 (just above 100) and confirm
      via the oracle.

    The test checks the conditions hold and exercises the breakout-driven entry path.
    """
    import datetime

    t = n - 1  # last bar index

    # Build close series: base 100, overrides below
    close_arr = np.full(n, 100.0, dtype=np.float64)
    # bar 0: slightly below 100 so momentum_ok=True (close[t]/close[0] - 1 > 0)
    close_arr[0] = 80.0
    # peak 180 bars before t: large close to inflate rolling-252 high of closes
    peak_idx = t - 180
    close_arr[peak_idx] = 200.0
    # last bar: 101.0 (> SMA200 which is close to 100 because most bars are ~100)
    close_arr[t] = 101.0

    # Build high series: base close + 0.5, but prior-55 window uses 99.0
    high_arr = close_arr + 0.5
    # Clamp the peak's high so it doesn't inflate the prior-55 donchian
    # (peak_idx is well outside the 55-bar prior window)
    prior_55_start = t - 55  # first bar of the prior-55 window
    # prior-55 window = [t-55 .. t-1]; we want max(high) in that window < close[t]=101
    high_arr[prior_55_start:t] = 99.0  # 99 < 101 → breakout_55=True

    # Build low series: close - 0.5 (just for ATR; no impact on the signals we test)
    low_arr = close_arr - 0.5

    # Build date column (arbitrary but ascending)
    base = datetime.date(2020, 1, 2)
    dates = [base.replace(year=base.year + i // 252, month=1, day=1) for i in range(n)]
    # Simpler: just add i days from base
    import datetime as dt
    dates = [dt.date(2020, 1, 2) + dt.timedelta(days=i) for i in range(n)]

    return pd.DataFrame({
        "date": dates,
        "open": close_arr - 0.25,
        "high": high_arr,
        "low": low_arr,
        "close": close_arr,
        "volume": np.full(n, 1_000_000, dtype=np.float64),
    })


class TestBreakoutDrivenEntry:
    """Exercise the breakout_55=True entry leg with a synthetic frame.

    Neither SPY nor XLK breaks out (they qualify via near_high).  This test
    constructs a name where near_high=False and breakout_55=True so the
    breakout leg is the deciding factor for entry=True.
    """

    def test_breakout_is_deciding_leg(self):
        """Synthetic frame: entry=True because breakout_55 fires (near_high=False)."""
        frame = _build_synthetic_breakout_frame(n=310)
        dec = decide(frame, "SYNTH")

        assert dec.reason == "ok", (
            f"SYNTH: expected reason='ok', got {dec.reason!r} — "
            "frame may not have enough bars or sub-signals are NaN"
        )
        assert dec.near_high is False, (
            f"SYNTH: near_high should be False (close={dec.close}, "
            f"nearness={dec.nearness:.4f}), but got near_high=True — "
            "the 252-closing-high peak is not inflating nearness as expected"
        )
        assert dec.breakout_55 is True, (
            f"SYNTH: breakout_55 should be True (close={dec.close}, "
            f"prior_donch_upper={dec.prior_donch_upper}), but got False — "
            "the high values in the prior-55 window may not be below close"
        )
        assert dec.entry is True, (
            f"SYNTH: entry should be True (trend_ok={dec.trend_ok}, "
            f"momentum_ok={dec.momentum_ok}, near_high={dec.near_high}, "
            f"breakout_55={dec.breakout_55})"
        )

        # Independent recomputation of entry from scratch
        ref_entry = dec.trend_ok and dec.momentum_ok and (dec.near_high or dec.breakout_55)
        assert dec.entry == ref_entry, (
            f"SYNTH: entry={dec.entry!r} does not match independent recomputation "
            f"trend_ok={dec.trend_ok} AND momentum_ok={dec.momentum_ok} AND "
            f"(near_high={dec.near_high} OR breakout_55={dec.breakout_55}) = {ref_entry!r}"
        )

        # Confirm breakout is truly the deciding leg: if we flip near_high and breakout_55
        # to both False, entry would be False — but that's entangled in the formula.
        # Instead, assert that near_high alone would NOT give entry (since it's False),
        # so breakout_55 is necessary for entry to be True.
        entry_without_breakout = dec.trend_ok and dec.momentum_ok and dec.near_high
        assert not entry_without_breakout, (
            "SYNTH: entry_without_breakout should be False (near_high=False) "
            "— breakout must be the deciding leg, but near_high is True here"
        )

    def test_trend_and_momentum_ok_in_synthetic(self):
        """Confirm the prerequisite sub-signals are correctly set in the synthetic frame."""
        frame = _build_synthetic_breakout_frame(n=310)
        dec = decide(frame, "SYNTH")

        assert dec.reason == "ok"
        assert dec.trend_ok is True, (
            f"SYNTH: trend_ok should be True (close={dec.close}, sma200={dec.sma200})"
        )
        assert dec.momentum_ok is True, (
            f"SYNTH: momentum_ok should be True (mom_252={dec.mom_252})"
        )


# ══════════════════════════════════════════════════════════════════════════════
# DIAGNOSTIC: print cross-checked numbers for the report
# ══════════════════════════════════════════════════════════════════════════════

def test_print_cross_checked_numbers(bars_spy, bars_xlk, decision_spy, decision_xlk):
    """Print the exact cross-checked values for both symbols for the T1.0 report.

    Not a gate per se — always passes — but ensures values are visible in output.
    """
    for symbol, bars, dec in [
        ("SPY", bars_spy, decision_spy),
        ("XLK", bars_xlk, decision_xlk),
    ]:
        t = len(bars) - 1
        close = bars["close"]
        high_np = bars["high"].to_numpy(dtype=np.float64)
        low_np = bars["low"].to_numpy(dtype=np.float64)
        close_np = bars["close"].to_numpy(dtype=np.float64)
        ref_atr = _ref_wilder_atr(high_np, low_np, close_np, 14)[t]
        print(
            f"\n[T1.0 report] {symbol} @ {dec.signal_date}:\n"
            f"  close          = {dec.close:.4f}\n"
            f"  sma200         = {dec.sma200:.4f}   (oracle={sma(close, 200).iloc[t]:.4f})\n"
            f"  trend_ok       = {dec.trend_ok}\n"
            f"  mom_252        = {dec.mom_252:.6f}  (oracle={trailing_return(close,252,0).iloc[t]:.6f})\n"
            f"  momentum_ok    = {dec.momentum_ok}\n"
            f"  nearness       = {dec.nearness:.6f}  (oracle={nearness_to_high(close,252).iloc[t]:.6f})\n"
            f"  near_high      = {dec.near_high}\n"
            f"  prior_donch_u  = {dec.prior_donch_upper:.4f}  "
            f"(oracle={_ref_rolling_high_shifted(bars['high'],55).iloc[t]:.4f})\n"
            f"  breakout_55    = {dec.breakout_55}\n"
            f"  atr14          = {dec.atr14:.6f}  (oracle={ref_atr:.6f})\n"
            f"  entry          = {dec.entry}\n"
            f"  reason         = {dec.reason}"
        )
    assert True  # always passes
