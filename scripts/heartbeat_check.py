#!/usr/bin/env python3
"""LIVE-02 external heartbeat — the death signal that survives a hang of the
loop agent (RUNBOOK_PAPER_BOOK.md "External heartbeat").

MCP-FREE on purpose: it only imports the pure `schedule_state` + `session_clock`
for the RTH gate and reads the `equity_curve.jsonl` last-row date + `book.json`
last_arm_date from file. It makes no broker call, so it keeps working when the
Robinhood connector is gone (the whole point — that is the failure it is meant
to catch). Run it from a scheduled task in its OWN context; the task pings the operator
only when stdout starts with `STALE` or `NOT_ARMED`.

Contract (single stdout line, machine-greppable first token):
  SILENT <reason>    -> outside RTH, pre-arm window, or awaiting first poll: do NOT notify.
  OK <age>           -> curve fresh within the staleness window: do NOT notify.
  STALE <message>    -> curve not written in > STALE_MIN during RTH: NOTIFY the operator.
  NOT_ARMED <message>-> loop not armed past the ARM deadline during RTH: NOTIFY the operator.

Exit code mirrors the token (0 = SILENT/OK, 2 = STALE/NOT_ARMED) for non-LLM callers.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from autotrader_live import schedule_state  # pure, MCP-free (uses session_clock internally)

ET = ZoneInfo("America/New_York")
STATE_DIR = REPO / "data" / "live" / "paper_book"
EQUITY_CURVE = STATE_DIR / "equity_curve.jsonl"
BOOK = STATE_DIR / "book.json"


def _now_et() -> dt.datetime:
    return dt.datetime.now(ET)


def _last_curve_row_date() -> str | None:
    if not EQUITY_CURVE.exists():
        return None
    last = None
    for line in EQUITY_CURVE.read_text().splitlines():
        if line.strip():
            last = line
    if last is None:
        return None
    ts = json.loads(last).get("ts")
    return schedule_state.poll_day_et(ts) if ts else None


def _last_arm_date() -> str | None:
    if not BOOK.exists():
        return None
    return json.loads(BOOK.read_text()).get("last_arm_date")


def evaluate() -> tuple[str, str]:
    now_et = _now_et()
    age_min = (
        (now_et - dt.datetime.fromtimestamp(EQUITY_CURVE.stat().st_mtime, tz=ET)).total_seconds() / 60.0
        if EQUITY_CURVE.exists() else float("inf"))
    return schedule_state.heartbeat_status(
        now_et, age_min=age_min, curve_last_row_date=_last_curve_row_date(),
        last_arm_date=_last_arm_date(), today=now_et.date())


def main() -> int:
    token, message = evaluate()
    print(f"{token} {message}")
    return 2 if token in {"STALE", "NOT_ARMED"} else 0


if __name__ == "__main__":
    raise SystemExit(main())
