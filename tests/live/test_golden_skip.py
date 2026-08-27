# tests/live/test_golden_skip.py
"""Unit tests for the infeasible-catastrophe-stop skip branch in golden._build_row.

When a name's entry=True but initial_catastrophe_stop(close, atr14, m) would
raise ValueError (ATR too wide — close - m*ATR <= 0), golden.py must NOT
propagate the exception.  Instead it records the row with:
  - sizing: null
  - initial_catastrophe_stop: null
  - "skip_reason": "infeasible_catastrophe_stop"

Normal rows (entry=True with feasible stop, and entry=False rows) must NOT
carry the "skip_reason" key.

The 15-ETF golden fixture must remain byte-identical throughout (none of the
real ETFs triggers the infeasible-stop branch).
"""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

# ── path shim ────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from autotrader_live.golden import _build_row, GOLDEN_PARAMS, GOLDEN_EQUITY


# ── helpers ───────────────────────────────────────────────────────────────────

def _make_bars(close_val: float, atr_multiplier: float = 1.0, n: int = 310) -> pd.DataFrame:
    """Build a minimal synthetic OHLC DataFrame with `n` bars.

    Parameters
    ----------
    close_val:
        Constant close price for all bars (except bar 0 which is set to close_val * 0.8
        so momentum_ok=True and trend_ok=True are plausible).
    atr_multiplier:
        Scales high/low spread to inflate ATR.  Pass a large value to force
        initial_catastrophe_stop to produce a non-positive result.
    n:
        Number of bars.  Must be >= 253 for sub-signals to be non-NaN.
    """
    spread = close_val * atr_multiplier
    close_arr = np.full(n, close_val, dtype=np.float64)
    close_arr[0] = close_val * 0.8  # ensure momentum_ok=True

    # High/low: close +/- half the spread
    high_arr = close_arr + spread * 0.5
    low_arr = np.maximum(close_arr - spread * 0.5, 0.01)  # keep positive

    dates = [dt.date(2015, 1, 2) + dt.timedelta(days=i) for i in range(n)]

    return pd.DataFrame({
        "date": dates,
        "open": close_arr - spread * 0.1,
        "high": high_arr,
        "low": low_arr,
        "close": close_arr,
        "volume": np.full(n, 1_000_000, dtype=np.float64),
    })


def _low_price_high_atr_bars(n: int = 310) -> pd.DataFrame:
    """Synthetic bars engineered to trigger the infeasible-stop branch.

    Design
    ------
    Uses a gently rising price from 0.5 to 1.5 over `n` bars, which gives:
      - trend_ok=True (close > SMA200, which lags the rising trend)
      - momentum_ok=True (252-bar return > 0)
      - near_high=True (rising series → nearness ≈ 1)

    The high-low SPREAD is set to 2.0 per bar (constant), producing a Wilder
    ATR ≈ 3.5 after warm-up.  With close ≈ 1.5 and m=2.0:
      stop = 1.5 - 2.0 * 3.5 ≈ -5.5  → ValueError from initial_catastrophe_stop.

    This ensures entry=True AND the catastrophe stop is infeasible, which
    drives the skip branch in _build_row.

    Parameters
    ----------
    n:
        Number of bars.  Must be >= 253 for non-NaN sub-signals.
    """
    close_arr = np.linspace(0.5, 1.5, n, dtype=np.float64)
    high_arr = close_arr + 2.0   # huge spread → ATR >> close / m
    low_arr = np.maximum(close_arr - 2.0, 0.001)

    dates = [dt.date(2015, 1, 2) + dt.timedelta(days=i) for i in range(n)]
    return pd.DataFrame({
        "date": dates,
        "open": close_arr,
        "high": high_arr,
        "low": low_arr,
        "close": close_arr,
        "volume": np.full(n, 1_000_000, dtype=np.float64),
    })


# ══════════════════════════════════════════════════════════════════════════════
# Tests
# ══════════════════════════════════════════════════════════════════════════════

