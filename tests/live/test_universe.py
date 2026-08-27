"""Tests for src/autotrader_live/universe.py (Task T2.2, Commit 2).

Strategy
--------
Build a ``StaticMarketData`` backed by real cached ETF bars (SPY, XLK, XLE,
AGG from DataStore) for the historicals path.  Synthetic ``ScanRow``,
``Tradability``, ``Fundamentals``, and ``EarningsEvent`` objects stand in for
everything else.

The signal_date used throughout is 2026-06-16 — the last bar date in the
cache.  Both SPY and XLK produce entry=True from real bars; XLE and AGG
produce entry=False.

Tuneable-defaults documented in comments so they're easy to find:
    near_threshold = 0.90   (default)
    blackout_days  = 5      (default)
    top_n          = 10     (default)
    rank_key       = "mom_252"  (default)
"""
from __future__ import annotations

import dataclasses
import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from autotrader.datastore import DataStore
from autotrader_live.cost_tier import TIER_MEGA_CAP, TIER_OTHER
from autotrader_live.mcp_live import (
    EarningsEvent,
    Fundamentals,
    ScanRow,
    StaticMarketData,
    Tradability,
)
from autotrader_live.universe import Candidate, UniverseResult, build_universe

# ── Shared fixtures ────────────────────────────────────────────────────────────

_SIGNAL_DATE = dt.date(2026, 6, 16)

# Cache path relative to repo root (resolved from test file location)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CACHE_DIR = str(_REPO_ROOT / "data" / "cache")


def _load_bars(symbol: str) -> pd.DataFrame:
    """Load real split-adjusted daily bars from the local cache."""
    ds = DataStore(_CACHE_DIR)
    return ds.load(symbol, "day", "split")


def _make_scan_row(
    symbol: str,
    instrument_type: str = "EQUITY",
    market_cap: float = 100e9,
) -> ScanRow:
    return ScanRow(
        symbol=symbol,
        instrument_id=f"id-{symbol}",
        instrument_type=instrument_type,
        name=symbol,
        price=100.0,
        last=100.0,
        market_cap=market_cap,
        volume=10e6,
        relative_volume=1.0,
        rsi=65.0,
        pct_change=0.5,
    )


def _make_tradability(symbol: str, *, tradeable: bool = True) -> Tradability:
    return Tradability(
        symbol=symbol,
        tradeable=tradeable,
        state="active",
        fractional=tradeable,
        short_selling=False,
    )


def _make_fundamentals(
    symbol: str,
    *,
    market_cap: float = 100e9,
    average_volume: float = 10e6,
) -> Fundamentals:
    return Fundamentals(
        symbol=symbol,
        market_cap=market_cap,
        average_volume=average_volume,
        avg_volume_30d=average_volume,
        high_52_weeks=200.0,
        sector="Technology",
        industry="Software",
    )


def _make_earnings(
    symbol: str,
    *,
    report_date: dt.date,
    reported: bool = False,
) -> EarningsEvent:
    return EarningsEvent(
        symbol=symbol,
        report_date=report_date,
        timing="am",
        verified=True,
        reported=reported,
    )


def _base_market_data(
    *,
    extra_scan_rows: list[ScanRow] | None = None,
    extra_tradability: dict[str, Tradability] | None = None,
    extra_fundamentals: dict[str, Fundamentals] | None = None,
    extra_historicals: dict[str, pd.DataFrame] | None = None,
    earnings: dict[str, EarningsEvent] | None = None,
) -> StaticMarketData:
    """Build a base StaticMarketData with SPY, XLK (entry=True) and XLE, AGG (entry=False)."""
    core_syms = ["SPY", "XLK", "XLE", "AGG"]

    scan_rows = [_make_scan_row(s) for s in core_syms]
    if extra_scan_rows:
        scan_rows = scan_rows + extra_scan_rows

    historicals = {s: _load_bars(s) for s in core_syms}
    if extra_historicals:
        historicals.update(extra_historicals)

    tradability = {s: _make_tradability(s) for s in core_syms}
    if extra_tradability:
        tradability.update(extra_tradability)

    fundamentals = {s: _make_fundamentals(s) for s in core_syms}
    if extra_fundamentals:
        fundamentals.update(extra_fundamentals)

    return StaticMarketData(
        scan_rows=scan_rows,
        historicals=historicals,
        quotes={},
        tradability=tradability,
        fundamentals=fundamentals,
        earnings=earnings or {},
    )


