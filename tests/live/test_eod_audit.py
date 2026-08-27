import datetime as dt
import importlib.util
import json
import subprocess
from pathlib import Path
from zoneinfo import ZoneInfo

_EOD_PATH = Path(__file__).resolve().parents[2] / "scripts" / "eod_audit.py"

def _load():
    spec = importlib.util.spec_from_file_location("eod_audit", _EOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

def test_eod_audit_commits_book_to_data_worktree_and_reports_gap(tmp_path, monkeypatch):
    mod = _load()
    state = tmp_path / "paper_book"
    state.mkdir(parents=True)
    (state / "book.json").write_text(json.dumps({"last_arm_date": "2026-06-23"}))
    (state / "equity_curve.jsonl").write_text(
        json.dumps({"ts": "2026-06-23T13:30:00Z", "total_equity": 2000.0}) + "\n")
    (state / "fills.jsonl").write_text("")
    # A throwaway git repo standing in for the paper-book-data worktree.
    wt = tmp_path / "book-data"
    wt.mkdir()
    subprocess.run(["git", "-C", str(wt), "init", "-q"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(wt), "config", "user.name", "t"], check=True)
    monkeypatch.setattr(mod, "STATE_DIR", state)
    monkeypatch.setattr(mod, "BOOK", state / "book.json")
    monkeypatch.setattr(mod, "EQUITY_CURVE", state / "equity_curve.jsonl")
    monkeypatch.setattr(mod, "FILLS", state / "fills.jsonl")
    monkeypatch.setattr(mod, "BOOK_DATA_WORKTREE", wt)
    monkeypatch.setattr(mod, "_now_et", lambda: dt.datetime(2026, 6, 23, 16, 5, tzinfo=ZoneInfo("America/New_York")))
    summary = mod.run(push=False)                          # no remote in the test
    assert (wt / "book.json").exists()                     # snapshot copied into the data worktree
    log = subprocess.run(["git", "-C", str(wt), "log", "--oneline"],
                         capture_output=True, text=True).stdout
    assert "EOD 2026-06-23" in log                         # committed to the data branch
    assert summary["missing"] == 25                        # 26 expected by 16:05, 1 actual
    assert "simulator-only" in summary["label"].lower()
