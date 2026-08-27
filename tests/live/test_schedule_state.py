# tests/live/test_schedule_state.py
import datetime as dt
from zoneinfo import ZoneInfo

from autotrader_live import schedule_state as ss
from autotrader_live.schedule_state import PollAction

ET = ZoneInfo("America/New_York")


def _et(y, mo, d, h, mi):
    return dt.datetime(y, mo, d, h, mi, tzinfo=ET)


def test_poll_day_et_utc_z_during_rth():
    # 20:30 UTC on 2026-06-23 == 16:30 ET same date.
    assert ss.poll_day_et("2026-06-23T20:30:00Z") == "2026-06-23"


def test_poll_day_et_naive_treated_as_utc():
    assert ss.poll_day_et("2026-06-23T14:00:00") == "2026-06-23"


def test_poll_day_et_offset_input():
    assert ss.poll_day_et("2026-06-23T16:30:00-04:00") == "2026-06-23"


def test_in_session_armed_today_is_normal():
    now = _et(2026, 6, 23, 11, 0)
    assert ss.poll_action(now, last_arm_date="2026-06-23",
                          last_poll_ts="2026-06-23T14:45:00Z", eod_done=False,
                          today=dt.date(2026, 6, 23)) is PollAction.NORMAL


def test_in_session_not_armed_self_heals():
    now = _et(2026, 6, 23, 11, 0)
    assert ss.poll_action(now, last_arm_date="2026-06-22",
                          last_poll_ts=None, eod_done=False,
                          today=dt.date(2026, 6, 23)) is PollAction.SELF_HEAL_ARM


def test_after_close_armed_polled_today_not_eod_is_final():
    now = _et(2026, 6, 23, 16, 5)   # after 16:00 close
    assert ss.poll_action(now, last_arm_date="2026-06-23",
                          last_poll_ts="2026-06-23T19:50:00Z", eod_done=False,
                          today=dt.date(2026, 6, 23)) is PollAction.FINAL


def test_after_close_eod_done_exits():
    now = _et(2026, 6, 23, 16, 30)
    assert ss.poll_action(now, last_arm_date="2026-06-23",
                          last_poll_ts="2026-06-23T20:00:00Z", eod_done=True,
                          today=dt.date(2026, 6, 23)) is PollAction.EXIT


def test_after_close_never_polled_today_exits():
    # Asleep all session: armed but no poll landed -> EXIT (EOD audit reports the gap).
    now = _et(2026, 6, 23, 16, 30)
    assert ss.poll_action(now, last_arm_date="2026-06-23",
                          last_poll_ts="2026-06-22T20:00:00Z", eod_done=False,
                          today=dt.date(2026, 6, 23)) is PollAction.EXIT


def test_weekend_exits():
    now = _et(2026, 6, 27, 11, 0)   # Saturday
    assert ss.poll_action(now, last_arm_date=None, last_poll_ts=None,
                          eod_done=False, today=dt.date(2026, 6, 27)) is PollAction.EXIT


def test_exactly_1600_et_is_after_close_final():
    # 16:00 ET is NOT in session (_OPEN <= t < _CLOSE); armed+polled today -> FINAL.
    now = _et(2026, 6, 23, 16, 0)
    assert ss.poll_action(now, last_arm_date="2026-06-23",
                          last_poll_ts="2026-06-23T19:45:00Z", eod_done=False,
                          today=dt.date(2026, 6, 23)) is PollAction.FINAL


def test_half_day_after_early_close_is_final():
    # 2026-11-27 closes 13:00 ET; a 13:30 ET fire is after close -> FINAL.
    now = _et(2026, 11, 27, 13, 30)
    assert ss.poll_action(now, last_arm_date="2026-11-27",
                          last_poll_ts="2026-11-27T17:45:00Z", eod_done=False,
                          today=dt.date(2026, 11, 27)) is PollAction.FINAL


def test_dst_spring_forward_weekday_in_session_normal():
    # 2026-03-09 (Mon after spring-forward) 11:00 ET is RTH; armed -> NORMAL.
    now = _et(2026, 3, 9, 11, 0)
    assert ss.poll_action(now, last_arm_date="2026-03-09",
                          last_poll_ts="2026-03-09T15:00:00Z", eod_done=False,
                          today=dt.date(2026, 3, 9)) is PollAction.NORMAL


def test_dst_fall_back_weekday_in_session_normal():
    # 2026-11-02 (Mon after fall-back) 11:00 ET is RTH; armed -> NORMAL.
    now = _et(2026, 11, 2, 11, 0)
    assert ss.poll_action(now, last_arm_date="2026-11-02",
                          last_poll_ts="2026-11-02T15:00:00Z", eod_done=False,
                          today=dt.date(2026, 11, 2)) is PollAction.NORMAL