# ── Test: ranking ──────────────────────────────────────────────────────────────

class TestRanking:
    """Entry-eligible names are ranked by mom_252 descending."""

    def test_selected_order_is_mom_252_descending(self):
        """SPY mom_252~0.257 and XLK mom_252~0.559 — XLK must come first."""
        md = _base_market_data()
        result = build_universe(md, signal_date=_SIGNAL_DATE)

        # Both SPY and XLK are entry-eligible with real bars
        selected_syms = [c.symbol for c in result.selected]
        assert "XLK" in selected_syms
        assert "SPY" in selected_syms
        # XLK has higher mom_252 → must appear before SPY
        assert selected_syms.index("XLK") < selected_syms.index("SPY")

    def test_selected_excludes_non_entry_names(self):
        """XLE and AGG (entry=False from real bars) must NOT be in selected."""
        md = _base_market_data()
        result = build_universe(md, signal_date=_SIGNAL_DATE)

        selected_syms = [c.symbol for c in result.selected]
        assert "XLE" not in selected_syms
        assert "AGG" not in selected_syms

    def test_selected_subset_of_candidates(self):
        """selected is a subset of candidates, not a new set."""
        md = _base_market_data()
        result = build_universe(md, signal_date=_SIGNAL_DATE)

        candidate_syms = {c.symbol for c in result.candidates}
        for c in result.selected:
            assert c.symbol in candidate_syms

    def test_top_n_is_respected(self):
        """top_n=1 caps selected at one name."""
        md = _base_market_data()
        result = build_universe(md, signal_date=_SIGNAL_DATE, top_n=1)

        assert len(result.selected) == 1
        # Should be XLK (highest mom_252)
        assert result.selected[0].symbol == "XLK"

    def test_tie_breaking_is_alphabetical(self):
        """When two names share the same rank_key value, ties break by symbol asc."""
        spy_bars = _load_bars("SPY")
        xlk_bars = _load_bars("XLK")

        # Build two synthetic entries with identical bars (identical mom_252)
        # Use SPY bars for both, giving them the same decision values
        md = StaticMarketData(
            scan_rows=[_make_scan_row("AAAA"), _make_scan_row("ZZZZ")],
            historicals={"AAAA": spy_bars.copy(), "ZZZZ": spy_bars.copy()},
            quotes={},
            tradability={
                "AAAA": _make_tradability("AAAA"),
                "ZZZZ": _make_tradability("ZZZZ"),
            },
            fundamentals={
                "AAAA": _make_fundamentals("AAAA"),
                "ZZZZ": _make_fundamentals("ZZZZ"),
            },
            earnings={},
        )
        result = build_universe(md, signal_date=_SIGNAL_DATE)

        # If both are entry-eligible and have identical mom_252, AAAA < ZZZZ alphabetically
        # so AAAA should appear first.
        selected_syms = [c.symbol for c in result.selected]
        if len(selected_syms) == 2:
            assert selected_syms[0] == "AAAA"
            assert selected_syms[1] == "ZZZZ"


# ── Test: non-tradeable symbol ─────────────────────────────────────────────────