class TestInfeasibleStopSkip:
    """_build_row skips (not crashes) when initial_catastrophe_stop would raise."""

    def test_skip_branch_no_exception(self):
        """Verify _build_row does not propagate ValueError for infeasible stop."""
        bars = _low_price_high_atr_bars()
        params = GOLDEN_PARAMS
        # Must not raise
        row = _build_row(
            bars, "INFEAS",
            equity=GOLDEN_EQUITY,
            near_threshold=params["near_threshold"],
            f=params["f"],
            k=params["k"],
            per_name_cap_frac=params["per_name_cap_frac"],
            m=params["m"],
        )
        assert row is not None, "_build_row returned None instead of a row"

    def test_skip_branch_produces_correct_skip_reason(self):
        """Skipped row must have skip_reason='infeasible_catastrophe_stop'."""
        bars = _low_price_high_atr_bars()
        params = GOLDEN_PARAMS
        row = _build_row(
            bars, "INFEAS",
            equity=GOLDEN_EQUITY,
            near_threshold=params["near_threshold"],
            f=params["f"],
            k=params["k"],
            per_name_cap_frac=params["per_name_cap_frac"],
            m=params["m"],
        )
        # Verify the synthetic frame actually produced entry=True (design check)
        assert row["decision"]["entry"], (
            "Test setup error: synthetic bars did not produce entry=True. "
            "The infeasible-stop branch requires entry=True first."
        )
        assert "skip_reason" in row, (
            f"Row for entry=True with infeasible stop is missing 'skip_reason'. "
            f"Row keys: {list(row.keys())}"
        )
        assert row["skip_reason"] == "infeasible_catastrophe_stop", (
            f"skip_reason={row['skip_reason']!r}, expected 'infeasible_catastrophe_stop'"
        )
        assert row["sizing"] is None, (
            f"sizing should be null on skipped row, got {row['sizing']!r}"
        )
        assert row["initial_catastrophe_stop"] is None, (
            f"initial_catastrophe_stop should be null on skipped row, "
            f"got {row['initial_catastrophe_stop']!r}"
        )

    def test_normal_rows_have_no_skip_reason(self):
        """Normal rows (feasible stop or entry=False) must NOT carry 'skip_reason'."""
        # A high-price name with tiny spread: stop is always feasible
        bars = _make_bars(close_val=500.0, atr_multiplier=0.01)
        params = GOLDEN_PARAMS
        row = _build_row(
            bars, "NORMAL",
            equity=GOLDEN_EQUITY,
            near_threshold=params["near_threshold"],
            f=params["f"],
            k=params["k"],
            per_name_cap_frac=params["per_name_cap_frac"],
            m=params["m"],
        )
        assert "skip_reason" not in row, (
            f"Normal row unexpectedly carries 'skip_reason': {row.get('skip_reason')!r}"
        )

    def test_entry_false_rows_have_no_skip_reason(self):
        """An entry=False row must not carry skip_reason (no stop attempted)."""
        # Manufacture a row that will be entry=False: close < SMA200
        # Use a downward-trending series
        n = 310
        close_arr = np.linspace(200.0, 50.0, n, dtype=np.float64)  # declining
        high_arr = close_arr + 1.0
        low_arr = close_arr - 1.0
        dates = [dt.date(2015, 1, 2) + dt.timedelta(days=i) for i in range(n)]
        bars = pd.DataFrame({
            "date": dates,
            "open": close_arr,
            "high": high_arr,
            "low": low_arr,
            "close": close_arr,
            "volume": np.full(n, 1_000_000, dtype=np.float64),
        })
        params = GOLDEN_PARAMS
        row = _build_row(
            bars, "NOENTRY",
            equity=GOLDEN_EQUITY,
            near_threshold=params["near_threshold"],
            f=params["f"],
            k=params["k"],
            per_name_cap_frac=params["per_name_cap_frac"],
            m=params["m"],
        )
        assert not row["decision"]["entry"], (
            "Test setup: expected entry=False for declining series"
        )
        assert "skip_reason" not in row, (
            "entry=False row must not carry 'skip_reason'"
        )
        assert row["sizing"] is None
        assert row["initial_catastrophe_stop"] is None


class TestGoldenFixtureUnchanged:
    """The 15-ETF fixture must stay byte-identical after the golden.py refactor."""

    def test_golden_replay_still_passes(self):
        """Import the golden replay module logic directly to confirm no regression.

        The real regression lock is in test_golden_replay.py; this is a quick
        sanity check that _build_row (the new helper) produces identical results
        for normal ETF names (none trigger the infeasible-stop branch).
        """
        cache_path = _REPO_ROOT / "data" / "cache"
        if not cache_path.exists():
            pytest.skip(f"Cache not found at {cache_path}")

        from autotrader.datastore import DataStore
        from autotrader_live.golden import build_snapshot, snapshot_json, GOLDEN_SYMBOLS

        store = DataStore(str(cache_path))
        snapshot = build_snapshot(store)

        # No row in the 15-ETF snapshot may carry skip_reason (all have feasible stops)
        for row in snapshot["decisions"]:
            assert "skip_reason" not in row, (
                f"{row['symbol']}: unexpected skip_reason in golden snapshot — "
                "the 15-ETF fixture contains a name with an infeasible stop, "
                "which means the golden fixture must be regenerated."
            )
