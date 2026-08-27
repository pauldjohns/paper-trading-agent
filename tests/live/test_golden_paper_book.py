# tests/live/test_golden_paper_book.py
import importlib.util
import json
import math
from pathlib import Path

from autotrader_live.paper_book import PaperBook

FIX = Path(__file__).parent / "fixtures" / "golden_paper_book"


def _load_replay():
    """Load replay() from the sibling scenario module by PATH (not package import),
    matching the repo convention of resolving fixtures relative to the test file
    (commit 2f3a7b2) — robust regardless of pytest rootdir / __init__ presence."""
    path = Path(__file__).parent / "_paper_book_scenario.py"
    spec = importlib.util.spec_from_file_location("_paper_book_scenario", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.replay


def test_golden_book_byte_match(tmp_path):
    """Replay the scripted poll sequence; book.json must byte-match the frozen golden."""
    _load_replay()(tmp_path)
    produced = (tmp_path / "book.json").read_text()
    expected = (FIX / "book.json").read_text()
    assert produced == expected, "book.json drifted from the frozen golden"


def test_golden_equity_curve_structural(tmp_path):
    _load_replay()(tmp_path)
    prod = [json.loads(l) for l in (tmp_path / "equity_curve.jsonl").read_text().splitlines() if l.strip()]
    exp = [json.loads(l) for l in (FIX / "equity_curve.jsonl").read_text().splitlines() if l.strip()]
    assert len(prod) == len(exp)
    for p, e in zip(prod, exp):
        assert math.isclose(p["total_equity"], e["total_equity"], rel_tol=0, abs_tol=1e-9)


def test_golden_includes_winning_ratchet_stop_exit(tmp_path):
    """Spec §7: a winner exiting on a ratchet-stop touch realizes BELOW the stop
    (sell-side spread crossed). Asserts the scenario actually exercises that path."""
    _load_replay()(tmp_path)
    book = PaperBook.load(tmp_path)
    stop_fills = [f for f in book.fills if f.intent_type == "stop"]
    assert stop_fills, "scenario must include at least one stop exit"
    # the scenario's winning exit books a positive realized_pnl_delta on a ratcheted stop
    assert any(f.realized_pnl_delta > 0 for f in stop_fills), "scenario must include a winning ratchet-stop exit"
    # a winning exit's fill_id carries a non-zero ratchet_seq (the stop was raised before the touch)
    assert any(f.fill_id.rsplit(":", 1)[-1] != "0" for f in stop_fills if f.realized_pnl_delta > 0)