class TestTradeabilityGate:
    """Non-tradeable symbols are dropped with appropriate drop_reason."""

    def test_non_tradeable_symbol_has_drop_reason(self):
        """A symbol with tradeable=False is in candidates but NOT in selected."""
        extra_scan = [_make_scan_row("FAKE")]
        extra_trad = {"FAKE": _make_tradability("FAKE", tradeable=False)}
        extra_fund = {"FAKE": _make_fundamentals("FAKE")}
        extra_hist = {"FAKE": _load_bars("SPY")}  # plenty of bars, but not tradeable

        md = _base_market_data(
            extra_scan_rows=extra_scan,
            extra_tradability=extra_trad,
            extra_fundamentals=extra_fund,
            extra_historicals=extra_hist,
        )
        result = build_universe(md, signal_date=_SIGNAL_DATE)

        # FAKE must appear in candidates
        fake_cands = [c for c in result.candidates if c.symbol == "FAKE"]
        assert len(fake_cands) == 1
        fake = fake_cands[0]

        # Must have a drop_reason that mentions tradability
        assert fake.drop_reason is not None
        assert "tradeable" in fake.drop_reason or "tradab" in fake.drop_reason

        # Must NOT appear in selected
        assert not any(c.symbol == "FAKE" for c in result.selected)

    def test_non_equity_symbol_is_dropped(self):
        """instrument_type != 'EQUITY' → drop_reason='not_equity'."""
        non_eq_scan = [_make_scan_row("ETF_LIKE", instrument_type="ETF")]
        # Tradability present but instrument_type check happens first
        extra_trad = {"ETF_LIKE": _make_tradability("ETF_LIKE")}
        extra_fund = {"ETF_LIKE": _make_fundamentals("ETF_LIKE")}
        extra_hist = {"ETF_LIKE": _load_bars("SPY")}

        md = _base_market_data(
            extra_scan_rows=non_eq_scan,
            extra_tradability=extra_trad,
            extra_fundamentals=extra_fund,
            extra_historicals=extra_hist,
        )
        result = build_universe(md, signal_date=_SIGNAL_DATE)

        cand = next(c for c in result.candidates if c.symbol == "ETF_LIKE")
        assert cand.drop_reason == "not_equity"
        assert not any(c.symbol == "ETF_LIKE" for c in result.selected)


# ── Test: earnings blackout ────────────────────────────────────────────────────

class TestEarningsBlackout:
    """Upcoming earnings within blackout_days → excluded from selected."""

    def test_earnings_within_blackout_excludes_from_selected(self):
        """SPY with an earnings event 3 days out → earnings_blackout=True, excluded."""
        # signal_date = 2026-06-16; report 3 days later = 2026-06-19 (< 5-day window)
        report_date = _SIGNAL_DATE + dt.timedelta(days=3)
        earnings = {"SPY": _make_earnings("SPY", report_date=report_date)}

        md = _base_market_data(earnings=earnings)
        result = build_universe(md, signal_date=_SIGNAL_DATE, blackout_days=5)

        spy_cand = next(c for c in result.candidates if c.symbol == "SPY")
        assert spy_cand.earnings_blackout is True
        assert spy_cand.drop_reason == "earnings_blackout"
        assert not any(c.symbol == "SPY" for c in result.selected)

    def test_earnings_on_signal_date_is_blackout(self):
        """Earnings exactly on signal_date → blackout (0 days ahead, inside window)."""
        earnings = {"XLK": _make_earnings("XLK", report_date=_SIGNAL_DATE)}

        md = _base_market_data(earnings=earnings)
        result = build_universe(md, signal_date=_SIGNAL_DATE, blackout_days=5)

        cand = next(c for c in result.candidates if c.symbol == "XLK")
        assert cand.earnings_blackout is True
        assert not any(c.symbol == "XLK" for c in result.selected)

    def test_earnings_on_last_blackout_day_is_excluded(self):
        """Earnings exactly on signal_date + blackout_days → still blackout (inclusive)."""
        report_date = _SIGNAL_DATE + dt.timedelta(days=5)  # exactly at boundary
        earnings = {"XLK": _make_earnings("XLK", report_date=report_date)}

        md = _base_market_data(earnings=earnings)
        result = build_universe(md, signal_date=_SIGNAL_DATE, blackout_days=5)

        cand = next(c for c in result.candidates if c.symbol == "XLK")
        assert cand.earnings_blackout is True
        assert not any(c.symbol == "XLK" for c in result.selected)

    def test_earnings_just_outside_blackout_is_not_excluded(self):
        """Earnings 6 days out with blackout_days=5 → NOT a blackout."""
        report_date = _SIGNAL_DATE + dt.timedelta(days=6)  # 1 day beyond window
        earnings = {"XLK": _make_earnings("XLK", report_date=report_date)}

        md = _base_market_data(earnings=earnings)
        result = build_universe(md, signal_date=_SIGNAL_DATE, blackout_days=5)

        cand = next(c for c in result.candidates if c.symbol == "XLK")
        assert cand.earnings_blackout is False
        # XLK should still be in selected (entry=True from real bars)
        assert any(c.symbol == "XLK" for c in result.selected)

    def test_reported_earnings_is_not_a_blackout(self):
        """An earnings event where reported=True must NOT trigger a blackout."""
        report_date = _SIGNAL_DATE + dt.timedelta(days=1)
        earnings = {"XLK": _make_earnings("XLK", report_date=report_date, reported=True)}

        md = _base_market_data(earnings=earnings)
        result = build_universe(md, signal_date=_SIGNAL_DATE, blackout_days=5)

        cand = next(c for c in result.candidates if c.symbol == "XLK")
        assert cand.earnings_blackout is False
        assert any(c.symbol == "XLK" for c in result.selected)