def test_heartbeat_outside_rth_silent():
    now = _et(2026, 6, 23, 8, 0)
    tok, _ = ss.heartbeat_status(now, age_min=999, curve_last_row_date=None,
                                 last_arm_date=None, today=dt.date(2026, 6, 23))
    assert tok == "SILENT"


def test_heartbeat_pre_arm_window_silent():
    now = _et(2026, 6, 23, 9, 35)   # RTH open, before ARM deadline (10:00 ET)
    tok, _ = ss.heartbeat_status(now, age_min=999, curve_last_row_date="2026-06-22",
                                 last_arm_date="2026-06-22", today=dt.date(2026, 6, 23))
    assert tok == "SILENT"


def test_heartbeat_not_armed_past_deadline_alerts():
    now = _et(2026, 6, 23, 10, 30)  # past ARM deadline, still not armed today
    tok, msg = ss.heartbeat_status(now, age_min=5, curve_last_row_date="2026-06-22",
                                   last_arm_date="2026-06-22", today=dt.date(2026, 6, 23))
    assert tok == "NOT_ARMED" and "armed" in msg.lower()


def test_heartbeat_armed_fresh_is_ok():
    now = _et(2026, 6, 23, 11, 0)
    tok, _ = ss.heartbeat_status(now, age_min=5, curve_last_row_date="2026-06-23",
                                 last_arm_date="2026-06-23", today=dt.date(2026, 6, 23))
    assert tok == "OK"


def test_heartbeat_armed_stale_alerts():
    now = _et(2026, 6, 23, 13, 0)
    tok, msg = ss.heartbeat_status(now, age_min=30, curve_last_row_date="2026-06-23",
                                   last_arm_date="2026-06-23", today=dt.date(2026, 6, 23))
    assert tok == "STALE" and "30" in msg


def test_heartbeat_armed_no_first_poll_past_grace_alerts():
    # Armed but the first poll never landed and we are past the first-poll grace.
    now = _et(2026, 6, 23, 11, 0)   # past first-poll deadline (10:30 ET)
    tok, _ = ss.heartbeat_status(now, age_min=90, curve_last_row_date="2026-06-22",
                                 last_arm_date="2026-06-23", today=dt.date(2026, 6, 23))
    assert tok == "STALE"


def test_heartbeat_armed_no_first_poll_within_grace_silent():
    now = _et(2026, 6, 23, 10, 5)   # armed, before first-poll deadline
    tok, _ = ss.heartbeat_status(now, age_min=10, curve_last_row_date="2026-06-22",
                                 last_arm_date="2026-06-23", today=dt.date(2026, 6, 23))
    assert tok == "SILENT"


def test_expected_rth_slots_full_day_matches_poll_schedule():
    # The expected grid MUST match the actual cron (POLL_SLOTS_ET): first fire
    # 09:45 ET (just after the 09:40 ARM), every 15 min, plus the 15:55 close bell.
    slots = ss.expected_rth_slots(dt.date(2026, 6, 23))
    assert slots[0] == _et(2026, 6, 23, 9, 45)
    assert slots[-1] == _et(2026, 6, 23, 15, 55)   # dedicated close-bell fire
    assert _et(2026, 6, 23, 15, 45) in slots
    assert len(slots) == 26                          # 09:45..15:45 (25) + 15:55

def test_expected_rth_slots_half_day():
    # 2026-11-27 closes 13:00 ET: 09:45..12:45, no 15:55 close bell.
    slots = ss.expected_rth_slots(dt.date(2026, 11, 27))
    assert slots[0] == _et(2026, 11, 27, 9, 45)
    assert slots[-1] == _et(2026, 11, 27, 12, 45)
    assert len(slots) == 13

def test_slot_gap_counts_missing():
    rows_ts = ["2026-06-23T13:45:00Z", "2026-06-23T14:00:00Z"]  # 09:45, 10:00 ET
    now = _et(2026, 6, 23, 16, 5)
    gap = ss.slot_gap(rows_ts, today=dt.date(2026, 6, 23), now_et=now)
    assert gap["expected"] == 26 and gap["actual"] == 2 and gap["missing"] == 24

def test_slot_gap_zero_when_complete():
    slots = ss.expected_rth_slots(dt.date(2026, 6, 23))
    rows_ts = [s.astimezone(dt.timezone.utc).isoformat() for s in slots]
    gap = ss.slot_gap(rows_ts, today=dt.date(2026, 6, 23),
                      now_et=_et(2026, 6, 23, 16, 5))
    assert gap["missing"] == 0

def test_slot_gap_zero_poll_day_reports_full_gap():
    # Slept through the whole session: no rows -> missing == expected (honest).
    gap = ss.slot_gap([], today=dt.date(2026, 6, 23), now_et=_et(2026, 6, 23, 16, 5))
    assert gap["actual"] == 0 and gap["missing"] == gap["expected"] == 26
