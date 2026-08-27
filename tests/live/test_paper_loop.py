# tests/live/test_paper_loop.py
import datetime as dt
import json

import pandas as pd
import pytest

from autotrader_live import paper_loop
from autotrader_live.mcp_live import (Fundamentals, Quote, ScanRow,
                                      StaticMarketData, Tradability)
from autotrader_live.paper_book import PaperBook, PaperPosition


def _bars_ending(signal_date, n=300, start=10.0, step=0.5):
    # strictly rising closes => trend_ok/momentum_ok/breakout all true; the LAST bar
    # is dated EXACTLY signal_date so completed_bar_guard(bars, signal_date) passes
    # (consecutive calendar days are fine — the guard checks ascending + last==signal_date,
    #  not trading-day-ness).
    rows = []
    px = start
    for i in range(n):
        d = signal_date - dt.timedelta(days=(n - 1 - i))
        rows.append((d, px, px + 0.2, px - 0.2, px, 1_000_000))
        px += step
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])


def _md(signal_date, *, bars=None):
    bars = _bars_ending(signal_date) if bars is None else bars
    last = float(bars["close"].iloc[-1])
    scan = [ScanRow("AMD", "id", "EQUITY", "AMD", last, last, 1e11, 9e6, 1.2, 65.0, 1.0)]
    quotes = {"AMD": Quote("AMD", last, signal_date, False, "x", last-0.05, last+0.05,
                           last, last, True, "active")}
    trad = {"AMD": Tradability("AMD", True, "active", True, False)}
    fund = {"AMD": Fundamentals("AMD", 1e11, 9e6, 9e6, last*1.1, "Tech", "Semis")}
    return StaticMarketData(scan_rows=scan, historicals={"AMD": bars}, quotes=quotes,
                            tradability=trad, fundamentals=fund, earnings={})


def test_arm_day_arms_qualifier_and_freezes_notional():
    signal_date = dt.date(2026, 6, 22)
    today = dt.date(2026, 6, 23)
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    paper_loop.arm_day(book, _md(signal_date), signal_date=signal_date, today=today)
    assert "AMD" in book.armed
    a = book.armed["AMD"]
    assert a.target_notional == pytest.approx(min(0.15*2000.0, a.target_notional))
    assert a.target_notional <= 300.0 + 1e-9          # per_name_cap_frac=0.15 binds
    assert book.last_arm_date == "2026-06-23"
    assert book.filled_today == set()


def test_arm_day_idempotent_same_day():
    signal_date, today = dt.date(2026, 6, 22), dt.date(2026, 6, 23)
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    paper_loop.arm_day(book, _md(signal_date), signal_date=signal_date, today=today)
    book.armed["AMD"] = book.armed["AMD"]
    paper_loop.arm_day(book, _md(signal_date), signal_date=signal_date, today=today)  # no-op
    assert book.last_arm_date == "2026-06-23"


def test_arm_day_skips_lookahead_bar():
    # historicals whose LAST bar is dated AFTER signal_date (an unsettled today-bar)
    # must be skipped by the completed_bar_guard — never armed off look-ahead data.
    signal_date, today = dt.date(2026, 6, 22), dt.date(2026, 6, 23)
    lookahead_bars = _bars_ending(dt.date(2026, 6, 23))   # last bar = 2026-06-23 > signal_date
    md = _md(signal_date, bars=lookahead_bars)
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    paper_loop.arm_day(book, md, signal_date=signal_date, today=today)
    assert "AMD" not in book.armed                          # guard refused the look-ahead bar
    assert book.last_arm_date == "2026-06-23"               # day still marked armed (no re-arm loop)