# ── Test: insufficient history ────────────────────────────────────────────────

class TestInsufficientHistory:
    """Symbols with fewer than 253 bars are dropped."""

    def test_short_bars_sets_drop_reason(self):
        """A symbol with only 100 bars → drop_reason='insufficient_history'."""
        spy_bars = _load_bars("SPY")
        short_bars = spy_bars.tail(100).reset_index(drop=True)

        extra_scan = [_make_scan_row("SHORT")]
        extra_trad = {"SHORT": _make_tradability("SHORT")}
        extra_fund = {"SHORT": _make_fundamentals("SHORT")}
        extra_hist = {"SHORT": short_bars}

        md = _base_market_data(
            extra_scan_rows=extra_scan,
            extra_tradability=extra_trad,
            extra_fundamentals=extra_fund,
            extra_historicals=extra_hist,
        )
        result = build_universe(md, signal_date=_SIGNAL_DATE)

        cand = next(c for c in result.candidates if c.symbol == "SHORT")
        assert cand.drop_reason == "insufficient_history"
        assert not any(c.symbol == "SHORT" for c in result.selected)

    def test_exactly_252_bars_is_insufficient(self):
        """252 bars = one short of minimum 253; must be dropped."""
        spy_bars = _load_bars("SPY")
        bars_252 = spy_bars.tail(252).reset_index(drop=True)

        extra_scan = [_make_scan_row("BARELY")]
        extra_trad = {"BARELY": _make_tradability("BARELY")}
        extra_fund = {"BARELY": _make_fundamentals("BARELY")}
        extra_hist = {"BARELY": bars_252}

        md = _base_market_data(
            extra_scan_rows=extra_scan,
            extra_tradability=extra_trad,
            extra_fundamentals=extra_fund,
            extra_historicals=extra_hist,
        )
        result = build_universe(md, signal_date=_SIGNAL_DATE)

        cand = next(c for c in result.candidates if c.symbol == "BARELY")
        assert cand.drop_reason == "insufficient_history"


# ── Test: cost tier assignment ────────────────────────────────────────────────

class TestCostTierAssignment:
    """Cost tier is derived from fundamentals and pinned on the Candidate."""

    def test_mega_cap_fundamentals_assigns_tier_mega_cap(self):
        """market_cap=100e9 + average_volume=10e6 → TIER_MEGA_CAP for XLK."""
        extra_fund = {"XLK": _make_fundamentals("XLK", market_cap=100e9, average_volume=10e6)}
        md = _base_market_data(extra_fundamentals=extra_fund)
        result = build_universe(md, signal_date=_SIGNAL_DATE)

        xlk = next(c for c in result.candidates if c.symbol == "XLK")
        assert xlk.cost_tier == TIER_MEGA_CAP
        assert xlk.cost_tier.roundtrip_bps == 20.0

    def test_small_cap_fundamentals_assigns_tier_other(self):
        """market_cap=1e9 (below $50B threshold) → TIER_OTHER."""
        extra_fund = {"XLK": _make_fundamentals("XLK", market_cap=1e9, average_volume=10e6)}
        md = _base_market_data(extra_fundamentals=extra_fund)
        result = build_universe(md, signal_date=_SIGNAL_DATE)

        xlk = next(c for c in result.candidates if c.symbol == "XLK")
        assert xlk.cost_tier == TIER_OTHER
        assert xlk.cost_tier.roundtrip_bps == 50.0

    def test_missing_fundamentals_assigns_tier_other(self):
        """If no fundamentals entry for a symbol, cost_tier must be TIER_OTHER."""
        # Build a market_data with no fundamentals for SPY
        spy_bars = _load_bars("SPY")
        md = StaticMarketData(
            scan_rows=[_make_scan_row("SPY")],
            historicals={"SPY": spy_bars},
            quotes={},
            tradability={"SPY": _make_tradability("SPY")},
            fundamentals={},  # deliberately empty
            earnings={},
        )
        result = build_universe(md, signal_date=_SIGNAL_DATE)

        spy = next(c for c in result.candidates if c.symbol == "SPY")
        assert spy.cost_tier == TIER_OTHER

    def test_cost_tier_pinned_in_selected(self):
        """The cost tier on a selected Candidate matches what cost_tier_for would return."""
        extra_fund = {"XLK": _make_fundamentals("XLK", market_cap=200e9, average_volume=20e6)}
        md = _base_market_data(extra_fundamentals=extra_fund)
        result = build_universe(md, signal_date=_SIGNAL_DATE)

        xlk_sel = next((c for c in result.selected if c.symbol == "XLK"), None)
        assert xlk_sel is not None
        assert xlk_sel.cost_tier == TIER_MEGA_CAP


