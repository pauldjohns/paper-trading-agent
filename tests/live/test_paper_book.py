# tests/live/test_paper_book.py
import dataclasses
import datetime as dt
import json
import pytest
from autotrader_live.paper_book import PaperPosition


def _pos(**kw):
    base = dict(symbol="AMD", shares=2.0, entry_price=100.0, entry_ts="2026-06-23T14:00:00Z",
               atr_at_entry=5.0, current_stop=90.0, highest_high_since_entry=100.0,
               ratchet_seq=0, cost_tier_bps=20.0)
    base.update(kw)
    return PaperPosition(**base)


def test_paper_position_valid():
    p = _pos()
    assert p.symbol == "AMD" and p.shares == 2.0 and p.current_stop == 90.0


@pytest.mark.parametrize("field,bad", [
    ("shares", 0.0), ("shares", -1.0), ("entry_price", 0.0),
    ("current_stop", 0.0), ("current_stop", -5.0), ("atr_at_entry", 0.0),
])
def test_paper_position_guards(field, bad):
    with pytest.raises(ValueError):
        _pos(**{field: bad})


from autotrader_live.paper_book import ArmedEntry, Fill

def test_armed_entry_and_fill():
    a = ArmedEntry(symbol="AMD", entry_ref=101.0, ref_basis="breakout",
                   target_notional=200.0, atr_at_arm=5.0, cost_tier_bps=20.0,
                   arm_date="2026-06-23")
    assert a.ref_basis == "breakout"
    f = Fill(fill_id="AMD:entry:2026-06-23", ts="2026-06-23T14:00:00Z", symbol="AMD",
             side="buy", intent_type="entry", price=101.5, shares=1.97, notional=200.0,
             entry_ref=101.0, ref_basis="breakout", bid=101.4, ask=101.6,
             last_trade_price=101.5, previous_close=100.0, spread=0.2,
             cost_tier_bps=20.0, realized_pnl_delta=0.0)
    assert f.fill_id == "AMD:entry:2026-06-23" and f.realized_pnl_delta == 0.0


from autotrader_live.paper_book import PaperBook

def test_book_new_and_roundtrip(tmp_path):
    book = PaperBook.new(start_capital=2000.0, start_ts="2026-06-23T13:30:00Z",
                         token_issue_ts="2026-06-23T13:00:00Z")
    assert book.cash == 2000.0 and book.realized_pnl == 0.0
    book.save(tmp_path)
    assert (tmp_path / "book.json").exists()
    reloaded = PaperBook.load(tmp_path)
    assert reloaded.cash == 2000.0
    assert reloaded.start_capital == 2000.0
    assert reloaded.data_source == "robinhood_mcp_live"

def test_book_load_missing_returns_none(tmp_path):
    assert PaperBook.load(tmp_path) is None

def test_book_save_is_deterministic(tmp_path):
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    book.save(tmp_path)
    first = (tmp_path / "book.json").read_text()
    book2 = PaperBook.load(tmp_path)
    book2.save(tmp_path)
    assert (tmp_path / "book.json").read_text() == first


from autotrader_live.paper_book import BookSnapshot
from autotrader_live.mcp_live import Quote

def _quote(sym, last):
    return Quote(symbol=sym, settled_close=last, settled_close_date=dt.date(2026,6,22),
                 settled_close_interpolated=False, settled_close_source="x",
                 bid=last-0.05, ask=last+0.05, last_trade_price=last,
                 previous_close=last, has_traded=True, state="active")

def test_mark_to_market():
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    book.cash = 1800.0
    book.positions["AMD"] = PaperPosition(symbol="AMD", shares=2.0, entry_price=100.0,
        entry_ts="t", atr_at_entry=5.0, current_stop=90.0,
        highest_high_since_entry=100.0, ratchet_seq=0, cost_tier_bps=20.0)
    snap = book.mark_to_market({"AMD": _quote("AMD", 110.0)})
    assert snap.positions_mv == pytest.approx(220.0)        # 2 * 110
    assert snap.total_equity == pytest.approx(2020.0)       # 1800 + 220
    assert snap.unrealized_pnl == pytest.approx(20.0)       # 2 * (110 - 100)
    assert snap.n_positions == 1
    assert snap.n_marked_at_cost == 0

def test_mark_to_market_missing_quote_marks_at_cost():
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    book.cash = 1800.0
    book.positions["AMD"] = PaperPosition(symbol="AMD", shares=2.0, entry_price=100.0,
        entry_ts="t", atr_at_entry=5.0, current_stop=90.0,
        highest_high_since_entry=100.0, ratchet_seq=0, cost_tier_bps=20.0)
    snap = book.mark_to_market({})  # no quote
    assert snap.positions_mv == pytest.approx(200.0)        # marked at cost
    assert snap.unrealized_pnl == pytest.approx(0.0)
    assert snap.n_marked_at_cost == 1                       # outage is auditable, not hidden