def test_advance_poll_fills_triggered_entry(tmp_path):
    signal_date, today = dt.date(2026, 6, 22), dt.date(2026, 6, 23)
    md = _md(signal_date)
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    paper_loop.arm_day(book, md, signal_date=signal_date, today=today)
    a = book.armed["AMD"]
    # quote whose last is strictly above the entry_ref -> trigger
    q = {"AMD": Quote("AMD", a.entry_ref, signal_date, False, "x",
                      a.entry_ref+0.9, a.entry_ref+1.1, a.entry_ref + 1.0,
                      a.entry_ref, True, "active")}
    paper_loop.advance_poll(book, q, md, ts="2026-06-23T14:00:00Z", state_dir=tmp_path)
    assert "AMD" in book.positions
    assert "AMD" in book.filled_today and "AMD" not in book.armed
    pos = book.positions["AMD"]
    assert pos.current_stop == pytest.approx(pos.entry_price - 2.0 * pos.atr_at_entry)
    assert book.cash < 2000.0
    # equity row written
    assert (tmp_path / "equity_curve.jsonl").exists()


def test_advance_poll_no_trigger_leaves_armed(tmp_path):
    signal_date, today = dt.date(2026, 6, 22), dt.date(2026, 6, 23)
    md = _md(signal_date)
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    paper_loop.arm_day(book, md, signal_date=signal_date, today=today)
    a = book.armed["AMD"]
    q = {"AMD": Quote("AMD", a.entry_ref, signal_date, False, "x",
                      a.entry_ref-1.1, a.entry_ref-0.9, a.entry_ref - 1.0,
                      a.entry_ref, True, "active")}  # last below ref
    # Use a different-day ISO ts so poll_day_et("2026-06-24T14:00:00Z") != last_arm_date
    # ("2026-06-23") and take_entries stays False (same intent as the original "t2").
    paper_loop.advance_poll(book, q, md, ts="2026-06-24T14:00:00Z", state_dir=tmp_path)
    assert "AMD" in book.armed and "AMD" not in book.positions


def test_advance_poll_stops_out_and_ratchets(tmp_path):
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    book.cash = 1800.0
    book.positions["AMD"] = PaperPosition("AMD", 2.0, 100.0, "t", 5.0, 90.0, 100.0, 0, 20.0)
    md = _md(dt.date(2026, 6, 22))
    # Poll 1: price rallies to 120 -> ratchet stop up to 120-15=105 (no fill)
    q1 = {"AMD": Quote("AMD", 100.0, dt.date(2026, 6, 22), False, "x", 119.9, 120.1, 120.0, 100.0, True, "active")}
    paper_loop.advance_poll(book, q1, md, ts="2026-06-23T14:00:00Z", state_dir=tmp_path)
    assert book.positions["AMD"].current_stop == pytest.approx(105.0)
    assert "AMD" in book.positions
    # Poll 2: bid drops to 104 (<=105) -> stop-out at bid - slippage
    q2 = {"AMD": Quote("AMD", 100.0, dt.date(2026, 6, 22), False, "x", 104.0, 104.2, 104.1, 100.0, True, "active")}
    paper_loop.advance_poll(book, q2, md, ts="2026-06-23T15:00:00Z", state_dir=tmp_path)
    assert "AMD" not in book.positions
    assert book.realized_pnl == pytest.approx((104.0*(1-3/1e4) - 100.0) * 2.0)


def test_advance_poll_never_force_sells_on_missing_quote(tmp_path):
    # spec §1.6/§7: an open position with NO usable quote survives the poll un-sold.
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    book.cash = 1800.0
    book.positions["AMD"] = PaperPosition("AMD", 2.0, 100.0, "2026-06-23T14:00:00Z", 5.0,
                                          95.0, 100.0, 0, 20.0)
    n_fills_before = len(book.fills)
    paper_loop.advance_poll(book, {}, _md(dt.date(2026, 6, 22)), ts="2026-06-23T15:00:00Z",
                            state_dir=tmp_path)            # empty quotes => data outage
    assert "AMD" in book.positions                         # NOT force-sold
    assert book.positions["AMD"].current_stop == 95.0      # stop unchanged
    assert len(book.fills) == n_fills_before               # no exit fill appended
    row = json.loads((tmp_path / "equity_curve.jsonl").read_text().splitlines()[-1])
    assert row["n_marked_at_cost"] == 1                    # outage flagged in the curve