# ── Test: determinism ─────────────────────────────────────────────────────────

class TestDeterminism:
    """Two identical calls produce byte-identical results."""

    def test_two_runs_are_identical(self):
        """Running build_universe twice with the same inputs must return the same result."""
        md = _base_market_data()

        r1 = build_universe(md, signal_date=_SIGNAL_DATE)
        r2 = build_universe(md, signal_date=_SIGNAL_DATE)

        assert r1.signal_date == r2.signal_date
        assert r1.params == r2.params
        assert len(r1.candidates) == len(r2.candidates)
        assert len(r1.selected) == len(r2.selected)

        for c1, c2 in zip(r1.candidates, r2.candidates):
            assert c1 == c2, f"Candidate mismatch for {c1.symbol}"

        for s1, s2 in zip(r1.selected, r2.selected):
            assert s1 == s2, f"Selected mismatch for {s1.symbol}"

    def test_scan_order_is_preserved_in_candidates(self):
        """candidates must follow the scan order (not re-sorted)."""
        md = _base_market_data()
        result = build_universe(md, signal_date=_SIGNAL_DATE)

        scan_order = [r.symbol for r in md.scan()]
        cand_order = [c.symbol for c in result.candidates]
        assert cand_order == scan_order


# ── Test: error robustness ────────────────────────────────────────────────────

class TestErrorRobustness:
    """One bad name does not crash the build; it gets a drop_reason starting 'error:'."""

    def test_decide_raises_becomes_error_candidate(self):
        """A symbol whose historicals make decide() raise → drop_reason starts 'error:'."""
        # Create a DataFrame with valid shape but invalid content to make decide() raise.
        # We'll give it a column that triggers a ValueError in decide():
        # Pass an empty DataFrame (0 rows) which raises in decide().
        # But build_universe already guards for len < 253, so we need it to pass
        # the bar-count guard but fail inside decide().
        # Easiest: provide bars with wrong column names so decide() raises ValueError.
        bad_bars = pd.DataFrame({
            "date": pd.date_range("2020-01-01", periods=300).date,
            # Missing "high", "low", "close" — decide() raises ValueError
        })

        extra_scan = [_make_scan_row("BADNAME")]
        extra_trad = {"BADNAME": _make_tradability("BADNAME")}
        extra_fund = {"BADNAME": _make_fundamentals("BADNAME")}
        extra_hist = {"BADNAME": bad_bars}

        md = _base_market_data(
            extra_scan_rows=extra_scan,
            extra_tradability=extra_trad,
            extra_fundamentals=extra_fund,
            extra_historicals=extra_hist,
        )

        # Must not raise
        result = build_universe(md, signal_date=_SIGNAL_DATE)

        bad_cand = next(c for c in result.candidates if c.symbol == "BADNAME")
        assert bad_cand.drop_reason is not None
        assert bad_cand.drop_reason.startswith("error:")
        assert not any(c.symbol == "BADNAME" for c in result.selected)

        # Other names must still be processed correctly
        assert any(c.symbol == "XLK" for c in result.selected)

    def test_no_historicals_key_becomes_no_historicals(self):
        """A symbol not in the historicals dict → drop_reason='no_historicals'."""
        extra_scan = [_make_scan_row("NOHIST")]
        extra_trad = {"NOHIST": _make_tradability("NOHIST")}
        extra_fund = {"NOHIST": _make_fundamentals("NOHIST")}
        # Deliberately no entry in extra_historicals

        md = _base_market_data(
            extra_scan_rows=extra_scan,
            extra_tradability=extra_trad,
            extra_fundamentals=extra_fund,
        )
        result = build_universe(md, signal_date=_SIGNAL_DATE)

        cand = next(c for c in result.candidates if c.symbol == "NOHIST")
        assert cand.drop_reason == "no_historicals"
        assert not any(c.symbol == "NOHIST" for c in result.selected)

    def test_all_other_names_succeed_when_one_errors(self):
        """Even with a bad name injected, SPY and XLK are still processed."""
        bad_bars = pd.DataFrame({"date": pd.date_range("2020-01-01", periods=300).date})

        extra_scan = [_make_scan_row("OOPS")]
        extra_trad = {"OOPS": _make_tradability("OOPS")}
        extra_fund = {"OOPS": _make_fundamentals("OOPS")}
        extra_hist = {"OOPS": bad_bars}

        md = _base_market_data(
            extra_scan_rows=extra_scan,
            extra_tradability=extra_trad,
            extra_fundamentals=extra_fund,
            extra_historicals=extra_hist,
        )
        result = build_universe(md, signal_date=_SIGNAL_DATE)

        # SPY and XLK must still appear and XLK must be in selected
        assert any(c.symbol == "SPY" for c in result.candidates)
        assert any(c.symbol == "XLK" for c in result.candidates)
        assert any(c.symbol == "XLK" for c in result.selected)


