# src/autotrader_live/schedule_state.py
"""Pure, MCP-free scheduling decisions for the LIVE-03 headless loop.

The scheduled-task agents make the broker MCP reads; this module makes every
*decidable* call (what to do this poll, is the loop alive, where are the gaps)
so the logic is unit-tested rather than buried in a prompt. NO broker calls,
NO file I/O — callers pass facts in, get a decision out.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from zoneinfo import ZoneInfo

from autotrader_live import session_clock  # pure, MCP-free

ET = ZoneInfo("America/New_York")


class PollAction(str, Enum):
    NORMAL = "NORMAL"                # in session, armed today -> normal poll
    FINAL = "FINAL"                  # after close, armed+polled today, EOD not done -> last poll
    SELF_HEAL_ARM = "SELF_HEAL_ARM"  # in session but not armed today -> run ARM first
    EXIT = "EXIT"                    # nothing to do


def poll_day_et(ts_iso: str) -> str:
    """ET calendar date (YYYY-MM-DD) for a poll timestamp.

    Accepts 'Z', explicit offset, or naive ISO. Naive is treated as UTC (the
    agent passes UTC 'Z' timestamps). Deriving from ET — not a raw ``ts[:10]``
    UTC slice — is the spec's defensive guard against a malformed/local ts.
    """
    t = dt.datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=dt.timezone.utc)
    return t.astimezone(ET).date().isoformat()


def poll_action(now_et: dt.datetime, *, last_arm_date: str | None,
                last_poll_ts: str | None, eod_done: bool,
                today: dt.date) -> PollAction:
    """Decide what this POLL fire should do. See PollAction."""
    today_iso = today.isoformat()
    armed_today = (last_arm_date == today_iso)

    if session_clock.is_regular_session(now_et):
        return PollAction.NORMAL if armed_today else PollAction.SELF_HEAL_ARM

    # Outside the session: only the close-bell final poll is allowed, and only
    # once, and only on a day we actually traded (a poll already landed today).
    polled_today = bool(last_poll_ts) and poll_day_et(last_poll_ts) == today_iso
    if armed_today and polled_today and not eod_done:
        return PollAction.FINAL
    return PollAction.EXIT


STALE_MIN = 20.0                       # RUNBOOK: curve stale > ~20 min in RTH => loop likely dead
_ARM_DEADLINE = dt.time(10, 0)         # ~09:40 ET ARM cron + grace; past this & un-armed -> alert
_FIRST_POLL_DEADLINE = dt.time(10, 30) # armed but no poll row by here -> the loop died between arm & poll


def heartbeat_status(now_et: dt.datetime, *, age_min: float,
                     curve_last_row_date: str | None, last_arm_date: str | None,
                     today: dt.date, stale_min: float = STALE_MIN
                     ) -> tuple[str, str]:
    """Liveness decision. Returns (token, message); token in
    {SILENT, OK, STALE, NOT_ARMED}. MCP-free — the caller supplies the curve's
    last-row date + file age and the book's last_arm_date (all file reads).
    """
    today_iso = today.isoformat()
    now_t = now_et.astimezone(ET).timetz().replace(tzinfo=None)

    if not session_clock.is_regular_session(now_et):
        return ("SILENT", "outside-RTH (no heartbeat expected)")

    if last_arm_date != today_iso:
        if now_t >= _ARM_DEADLINE:
            return ("NOT_ARMED",
                    f"paper book NOT ARMED today ({today_iso}) past "
                    f"{_ARM_DEADLINE:%H:%M} ET - ARM task may have failed")
        return ("SILENT", "pre-arm window (before ARM deadline)")

    # Armed today.
    if curve_last_row_date == today_iso:
        if age_min > stale_min:
            return ("STALE",
                    f"equity_curve last written {age_min:.0f} min ago during RTH "
                    f"({now_et:%H:%M ET}) - loop may be dead")
        return ("OK", f"equity_curve fresh ({age_min:.0f} min old, {now_et:%H:%M ET})")

    # Armed but today's first poll row has not landed yet.
    if now_t >= _FIRST_POLL_DEADLINE:
        return ("STALE",
                f"armed today but no poll row landed by {now_t:%H:%M} ET - "
                f"loop died between ARM and first POLL")
    return ("SILENT", "armed, awaiting first poll")


# SINGLE SOURCE OF TRUTH for the poll schedule. The automation/README.md cron is
# hand-derived from these (cross-referenced there); expected_rth_slots derives the
# audit grid from them so the slot-gap audit has ZERO phantom gaps on a clean day.
POLL_FIRST_ET = dt.time(9, 45)        # first fire, just after the 09:40 ET ARM
POLL_CADENCE_MIN = 15
POLL_CLOSE_BELL_ET = dt.time(15, 55)  # dedicated close-bell fire (full days only)


def expected_rth_slots(day: dt.date, *, cadence_min: int = POLL_CADENCE_MIN
                       ) -> list[dt.datetime]:
    """ET datetimes of every expected poll fire for `day`: POLL_FIRST_ET..<close at
    cadence, plus the close-bell fire on full days. Half-days (13:00 ET close,
    honored via session_clock) have no dedicated close-bell fire — that case is
    covered only by the FINAL boundary poll (documented limitation)."""
    half = day in session_clock.half_days()
    close_t = dt.time(13, 0) if half else dt.time(16, 0)
    first = dt.datetime.combine(day, POLL_FIRST_ET, tzinfo=ET)
    close_dt = dt.datetime.combine(day, close_t, tzinfo=ET)
    slots, t = [], first
    while t < close_dt:
        slots.append(t)
        t += dt.timedelta(minutes=cadence_min)
    if not half:
        slots.append(dt.datetime.combine(day, POLL_CLOSE_BELL_ET, tzinfo=ET))
    return slots


def slot_gap(curve_row_ts: list[str], *, today: dt.date, now_et: dt.datetime
             ) -> dict:
    """Diff expected RTH slots (up to now) vs actual equity_curve rows for `today`.
    Returns {expected, actual, missing}. A poll gap defers a stop-check — honest,
    not hidden."""
    expected = [s for s in expected_rth_slots(today) if s <= now_et]
    actual = sum(1 for ts in curve_row_ts if poll_day_et(ts) == today.isoformat())
    return {"expected": len(expected), "actual": actual,
            "missing": max(0, len(expected) - actual)}