def test_advance_poll_never_force_sells_on_unfillable_quote(tmp_path):
    # The bad-data half of the never-force-sell invariant: an open position with a
    # PRESENT but UNFILLABLE quote (crossed bid>ask) whose bid is at/below the stop
    # survives un-sold — bad data must never trip a stop.
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    book.cash = 1800.0
    book.positions["AMD"] = PaperPosition("AMD", 2.0, 100.0, "2026-06-23T14:00:00Z", 5.0,
                                          95.0, 100.0, 0, 20.0)
    n_fills_before = len(book.fills)
    # crossed bid>ask (bid=94, ask=93) => unfillable; bid (94) <= stop (95) WOULD fire
    # a stop on clean data, but the quote is bad, so the position must stand.
    q = {"AMD": Quote("AMD", 100.0, dt.date(2026, 6, 22), False, "x",
                      94.0, 93.0, 94.0, 100.0, True, "active")}
    paper_loop.advance_poll(book, q, _md(dt.date(2026, 6, 22)), ts="2026-06-23T15:00:00Z",
                            state_dir=tmp_path)
    assert "AMD" in book.positions                         # NOT force-sold on bad data
    assert book.positions["AMD"].current_stop == 95.0      # stop unchanged (no ratchet either)
    assert len(book.fills) == n_fills_before               # no exit fill appended
    row = json.loads((tmp_path / "equity_curve.jsonl").read_text().splitlines()[-1])
    assert row["n_marked_at_cost"] == 1                    # unfillable quote flagged


def test_advance_poll_skips_entries_before_arm(tmp_path):
    # Stale-arm ordering guard: a poll dated on a NEW day, BEFORE that day's arm_day,
    # must take NO entries even when the quote would trigger the (stale) armed name.
    signal_date, today = dt.date(2026, 6, 22), dt.date(2026, 6, 23)
    md = _md(signal_date)
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    paper_loop.arm_day(book, md, signal_date=signal_date, today=today)  # arms AMD for day1
    a = book.armed["AMD"]
    cash_before = book.cash
    # a quote that WOULD trigger the armed entry, but the poll ts is day-2 (before re-arm)
    q = {"AMD": Quote("AMD", a.entry_ref, signal_date, False, "x",
                      a.entry_ref+0.9, a.entry_ref+1.1, a.entry_ref + 1.0,
                      a.entry_ref, True, "active")}
    paper_loop.advance_poll(book, q, md, ts="2026-06-24T14:00:00Z", state_dir=tmp_path)
    assert "AMD" not in book.positions                     # stale arm -> no entry taken
    assert book.cash == pytest.approx(cash_before)         # no cash spent
    row = json.loads((tmp_path / "equity_curve.jsonl").read_text().splitlines()[-1])
    assert row["entries_taken"] is False                   # stale-arm skip is visible


def test_no_double_enter_after_crash(tmp_path):
    signal_date, today = dt.date(2026, 6, 22), dt.date(2026, 6, 23)
    md = _md(signal_date)
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    paper_loop.arm_day(book, md, signal_date=signal_date, today=today)
    a = book.armed["AMD"]
    q = {"AMD": Quote("AMD", a.entry_ref, signal_date, False, "x",
                      a.entry_ref+0.9, a.entry_ref+1.1, a.entry_ref + 1.0, a.entry_ref, True, "active")}
    # ts must be ISO-dated `today` so the arm-ordering guard (poll_day == last_arm_date)
    # lets the entry through — the whole point of this crash test is that an entry DID
    # commit before the simulated crash.
    paper_loop.advance_poll(book, q, md, ts="2026-06-23T14:00:00Z", state_dir=tmp_path)
    cash_after = book.cash
    # Simulate crash+restart: reload from disk, re-arm (idempotent), re-poll same day
    reloaded = PaperBook.load(tmp_path)
    paper_loop.arm_day(reloaded, md, signal_date=signal_date, today=today)  # no-op (same day)
    paper_loop.advance_poll(reloaded, q, md, ts="2026-06-23T15:00:00Z", state_dir=tmp_path)
    assert reloaded.cash == pytest.approx(cash_after)        # no second buy
    assert len([f for f in reloaded.fills if f.fill_id == "AMD:entry:2026-06-23"]) == 1