# ── Test: result structure ────────────────────────────────────────────────────

class TestResultStructure:
    """UniverseResult has the expected shape and param pinning."""

    def test_params_are_pinned(self):
        """The params dict on the result reflects the call arguments."""
        md = _base_market_data()
        result = build_universe(
            md,
            signal_date=_SIGNAL_DATE,
            near_threshold=0.85,
            blackout_days=3,
            top_n=5,
            rank_key="mom_252",
        )
        assert result.params["near_threshold"] == 0.85
        assert result.params["blackout_days"] == 3
        assert result.params["top_n"] == 5
        assert result.params["rank_key"] == "mom_252"

    def test_signal_date_is_recorded(self):
        md = _base_market_data()
        result = build_universe(md, signal_date=_SIGNAL_DATE)
        assert result.signal_date == _SIGNAL_DATE

    def test_all_scan_rows_appear_in_candidates(self):
        """Every symbol from scan() must appear exactly once in candidates."""
        md = _base_market_data()
        scan_syms = [r.symbol for r in md.scan()]
        result = build_universe(md, signal_date=_SIGNAL_DATE)
        cand_syms = [c.symbol for c in result.candidates]
        assert sorted(cand_syms) == sorted(scan_syms)

    def test_selected_are_frozen_dataclasses(self):
        """Candidates in selected must be frozen (no mutation)."""
        md = _base_market_data()
        result = build_universe(md, signal_date=_SIGNAL_DATE)
        for c in result.selected:
            with pytest.raises((AttributeError, TypeError)):
                c.symbol = "mutated"  # type: ignore[misc]

    def test_entry_eligible_have_no_drop_reason(self):
        """All candidates in selected must have drop_reason=None."""
        md = _base_market_data()
        result = build_universe(md, signal_date=_SIGNAL_DATE)
        for c in result.selected:
            assert c.drop_reason is None, (
                f"{c.symbol} is in selected but has drop_reason={c.drop_reason!r}"
            )

    def test_entry_eligible_have_decision_entry_true(self):
        """All candidates in selected must have decision.entry=True."""
        md = _base_market_data()
        result = build_universe(md, signal_date=_SIGNAL_DATE)
        for c in result.selected:
            assert c.decision is not None
            assert c.decision.entry is True, (
                f"{c.symbol} is in selected but decision.entry={c.decision.entry}"
            )

    def test_entry_eligible_are_tradeable(self):
        """All candidates in selected must have tradeable=True."""
        md = _base_market_data()
        result = build_universe(md, signal_date=_SIGNAL_DATE)
        for c in result.selected:
            assert c.tradeable is True

    def test_entry_eligible_not_in_blackout(self):
        """No candidate in selected should have earnings_blackout=True."""
        md = _base_market_data()
        result = build_universe(md, signal_date=_SIGNAL_DATE)
        for c in result.selected:
            assert c.earnings_blackout is False
