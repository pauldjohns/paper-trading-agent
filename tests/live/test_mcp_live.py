# tests/live/test_mcp_live.py
"""TDD tests for mcp_live normalizers + MarketData protocol + StaticMarketData.

All tests are OFFLINE — they load captured JSON fixtures from
tests/live/fixtures/mcp_samples/ and assert normalizers produce correct typed output.
No network, no MCP calls.
"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
from pathlib import Path

import pandas as pd
import pytest

# Under test
from autotrader_live.mcp_live import (
    EarningsEvent,
    Fundamentals,
    MarketData,
    Quote,
    ScanRow,
    StaticMarketData,
    Tradability,
    normalize_bars,
    normalize_earnings,
    normalize_fundamentals,
    normalize_quotes,
    normalize_scan,
    normalize_tradability,
)

# ── fixture paths ──────────────────────────────────────────────────────────────
_FIXTURE_DIR = Path(__file__).parent / "fixtures" / "mcp_samples"


def _load(name: str) -> dict:
    return json.loads((_FIXTURE_DIR / name).read_text())


# =============================================================================
# normalize_bars
# =============================================================================


class TestNormalizeBars:
    def _raw(self):
        return _load("historicals_AMD.json")

    def test_returns_dataframe(self):
        df = normalize_bars(self._raw())
        assert isinstance(df, pd.DataFrame)

    def test_columns_exact(self):
        df = normalize_bars(self._raw())
        assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]

    def test_row_count_drops_interpolated(self):
        """10 raw bars − 1 interpolated → 9 settled bars."""
        df = normalize_bars(self._raw())
        assert len(df) == 9

    def test_last_settled_date(self):
        """Last settled bar is 2026-06-18 (Juneteenth absent, 2026-06-22 dropped)."""
        df = normalize_bars(self._raw())
        assert df.iloc[-1]["date"] == dt.date(2026, 6, 18)

    def test_date_type(self):
        df = normalize_bars(self._raw())
        assert all(isinstance(d, dt.date) and not isinstance(d, dt.datetime) for d in df["date"])

    def test_ohlc_float(self):
        df = normalize_bars(self._raw())
        for col in ("open", "high", "low", "close"):
            assert df[col].dtype == float, f"{col} should be float"

    def test_volume_int(self):
        df = normalize_bars(self._raw())
        assert df["volume"].dtype in (int, "int64", "int32")

    def test_first_bar_values(self):
        """Spot-check first bar: 2026-06-08, open 485.0."""
        df = normalize_bars(self._raw())
        row = df.iloc[0]
        assert row["date"] == dt.date(2026, 6, 8)
        assert row["open"] == pytest.approx(485.0)
        assert row["close"] == pytest.approx(490.33)

    def test_sorted_ascending(self):
        df = normalize_bars(self._raw())
        dates = df["date"].tolist()
        assert dates == sorted(dates)

    def test_no_duplicate_dates(self):
        df = normalize_bars(self._raw())
        assert len(df["date"].unique()) == len(df)

    def test_strictly_increasing(self):
        df = normalize_bars(self._raw())
        dates = df["date"].tolist()
        for a, b in zip(dates, dates[1:]):
            assert b > a

    def test_datastore_roundtrip(self):
        """DataFrame must be writable to autotrader.datastore.DataStore."""
        from autotrader.datastore import DataStore

        df = normalize_bars(self._raw())
        with tempfile.TemporaryDirectory() as tmpdir:
            store = DataStore(tmpdir)
            # Should not raise
            store.write("AMD", "day", "split", df)
            loaded = store.load("AMD", "day", "split")
        assert len(loaded) == 9
        assert list(loaded.columns) == ["date", "open", "high", "low", "close", "volume"]
        assert loaded.iloc[-1]["date"] == dt.date(2026, 6, 18)

    def test_empty_after_drop_raises(self):
        """If ALL bars are interpolated, normalize_bars must raise ValueError."""
        raw = _load("historicals_AMD.json")
        # Force all bars to be interpolated
        for bar in raw["data"]["results"][0]["bars"]:
            bar["interpolated"] = True
        with pytest.raises(ValueError, match="(?i)empty|no settled|interpolated"):
            normalize_bars(raw)

    def test_interpolated_bar_volume_zero_check(self):
        """The interpolated 2026-06-22 bar (volume=0, OHLC=prior close) is absent."""
        df = normalize_bars(self._raw())
        dates = df["date"].tolist()
        assert dt.date(2026, 6, 22) not in dates

    def test_float_string_volume_tolerated(self):
        """normalize_bars must accept float-string volume like '25158813.000000'."""
        raw = _load("historicals_AMD.json")
        # Replace all volume values with float-string representation
        for bar in raw["data"]["results"][0]["bars"]:
            bar["volume"] = f"{int(bar['volume'])}.000000"
        df = normalize_bars(raw)
        # Volume must still be int dtype
        assert df["volume"].dtype in (int, "int64", "int32")
        # And the values must be positive integers
        assert (df["volume"] >= 0).all()


# =============================================================================
# normalize_scan
# =============================================================================


class TestNormalizeScan:
    def _raw(self):
        return _load("scan_sample.json")

    def test_returns_list_of_scan_rows(self):
        rows = normalize_scan(self._raw())
        assert isinstance(rows, list)
        assert all(isinstance(r, ScanRow) for r in rows)

    def test_row_count(self):
        rows = normalize_scan(self._raw())
        assert len(rows) == 4

    def test_symbols_present(self):
        rows = normalize_scan(self._raw())
        symbols = {r.symbol for r in rows}
        assert symbols == {"TSM", "MU", "AMD", "JPM"}

    def test_tsm_price_float(self):
        rows = normalize_scan(self._raw())
        tsm = next(r for r in rows if r.symbol == "TSM")
        assert tsm.price == pytest.approx(467.67)

    def test_tsm_market_cap_float(self):
        rows = normalize_scan(self._raw())
        tsm = next(r for r in rows if r.symbol == "TSM")
        assert tsm.market_cap == pytest.approx(1.973481826093e12)

    def test_amd_rsi_float(self):
        rows = normalize_scan(self._raw())
        amd = next(r for r in rows if r.symbol == "AMD")
        assert amd.rsi == pytest.approx(63.08049487787472)

    def test_jpm_pct_change_float(self):
        rows = normalize_scan(self._raw())
        jpm = next(r for r in rows if r.symbol == "JPM")
        assert jpm.pct_change == pytest.approx(-0.001417883431881342)

    def test_instrument_id_present(self):
        rows = normalize_scan(self._raw())
        tsm = next(r for r in rows if r.symbol == "TSM")
        assert tsm.instrument_id == "ca4821f9-06c3-4c22-bbb8-efe569f23d2b"

    def test_instrument_type(self):
        rows = normalize_scan(self._raw())
        assert all(r.instrument_type == "EQUITY" for r in rows)

    def test_name_str(self):
        rows = normalize_scan(self._raw())
        tsm = next(r for r in rows if r.symbol == "TSM")
        assert tsm.name == "Taiwan Semiconductor Manufacturing"

    def test_handles_data_result_results_shape(self):
        """Also handle run_scan response shape: {data: {result: {results: [...]}}}."""
        raw = _load("scan_sample.json")
        wrapped = {"data": {"result": {"results": raw["results"]}}}
        rows = normalize_scan(wrapped)
        assert len(rows) == 4

    def test_frozen_dataclass(self):
        rows = normalize_scan(self._raw())
        with pytest.raises((AttributeError, TypeError)):
            rows[0].symbol = "X"  # type: ignore[misc]

    def test_string_to_float_error(self):
        """Non-numeric string in a column must raise ValueError, not produce NaN."""
        raw = _load("scan_sample.json")
        raw["results"][0]["columns"]["Close"] = "NOT_A_NUMBER"
        with pytest.raises((ValueError, TypeError)):
            normalize_scan(raw)


# =============================================================================
# normalize_quotes
# =============================================================================


class TestNormalizeQuotes:
    def _raw(self):
        return _load("quotes_AMD.json")

    def test_returns_dict(self):
        result = normalize_quotes(self._raw())
        assert isinstance(result, dict)

    def test_amd_key_present(self):
        result = normalize_quotes(self._raw())
        assert "AMD" in result

    def test_quote_type(self):
        result = normalize_quotes(self._raw())
        assert isinstance(result["AMD"], Quote)

    def test_settled_close(self):
        q = normalize_quotes(self._raw())["AMD"]
        assert q.settled_close == pytest.approx(551.63)

    def test_settled_close_date(self):
        q = normalize_quotes(self._raw())["AMD"]
        assert q.settled_close_date == dt.date(2026, 6, 22)

    def test_settled_close_not_interpolated(self):
        q = normalize_quotes(self._raw())["AMD"]
        assert q.settled_close_interpolated is False

    def test_settled_close_source(self):
        q = normalize_quotes(self._raw())["AMD"]
        assert q.settled_close_source == "sip-list-exchange-close"

    def test_bid_ask_present(self):
        q = normalize_quotes(self._raw())["AMD"]
        assert q.bid == pytest.approx(542.31)
        assert q.ask == pytest.approx(543.40)

    def test_spread(self):
        q = normalize_quotes(self._raw())["AMD"]
        assert q.spread == pytest.approx(543.40 - 542.31)

    def test_last_trade_price(self):
        q = normalize_quotes(self._raw())["AMD"]
        assert q.last_trade_price == pytest.approx(551.67)

    def test_previous_close(self):
        q = normalize_quotes(self._raw())["AMD"]
        assert q.previous_close == pytest.approx(551.63)

    def test_has_traded(self):
        q = normalize_quotes(self._raw())["AMD"]
        assert q.has_traded is True

    def test_state(self):
        q = normalize_quotes(self._raw())["AMD"]
        assert q.state == "active"

    def test_spread_none_when_no_bid(self):
        """Bid of 0 → bid=None, spread=None."""
        raw = _load("quotes_AMD.json")
        raw["data"]["results"][0]["quote"]["bid_price"] = "0.000000"
        raw["data"]["results"][0]["quote"]["ask_price"] = "543.400000"
        q = normalize_quotes(raw)["AMD"]
        assert q.bid is None
        assert q.spread is None

    def test_frozen_dataclass(self):
        result = normalize_quotes(self._raw())
        with pytest.raises((AttributeError, TypeError)):
            result["AMD"].symbol = "X"  # type: ignore[misc]


# =============================================================================
# normalize_tradability
# =============================================================================


class TestNormalizeTradability:
    def _raw(self):
        return _load("tradability_AMD.json")

    def test_returns_dict(self):
        result = normalize_tradability(self._raw())
        assert isinstance(result, dict)

    def test_amd_key(self):
        result = normalize_tradability(self._raw())
        assert "AMD" in result

    def test_tradability_type(self):
        result = normalize_tradability(self._raw())
        assert isinstance(result["AMD"], Tradability)

    def test_amd_tradeable_true(self):
        """AMD passes all four clauses → tradeable True."""
        result = normalize_tradability(self._raw())
        assert result["AMD"].tradeable is True

    def test_raw_state(self):
        result = normalize_tradability(self._raw())
        assert result["AMD"].state == "active"

    def test_fractional(self):
        result = normalize_tradability(self._raw())
        assert result["AMD"].fractional is True

    def test_short_selling(self):
        result = normalize_tradability(self._raw())
        assert result["AMD"].short_selling is True

    def test_gate_fails_non_active_state(self):
        raw = _load("tradability_AMD.json")
        raw["data"]["results"][0]["state"] = "inactive"
        result = normalize_tradability(raw)
        assert result["AMD"].tradeable is False

    def test_gate_fails_non_tradable_fractional(self):
        raw = _load("tradability_AMD.json")
        raw["data"]["results"][0]["fractional_tradability"] = "untradable"
        result = normalize_tradability(raw)
        assert result["AMD"].tradeable is False

    def test_gate_fails_tradeable_false(self):
        raw = _load("tradability_AMD.json")
        raw["data"]["results"][0]["tradeable"] = False
        result = normalize_tradability(raw)
        assert result["AMD"].tradeable is False

    def test_gate_fails_account_type_tradability(self):
        raw = _load("tradability_AMD.json")
        raw["data"]["results"][0]["account_type_tradabilities"][0][
            "account_type_tradability"
        ] = "untradable"
        result = normalize_tradability(raw)
        assert result["AMD"].tradeable is False

    def test_gate_fails_wrong_account_type(self):
        """If no entry matches the requested account_type → tradeable False."""
        raw = _load("tradability_AMD.json")
        raw["data"]["results"][0]["account_type_tradabilities"][0]["account_type"] = "ira"
        result = normalize_tradability(raw, account_type="individual")
        assert result["AMD"].tradeable is False

    def test_frozen_dataclass(self):
        result = normalize_tradability(self._raw())
        with pytest.raises((AttributeError, TypeError)):
            result["AMD"].symbol = "X"  # type: ignore[misc]


# =============================================================================
# normalize_fundamentals
# =============================================================================


class TestNormalizeFundamentals:
    def _raw(self):
        return _load("fundamentals_AMD.json")

    def test_returns_dict(self):
        result = normalize_fundamentals(self._raw())
        assert isinstance(result, dict)

    def test_amd_key(self):
        result = normalize_fundamentals(self._raw())
        assert "AMD" in result

    def test_fundamentals_type(self):
        result = normalize_fundamentals(self._raw())
        assert isinstance(result["AMD"], Fundamentals)

    def test_market_cap(self):
        f = normalize_fundamentals(self._raw())["AMD"]
        assert f.market_cap == pytest.approx(8.99553417070022e11, rel=1e-4)

    def test_average_volume(self):
        f = normalize_fundamentals(self._raw())["AMD"]
        assert f.average_volume == pytest.approx(3.1158592147014e7, rel=1e-4)

    def test_avg_volume_30d(self):
        f = normalize_fundamentals(self._raw())["AMD"]
        assert f.avg_volume_30d == pytest.approx(3.16910079008e7, rel=1e-4)

    def test_high_52_weeks(self):
        f = normalize_fundamentals(self._raw())["AMD"]
        assert f.high_52_weeks == pytest.approx(562.9899, rel=1e-4)

    def test_sector(self):
        f = normalize_fundamentals(self._raw())["AMD"]
        assert f.sector == "Electronic Technology"

    def test_industry(self):
        f = normalize_fundamentals(self._raw())["AMD"]
        assert f.industry == "Semiconductors"

    def test_frozen_dataclass(self):
        result = normalize_fundamentals(self._raw())
        with pytest.raises((AttributeError, TypeError)):
            result["AMD"].symbol = "X"  # type: ignore[misc]


# =============================================================================
# normalize_earnings
# =============================================================================


class TestNormalizeEarnings:
    def _raw(self):
        return _load("earnings_calendar.json")

    def test_returns_dict(self):
        result = normalize_earnings(self._raw())
        assert isinstance(result, dict)

    def test_mu_present(self):
        result = normalize_earnings(self._raw())
        assert "MU" in result

    def test_earnings_event_type(self):
        result = normalize_earnings(self._raw())
        assert isinstance(result["MU"], EarningsEvent)

    def test_mu_report_date(self):
        result = normalize_earnings(self._raw())
        assert result["MU"].report_date == dt.date(2026, 6, 24)

    def test_mu_timing(self):
        result = normalize_earnings(self._raw())
        assert result["MU"].timing == "pm"

    def test_mu_verified(self):
        result = normalize_earnings(self._raw())
        assert result["MU"].verified is True

    def test_mu_not_reported(self):
        """eps.actual is null → not yet reported."""
        result = normalize_earnings(self._raw())
        assert result["MU"].reported is False

    def test_masi_not_verified(self):
        result = normalize_earnings(self._raw())
        assert result["MASI"].verified is False

    def test_all_symbols_present(self):
        result = normalize_earnings(self._raw())
        expected = {"MASI", "FDX", "CCL", "KBH", "MU", "PAYX", "DRI", "WBA"}
        assert set(result.keys()) == expected

    def test_reported_true_when_actual_set(self):
        """If eps.actual is non-null, reported=True."""
        raw = _load("earnings_calendar.json")
        raw["data"]["results"][0]["eps"]["actual"] = "1.23"  # MASI
        result = normalize_earnings(raw)
        assert result["MASI"].reported is True

    def test_frozen_dataclass(self):
        result = normalize_earnings(self._raw())
        with pytest.raises((AttributeError, TypeError)):
            result["MU"].symbol = "X"  # type: ignore[misc]

    def test_duplicate_symbol_keeps_earliest_upcoming(self):
        """Multiple rows for same symbol → keep earliest report_date where reported=False."""
        raw = _load("earnings_calendar.json")
        # Add an extra MU row with a later date and actual=None
        extra = {
            "symbol": "MU",
            "year": 2026,
            "quarter": 4,
            "eps": {"estimate": "25.00", "actual": None},
            "report": {"date": "2027-01-01", "timing": "am", "verified": True},
        }
        raw["data"]["results"].append(extra)
        result = normalize_earnings(raw)
        assert result["MU"].report_date == dt.date(2026, 6, 24)


# =============================================================================
# MarketData protocol + StaticMarketData
# =============================================================================


class TestStaticMarketData:
    """StaticMarketData must satisfy the MarketData protocol."""

    def _make(self) -> StaticMarketData:
        raw_hist = _load("historicals_AMD.json")
        raw_scan = _load("scan_sample.json")
        raw_quotes = _load("quotes_AMD.json")
        raw_trad = _load("tradability_AMD.json")
        raw_fund = _load("fundamentals_AMD.json")
        raw_earn = _load("earnings_calendar.json")

        return StaticMarketData(
            scan_rows=normalize_scan(raw_scan),
            historicals={"AMD": normalize_bars(raw_hist)},
            quotes=normalize_quotes(raw_quotes),
            tradability=normalize_tradability(raw_trad),
            fundamentals=normalize_fundamentals(raw_fund),
            earnings=normalize_earnings(raw_earn),
        )

    def test_scan_returns_list(self):
        smd = self._make()
        rows = smd.scan()
        assert isinstance(rows, list)
        assert all(isinstance(r, ScanRow) for r in rows)

    def test_historicals_returns_dataframe(self):
        smd = self._make()
        df = smd.historicals("AMD")
        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]

    def test_quotes_returns_dict(self):
        smd = self._make()
        q = smd.quotes(["AMD"])
        assert isinstance(q, dict)
        assert "AMD" in q

    def test_tradability_returns_dict(self):
        smd = self._make()
        t = smd.tradability(["AMD"])
        assert isinstance(t, dict)
        assert "AMD" in t

    def test_fundamentals_returns_dict(self):
        smd = self._make()
        f = smd.fundamentals(["AMD"])
        assert isinstance(f, dict)
        assert "AMD" in f

    def test_earnings_returns_dict(self):
        smd = self._make()
        e = smd.earnings()
        assert isinstance(e, dict)
        assert "MU" in e

    def test_protocol_conformance(self):
        """StaticMarketData must satisfy the MarketData structural protocol."""
        from typing import runtime_checkable

        import typing

        # MarketData is a Protocol; verify StaticMarketData is an instance
        # via runtime_checkable (the module marks it @runtime_checkable)
        smd = self._make()
        assert isinstance(smd, MarketData)

    def test_missing_symbol_historicals_raises(self):
        smd = self._make()
        with pytest.raises(KeyError):
            smd.historicals("ZZZZZ")