def _arm_open_stop(book, md, signal_date, today, tmp_path):
    """Arm + trigger an entry (poll 1) + force a stop-out (poll 2) for AMD on `today`."""
    paper_loop.arm_day(book, md, signal_date=signal_date, today=today)
    a = book.armed["AMD"]
    day = today.isoformat()
    q1 = {"AMD": Quote("AMD", a.entry_ref, signal_date, False, "x",
                       a.entry_ref+0.9, a.entry_ref+1.1, a.entry_ref+1.0, a.entry_ref, True, "active")}
    paper_loop.advance_poll(book, q1, md, ts=f"{day}T14:00:00Z", state_dir=tmp_path)
    stop = book.positions["AMD"].current_stop
    prev = book.positions["AMD"].entry_price
    # bid below the stop but a small, fillable move (passes the <=50% sanity gate)
    q2 = {"AMD": Quote("AMD", prev, signal_date, False, "x",
                       stop-0.10, stop+0.10, stop-0.05, prev, True, "active")}
    paper_loop.advance_poll(book, q2, md, ts=f"{day}T15:00:00Z", state_dir=tmp_path)


def test_cross_day_stop_out_books_both(tmp_path):
    # The HIGH fix: a name that opens and stops out flat (ratchet_seq=0) on TWO
    # different days must book BOTH exits (distinct date-namespaced stop fill_ids),
    # not silently dedup the second.
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    _arm_open_stop(book, _md(dt.date(2026, 6, 22)), dt.date(2026, 6, 22), dt.date(2026, 6, 23), tmp_path)
    assert "AMD" not in book.positions
    realized_after_day1 = book.realized_pnl
    assert realized_after_day1 < 0                                   # flat stop-out is a small loss
    _arm_open_stop(book, _md(dt.date(2026, 6, 23)), dt.date(2026, 6, 23), dt.date(2026, 6, 24), tmp_path)
    assert "AMD" not in book.positions
    assert book.realized_pnl < realized_after_day1                   # SECOND loss booked too
    stop_fills = [f for f in book.fills if f.intent_type == "stop"]
    assert len(stop_fills) == 2
    assert {f.fill_id for f in stop_fills} == {"AMD:stop:2026-06-23:0", "AMD:stop:2026-06-24:0"}


def test_advance_poll_derives_poll_day_via_et(tmp_path, monkeypatch):
    """advance_poll must route poll_day through schedule_state.poll_day_et (ET),
    not a raw ts[:10] UTC slice. Patch the shared module attr and confirm it's
    consulted with the exact ts."""
    import autotrader_live.paper_loop as pl       # the module under test
    from autotrader_live import schedule_state
    seen = []
    real = schedule_state.poll_day_et
    # No raising=False: at Step 1 (before the Step-3 import lands) pl.schedule_state
    # does not exist, so this errors loudly -> the test is genuinely red first.
    monkeypatch.setattr(pl.schedule_state, "poll_day_et",
                        lambda ts: (seen.append(ts), real(ts))[1])

    signal_date = dt.date(2026, 6, 22)
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    paper_loop.arm_day(book, _md(signal_date), signal_date=signal_date,
                       today=dt.date(2026, 6, 23))
    # Empty quotes: advance_poll still computes poll_day at the top before any
    # fill/ratchet work, so the derivation is exercised without needing a trigger.
    paper_loop.advance_poll(book, {}, _md(signal_date),
                            ts="2026-06-23T14:00:00Z", state_dir=tmp_path)
    assert seen == ["2026-06-23T14:00:00Z"]
