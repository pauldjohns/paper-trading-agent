# tests/live/test_run_paper_book.py
"""Driver-level smoke gate for scripts/run_paper_book.py (P4.1).

Synthesizes a minimal, MCP-free raw-JSON set (scan + historicals + quotes +
tradability + fundamentals + earnings) in tmp_path, points the driver's module
constants at it, and drives cmd_arm -> cmd_poll -> cmd_status end-to-end. Asserts
the driver normalizes centrally, arms a qualifier, fills a triggered entry, and
writes book.json + equity_curve.jsonl. This is the durable offline gate for the
glue driver (the pure package has its own unit/golden coverage).
"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

ET = ZoneInfo("America/New_York")

_DRIVER_PATH = Path(__file__).resolve().parents[2] / "scripts" / "run_paper_book.py"

SIGNAL_DATE = dt.date(2026, 6, 22)
TODAY = dt.date(2026, 6, 23)


def _load_driver():
    """Import the driver by PATH (it is in scripts/, not on the package path)."""
    spec = importlib.util.spec_from_file_location("run_paper_book", _DRIVER_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ── synthesized raw MCP JSON (shapes match mcp_live.normalize_* ) ─────────────

def _rising_bars_raw(symbol: str, signal_date: dt.date, n: int = 300,
                     start: float = 10.0, step: float = 0.5) -> dict:
    """A get_equity_historicals-shaped payload of strictly-rising daily bars
    whose LAST bar is dated EXACTLY signal_date (so completed_bar_guard passes
    and the trend/momentum/breakout signals all fire)."""
    bars = []
    px = start
    for i in range(n):
        d = signal_date - dt.timedelta(days=(n - 1 - i))
        bars.append({
            "begins_at": f"{d.isoformat()}T00:00:00Z",
            "open_price": f"{px:.6f}",
            "high_price": f"{px + 0.2:.6f}",
            "low_price": f"{px - 0.2:.6f}",
            "close_price": f"{px:.6f}",
            "volume": 1_000_000,
            "session": "reg",
        })
        px += step
    return {"data": {"results": [{"symbol": symbol, "interval": "day",
                                  "bounds": "regular", "bars": bars}]}}


def _scan_raw(symbol: str, last: float) -> dict:
    return {"results": [{
        "ticker": symbol,
        "instrument_id": "id-" + symbol,
        "instrument_type": "EQUITY",
        "columns": {
            "Symbol": symbol, "Name": symbol, "Close": f"{last:.2f}",
            "Last": f"{last:.2f}", "Market cap": "1.0e+11", "Volume": "9.0e+06",
            "Relative volume": "1.2", "RSI": "65.0", "% Change": "1.0",
        },
    }]}


def _quotes_raw(symbol: str, *, bid: float, ask: float, last: float,
                prev_close: float, settled_close: float,
                settled_date: dt.date) -> dict:
    return {"data": {"results": [{
        "quote": {
            "symbol": symbol,
            "last_trade_price": f"{last:.6f}",
            "adjusted_previous_close": f"{prev_close:.6f}",
            "previous_close": f"{prev_close:.6f}",
            "bid_price": f"{bid:.6f}",
            "ask_price": f"{ask:.6f}",
            "has_traded": True,
            "state": "active",
        },
        "close": {"symbol": symbol, "date": settled_date.isoformat(),
                  "price": f"{settled_close:.2f}", "interpolated": False,
                  "source": "sip-list-exchange-close"},
    }]}}


def _tradability_raw(symbol: str) -> dict:
    return {"data": {"results": [{
        "symbol": symbol, "state": "active", "tradeable": True,
        "fractional_tradability": "tradable", "short_selling_tradability": "tradable",
        "account_type_tradabilities": [
            {"account_type": "individual", "account_type_tradability": "tradable"}],
    }]}}


def _fundamentals_raw(symbol: str, last: float) -> dict:
    return {"data": {"results": [{
        "symbol": symbol, "market_cap": "1.0e+11",
        "average_volume": "9.0e+06", "average_volume_30_days": "9.0e+06",
        "high_52_weeks": f"{last * 1.1:.6f}", "high_52_weeks_date": "2026-06-22",
        "sector": "Tech", "industry": "Semis",
    }]}}


def _earnings_raw() -> dict:
    # No upcoming earnings for our symbol -> no blackout.
    return {"data": {"results": []}}


def _write_raw_set(raw_dir: Path, *, symbol: str, settled_close: float,
                   quote_kwargs: dict) -> None:
    raw_dir.mkdir(parents=True, exist_ok=True)
    (raw_dir / "scan.json").write_text(json.dumps(_scan_raw(symbol, settled_close)))
    (raw_dir / f"hist_{symbol}.json").write_text(
        json.dumps(_rising_bars_raw(symbol, SIGNAL_DATE)))
    (raw_dir / "tradability.json").write_text(json.dumps(_tradability_raw(symbol)))
    (raw_dir / "fundamentals.json").write_text(
        json.dumps(_fundamentals_raw(symbol, settled_close)))
    (raw_dir / "earnings.json").write_text(json.dumps(_earnings_raw()))
    (raw_dir / "quotes.json").write_text(json.dumps(_quotes_raw(symbol, **quote_kwargs)))


@pytest.fixture()
def driver(tmp_path, monkeypatch):
    """Driver module with STATE_DIR/RAW redirected into tmp_path."""
    mod = _load_driver()
    state_dir = tmp_path / "paper_book"
    raw = state_dir / "raw"
    monkeypatch.setattr(mod, "STATE_DIR", state_dir)
    monkeypatch.setattr(mod, "RAW", raw)
    return mod


def _settled_close_for_symbol(mod, symbol: str = "AMD") -> float:
    """The last rising-bar close == StaticMarketData last close (signal close)."""
    md = mod.build_market_data()
    return float(md.historicals(symbol)["close"].iloc[-1])


def test_arm_then_poll_fills_and_writes_book(driver, capsys):
    mod = driver
    symbol = "AMD"
    # First write a raw set with a placeholder quote just to compute the settled
    # close (== last rising bar close); then rewrite quotes to trigger an entry.
    _write_raw_set(mod.RAW, symbol=symbol, settled_close=100.0, quote_kwargs=dict(
        bid=99.0, ask=101.0, last=100.0, prev_close=100.0,
        settled_close=100.0, settled_date=SIGNAL_DATE))
    settled = _settled_close_for_symbol(mod, symbol)

    # ARM: build market data, arm the qualifier, save book.json.
    mod.cmd_arm(SIGNAL_DATE.isoformat(), TODAY.isoformat())
    out = capsys.readouterr().out
    assert "ARM" in out and symbol in out

    book_path = mod.STATE_DIR / "book.json"
    assert book_path.exists()
    book = json.loads(book_path.read_text())
    assert book["last_arm_date"] == TODAY.isoformat()
    assert symbol in book["armed"], "qualifier should be armed"
    entry_ref = book["armed"][symbol]["entry_ref"]

    # POLL: rewrite quotes so last_trade_price is strictly above the entry_ref
    # (breakout trigger) and the move passes the <=50% sanity gate.
    trigger_last = entry_ref + 1.0
    _write_raw_set(mod.RAW, symbol=symbol, settled_close=settled, quote_kwargs=dict(
        bid=entry_ref + 0.9, ask=entry_ref + 1.1, last=trigger_last,
        prev_close=settled, settled_close=settled, settled_date=SIGNAL_DATE))

    ts = f"{TODAY.isoformat()}T14:00:00Z"
    mod.cmd_poll(ts)
    poll_out = capsys.readouterr().out
    assert "POLL" in poll_out

    # The book now holds the position; equity curve row written.
    book2 = json.loads(book_path.read_text())
    assert symbol in book2["positions"], "triggered entry should produce a position"
    assert symbol in book2["filled_today"]
    assert book2["cash"] < mod.START_CAPITAL
    assert (mod.STATE_DIR / "equity_curve.jsonl").exists()
    rows = [json.loads(l) for l in
            (mod.STATE_DIR / "equity_curve.jsonl").read_text().splitlines() if l.strip()]
    assert len(rows) == 1
    assert rows[0]["ts"] == ts
    assert rows[0]["entries_taken"] is True


def test_status_runs_without_advancing(driver, capsys):
    mod = driver
    # No book yet -> status reports "not yet armed" and does not raise.
    mod.cmd_status()
    out = capsys.readouterr().out
    assert "no book" in out

    # Arm, then status should print the armed/flat book without writing a poll row.
    _write_raw_set(mod.RAW, symbol="AMD", settled_close=100.0, quote_kwargs=dict(
        bid=99.0, ask=101.0, last=100.0, prev_close=100.0,
        settled_close=100.0, settled_date=SIGNAL_DATE))
    mod.cmd_arm(SIGNAL_DATE.isoformat(), TODAY.isoformat())
    capsys.readouterr()

    mod.cmd_status()
    status_out = capsys.readouterr().out
    assert "STATUS" in status_out
    assert "OPEN POSITIONS (0)" in status_out  # armed but not filled -> flat
    # status must not create an equity curve (no advance).
    assert not (mod.STATE_DIR / "equity_curve.jsonl").exists()


def test_poll_before_arm_raises(driver):
    mod = driver
    with pytest.raises(SystemExit):
        mod.cmd_poll(f"{TODAY.isoformat()}T14:00:00Z")


def test_no_place_invariant_in_driver():
    # The LIVE-02 driver must never reference any MCP mutation / broker-call token.
    # Scoped to run_paper_book.py ONLY (not all of scripts/): the LIVE-01 driver
    # run_paper_monitor_live.py mentions `review_equity_order` in a docstring and
    # would trip a scripts/-wide glob.
    source = _DRIVER_PATH.read_text(encoding="utf-8")
    forbidden = [
        "place_equity_order", "place_option_order",
        "cancel_equity_order", "cancel_option_order",
        "review_equity_order", "mcp__",
    ]
    for token in forbidden:
        assert token not in source, (
            f"NO-PLACE INVARIANT VIOLATED in run_paper_book.py: found {token!r}")


def test_driver_records_non_order_capable_account(driver, capsys):
    mod = driver
    _write_raw_set(mod.RAW, symbol="AMD", settled_close=100.0, quote_kwargs=dict(
        bid=99.0, ask=101.0, last=100.0, prev_close=100.0,
        settled_close=100.0, settled_date=SIGNAL_DATE))
    mod.cmd_arm(SIGNAL_DATE.isoformat(), TODAY.isoformat())
    out = capsys.readouterr().out
    assert "987654321" in out
    assert "123456789" not in out


def test_poll_decide_self_heal_when_unarmed(driver, capsys):
    mod = driver
    _write_raw_set(mod.RAW, symbol="AMD", settled_close=100.0, quote_kwargs=dict(
        bid=99.0, ask=101.0, last=100.0, prev_close=100.0,
        settled_close=100.0, settled_date=SIGNAL_DATE))
    mod.cmd_arm(SIGNAL_DATE.isoformat(), TODAY.isoformat())   # arms for TODAY (2026-06-23)
    capsys.readouterr()
    # A weekday RTH "now" on a LATER day than last_arm_date -> SELF_HEAL_ARM.
    mod.cmd_poll_decide("2026-06-24T15:00:00Z")   # 11:00 ET Wed, armed date is 06-23
    assert "SELF_HEAL_ARM" in capsys.readouterr().out

def test_poll_decide_normal_when_armed_today(driver, capsys):
    mod = driver
    _write_raw_set(mod.RAW, symbol="AMD", settled_close=100.0, quote_kwargs=dict(
        bid=99.0, ask=101.0, last=100.0, prev_close=100.0,
        settled_close=100.0, settled_date=SIGNAL_DATE))
    mod.cmd_arm(SIGNAL_DATE.isoformat(), TODAY.isoformat())
    capsys.readouterr()
    mod.cmd_poll_decide(f"{TODAY.isoformat()}T15:00:00Z")   # 11:00 ET, armed today
    assert "NORMAL" in capsys.readouterr().out

def test_mark_eod_writes_marker_and_decide_exits(driver, capsys):
    mod = driver
    _write_raw_set(mod.RAW, symbol="AMD", settled_close=100.0, quote_kwargs=dict(
        bid=99.0, ask=101.0, last=100.0, prev_close=100.0,
        settled_close=100.0, settled_date=SIGNAL_DATE))
    mod.cmd_arm(SIGNAL_DATE.isoformat(), TODAY.isoformat())
    ts = f"{TODAY.isoformat()}T15:00:00Z"
    mod.cmd_poll(ts)             # one poll today so polled_today is true
    mod.cmd_mark_eod(TODAY.isoformat())
    assert (mod.STATE_DIR / f"eod_done_{TODAY.isoformat()}").exists()
    capsys.readouterr()
    # After close, eod done -> EXIT.
    mod.cmd_poll_decide(f"{TODAY.isoformat()}T20:30:00Z")   # 16:30 ET, after close
    assert "EXIT" in capsys.readouterr().out

def test_poll_decide_no_book_in_session_self_heals(driver, capsys):
    # Brand-new machine: no book.json yet. An in-session fire -> SELF_HEAL_ARM
    # (last_arm_date is None); an off-hours fire -> EXIT.
    mod = driver  # STATE_DIR redirected to an empty tmp dir, no book
    mod.cmd_poll_decide(f"{TODAY.isoformat()}T15:00:00Z")   # 11:00 ET weekday
    assert "SELF_HEAL_ARM" in capsys.readouterr().out
    mod.cmd_poll_decide(f"{TODAY.isoformat()}T03:00:00Z")   # 23:00 ET prior day, closed
    assert "EXIT" in capsys.readouterr().out
