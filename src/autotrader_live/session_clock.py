# src/autotrader_live/session_clock.py
"""ET market-session timing for the LIVE-02 loop (the date-only TradingCalendar
has no hours). The AUTHORITATIVE open/halt signal at runtime is the broker quote
`state` (covers unscheduled halts); this clock paces sleeps and the nightly pause.

Consumed by the AGENT LOOP / driver pacing (RUNBOOK), NOT by the pure `paper_loop`
package — its not-wired-into-paper_loop status is intentional, not an oversight.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
_OPEN = dt.time(9, 30)
_CLOSE = dt.time(16, 0)
_EARLY_CLOSE = dt.time(13, 0)

# Known/holiday-adjacent early-close dates (extend as needed each year).
_HALF_DAYS: set[dt.date] = {
    dt.date(2026, 11, 27),   # day after Thanksgiving
    dt.date(2026, 12, 24),   # Christmas Eve
}
# Full market holidays the loop must treat as closed (the date-only calendar also
# omits them; this set is for wall-clock gating when the calendar isn't consulted).
_HOLIDAYS: set[dt.date] = {
    dt.date(2026, 1, 1), dt.date(2026, 1, 19), dt.date(2026, 2, 16),
    dt.date(2026, 4, 3), dt.date(2026, 5, 25), dt.date(2026, 6, 19),
    dt.date(2026, 7, 3), dt.date(2026, 9, 7), dt.date(2026, 11, 26),
    dt.date(2026, 12, 25),
}


def _close_time(d: dt.date) -> dt.time:
    return _EARLY_CLOSE if d in _HALF_DAYS else _CLOSE


def is_regular_session(now_et: dt.datetime) -> bool:
    now_et = now_et.astimezone(ET)   # defensively convert any tz-aware input to ET
    d = now_et.date()
    if d.weekday() >= 5 or d in _HOLIDAYS:
        return False
    return _OPEN <= now_et.timetz().replace(tzinfo=None) < _close_time(d)


def minutes_to_close(now_et: dt.datetime) -> int:
    now_et = now_et.astimezone(ET)   # defensively convert any tz-aware input to ET
    close_dt = dt.datetime.combine(now_et.date(), _close_time(now_et.date()), tzinfo=ET)
    return max(0, int((close_dt - now_et).total_seconds() // 60))


def half_days() -> set[dt.date]:
    """Public accessor for the early-close dates (avoids reaching into _HALF_DAYS)."""
    return set(_HALF_DAYS)
