# tests/live/test_golden_replay.py
"""T1.5 — Deterministic offline-replay golden lock.

This file is a REGRESSION LOCK, not a correctness check.  Correctness was
established in T1.0 (test_t1_0_correctness_gate.py).  Here we:

  Test A — Byte equality
      Re-run ``build_snapshot(DataStore(<cache>))`` and assert the serialised
      JSON is IDENTICAL to the committed fixture, character for character.  Any
      future code change that shifts a decision, sizing value, or stop price
      will fail this test.

  Test B — Structural sanity
      Load the frozen fixture directly and assert structural invariants:
        - exactly 15 decisions
        - symbols == GOLDEN_SYMBOLS (sorted ascending)
        - every reason == "ok"
        - SPY: entry=True, sizing non-null, initial_catastrophe_stop > 0 and
          < SPY close

  Test C — Round-trip dict equality
      ``json.loads`` of the frozen fixture must equal ``build_snapshot(...)``
      as a Python dict (deep equality, after the same rounding).

FIREWALL — this file does NOT modify any file in src/autotrader/ or
src/autotrader_live/.  It uses both packages read-only.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from autotrader.datastore import DataStore
from autotrader_live.golden import (
    GOLDEN_SYMBOLS,
    build_snapshot,
    snapshot_json,
)

# ── path constants (resolved relative to this file — robust to any CWD) ───────
_FIXTURE = Path(__file__).parent / "fixtures" / "golden_trend_decisions.json"
_CACHE = Path(__file__).resolve().parents[2] / "data" / "cache"


# ── shared fixtures ────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def cache_exists() -> None:
    """Assert the cache directory exists; skip all tests if not."""
    if not _CACHE.exists():
        pytest.skip(f"Cache directory not found: {_CACHE} — run build_price_cache.py first")


@pytest.fixture(scope="module")
def frozen_text() -> str:
    """Load the committed fixture text."""
    assert _FIXTURE.exists(), (
        f"Frozen fixture missing: {_FIXTURE}\n"
        "Run: python src/autotrader_live/golden.py  (from the repo root)"
    )
    return _FIXTURE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frozen_dict(frozen_text) -> dict:
    """Parse the committed fixture as a Python dict."""
    return json.loads(frozen_text)


@pytest.fixture(scope="module")
def live_snapshot(cache_exists) -> dict:
    """Build a fresh snapshot from the cache."""
    store = DataStore(str(_CACHE))
    return build_snapshot(store)


@pytest.fixture(scope="module")
def live_text(live_snapshot) -> str:
    """Serialise the fresh snapshot to JSON."""
    return snapshot_json(live_snapshot)


# ══════════════════════════════════════════════════════════════════════════════
# Test A — Byte (character) equality: the determinism lock
# ══════════════════════════════════════════════════════════════════════════════

class TestDeterminismLock:
    """Any drift from the frozen fixture fails here."""

    def test_json_identical_to_frozen_fixture(self, live_text, frozen_text):
        """``snapshot_json(build_snapshot(cache))`` must equal the frozen file exactly.

        This is the regression lock.  A code change that shifts any decision,
        sizing value, stop price, or rounding will cause this test to fail.
        """
        assert live_text == frozen_text, (
            "Golden replay mismatch — the today-decision pipeline output has "
            "diverged from the frozen fixture.\n"
            "If this is an INTENTIONAL change, regenerate the fixture:\n"
            "  python src/autotrader_live/golden.py\n"
            "then review and commit the new fixture alongside the code change."
        )

    def test_two_independent_builds_are_identical(self, cache_exists):
        """Re-run build_snapshot twice and confirm identical output.

        This directly verifies that the builder is deterministic across
        consecutive calls (no hidden state, no timestamp injection, etc.).
        """
        store = DataStore(str(_CACHE))
        text_a = snapshot_json(build_snapshot(store))
        text_b = snapshot_json(build_snapshot(store))
        assert text_a == text_b, (
            "build_snapshot() is NOT deterministic: two consecutive builds "
            "produced different JSON output."
        )


# ══════════════════════════════════════════════════════════════════════════════
# Test B — Structural sanity against the frozen fixture
# ══════════════════════════════════════════════════════════════════════════════

class TestStructuralSanity:
    """Structural invariants checked against the frozen fixture directly."""

    def test_exactly_15_decisions(self, frozen_dict):
        decisions = frozen_dict["decisions"]
        assert len(decisions) == 15, (
            f"Expected 15 decisions in frozen fixture, got {len(decisions)}"
        )

    def test_symbols_match_golden_symbols_sorted(self, frozen_dict):
        """Symbols in the fixture must equal GOLDEN_SYMBOLS sorted ascending."""
        fixture_symbols = [d["symbol"] for d in frozen_dict["decisions"]]
        expected = sorted(GOLDEN_SYMBOLS)
        assert fixture_symbols == expected, (
            f"Symbol list mismatch.\n  fixture : {fixture_symbols}\n  expected: {expected}"
        )

    def test_all_decisions_reason_ok(self, frozen_dict):
        """All 15 ETFs have 5396 bars — every decision must be reason='ok'."""
        bad = [
            (d["symbol"], d["decision"]["reason"])
            for d in frozen_dict["decisions"]
            if d["decision"]["reason"] != "ok"
        ]
        assert not bad, (
            f"Unexpected reason values (should all be 'ok'): {bad}"
        )

    def test_no_nan_in_float_fields(self, frozen_dict):
        """No float field in the fixture may be NaN (serialised as JSON null or literal)."""
        def _check_no_nan(obj, path=""):
            if isinstance(obj, float):
                assert not math.isnan(obj), f"NaN at {path}"
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    _check_no_nan(v, f"{path}.{k}")
            elif isinstance(obj, list):
                for i, v in enumerate(obj):
                    _check_no_nan(v, f"{path}[{i}]")

        _check_no_nan(frozen_dict)

    def test_spy_entry_true_with_nonnull_sizing(self, frozen_dict):
        """SPY must have entry=True (established by T1.0 correctness gate)."""
        spy = next(
            (d for d in frozen_dict["decisions"] if d["symbol"] == "SPY"), None
        )
        assert spy is not None, "SPY not found in frozen fixture"
        assert spy["decision"]["entry"] is True, (
            f"SPY entry expected True, got {spy['decision']['entry']!r}"
        )
        assert spy["sizing"] is not None, (
            "SPY sizing must be non-null when entry=True"
        )

    def test_spy_initial_catastrophe_stop_positive_and_below_close(self, frozen_dict):
        """SPY initial_catastrophe_stop must be > 0 and < SPY close."""
        spy = next(d for d in frozen_dict["decisions"] if d["symbol"] == "SPY")
        stop = spy["initial_catastrophe_stop"]
        close = spy["decision"]["close"]
        assert stop is not None, "SPY initial_catastrophe_stop must be non-null"
        assert isinstance(stop, (int, float)), (
            f"SPY stop must be numeric, got {type(stop)}"
        )
        assert stop > 0, f"SPY stop must be > 0, got {stop}"
        assert stop < close, (
            f"SPY stop ({stop}) must be < SPY close ({close})"
        )

    def test_schema_version_and_signal_basis(self, frozen_dict):
        """Top-level metadata fields must match expectations."""
        assert frozen_dict["schema_version"] == 1
        assert frozen_dict["signal_basis"] == "split"
        assert frozen_dict["equity"] == 100_000.0

    def test_entry_true_symbols_have_sizing_and_stop(self, frozen_dict):
        """Every decision with entry=True must have non-null sizing and stop;
        every entry=False must have null sizing and stop."""
        for d in frozen_dict["decisions"]:
            sym = d["symbol"]
            entry = d["decision"]["entry"]
            has_sizing = d["sizing"] is not None
            has_stop = d["initial_catastrophe_stop"] is not None
            if entry:
                assert has_sizing, f"{sym}: entry=True but sizing is null"
                assert has_stop, f"{sym}: entry=True but stop is null"
            else:
                assert not has_sizing, f"{sym}: entry=False but sizing is non-null"
                assert not has_stop, f"{sym}: entry=False but stop is non-null"


# ══════════════════════════════════════════════════════════════════════════════
# Test C — Round-trip dict equality
# ══════════════════════════════════════════════════════════════════════════════

class TestRoundTrip:
    """``json.loads`` of the frozen fixture must equal ``build_snapshot(...)``."""

    def test_roundtrip_dict_equality(self, live_snapshot, frozen_dict):
        """Deep dict equality between the fresh build and the parsed frozen fixture.

        This catches rounding discrepancies, missing keys, and ordering
        differences in the data layer — complementary to the byte-equality check
        which also catches whitespace/formatting drift.
        """
        assert live_snapshot == frozen_dict, (
            "Round-trip dict mismatch: build_snapshot() dict != json.loads(frozen_fixture).\n"
            "This means the live build and the frozen fixture are structurally diverged "
            "even if both are individually valid JSON."
        )