def test_mark_to_market_unfillable_quote_marks_at_cost():
    # A PRESENT but UNFILLABLE quote (halted / not-traded) with a positive last
    # ABOVE entry must NOT bank a fabricated gain — mark at cost, flag the outage.
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    book.cash = 1800.0
    book.positions["AMD"] = PaperPosition(symbol="AMD", shares=2.0, entry_price=100.0,
        entry_ts="t", atr_at_entry=5.0, current_stop=90.0,
        highest_high_since_entry=100.0, ratchet_seq=0, cost_tier_bps=20.0)
    halted = Quote(symbol="AMD", settled_close=110.0, settled_close_date=dt.date(2026,6,22),
                   settled_close_interpolated=False, settled_close_source="x",
                   bid=109.95, ask=110.05, last_trade_price=110.0, previous_close=110.0,
                   has_traded=False, state="halted")  # present but NOT fillable
    snap = book.mark_to_market({"AMD": halted})
    assert snap.positions_mv == pytest.approx(200.0)        # marked at cost (2 * 100), not 220
    assert snap.unrealized_pnl == pytest.approx(0.0)        # fabricated +20 gain NOT recorded
    assert snap.n_marked_at_cost == 1                       # unfillable quote is auditable


def _fill(fill_id, side, intent, price, shares, realized=0.0, sym="AMD"):
    return Fill(fill_id=fill_id, ts="t", symbol=sym, side=side, intent_type=intent,
                price=price, shares=shares, notional=price*shares, entry_ref=None,
                ref_basis=None, bid=price, ask=price, last_trade_price=price,
                previous_close=price, spread=0.0, cost_tier_bps=20.0,
                realized_pnl_delta=realized)

def test_apply_entry_then_exit():
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    book.armed["AMD"] = ArmedEntry("AMD", 99.0, "near_high", 200.0, 5.0, 20.0, "2026-06-23")
    pos = PaperPosition(symbol="AMD", shares=2.0, entry_price=100.0, entry_ts="t",
        atr_at_entry=5.0, current_stop=90.0, highest_high_since_entry=100.0,
        ratchet_seq=0, cost_tier_bps=20.0)
    book.apply_entry(pos, _fill("AMD:entry:2026-06-23", "buy", "entry", 100.0, 2.0))
    assert book.cash == pytest.approx(1800.0)
    assert "AMD" in book.positions and "AMD" in book.filled_today
    assert "AMD" not in book.armed
    # idempotent: re-applying the same fill_id is a no-op
    book.apply_entry(pos, _fill("AMD:entry:2026-06-23", "buy", "entry", 100.0, 2.0))
    assert book.cash == pytest.approx(1800.0)
    # exit at 110 -> realized +20, cash back
    book.apply_exit(_fill("AMD:stop:0", "sell", "stop", 110.0, 2.0, realized=20.0))
    assert book.cash == pytest.approx(2020.0)
    assert book.realized_pnl == pytest.approx(20.0)
    assert "AMD" not in book.positions

def test_replace_position_ratchet():
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    pos = PaperPosition("AMD", 2.0, 100.0, "t", 5.0, 90.0, 100.0, 0, 20.0)
    book.positions["AMD"] = pos
    higher = dataclasses.replace(pos, current_stop=95.0, ratchet_seq=1, highest_high_since_entry=110.0)
    book.replace_position(higher)
    assert book.positions["AMD"].current_stop == 95.0


def test_reconcile_logs_appends_missing(tmp_path):
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    pos = PaperPosition("AMD", 2.0, 100.0, "t", 5.0, 90.0, 100.0, 0, 20.0)
    book.apply_entry(pos, _fill("AMD:entry:2026-06-23", "buy", "entry", 100.0, 2.0))
    book.save(tmp_path)  # writes book.json AND fills.jsonl
    lines = (tmp_path / "fills.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    # Simulate a crash where fills.jsonl was truncated but book.json kept the fill
    (tmp_path / "fills.jsonl").write_text("")
    reloaded = PaperBook.load(tmp_path)              # load reconciles
    lines2 = (tmp_path / "fills.jsonl").read_text().strip().splitlines()
    assert len(lines2) == 1                          # re-appended from book.json
    assert json.loads(lines2[0])["fill_id"] == "AMD:entry:2026-06-23"
