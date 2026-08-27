#!/usr/bin/env python
"""Regenerate the frozen golden for the paper-book replay lock (P3.5).

Deletes + recreates tests/live/fixtures/golden_paper_book/, runs the scripted
`replay()` into it, and leaves book.json + fills.jsonl + equity_curve.jsonl there.

The rmtree is REQUIRED: equity_curve.jsonl is append-only and never rewritten, so
without a clean slate a second regen double-appends the curve and corrupts the
golden. Run ONCE, eyeball the numbers against the hand-math in _paper_book_scenario.py,
then commit the fixtures.

Usage (from the repo root, with the project venv):
    PYTHONPATH=src .venv/bin/python scripts/regen_paper_book_golden.py
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
_SCENARIO = _REPO_ROOT / "tests" / "live" / "_paper_book_scenario.py"
_FIX = _REPO_ROOT / "tests" / "live" / "fixtures" / "golden_paper_book"


def _load_replay():
    if str(_SRC) not in sys.path:
        sys.path.insert(0, str(_SRC))
    spec = importlib.util.spec_from_file_location("_paper_book_scenario", _SCENARIO)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.replay


def main() -> None:
    replay = _load_replay()
    if _FIX.exists():
        shutil.rmtree(_FIX)
    _FIX.mkdir(parents=True, exist_ok=True)
    replay(_FIX)
    print(f"Regenerated golden fixtures in {_FIX}")
    for name in ("book.json", "fills.jsonl", "equity_curve.jsonl"):
        p = _FIX / name
        print(f"  {name}: {'OK' if p.exists() else 'MISSING'} ({p.stat().st_size if p.exists() else 0} bytes)")


if __name__ == "__main__":
    main()
