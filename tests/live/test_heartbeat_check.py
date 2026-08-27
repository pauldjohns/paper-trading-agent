"""The MCP-free heartbeat script, rewired onto schedule_state.heartbeat_status."""
import datetime as dt
import importlib.util
import json
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
_HB_PATH = Path(__file__).resolve().parents[2] / "scripts" / "heartbeat_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("heartbeat_check", _HB_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_book(state_dir: Path, last_arm_date: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "book.json").write_text(json.dumps({"last_arm_date": last_arm_date}))


def _write_curve(state_dir: Path, ts: str) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "equity_curve.jsonl").write_text(json.dumps({"ts": ts}) + "\n")


def test_not_armed_today_returns_not_armed(tmp_path, monkeypatch):
    mod = _load()
    state = tmp_path / "paper_book"
    _write_book(state, last_arm_date="2026-06-22")
    _write_curve(state, "2026-06-22T20:00:00Z")
    monkeypatch.setattr(mod, "STATE_DIR", state)
    monkeypatch.setattr(mod, "EQUITY_CURVE", state / "equity_curve.jsonl")
    monkeypatch.setattr(mod, "BOOK", state / "book.json")
    # 2026-06-23 10:30 ET, past the ARM deadline, not armed today.
    monkeypatch.setattr(mod, "_now_et", lambda: dt.datetime(2026, 6, 23, 10, 30, tzinfo=ET))
    token, _ = mod.evaluate()
    assert token == "NOT_ARMED"
