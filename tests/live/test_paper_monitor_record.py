"""Tests for T3b: record_day, record_skip, load_day_record, is_day_complete,
append_telemetry, and run_day in src/autotrader_live/paper_monitor.py.

Strategy
--------
All tests are OFFLINE — no MCP calls, no real broker.
- ``StaticMarketData`` from real cached ETF bars (SPY, XLK, XLE, AGG) for
  the full plan_day path, reusing the same fixtures as test_paper_monitor_plan.py.
- ``PaperBroker`` with a dict-lookup fake responder that returns a plausible
  raw review dict for each intent, keyed by ref_id.
- ``tmp_path`` (pytest fixture) for all file I/O — no writes to the repo.

Test groups
-----------
- TestDayRecord         : to_dict / from_dict round-trip; determinism.
- TestAtomicWrite       : no .tmp file after write; JSON valid + sorted.
- TestRecordDay         : happy path; review merge; orphan/missing reviews.
- TestRecordSkip        : status='skipped'; is_day_complete returns False.
- TestLoadDayRecord     : round-trip; missing file → None.
- TestIsDayComplete     : True for 'complete', False for 'skipped', False for absent.
- TestAppendTelemetry   : JSONL row counts; two days → two rows.
- TestRunDayIdempotency : second call returns existing record; re-plan NOT called.
- TestRunDayEndToEnd    : full run_day with PaperBroker produces complete record.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from autotrader.datastore import DataStore
from autotrader_live.broker import (
    OrderIntent,
    PaperBroker,
    ReviewResult,
)
from autotrader_live.mcp_live import (
    EarningsEvent,
    Fundamentals,
    ScanRow,
    StaticMarketData,
    Tradability,
)
from autotrader_live.paper_monitor import (
    DayPlan,
    DayRecord,
    Position,
    _atomic_write_json,
    append_telemetry,
    is_day_complete,
    load_day_record,
    plan_day,
    record_day,
    record_skip,
    run_day,
)

# ── Constants ─────────────────────────────────────────────────────────────────

_SIGNAL_DATE = dt.date(2026, 6, 16)
_ACCOUNT = "TEST-ACCOUNT-001"
# Use a non-zero equity for calls that go through plan_day (size() requires equity > 0).
# The DayRecord itself still supports equity=0.0 (paper-at-$0 phase); the plan_day
# path just needs a valid equity to size positions during the test run.
_EQUITY_PLAN = 100_000.0
_EQUITY_RECORD = 0.0   # what we store in the record (paper-at-$0 phase)
_RUN_TS = "2026-06-17T09:45:00Z"

# Resolve repo root from test file location
_REPO_ROOT = Path(__file__).resolve().parents[2]
_CACHE_DIR = str(_REPO_ROOT / "data" / "cache")


# ── Shared helpers ────────────────────────────────────────────────────────────


def _load_bars(symbol: str) -> pd.DataFrame:
    ds = DataStore(_CACHE_DIR)
    return ds.load(symbol, "day", "split")


def _make_scan_row(symbol: str) -> ScanRow:
    return ScanRow(
        symbol=symbol,
        instrument_id=f"id-{symbol}",
        instrument_type="EQUITY",
        name=symbol,
        price=100.0,
        last=100.0,
        market_cap=100e9,
        volume=10e6,
        relative_volume=1.0,
        rsi=65.0,
        pct_change=0.5,
    )


def _make_tradability(symbol: str) -> Tradability:
    return Tradability(
        symbol=symbol,
        tradeable=True,
        state="active",
        fractional=True,
        short_selling=False,
    )


def _make_fundamentals(symbol: str) -> Fundamentals:
    return Fundamentals(
        symbol=symbol,
        market_cap=100e9,
        average_volume=10e6,
        avg_volume_30d=10e6,
        high_52_weeks=200.0,
        sector="Technology",
        industry="Software",
    )


def _base_market_data() -> StaticMarketData:
    """StaticMarketData with SPY, XLK (entry=True) and XLE, AGG (entry=False)."""
    core_syms = ["SPY", "XLK", "XLE", "AGG"]
    return StaticMarketData(
        scan_rows=[_make_scan_row(s) for s in core_syms],
        historicals={s: _load_bars(s) for s in core_syms},
        quotes={},
        tradability={s: _make_tradability(s) for s in core_syms},
        fundamentals={s: _make_fundamentals(s) for s in core_syms},
        earnings={},
    )


def _fake_review_raw(intent: OrderIntent) -> dict:
    """Return a plausible raw MCP review-response dict for any intent."""
    return {
        "symbol": intent.symbol,
        "side": intent.side,
        "type": intent.order_type,
        "quantity": intent.quantity or "1.000000",
        "stop_price": intent.stop_price,
        "order_checks": {},
        "market_data_disclosure": "Market data is delayed.",
        "quote_data": {
            "last_trade_price": "150.00",
            "bid_price": "149.90",
            "ask_price": "150.10",
            "previous_close": "148.00",
            "previous_close_date": "2026-06-13",
            "has_traded": True,
            "state": "active",
        },
    }


def _make_paper_broker() -> PaperBroker:
    """PaperBroker backed by a fake dict-lookup responder."""
    return PaperBroker(responder=_fake_review_raw)


# ── Build a minimal DayRecord for unit tests ──────────────────────────────────


def _make_day_record(
    *,
    status: str = "complete",
    skip_reason: str | None = None,
    reviews: list[dict] | None = None,
    order_intents: list[dict] | None = None,
) -> DayRecord:
    return DayRecord(
        signal_date=_SIGNAL_DATE,
        status=status,
        skip_reason=skip_reason,
        run_timestamp=_RUN_TS,
        account_number=_ACCOUNT,
        equity=_EQUITY_RECORD,
        selected=[],
        order_intents=order_intents or [],
        reviews=reviews or [],
        held_halted=[],
        skipped=[],
        loss_halt={"triggered": False},
        reconciled=True,
        notes=[],
    )


# ── TestDayRecord ─────────────────────────────────────────────────────────────


class TestDayRecord:
    """DayRecord to_dict / from_dict round-trip and determinism."""

    def test_round_trip_empty(self):
        """A minimal DayRecord survives to_dict → from_dict unchanged."""
        rec = _make_day_record()
        restored = DayRecord.from_dict(rec.to_dict())
        assert restored.signal_date == rec.signal_date
        assert restored.status == rec.status
        assert restored.skip_reason == rec.skip_reason
        assert restored.run_timestamp == rec.run_timestamp
        assert restored.account_number == rec.account_number
        assert restored.equity == rec.equity
        assert restored.reconciled == rec.reconciled

    def test_round_trip_date_serialization(self):
        """signal_date survives as a date, not a string."""
        rec = _make_day_record()
        restored = DayRecord.from_dict(rec.to_dict())
        assert isinstance(restored.signal_date, dt.date)
        assert restored.signal_date == _SIGNAL_DATE

    def test_to_dict_deterministic(self):
        """Two to_dict() calls on the same record produce identical output."""
        rec = _make_day_record()
        d1 = rec.to_dict()
        d2 = rec.to_dict()
        assert d1 == d2
        # Also check that JSON serialization is byte-identical
        assert json.dumps(d1, sort_keys=True) == json.dumps(d2, sort_keys=True)

    def test_to_dict_floats_rounded(self):
        """Floats in to_dict are rounded to 10 decimal places."""
        rec = DayRecord(
            signal_date=_SIGNAL_DATE,
            status="complete",
            skip_reason=None,
            run_timestamp=_RUN_TS,
            account_number=_ACCOUNT,
            equity=1.0 / 3.0,  # irrational float
            selected=[],
            order_intents=[],
            reviews=[],
            held_halted=[],
            skipped=[],
            loss_halt={},
            reconciled=True,
            notes=[],
        )
        d = rec.to_dict()
        # equity should be rounded to 10dp
        assert d["equity"] == round(1.0 / 3.0, 10)

    def test_status_complete_skip_reason_none(self):
        """A 'complete' record has skip_reason=None in to_dict."""
        rec = _make_day_record(status="complete")
        assert rec.to_dict()["skip_reason"] is None

    def test_status_skipped_has_reason(self):
        """A 'skipped' record preserves skip_reason through round-trip."""
        rec = _make_day_record(status="skipped", skip_reason="auth_token_expired")
        d = rec.to_dict()
        assert d["status"] == "skipped"
        assert d["skip_reason"] == "auth_token_expired"
        restored = DayRecord.from_dict(d)
        assert restored.skip_reason == "auth_token_expired"

    def test_data_source_field_present_in_to_dict(self):
        """to_dict() must include a 'data_source' key."""
        rec = _make_day_record()
        d = rec.to_dict()
        assert "data_source" in d, "DayRecord.to_dict() must contain 'data_source'"
        assert d["data_source"] == "robinhood_mcp_live"

    def test_data_source_round_trip(self):
        """data_source survives to_dict → from_dict unchanged."""
        rec = _make_day_record()
        restored = DayRecord.from_dict(rec.to_dict())
        assert restored.data_source == rec.data_source
        assert restored.data_source == "robinhood_mcp_live"

    def test_data_source_default_on_missing_key(self):
        """from_dict must tolerate a missing 'data_source' key (backward compat)."""
        rec = _make_day_record()
        d = rec.to_dict()
        del d["data_source"]
        restored = DayRecord.from_dict(d)
        assert restored.data_source == "robinhood_mcp_live"


# ── TestAtomicWrite ───────────────────────────────────────────────────────────


class TestAtomicWrite:
    """_atomic_write_json leaves no .tmp; written JSON is valid and sorted."""

    def test_no_tmp_file_remains(self, tmp_path: Path):
        out = tmp_path / "test.json"
        _atomic_write_json(out, {"key": "value"})
        assert out.exists()
        tmp = tmp_path / "test.json.tmp"
        assert not tmp.exists(), ".tmp file must not remain after successful write"

    def test_output_is_valid_json(self, tmp_path: Path):
        obj = {"z": 2, "a": 1, "m": [3, 1, 2]}
        out = tmp_path / "out.json"
        _atomic_write_json(out, obj)
        parsed = json.loads(out.read_text(encoding="utf-8"))
        assert parsed == obj

    def test_output_is_sorted(self, tmp_path: Path):
        """sort_keys=True means keys appear in alphabetical order."""
        obj = {"zebra": 1, "apple": 2, "mango": 3}
        out = tmp_path / "sorted.json"
        _atomic_write_json(out, obj)
        raw_text = out.read_text(encoding="utf-8")
        # Parse and re-dump with sort_keys to compare
        parsed = json.loads(raw_text)
        assert list(parsed.keys()) == sorted(parsed.keys())

    def test_overwrites_existing_file(self, tmp_path: Path):
        out = tmp_path / "overwrite.json"
        _atomic_write_json(out, {"v": 1})
        _atomic_write_json(out, {"v": 2})
        parsed = json.loads(out.read_text(encoding="utf-8"))
        assert parsed["v"] == 2


# ── TestRecordDay ─────────────────────────────────────────────────────────────


class TestRecordDay:
    """record_day: happy path, review merging, missing/orphan detection."""

    def _run_plan(self) -> DayPlan:
        md = _base_market_data()
        return plan_day(
            md, {}, signal_date=_SIGNAL_DATE,
            account_number=_ACCOUNT, equity=_EQUITY_PLAN,
        )

    def test_writes_json_at_expected_path(self, tmp_path: Path):
        plan = self._run_plan()
        broker = _make_paper_broker()
        reviews = [broker.review(i) for i in plan.order_intents]
        record_day(
            plan, reviews,
            state_dir=tmp_path,
            run_timestamp=_RUN_TS,
            account_number=_ACCOUNT,
            equity=_EQUITY_RECORD,
        )
        expected = tmp_path / f"{_SIGNAL_DATE.isoformat()}.json"
        assert expected.exists(), f"Expected state file not found: {expected}"

    def test_status_is_complete(self, tmp_path: Path):
        plan = self._run_plan()
        broker = _make_paper_broker()
        reviews = [broker.review(i) for i in plan.order_intents]
        rec = record_day(
            plan, reviews,
            state_dir=tmp_path, run_timestamp=_RUN_TS,
            account_number=_ACCOUNT, equity=_EQUITY_RECORD,
        )
        assert rec.status == "complete"
        assert rec.skip_reason is None

    def test_round_trip_via_load(self, tmp_path: Path):
        """record_day → load_day_record → from_dict == original."""
        plan = self._run_plan()
        broker = _make_paper_broker()
        reviews = [broker.review(i) for i in plan.order_intents]
        rec = record_day(
            plan, reviews,
            state_dir=tmp_path, run_timestamp=_RUN_TS,
            account_number=_ACCOUNT, equity=_EQUITY_RECORD,
        )
        loaded = load_day_record(tmp_path, _SIGNAL_DATE)
        assert loaded is not None
        assert loaded.signal_date == rec.signal_date
        assert loaded.status == rec.status
        assert loaded.run_timestamp == rec.run_timestamp
        assert len(loaded.reviews) == len(rec.reviews)
        assert len(loaded.order_intents) == len(rec.order_intents)

    def test_reviews_recorded(self, tmp_path: Path):
        """All reviews are in the DayRecord."""
        plan = self._run_plan()
        broker = _make_paper_broker()
        reviews = [broker.review(i) for i in plan.order_intents]
        rec = record_day(
            plan, reviews,
            state_dir=tmp_path, run_timestamp=_RUN_TS,
            account_number=_ACCOUNT, equity=_EQUITY_RECORD,
        )
        assert len(rec.reviews) == len(reviews)

    def test_intent_with_no_review_adds_note(self, tmp_path: Path):
        """If an intent has no matching review, a warning note is added."""
        plan = self._run_plan()
        # Pass empty reviews so all intents are unmatched
        rec = record_day(
            plan, [],
            state_dir=tmp_path, run_timestamp=_RUN_TS,
            account_number=_ACCOUNT, equity=_EQUITY_RECORD,
        )
        unmatched_notes = [n for n in rec.notes if "has no matching review" in n]
        assert len(unmatched_notes) == len(plan.order_intents), (
            f"Expected {len(plan.order_intents)} warning notes for unmatched intents, "
            f"got {len(unmatched_notes)}: {rec.notes}"
        )

    def test_orphan_review_adds_note(self, tmp_path: Path):
        """A review whose ref_id matches no intent gets a warning note."""
        plan = self._run_plan()
        # Build an orphan review by making a ReviewResult with a bogus ref_id
        orphan = ReviewResult(
            ref_id="deadbeef" * 8,  # 64 hex chars, no intent matches
            symbol="ORPHAN",
            side="buy",
            order_type="market",
            quantity="1.0",
            stop_price=None,
            alert_type=None,
            alerts_raw={},
            last_trade_price=100.0,
            bid=None,
            ask=None,
            previous_close=99.0,
            previous_close_date=None,
            has_traded=True,
            state="active",
            market_data_disclosure="",
            spread=None,
        )
        rec = record_day(
            plan, [orphan],
            state_dir=tmp_path, run_timestamp=_RUN_TS,
            account_number=_ACCOUNT, equity=_EQUITY_RECORD,
        )
        orphan_notes = [n for n in rec.notes if "has no matching intent" in n]
        assert len(orphan_notes) >= 1

    def test_no_tmp_file_after_write(self, tmp_path: Path):
        """record_day must not leave a .tmp file after successful write."""
        plan = self._run_plan()
        broker = _make_paper_broker()
        reviews = [broker.review(i) for i in plan.order_intents]
        record_day(
            plan, reviews,
            state_dir=tmp_path, run_timestamp=_RUN_TS,
            account_number=_ACCOUNT, equity=_EQUITY_RECORD,
        )
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == [], f"Leftover .tmp files: {tmp_files}"

    def test_written_json_is_valid(self, tmp_path: Path):
        """The written state file is valid JSON."""
        plan = self._run_plan()
        broker = _make_paper_broker()
        reviews = [broker.review(i) for i in plan.order_intents]
        record_day(
            plan, reviews,
            state_dir=tmp_path, run_timestamp=_RUN_TS,
            account_number=_ACCOUNT, equity=_EQUITY_RECORD,
        )
        path = tmp_path / f"{_SIGNAL_DATE.isoformat()}.json"
        parsed = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict)
        assert parsed["status"] == "complete"


# ── TestRecordSkip ────────────────────────────────────────────────────────────


class TestRecordSkip:
    """record_skip: attributed skip record written; is_day_complete returns False."""

    def test_writes_skipped_status(self, tmp_path: Path):
        rec = record_skip(
            tmp_path, _SIGNAL_DATE, "auth_token_expired",
            run_timestamp=_RUN_TS, account_number=_ACCOUNT,
        )
        assert rec.status == "skipped"
        assert rec.skip_reason == "auth_token_expired"

    def test_is_day_complete_false_for_skip(self, tmp_path: Path):
        record_skip(
            tmp_path, _SIGNAL_DATE, "mcp_fetch_failed",
            run_timestamp=_RUN_TS, account_number=_ACCOUNT,
        )
        assert is_day_complete(tmp_path, _SIGNAL_DATE) is False

    def test_skip_note_is_attributed(self, tmp_path: Path):
        """The notes list must contain a loud attribution."""
        rec = record_skip(
            tmp_path, _SIGNAL_DATE, "network_timeout",
            run_timestamp=_RUN_TS, account_number=_ACCOUNT,
        )
        assert any("SKIP ATTRIBUTED" in n for n in rec.notes)
        assert any("network_timeout" in n for n in rec.notes)

    def test_skip_file_written_at_expected_path(self, tmp_path: Path):
        record_skip(
            tmp_path, _SIGNAL_DATE, "test_reason",
            run_timestamp=_RUN_TS, account_number=_ACCOUNT,
        )
        expected = tmp_path / f"{_SIGNAL_DATE.isoformat()}.json"
        assert expected.exists()

    def test_skip_round_trip(self, tmp_path: Path):
        """record_skip → load_day_record produces a 'skipped' DayRecord."""
        record_skip(
            tmp_path, _SIGNAL_DATE, "auth_token_expired",
            run_timestamp=_RUN_TS, account_number=_ACCOUNT,
        )
        loaded = load_day_record(tmp_path, _SIGNAL_DATE)
        assert loaded is not None
        assert loaded.status == "skipped"
        assert loaded.skip_reason == "auth_token_expired"

    def test_no_tmp_file_after_skip(self, tmp_path: Path):
        record_skip(
            tmp_path, _SIGNAL_DATE, "any_reason",
            run_timestamp=_RUN_TS, account_number=_ACCOUNT,
        )
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []


# ── TestLoadDayRecord ─────────────────────────────────────────────────────────


class TestLoadDayRecord:
    """load_day_record: returns DayRecord or None."""

    def test_returns_none_when_no_file(self, tmp_path: Path):
        assert load_day_record(tmp_path, _SIGNAL_DATE) is None

    def test_returns_record_when_file_exists(self, tmp_path: Path):
        record_skip(
            tmp_path, _SIGNAL_DATE, "test",
            run_timestamp=_RUN_TS, account_number=_ACCOUNT,
        )
        rec = load_day_record(tmp_path, _SIGNAL_DATE)
        assert rec is not None
        assert rec.signal_date == _SIGNAL_DATE

    def test_from_dict_to_dict_equality(self, tmp_path: Path):
        """DayRecord.from_dict(rec.to_dict()) == rec (field by field)."""
        rec = _make_day_record()
        restored = DayRecord.from_dict(rec.to_dict())
        assert restored.signal_date == rec.signal_date
        assert restored.status == rec.status
        assert restored.skip_reason == rec.skip_reason
        assert restored.run_timestamp == rec.run_timestamp
        assert restored.account_number == rec.account_number
        assert restored.equity == rec.equity
        assert restored.selected == rec.selected
        assert restored.order_intents == rec.order_intents
        assert restored.reviews == rec.reviews
        assert restored.held_halted == rec.held_halted
        assert restored.skipped == rec.skipped
        assert restored.reconciled == rec.reconciled
        assert restored.notes == rec.notes


# ── TestIsDayComplete ─────────────────────────────────────────────────────────


class TestIsDayComplete:
    """is_day_complete: True for 'complete', False for 'skipped', False for absent."""

    def test_false_when_no_file(self, tmp_path: Path):
        assert is_day_complete(tmp_path, _SIGNAL_DATE) is False

    def test_false_for_skipped(self, tmp_path: Path):
        record_skip(
            tmp_path, _SIGNAL_DATE, "test",
            run_timestamp=_RUN_TS, account_number=_ACCOUNT,
        )
        assert is_day_complete(tmp_path, _SIGNAL_DATE) is False

    def test_true_after_record_day(self, tmp_path: Path):
        md = _base_market_data()
        plan = plan_day(
            md, {}, signal_date=_SIGNAL_DATE,
            account_number=_ACCOUNT, equity=_EQUITY_PLAN,
        )
        broker = _make_paper_broker()
        reviews = [broker.review(i) for i in plan.order_intents]
        record_day(
            plan, reviews,
            state_dir=tmp_path, run_timestamp=_RUN_TS,
            account_number=_ACCOUNT, equity=_EQUITY_RECORD,
        )
        assert is_day_complete(tmp_path, _SIGNAL_DATE) is True


# ── TestAppendTelemetry ───────────────────────────────────────────────────────


class TestAppendTelemetry:
    """append_telemetry: JSONL row counts; two days → two rows."""

    def test_creates_file_if_absent(self, tmp_path: Path):
        path = tmp_path / "telemetry.jsonl"
        assert not path.exists()
        append_telemetry(path, _make_day_record())
        assert path.exists()

    def test_one_row_per_call(self, tmp_path: Path):
        path = tmp_path / "telemetry.jsonl"
        append_telemetry(path, _make_day_record())
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        assert len(rows) == 1

    def test_two_days_two_rows(self, tmp_path: Path):
        path = tmp_path / "telemetry.jsonl"
        date1 = dt.date(2026, 6, 16)
        date2 = dt.date(2026, 6, 17)
        rec1 = DayRecord(
            signal_date=date1, status="complete", skip_reason=None,
            run_timestamp=_RUN_TS, account_number=_ACCOUNT, equity=0.0,
            selected=[], order_intents=[], reviews=[], held_halted=[],
            skipped=[], loss_halt={}, reconciled=True, notes=[],
        )
        rec2 = DayRecord(
            signal_date=date2, status="skipped", skip_reason="test",
            run_timestamp=_RUN_TS, account_number=_ACCOUNT, equity=0.0,
            selected=[], order_intents=[], reviews=[], held_halted=[],
            skipped=[], loss_halt={}, reconciled=False, notes=[],
        )
        append_telemetry(path, rec1)
        append_telemetry(path, rec2)
        rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
        assert len(rows) == 2
        assert rows[0]["signal_date"] == date1.isoformat()
        assert rows[1]["signal_date"] == date2.isoformat()

    def test_row_has_required_fields(self, tmp_path: Path):
        path = tmp_path / "telemetry.jsonl"
        rec = _make_day_record()
        append_telemetry(path, rec)
        row = json.loads(path.read_text().splitlines()[0])
        required = {
            "signal_date", "status", "n_selected", "n_intents", "n_reviews",
            "n_held_halted", "n_skipped", "reconciled", "run_timestamp",
            "data_source",
        }
        assert required.issubset(set(row.keys())), (
            f"Missing fields: {required - set(row.keys())}"
        )

    def test_telemetry_data_source_field(self, tmp_path: Path):
        """append_telemetry must include data_source in the JSONL row."""
        path = tmp_path / "telemetry.jsonl"
        rec = _make_day_record()
        append_telemetry(path, rec)
        row = json.loads(path.read_text().splitlines()[0])
        assert row["data_source"] == "robinhood_mcp_live"

    def test_row_counts_match_record(self, tmp_path: Path):
        path = tmp_path / "telemetry.jsonl"
        rec = DayRecord(
            signal_date=_SIGNAL_DATE, status="complete", skip_reason=None,
            run_timestamp=_RUN_TS, account_number=_ACCOUNT, equity=0.0,
            selected=[{"symbol": "SPY"}],
            order_intents=[{"ref_id": "abc"}, {"ref_id": "def"}],
            reviews=[{"ref_id": "abc"}],
            held_halted=["XYZ"],
            skipped=[["BAD", "reason"]],
            loss_halt={}, reconciled=True, notes=[],
        )
        append_telemetry(path, rec)
        row = json.loads(path.read_text().splitlines()[0])
        assert row["n_selected"] == 1
        assert row["n_intents"] == 2
        assert row["n_reviews"] == 1
        assert row["n_held_halted"] == 1
        assert row["n_skipped"] == 1
        assert row["reconciled"] is True

    def test_creates_parent_dirs(self, tmp_path: Path):
        nested = tmp_path / "a" / "b" / "telemetry.jsonl"
        append_telemetry(nested, _make_day_record())
        assert nested.exists()


# ── TestRunDayIdempotency ─────────────────────────────────────────────────────


class TestRunDayIdempotency:
    """run_day: second call returns existing record; scan() NOT called again."""

    def test_second_call_returns_existing_record(self, tmp_path: Path):
        """run_day twice → second call returns the first record unchanged."""
        md = _base_market_data()
        broker = _make_paper_broker()

        rec1 = run_day(
            md, broker, {},
            signal_date=_SIGNAL_DATE, account_number=_ACCOUNT, equity=_EQUITY_PLAN,
            state_dir=tmp_path / "state",
            telemetry_path=tmp_path / "telemetry.jsonl",
            run_timestamp=_RUN_TS,
        )

        # Create a MarketData whose scan() raises if called — proves no re-plan
        class _BombMarketData:
            def scan(self):  # type: ignore[override]
                raise AssertionError("scan() called on second run — idempotency broken!")
            def historicals(self, sym: str):  # type: ignore[override]
                raise AssertionError("historicals() called on second run — idempotency broken!")
            def quotes(self, syms):  # type: ignore[override]
                return {}
            def tradability(self, syms):  # type: ignore[override]
                return {}
            def fundamentals(self, syms):  # type: ignore[override]
                return {}
            def earnings(self):  # type: ignore[override]
                return {}

        rec2 = run_day(
            _BombMarketData(), broker, {},
            signal_date=_SIGNAL_DATE, account_number=_ACCOUNT, equity=_EQUITY_PLAN,
            state_dir=tmp_path / "state",
            telemetry_path=tmp_path / "telemetry.jsonl",
            run_timestamp="2026-06-17T10:00:00Z",  # different timestamp
        )

        # Second call must return the FIRST record (same run_timestamp)
        assert rec2.run_timestamp == rec1.run_timestamp
        assert rec2.signal_date == rec1.signal_date
        assert rec2.status == "complete"

    def test_idempotent_call_does_not_duplicate_telemetry(self, tmp_path: Path):
        """Two run_day calls on the same date write only ONE telemetry row."""
        md = _base_market_data()
        broker = _make_paper_broker()
        tpath = tmp_path / "telemetry.jsonl"
        sdir = tmp_path / "state"

        run_day(
            md, broker, {},
            signal_date=_SIGNAL_DATE, account_number=_ACCOUNT, equity=_EQUITY_PLAN,
            state_dir=sdir, telemetry_path=tpath, run_timestamp=_RUN_TS,
        )

        class _BombMarketData:
            def scan(self): raise AssertionError("should not be called")
            def historicals(self, sym): raise AssertionError("should not be called")
            def quotes(self, syms): return {}
            def tradability(self, syms): return {}
            def fundamentals(self, syms): return {}
            def earnings(self): return {}

        run_day(
            _BombMarketData(), broker, {},
            signal_date=_SIGNAL_DATE, account_number=_ACCOUNT, equity=_EQUITY_PLAN,
            state_dir=sdir, telemetry_path=tpath, run_timestamp=_RUN_TS,
        )

        rows = [json.loads(line) for line in tpath.read_text().splitlines() if line.strip()]
        assert len(rows) == 1, (
            f"Expected 1 telemetry row (idempotent), got {len(rows)}"
        )


# ── TestRunDayEndToEnd ────────────────────────────────────────────────────────


class TestRunDayEndToEnd:
    """Full run_day with PaperBroker produces a complete record with reviews."""

    def test_produces_complete_record(self, tmp_path: Path):
        md = _base_market_data()
        broker = _make_paper_broker()

        rec = run_day(
            md, broker, {},
            signal_date=_SIGNAL_DATE, account_number=_ACCOUNT, equity=_EQUITY_PLAN,
            state_dir=tmp_path / "state",
            telemetry_path=tmp_path / "telemetry.jsonl",
            run_timestamp=_RUN_TS,
        )

        assert rec.status == "complete"
        assert rec.signal_date == _SIGNAL_DATE
        assert rec.run_timestamp == _RUN_TS
        assert rec.account_number == _ACCOUNT

    def test_reviews_match_intents(self, tmp_path: Path):
        """One review per intent; all ref_ids accounted for."""
        md = _base_market_data()
        broker = _make_paper_broker()

        rec = run_day(
            md, broker, {},
            signal_date=_SIGNAL_DATE, account_number=_ACCOUNT, equity=_EQUITY_PLAN,
            state_dir=tmp_path / "state",
            telemetry_path=tmp_path / "telemetry.jsonl",
            run_timestamp=_RUN_TS,
        )

        # The number of reviews must equal the number of intents
        assert len(rec.reviews) == len(rec.order_intents), (
            f"Expected {len(rec.order_intents)} reviews, got {len(rec.reviews)}"
        )

    def test_telemetry_row_written(self, tmp_path: Path):
        md = _base_market_data()
        broker = _make_paper_broker()
        tpath = tmp_path / "telemetry.jsonl"

        run_day(
            md, broker, {},
            signal_date=_SIGNAL_DATE, account_number=_ACCOUNT, equity=_EQUITY_PLAN,
            state_dir=tmp_path / "state",
            telemetry_path=tpath,
            run_timestamp=_RUN_TS,
        )

        assert tpath.exists()
        rows = [json.loads(line) for line in tpath.read_text().splitlines() if line.strip()]
        assert len(rows) == 1
        assert rows[0]["signal_date"] == _SIGNAL_DATE.isoformat()
        assert rows[0]["status"] == "complete"

    def test_state_file_loadable(self, tmp_path: Path):
        """The state file can be reloaded as a DayRecord."""
        md = _base_market_data()
        broker = _make_paper_broker()
        sdir = tmp_path / "state"

        run_day(
            md, broker, {},
            signal_date=_SIGNAL_DATE, account_number=_ACCOUNT, equity=_EQUITY_PLAN,
            state_dir=sdir, telemetry_path=tmp_path / "telemetry.jsonl",
            run_timestamp=_RUN_TS,
        )

        loaded = load_day_record(sdir, _SIGNAL_DATE)
        assert loaded is not None
        assert loaded.status == "complete"

    def test_deterministic_given_fixed_timestamp(self, tmp_path: Path):
        """Two run_day calls with identical inputs produce byte-identical JSON state files."""
        md = _base_market_data()
        broker1 = _make_paper_broker()
        broker2 = _make_paper_broker()

        sdir1 = tmp_path / "state1"
        sdir2 = tmp_path / "state2"
        tpath1 = tmp_path / "t1.jsonl"
        tpath2 = tmp_path / "t2.jsonl"

        run_day(
            md, broker1, {},
            signal_date=_SIGNAL_DATE, account_number=_ACCOUNT, equity=_EQUITY_PLAN,
            state_dir=sdir1, telemetry_path=tpath1, run_timestamp=_RUN_TS,
        )
        run_day(
            md, broker2, {},
            signal_date=_SIGNAL_DATE, account_number=_ACCOUNT, equity=_EQUITY_PLAN,
            state_dir=sdir2, telemetry_path=tpath2, run_timestamp=_RUN_TS,
        )

        file1 = (sdir1 / f"{_SIGNAL_DATE.isoformat()}.json").read_text(encoding="utf-8")
        file2 = (sdir2 / f"{_SIGNAL_DATE.isoformat()}.json").read_text(encoding="utf-8")
        assert file1 == file2, "Two runs with identical inputs must produce identical JSON files"


class TestEarningsFlagsInRecord:
    """earnings_flags (symbol -> earnings_blackout) must survive into the persisted
    record so the audit trail shows WHY a name was excluded. Regression: the field
    was originally absent from DayRecord, silently losing the blackout reason."""

    def test_earnings_flags_in_to_dict_and_round_trip(self):
        rec = DayRecord(
            signal_date=_SIGNAL_DATE, status="complete", skip_reason=None,
            run_timestamp=_RUN_TS, account_number=_ACCOUNT, equity=_EQUITY_RECORD,
            selected=[], order_intents=[], reviews=[], held_halted=[], skipped=[],
            loss_halt={}, reconciled=True, notes=[],
            earnings_flags={"MU": True, "AMD": False},
        )
        assert rec.to_dict()["earnings_flags"] == {"MU": True, "AMD": False}
        back = DayRecord.from_dict(rec.to_dict())
        assert back.earnings_flags == {"MU": True, "AMD": False}

    def test_earnings_flags_defaults_empty_and_back_compat(self):
        rec = _make_day_record()
        assert rec.earnings_flags == {}
        d = rec.to_dict()
        del d["earnings_flags"]  # an old record file with no earnings_flags key
        assert DayRecord.from_dict(d).earnings_flags == {}
