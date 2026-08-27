#!/usr/bin/env python3
"""LIVE-03 end-of-day: slot-gap audit + off-machine book backup (git) + EOD summary.

MCP-free. Run from the EOD scheduled task (~16:45 ET, after the FINAL poll). Diffs
expected RTH poll slots vs actual equity_curve rows (a sleep/app-closed gap is
visible same-day), snapshots the book to a PRIVATE GitHub data branch (the record
otherwise lives only on the laptop), and prints a one-line EOD summary the task
pushes to the operator.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from autotrader_live import schedule_state  # noqa: E402

ET = ZoneInfo("America/New_York")
STATE_DIR = REPO / "data" / "live" / "paper_book"
BOOK = STATE_DIR / "book.json"
EQUITY_CURVE = STATE_DIR / "equity_curve.jsonl"
FILLS = STATE_DIR / "fills.jsonl"
DATA_BRANCH = "paper-book-data"
# A ONE-TIME git worktree of the PRIVATE repo checked out on the orphan
# paper-book-data branch (see RUNBOOK setup). Committing here never touches the
# loop's working branch — the off-machine record lives on GitHub (private).
BOOK_DATA_WORKTREE = Path(os.environ.get(
    "BOOK_DATA_WORKTREE", Path.home() / "auto-trader-book-data"))
LABEL = "live-money account, simulator-only, orders forbidden; neg-to-breakeven after costs"


def _now_et() -> dt.datetime:
    return dt.datetime.now(ET)


def backup_to_git(date_iso: str, *, worktree: Path | None = None, push: bool = True) -> str:
    """Snapshot book.json/equity_curve.jsonl/fills.jsonl into the data worktree,
    commit, and (optionally) push origin/paper-book-data. Idempotent — a no-change
    EOD commits nothing (and does not fail)."""
    wt = Path(worktree or BOOK_DATA_WORKTREE)
    for f in (BOOK, EQUITY_CURVE, FILLS):
        if f.exists():
            shutil.copy2(f, wt / f.name)
    subprocess.run(["git", "-C", str(wt), "add", "-A"], check=True)
    r = subprocess.run(["git", "-C", str(wt), "commit", "-m", f"EOD {date_iso}"],
                       capture_output=True, text=True)
    if r.returncode != 0 and "nothing to commit" not in (r.stdout + r.stderr):
        raise RuntimeError(f"git commit failed: {r.stderr.strip()}")
    if push:
        subprocess.run(["git", "-C", str(wt), "push", "origin", DATA_BRANCH], check=True)
    return str(wt)


def run(*, push: bool = True) -> dict:
    now_et = _now_et()
    today = now_et.date()
    rows = [json.loads(l) for l in EQUITY_CURVE.read_text().splitlines() if l.strip()] \
        if EQUITY_CURVE.exists() else []
    gap = schedule_state.slot_gap([r["ts"] for r in rows], today=today, now_et=now_et)
    last = rows[-1] if rows else {}
    backup = backup_to_git(today.isoformat(), push=push)
    return {**gap,
            "total_equity": last.get("total_equity"),
            "realized_pnl_cum": last.get("realized_pnl_cum"),
            "backup": backup, "label": LABEL}


def main() -> int:
    s = run()
    print(f"EOD {s['label']}")
    print(f"slots expected={s['expected']} actual={s['actual']} missing={s['missing']}")
    print(f"total_equity={s['total_equity']} realized_pnl_cum={s['realized_pnl_cum']}")
    print(f"book pushed -> {DATA_BRANCH} ({s['backup']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
