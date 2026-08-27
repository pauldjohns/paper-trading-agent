# Scheduled-poll autonomy (LIVE-03) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the session-bound LIVE-02 paper loop into a hands-off, headless, scheduled-poll runner (ARM + POLL + HEARTBEAT + EOD recurring tasks) that is paper-only and broker read-only, with real orders **structurally impossible** via account lockdown — accruing an honest forward record, not alpha.

**Architecture:** Each scheduled task is a recurring headless Claude agent that (a) makes the broker MCP **read** calls, (b) writes raw JSON verbatim, and (c) drives the existing pure `scripts/run_paper_book.py` over `book.json`. All *decidable* logic (poll state machine, ET poll-day, heartbeat liveness, slot-gap audit) is pushed into a new pure, MCP-free module `src/autotrader_live/schedule_state.py` so it is unit-testable; the task prompts are thin orchestration shells that call tested helpers and are validated by the widened no-place invariant scan + the manual dry-run gate. The catastrophic path (a real order) is closed at the **account layer**: the loop records the non-order-capable account `987654321`, and the operator disables agentic on `123456789`.

**Tech Stack:** Python 3.11 (run via `.venv/bin/python`), pytest, `zoneinfo`, the existing `autotrader_live` package + `scripts/run_paper_book.py` driver, the platform `scheduled-tasks` MCP (recurring cron) + `PushNotification`.

---

## ⚠️ READ BEFORE EXECUTING

**Environment (non-negotiable):**
- Run ALL python/pytest with `.venv/bin/python` (3.11). Bare `python` is 3.9 → ~100 false failures.
- Shell cwd RESETS between calls — always use absolute paths.
- Work in the worktree `<worktree>` on branch `claude/live-03-autonomy` (already created off `origin/main`; inherits the OHLCV cache + venv). A fresh worktree has NO `data/cache/*.parquet` → ~100 false failures.
- Baseline at start: **653 live tests** collected under `tests/live/` (`pytest tests/live/ -q --collect-only`).

**Protected artifacts — DO NOT TOUCH (regenerate only with explicit authorization):**
- `src/autotrader/**` — the frozen offline engine, strategy params, `STRATEGY_COST_FLOORS`. The firewall holds: `git diff origin/main -- src/autotrader/` must stay **EMPTY** at every commit.
- Strategy knobs in `src/autotrader_live/paper_loop.py` (`NEAR_THRESHOLD`, `F`, `K`, `PER_NAME_CAP_FRAC`, `TOP_N`, `M`, `BLACKOUT_DAYS`, `SLIPPAGE_BPS`) — FROZEN.
- Golden fixtures: `tests/fixtures/golden_*.json`, `tests/live/fixtures/golden_paper_book/*`, `tests/live/fixtures/golden_trend_decisions.json`. **Task 5 touches `advance_poll` and MUST prove the paper-book golden is byte-unchanged; if it changes, STOP and escalate — do not regenerate.**
- `PaperBook` serialization (`to_dict`/`from_dict` in `src/autotrader_live/paper_book.py`) — do NOT add fields. The `eod_done` state lives in a SEPARATE marker file, never in `book.json` (Task 6).

**Honest framing (carry into every artifact):** the strategy is neg-to-breakeven after costs. The deliverable is the **safe autonomy harness + an honest forward record**, not edge. Every curve view/notification must say so.

**Hard safety constraint:** the package, the loop scripts, and the task prompts must NEVER reference any order-surface tool: `place_equity_order`, `cancel_equity_order`, `place_option_order`, `cancel_option_order`, `review_equity_order`, `review_option_order`. The autonomous loop must NOT be armed for real until the GATING checklist (Task 14) passes: (1) agentic disabled on `123456789`, AND (2) a dry-run confirms per-task tool-approval is strictly per-tool AND the full read surface auto-fires.

---

## Spec gaps / contradictions surfaced while planning (DECISIONS FOR PAUL)

These were found by reading the code against the spec. Each has a recommended default baked into the plan; flag if you disagree.

1. **The "~95h wall-clock token guard" does not exist in code.** Repo-wide grep finds no active wall-clock token gate in the LIVE-02 loop; `PaperBook.token_issue_ts` is plumbed but never used as a gate (the driver passes `token_issue_ts=None`). So "drop the guard" is **documentation + prompt design**, not code removal. **Default:** Task 14 records the decision in the RUNBOOK (death signal = broker-auth-failure, surfaced by the heartbeat + the ARM/POLL prompts' auth-error handling), leaves the unused `token_issue_ts` field untouched (removing it would touch `PaperBook` serialization → golden risk), and does **not** build the optional "~90h soft morning warning" (YAGNI — spec marks it optional).

2. **`test_no_place_invariant` widening collides with `run_paper_monitor_live.py` — REVISED to fail-closed after review.** The existing `tests/live/test_run_paper_book.py::test_no_place_invariant_in_driver` deliberately scopes the scan to `run_paper_book.py` only, because a `scripts/`-wide glob trips on `scripts/run_paper_monitor_live.py`, which names `review_equity_order` (line 9) **and** hard-codes the order-capable account `ACCOUNT = "123456789"` (line 39) and imports `PaperBroker` (it is the superseded LIVE-01 supervised review-monitor; dead under account-lockdown; NOT one of the autonomous tasks). The first plan draft used an *allowlist* of the loop surface; **reviewer A argued that fails open over time** (a future order-touching script added under `scripts/` silently escapes an explicit allowlist), and `run_paper_monitor_live.py` is the one committed script pairing the order-review tool with the order-capable account, invisible to every scan. **REVISED default (Tasks 12b + 13):** scan **all of `scripts/*.py`** (fail-closed — any new order-touching script trips it by default) and resolve the single true positive by **relocating `run_paper_monitor_live.py` → `scripts/legacy/`** (a directory the scan excludes) with a "superseded, non-arming" header. Reviewer C preferred the allowlist (don't relocate a committed file); the fail-closed construction wins because the project's whole premise is "structurally impossible *over time*," and the relocation also retires the one script that names an order tool against the order-capable account. **DECIDED (the operator, 2026-06-24): fail-closed (default).**

3. **Spec header says "three scheduled tasks" but the body references a distinct "EOD task"** (Data flow: "The EOD task also copies `book.json` off-machine"; Notifications: "one EOD summary (incl. the slot-gap count)"). **Default:** implement **four** recurring tasks — ARM, POLL, HEARTBEAT, EOD. The POLL `FINAL` action captures the close-bell poll + sets `eod_done`; the separate EOD task (16:45 ET — see Decision #7) runs the slot-gap audit, copies `book.json` off-machine, and pushes the single EOD summary including the slot-gap count. **Alternative:** fold audit+copy+summary into the POLL `FINAL` fire (keeps "three tasks" but makes the close-bell-racing fire heavier and couples backup to a poll that may be jittered).

4. **Off-machine backup destination — DECIDED (the operator, 2026-06-24): private GitHub data branch.** `scripts/eod_audit.py` snapshots `book.json` + `equity_curve.jsonl` + `fills.jsonl` into a one-time git worktree checked out on an orphan `paper-book-data` branch of the **PRIVATE** repo, commits per EOD, and pushes to GitHub (Task 7). Committing in the separate worktree never touches the loop's working branch. `BOOK_DATA_WORKTREE` is configurable (env). The backup function is tested against a throwaway `git init` repo with `push=False`. (Note: pushing publishes the paper record to GitHub's servers — acceptable because the repo is private and the book holds no credentials, only paper P&L/positions.)

5. **ET poll-day is defensive, not a live bug.** RTH (13:30–20:00 UTC) shares its calendar date with ET, so today's `ts[:10]` UTC slice already equals the ET date for every poll. Task 5 still implements the ET derivation per spec, but it is a foot-gun guard (malformed/local ts), and it touches the golden-covered `advance_poll` — hence the hard "golden byte-unchanged or STOP" gate. (Reviewer A flagged this as droppable since it is not a safety control and adds golden risk for zero current behavioral change; kept per spec, but it may be landed in a separate commit from the safety-critical work so a golden surprise can't block the firewall.) **DECIDED (the operator, 2026-06-24): keep per spec (default).**

6. **The arm/poll graph ALREADY imports `broker.py` — the spec §7 and the first plan draft were both wrong (found by all three reviewers, then verified empirically).** `src/autotrader_live/paper_monitor.py:52` does `from autotrader_live.broker import OrderIntent, ReviewResult`, and `paper_loop` imports `paper_monitor`, so `run_paper_book.py`, `validate_raw.py`, and `compute_signal_date.py` all pull `autotrader_live.broker` (hence `LiveBroker`) into `sys.modules` today (confirmed: `broker_in_graph=True` for all three). So a naive "loop never imports broker" test FAILS on first run, and the spec's "fence the latent LiveBroker/OrderIntent" is **not** satisfied by a regression-lock — the property is false now. **Default (REVISED Task 8):** make the fence real — relocate the **inert** `OrderIntent` / `ReviewResult` / `normalize_review_response` (frozen dataclasses + a pure parser; they make NO MCP call) into a new pure module `src/autotrader_live/order_types.py`; repoint `paper_monitor.py:52` at it; have `broker.py` **re-export** them (so the LIVE-01 path and `tests/live/test_broker.py` are untouched). Then `broker.py` — which holds `LiveBroker` (the funded-phase order-executing stub) + `PaperBroker`'s placement surface — is no longer in the loop graph, and the fence test passes **honestly**. The loop still reaches `order_types` (inert `OrderIntent`), which is acceptable and documented: a dataclass cannot place an order. **The decision for the operator:** relocate (default, honors spec intent) vs. redefine the fence to "loop never reaches the order-*executing* surface (`LiveBroker`/`place_*`/`cancel`)" and accept `broker.py` in the graph. Relocate is cleaner and the recommended default. **DECIDED (the operator, 2026-06-24): relocate (default).**

7. **The cron windows in the first draft were wrong (reviewer B, verified).** The draft POLL cron (`0,15,30,45 8-14` MT) put the first fire at 10:00 ET (missing the 9:45–10:00 ET post-ARM window) and the last in-session fire at 15:45 ET, so **no scheduled in-session poll lands at 15:55–16:00 ET** — the close-bell breakout the spec says must not be missed — and the EOD slot-gap audit reported a phantom ~2 missing slots every day (expected grid started 9:30 ET, cron started 10:00 ET). **REVISED (Tasks 7, 12):** the POLL schedule is a single source of truth (`schedule_state.POLL_SLOTS_ET`): fires at **9:45 ET (just after the 9:40 ET ARM), then every 15 min to 15:45 ET, plus a dedicated 15:55 ET close-bell fire**; `expected_rth_slots` derives from the same list so the audit has zero phantom gaps; the EOD task moves to **16:45 ET** (after the FINAL poll, removing the EOD/FINAL ordering race). Half-day early-close (13:00 ET) close-bell is covered only by the FINAL boundary fire — documented as a known limitation, not silently dropped.

---

## File structure

**New files:**
- `src/autotrader_live/schedule_state.py` — pure, MCP-free scheduling decisions: `PollAction`, `poll_day_et`, `poll_action`, `heartbeat_status`, `POLL_SLOTS_ET`, `expected_rth_slots`, `slot_gap`. One responsibility: "given facts (clock, book dates, markers), what should the loop do?"
- `src/autotrader_live/order_types.py` — the **inert** order dataclasses moved out of `broker.py`: `OrderIntent`, `ReviewResult`, `normalize_review_response`. No MCP calls, no `LiveBroker`. This is the fence (Task 8) — the loop reaches these inert types but never `broker.py`/`LiveBroker`.
- `scripts/legacy/run_paper_monitor_live.py` — relocated superseded LIVE-01 review-monitor (Task 12b), excluded from the fail-closed `scripts/*.py` no-place scan.
- `scripts/eod_audit.py` — once-daily EOD: slot-gap audit + off-machine `book.json` copy + EOD summary line.
- `automation/prompts/arm_task.md`, `automation/prompts/poll_task.md`, `automation/prompts/heartbeat_task.md`, `automation/prompts/eod_task.md` — the four recurring-task agent prompts.
- `automation/README.md` — index of the prompts + the exact recurring-cron specs (local-hour) + the kill-switch gesture.
- Tests: `tests/live/test_schedule_state.py`, `tests/live/test_eod_audit.py`, `tests/live/test_loop_import_fence.py`, and extensions to `tests/live/test_validate_raw.py` (new), `tests/live/test_heartbeat_check.py` (new), `tests/live/test_run_paper_book.py` (extend), `tests/live/test_no_place_invariant.py` (extend).

**Modified files:**
- `scripts/run_paper_book.py` — record account `987654321`; ET poll-day; new `poll-decide` + `mark-eod` CLI modes.
- `src/autotrader_live/paper_loop.py` — `advance_poll` derives `poll_day` via `schedule_state.poll_day_et` (Task 5; golden-gated).
- `src/autotrader_live/broker.py` + `src/autotrader_live/paper_monitor.py` — relocate the inert order dataclasses to `order_types.py`; `broker.py` re-exports; `paper_monitor.py:52` repoints to `order_types` (Task 8). Removes `broker.py` from the loop graph.
- `scripts/validate_raw.py` — coverage + freshness checks + `arm_complete` sentinel.
- `scripts/heartbeat_check.py` — rewired onto `schedule_state.heartbeat_status` (armed-today + NOT_ARMED).
- `tests/live/test_no_place_invariant.py` — add `review_option_order`; scan loop scripts + prompts (Task 12).
- `RUNBOOK_PAPER_BOOK.md` — add the Autonomous mode section + gating checklist + dropped-guard note + mode-coexistence (Task 14).
- The auto-memory note `live02-paper-book-plan.md` / a new `live03-autonomy-plan.md` (Task 15).

---

## Phase A — pure helpers, driver, scripts (testable Python)

### Task 1: POLL state machine + ET poll-day (`schedule_state.py`)

**Files:**
- Create: `src/autotrader_live/schedule_state.py`
- Test: `tests/live/test_schedule_state.py`

- [ ] **Step 1: Write the failing tests**

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/live/test_schedule_state.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'autotrader_live.schedule_state'`.

- [ ] **Step 3: Write the implementation**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/live/test_schedule_state.py -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader_live/schedule_state.py tests/live/test_schedule_state.py
git commit -m "feat(live03): pure POLL state machine + ET poll-day in schedule_state"
```

---

### Task 2: Heartbeat "armed-today" liveness + rewire `heartbeat_check.py`

**Files:**
- Modify: `src/autotrader_live/schedule_state.py` (add `heartbeat_status`)
- Modify: `scripts/heartbeat_check.py`
- Test: `tests/live/test_schedule_state.py` (add cases), `tests/live/test_heartbeat_check.py` (new)

- [ ] **Step 1: Write the failing tests (pure liveness)**

```python
# append to tests/live/test_schedule_state.py
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/live/test_schedule_state.py -q -k heartbeat`
Expected: FAIL — `AttributeError: module 'autotrader_live.schedule_state' has no attribute 'heartbeat_status'`.

- [ ] **Step 3: Implement `heartbeat_status`**

```python
# append to src/autotrader_live/schedule_state.py
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
```

- [ ] **Step 4: Write the failing test for the rewired script**

```python
# tests/live/test_heartbeat_check.py
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
```

- [ ] **Step 5: Rewire `scripts/heartbeat_check.py`**

Replace the body of `main()` with a thin shell over `schedule_state.heartbeat_status`. Add an `evaluate()` returning `(token, message)` (for tests), a `_now_et()` seam (for tests), and module constants `STATE_DIR`, `BOOK`. Read the LAST `equity_curve.jsonl` row's `ts` date (not just mtime) + `book.json`'s `last_arm_date`; pass file age + those facts to the pure function:

```python
# scripts/heartbeat_check.py — replace ET/EQUITY_CURVE/STALE_MIN block + main()
from autotrader_live import schedule_state, session_clock  # noqa: F401  (session_clock via schedule_state)

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
```

(Keep `import json` at the top of the file; it is already imported by the rewired body. Remove the old `STALE_MIN` constant — it now lives in `schedule_state`.)

- [ ] **Step 6: Run both test files green**

Run: `.venv/bin/python -m pytest tests/live/test_schedule_state.py tests/live/test_heartbeat_check.py -q`
Expected: PASS (16 in schedule_state + 1 heartbeat).

- [ ] **Step 7: Commit**

```bash
git add src/autotrader_live/schedule_state.py scripts/heartbeat_check.py \
        tests/live/test_schedule_state.py tests/live/test_heartbeat_check.py
git commit -m "feat(live03): heartbeat keyed on armed-today + NOT_ARMED alert"
```

---

### Task 3: `validate_raw.py` — coverage + freshness + `arm_complete` sentinel

**Files:**
- Modify: `scripts/validate_raw.py`
- Test: `tests/live/test_validate_raw.py` (new)

**Behavior to add:** after the existing hist validation, assert (a) **coverage** — every top-N symbol is present in normalized tradability AND fundamentals AND quotes (else a problem per missing symbol); (b) **freshness** — every consumed raw file's mtime is today (the agent passes `today`; a stale file from a prior day is a problem). Write the `raw/arm_complete` sentinel ONLY on success (return 0); on any failure ensure the sentinel is absent. Extend usage to `validate_raw.py <signal_date> [top_n=30] [today_iso]` (today defaults to ET today).

- [ ] **Step 1: Write the failing tests**

```python
# tests/live/test_validate_raw.py
"""Coverage + freshness + arm_complete sentinel on the central ARM ingest."""
import datetime as dt
import importlib.util
import json
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

_VR_PATH = Path(__file__).resolve().parents[2] / "scripts" / "validate_raw.py"
# TODAY must be the REAL ET date: the freshness check compares each raw file's
# mtime-date (files are written "now") against the `today` argv, so a hardcoded
# date would flag every freshly-written file as stale on any other day. SIGNAL is
# static — it only drives the date-independent hist last-bar / completed_bar_guard
# checks (the synthesized bars end exactly at SIGNAL regardless of the real date).
TODAY = dt.datetime.now(ZoneInfo("America/New_York")).date()
SIGNAL = dt.date(2026, 6, 22)


def _load():
    spec = importlib.util.spec_from_file_location("validate_raw", _VR_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _rising_bars(symbol, signal_date, n=300, start=10.0, step=0.5):
    bars, px = [], start
    for i in range(n):
        d = signal_date - dt.timedelta(days=(n - 1 - i))
        bars.append({"begins_at": f"{d.isoformat()}T00:00:00Z",
                     "open_price": f"{px:.6f}", "high_price": f"{px+0.2:.6f}",
                     "low_price": f"{px-0.2:.6f}", "close_price": f"{px:.6f}",
                     "volume": 1_000_000, "session": "reg"})
        px += step
    return {"data": {"results": [{"symbol": symbol, "interval": "day",
                                  "bounds": "regular", "bars": bars}]}}, px - step


def _write_full_raw(raw: Path, syms, last_close):
    raw.mkdir(parents=True, exist_ok=True)
    scan_rows = [{"ticker": s, "instrument_id": "id-" + s, "instrument_type": "EQUITY",
                  "columns": {"Symbol": s, "Name": s, "Close": f"{last_close:.2f}",
                              "Last": f"{last_close:.2f}", "Market cap": "1.0e+11",
                              "Volume": "9.0e+06", "Relative volume": "1.2",
                              "RSI": "65.0", "% Change": "1.0"}} for s in syms]
    (raw / "scan_fetch.json").write_text(json.dumps({"data": {"result": {"results": scan_rows}}}))
    trad = [{"symbol": s, "state": "active", "tradeable": True,
             "fractional_tradability": "tradable", "short_selling_tradability": "tradable",
             "account_type_tradabilities": [{"account_type": "individual",
                                             "account_type_tradability": "tradable"}]} for s in syms]
    (raw / "trad_b0.json").write_text(json.dumps({"data": {"results": trad}}))
    fund = [{"symbol": s, "market_cap": "1.0e+11", "average_volume": "9.0e+06",
             "average_volume_30_days": "9.0e+06", "high_52_weeks": f"{last_close*1.1:.6f}",
             "high_52_weeks_date": "2026-06-22", "sector": "Tech", "industry": "Semis"} for s in syms]
    (raw / "fund_b0.json").write_text(json.dumps({"data": {"results": fund}}))
    quotes = [{"quote": {"symbol": s, "last_trade_price": f"{last_close:.6f}",
                         "adjusted_previous_close": f"{last_close:.6f}",
                         "previous_close": f"{last_close:.6f}", "bid_price": f"{last_close-1:.6f}",
                         "ask_price": f"{last_close+1:.6f}", "has_traded": True, "state": "active"},
               "close": {"symbol": s, "date": SIGNAL.isoformat(), "price": f"{last_close:.2f}",
                         "interpolated": False, "source": "sip-list-exchange-close"}} for s in syms]
    (raw / "quotes_b0.json").write_text(json.dumps({"data": {"results": quotes}}))
    (raw / "earnings.json").write_text(json.dumps({"data": {"results": []}}))
    for i, s in enumerate(syms):
        # Distinct start per symbol so bar content differs: validate_raw's existing
        # duplicate-bar transposition check flags byte-identical bars. The tiny
        # offset keeps each hist close within the scan-close match tolerance.
        bars, _ = _rising_bars(s, SIGNAL, start=10.0 + i * 0.01)
        (raw / f"hist_{s}.json").write_text(json.dumps(bars))


@pytest.fixture()
def raw(tmp_path, monkeypatch):
    mod = _load()
    rawdir = tmp_path / "raw"
    monkeypatch.setattr(mod, "RAW", rawdir)
    # Force "today" so freshly written files (mtime ~now) count as fresh.
    monkeypatch.setattr(mod, "_today", lambda: TODAY, raising=False)
    return mod, rawdir


def test_full_set_passes_and_writes_sentinel(raw):
    mod, rawdir = raw
    syms = ["AMD", "NVDA"]
    last = float(_rising_bars("AMD", SIGNAL)[1])
    _write_full_raw(rawdir, syms, last)
    rc = mod.main(["validate_raw.py", SIGNAL.isoformat(), "2", TODAY.isoformat()])
    assert rc == 0
    assert (rawdir / "arm_complete").exists()


def test_missing_quote_coverage_fails_no_sentinel(raw):
    mod, rawdir = raw
    syms = ["AMD", "NVDA"]
    last = float(_rising_bars("AMD", SIGNAL)[1])
    _write_full_raw(rawdir, syms, last)
    # Drop NVDA from quotes -> coverage gap.
    q = json.loads((rawdir / "quotes_b0.json").read_text())
    q["data"]["results"] = [r for r in q["data"]["results"] if r["quote"]["symbol"] != "NVDA"]
    (rawdir / "quotes_b0.json").write_text(json.dumps(q))
    rc = mod.main(["validate_raw.py", SIGNAL.isoformat(), "2", TODAY.isoformat()])
    assert rc == 2
    assert not (rawdir / "arm_complete").exists()


def test_stale_file_freshness_fails_no_sentinel(raw):
    mod, rawdir = raw
    syms = ["AMD", "NVDA"]
    last = float(_rising_bars("AMD", SIGNAL)[1])
    _write_full_raw(rawdir, syms, last)
    # Backdate one consumed file's mtime to yesterday.
    stale = rawdir / "quotes_b0.json"
    yest = time.time() - 36 * 3600
    os.utime(stale, (yest, yest))
    rc = mod.main(["validate_raw.py", SIGNAL.isoformat(), "2", TODAY.isoformat()])
    assert rc == 2
    assert not (rawdir / "arm_complete").exists()
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/live/test_validate_raw.py -q`
Expected: FAIL — sentinel not written / coverage+freshness not enforced (the current script lacks them).

- [ ] **Step 3: Implement the checks + sentinel in `validate_raw.py`**

Add a `_today()` seam, a `today` argv, the coverage + freshness checks, and sentinel handling. Insert after the existing per-symbol hist loop (after the `dups` block) and replace the final return logic:

```python
# add near the top of validate_raw.py (RAW already defined as a module constant)
import datetime as dt   # already imported
from zoneinfo import ZoneInfo  # add

def _today() -> dt.date:
    return dt.datetime.now(ZoneInfo("America/New_York")).date()

# inside main(), after `top_n = ...`:
    today = dt.date.fromisoformat(argv[3]) if len(argv) > 3 else _today()
    # Compute the sentinel path from the LIVE RAW (not a module-level constant) so
    # tests that monkeypatch `RAW` write/check the same path. Success-only artifact:
    # clear any stale one up front.
    sentinel = RAW / "arm_complete"
    if sentinel.exists():
        sentinel.unlink()

# after the `dups` / duplicate-bar block, before the `if problems:` print:
    # ── 5. Coverage: every top-N symbol present in trad + fund + quotes ─────────
    for s in top_syms:
        missing = [name for name, d in
                   (("tradability", trad), ("fundamentals", fund), ("quotes", quotes))
                   if s not in d]
        if missing:
            problems.append(f"{s}: missing from {', '.join(missing)} (coverage gap)")

    # ── 6. Freshness: every consumed raw SOURCE dump's mtime is `today` ─────────
    # Stat the source batch/dump files (not the merged canonical files, which the
    # script itself just rewrote with a "now" mtime). A stale source batch from a
    # prior day is the real risk. File date is taken in ET to match `today`.
    et = ZoneInfo("America/New_York")
    for pattern in ("scan_fetch.json", "earnings.json", "trad_b*.json",
                    "fund_b*.json", "quotes_b*.json", "hist_*.json"):
        for f in RAW.glob(pattern):
            mtime_date = dt.datetime.fromtimestamp(f.stat().st_mtime, tz=et).date()
            if mtime_date != today:
                problems.append(f"{f.name}: mtime {mtime_date} != today {today} (stale dump)")

# replace the success path at the very end of main():
    if problems:
        print(f"VALIDATION FAILED ({len(problems)} problem(s)):")
        for p in problems:
            print("  -", p)
        return 2
    sentinel.write_text(f"arm_complete signal_date={signal_date} today={today}\n")
    print(f"VALIDATION PASSED: coverage+freshness OK for {len(top_syms)} symbols; "
          f"sentinel -> {sentinel}")
    return 0
```

This is the ONLY freshness block (no competing canonical-file variant). It catches the stale `quotes_b0.json` the test backdates, and the ET-consistent file date avoids a local-midnight-vs-ET off-by-one-day false fail.

- [ ] **Step 4: Run tests green**

Run: `.venv/bin/python -m pytest tests/live/test_validate_raw.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add scripts/validate_raw.py tests/live/test_validate_raw.py
git commit -m "feat(live03): validate_raw coverage + freshness + arm_complete sentinel"
```

---

### Task 4: Driver records the non-order-capable account `987654321`

**Files:**
- Modify: `scripts/run_paper_book.py:46`
- Test: `tests/live/test_run_paper_book.py` (add a case)

**Why:** Safety/labeling — the loop's recorded account must be the non-order-capable `987654321` so the label matches reality (a `place_*` against it is broker-rejected). `ACCOUNT` is a display-only constant (the driver makes no MCP calls and `book.json` stores no account), so this only changes stdout.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/live/test_run_paper_book.py
def test_driver_records_non_order_capable_account(driver, capsys):
    mod = driver
    _write_raw_set(mod.RAW, symbol="AMD", settled_close=100.0, quote_kwargs=dict(
        bid=99.0, ask=101.0, last=100.0, prev_close=100.0,
        settled_close=100.0, settled_date=SIGNAL_DATE))
    mod.cmd_arm(SIGNAL_DATE.isoformat(), TODAY.isoformat())
    out = capsys.readouterr().out
    assert "987654321" in out
    assert "123456789" not in out
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/live/test_run_paper_book.py::test_driver_records_non_order_capable_account -q`
Expected: FAIL — stdout still contains `123456789`.

- [ ] **Step 3: Change the constant**

```python
# scripts/run_paper_book.py:46
ACCOUNT = "987654321"   # NON-order-capable account: the loop is read-only and a
                        # place_* against this account is broker-rejected. The label
                        # therefore matches reality (paper-only, orders impossible).
```

- [ ] **Step 4: Run the new test + the full driver file green**

Run: `.venv/bin/python -m pytest tests/live/test_run_paper_book.py -q`
Expected: PASS (all driver tests, incl. the new one).

- [ ] **Step 5: Commit**

```bash
git add scripts/run_paper_book.py tests/live/test_run_paper_book.py
git commit -m "feat(live03): driver records non-order-capable account 987654321"
```

---

### Task 5: ET poll-day wired into the driver + `advance_poll` (golden-gated)

**Files:**
- Modify: `src/autotrader_live/paper_loop.py` (`advance_poll`: `poll_day` derivation)
- Modify: `scripts/run_paper_book.py` (`cmd_poll`: `take_entries`)
- Test: `tests/live/test_paper_loop.py` (add a case), `tests/live/test_run_paper_book.py` (existing covers wiring)

**Risk gate:** `advance_poll` has golden coverage (`tests/live/test_golden_paper_book.py`). RTH UTC date == ET date, so the golden MUST be byte-unchanged. **If the golden test fails after this change, STOP and escalate — do not regenerate the fixture.**

- [ ] **Step 1: Write the failing test (ET attribution)**

```python
# append to tests/live/test_paper_loop.py (this file already imports dt, paper_loop,
# PaperBook, Quote, StaticMarketData and defines _md / _bars_ending).

def test_advance_poll_derives_poll_day_via_et(tmp_path, monkeypatch):
    """advance_poll must route poll_day through schedule_state.poll_day_et (ET),
    not a raw ts[:10] UTC slice. Patch the shared module attr and confirm it's
    consulted with the exact ts."""
    import autotrader_live.paper_loop as pl       # the module under test
    from autotrader_live import schedule_state
    seen = []
    real = schedule_state.poll_day_et
    # No raising=False: at Step 1 (before the Step-3 import lands) pl.schedule_state
    # does not exist, so this errors loudly -> the test is genuinely red first.
    monkeypatch.setattr(pl.schedule_state, "poll_day_et",
                        lambda ts: (seen.append(ts), real(ts))[1])

    signal_date = dt.date(2026, 6, 22)
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    paper_loop.arm_day(book, _md(signal_date), signal_date=signal_date,
                       today=dt.date(2026, 6, 23))
    # Empty quotes: advance_poll still computes poll_day at the top before any
    # fill/ratchet work, so the derivation is exercised without needing a trigger.
    paper_loop.advance_poll(book, {}, _md(signal_date),
                            ts="2026-06-23T14:00:00Z", state_dir=tmp_path)
    assert seen == ["2026-06-23T14:00:00Z"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `.venv/bin/python -m pytest tests/live/test_paper_loop.py -q -k et_poll_day`
Expected: FAIL — `advance_poll` still uses `ts[:10]`.

- [ ] **Step 3: Wire `poll_day_et` into `advance_poll`**

```python
# src/autotrader_live/paper_loop.py — add import
from autotrader_live import exits, fill_engine, paper_monitor, schedule_state
# ... in advance_poll, replace:
#   poll_day = ts[:10]
# with:
    poll_day = schedule_state.poll_day_et(ts)
```

```python
# scripts/run_paper_book.py — in cmd_poll, replace:
#   take_entries = (book.last_arm_date == ts_iso[:10])
# with:
    from autotrader_live import schedule_state
    take_entries = (book.last_arm_date == schedule_state.poll_day_et(ts_iso))
```

- [ ] **Step 4: Run the new test + the GOLDEN GATE**

Run: `.venv/bin/python -m pytest tests/live/test_paper_loop.py tests/live/test_golden_paper_book.py tests/live/test_run_paper_book.py -q`
Expected: ALL PASS, **including the golden unchanged**. If `test_golden_paper_book.py` fails → STOP, revert, escalate (the change altered persisted output — a protected artifact).

- [ ] **Step 5: Commit**

```bash
git add src/autotrader_live/paper_loop.py scripts/run_paper_book.py tests/live/test_paper_loop.py
git commit -m "fix(live03): derive poll_day from ET, not a UTC slice (golden unchanged)"
```

---

### Task 6: Driver `poll-decide` + `mark-eod` CLI modes (+ `eod_done` marker)

**Files:**
- Modify: `scripts/run_paper_book.py` (add `cmd_poll_decide`, `cmd_mark_eod`, wire into `main`)
- Test: `tests/live/test_run_paper_book.py` (add cases)

**Why:** The POLL agent must read a single decision token. `poll-decide <now_et_iso>` reads `book.json` + the `eod_done_<date>` marker, computes `schedule_state.poll_action(...)`, and prints the token (`NORMAL`/`FINAL`/`SELF_HEAL_ARM`/`EXIT`). `mark-eod <date>` writes the marker after the FINAL poll. The marker is a file (`data/live/paper_book/eod_done_<YYYY-MM-DD>`), NOT a `book.json` field (keeps the golden schema frozen).

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/live/test_run_paper_book.py
import datetime as dt
from zoneinfo import ZoneInfo
ET = ZoneInfo("America/New_York")

def test_poll_decide_self_heal_when_unarmed(driver, capsys):
    mod = driver
    _write_raw_set(mod.RAW, symbol="AMD", settled_close=100.0, quote_kwargs=dict(
        bid=99.0, ask=101.0, last=100.0, prev_close=100.0,
        settled_close=100.0, settled_date=SIGNAL_DATE))
    mod.cmd_arm(SIGNAL_DATE.isoformat(), TODAY.isoformat())   # arms for TODAY (2026-06-23)
    capsys.readouterr()
    # A weekday RTH "now" on a LATER day than last_arm_date -> SELF_HEAL_ARM.
    mod.cmd_poll_decide("2026-06-24T15:00:00Z")   # 11:00 ET Wed, armed date is 06-23
    assert "SELF_HEAL_ARM" in capsys.readouterr().out

def test_poll_decide_normal_when_armed_today(driver, capsys):
    mod = driver
    _write_raw_set(mod.RAW, symbol="AMD", settled_close=100.0, quote_kwargs=dict(
        bid=99.0, ask=101.0, last=100.0, prev_close=100.0,
        settled_close=100.0, settled_date=SIGNAL_DATE))
    mod.cmd_arm(SIGNAL_DATE.isoformat(), TODAY.isoformat())
    capsys.readouterr()
    mod.cmd_poll_decide(f"{TODAY.isoformat()}T15:00:00Z")   # 11:00 ET, armed today
    assert "NORMAL" in capsys.readouterr().out

def test_mark_eod_writes_marker_and_decide_exits(driver, capsys):
    mod = driver
    _write_raw_set(mod.RAW, symbol="AMD", settled_close=100.0, quote_kwargs=dict(
        bid=99.0, ask=101.0, last=100.0, prev_close=100.0,
        settled_close=100.0, settled_date=SIGNAL_DATE))
    mod.cmd_arm(SIGNAL_DATE.isoformat(), TODAY.isoformat())
    ts = f"{TODAY.isoformat()}T15:00:00Z"
    mod.cmd_poll(ts)             # one poll today so polled_today is true
    mod.cmd_mark_eod(TODAY.isoformat())
    assert (mod.STATE_DIR / f"eod_done_{TODAY.isoformat()}").exists()
    capsys.readouterr()
    # After close, eod done -> EXIT.
    mod.cmd_poll_decide(f"{TODAY.isoformat()}T20:30:00Z")   # 16:30 ET, after close
    assert "EXIT" in capsys.readouterr().out

def test_poll_decide_no_book_in_session_self_heals(driver, capsys):
    # Brand-new machine: no book.json yet. An in-session fire -> SELF_HEAL_ARM
    # (last_arm_date is None); an off-hours fire -> EXIT.
    mod = driver  # STATE_DIR redirected to an empty tmp dir, no book
    mod.cmd_poll_decide(f"{TODAY.isoformat()}T15:00:00Z")   # 11:00 ET weekday
    assert "SELF_HEAL_ARM" in capsys.readouterr().out
    mod.cmd_poll_decide(f"{TODAY.isoformat()}T03:00:00Z")   # 23:00 ET prior day, closed
    assert "EXIT" in capsys.readouterr().out
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/live/test_run_paper_book.py -q -k "poll_decide or mark_eod"`
Expected: FAIL — `cmd_poll_decide` / `cmd_mark_eod` not defined.

- [ ] **Step 3: Implement the modes**

```python
# scripts/run_paper_book.py — add near the imports
from autotrader_live import schedule_state

def _eod_marker(date_iso: str) -> Path:
    return STATE_DIR / f"eod_done_{date_iso}"

def cmd_poll_decide(now_et_iso: str) -> None:
    """Print the POLL action token for a headless fire. Reads book.json + the
    eod_done marker; makes NO fetch and NO mutation."""
    now_et = dt.datetime.fromisoformat(now_et_iso.replace("Z", "+00:00")).astimezone(
        schedule_state.ET)
    book = PaperBook.load(STATE_DIR)
    last_arm_date = book.last_arm_date if book else None
    last_poll_ts = book.last_poll_ts if book else None
    today = now_et.date()
    eod_done = _eod_marker(today.isoformat()).exists()
    action = schedule_state.poll_action(
        now_et, last_arm_date=last_arm_date, last_poll_ts=last_poll_ts,
        eod_done=eod_done, today=today)
    print(action.value)

def cmd_mark_eod(date_iso: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _eod_marker(date_iso).write_text(f"eod_done {date_iso}\n")
    print(f"eod_done -> {_eod_marker(date_iso)}")

# in main(), add:
    elif mode == "poll-decide":
        if len(argv) != 3:
            raise SystemExit("usage: run_paper_book.py poll-decide <now_et_iso>")
        cmd_poll_decide(argv[2])
    elif mode == "mark-eod":
        if len(argv) != 3:
            raise SystemExit("usage: run_paper_book.py mark-eod <date_iso>")
        cmd_mark_eod(argv[2])
```

Update the module docstring's mode list + usage to mention `poll-decide` and `mark-eod`.

- [ ] **Step 4: Run tests green**

Run: `.venv/bin/python -m pytest tests/live/test_run_paper_book.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add scripts/run_paper_book.py tests/live/test_run_paper_book.py
git commit -m "feat(live03): poll-decide + mark-eod driver modes over the eod_done marker"
```

---

### Task 7: EOD slot-gap audit + off-machine book copy (`scripts/eod_audit.py`)

**Files:**
- Modify: `src/autotrader_live/schedule_state.py` (add `expected_rth_slots`, `slot_gap`)
- Create: `scripts/eod_audit.py`
- Test: `tests/live/test_schedule_state.py` (add slot cases), `tests/live/test_eod_audit.py` (new)

- [ ] **Step 1: Write the failing pure-function tests**

```python
# append to tests/live/test_schedule_state.py
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `.venv/bin/python -m pytest tests/live/test_schedule_state.py -q -k slot`
Expected: FAIL — `expected_rth_slots`/`slot_gap` not defined.

- [ ] **Step 3: Implement the slot functions**

```python
# append to src/autotrader_live/schedule_state.py
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
```

This uses `session_clock.half_days()` — a small public accessor to add in Task 7 (reviewers flagged reaching into the private `session_clock._HALF_DAYS`). Add to `session_clock.py`:

```python
def half_days() -> set[dt.date]:
    """Public accessor for the early-close dates (avoids reaching into _HALF_DAYS)."""
    return set(_HALF_DAYS)
```

- [ ] **Step 4: Write the failing EOD-script test**

```python
# tests/live/test_eod_audit.py
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
```

- [ ] **Step 5: Implement `scripts/eod_audit.py`**

```python
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
```

One-time setup (document in the RUNBOOK, Task 14) — create the orphan data branch + worktree in the PRIVATE repo:

```bash
git checkout --orphan paper-book-data && git rm -rf . && \
  git commit --allow-empty -m "init paper-book-data" && git push -u origin paper-book-data && \
  git checkout claude/live-03-autonomy
git worktree add ~/<repo>-book-data <data-branch>
```

`run()` is **idempotent and re-runnable** (it reads current state, recopies, recomputes) so a jittered double-fire is harmless. The EOD/FINAL ordering race (reviewer B) is closed by scheduling the EOD task at **16:45 ET** (Task 12) — well after the 15:55 ET close-bell FINAL poll — so the backup + slot-gap audit always see the final row. On a slept-through (zero-poll) day there is no FINAL poll and no `eod_done`; the EOD task still runs, copies the book, and reports `missing == expected` (the honest full-gap record).

- [ ] **Step 6: Run both test files green**

Run: `.venv/bin/python -m pytest tests/live/test_schedule_state.py tests/live/test_eod_audit.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/autotrader_live/schedule_state.py scripts/eod_audit.py \
        tests/live/test_schedule_state.py tests/live/test_eod_audit.py
git commit -m "feat(live03): EOD slot-gap audit + off-machine book copy"
```

---

## Phase B — firewall hardening

### Task 8: Make the fence REAL — relocate inert order types so `broker.py`/`LiveBroker` leaves the loop graph

**Files:**
- Create: `src/autotrader_live/order_types.py`
- Modify: `src/autotrader_live/broker.py` (move dataclasses out; re-export), `src/autotrader_live/paper_monitor.py:52` (repoint)
- Create: `tests/live/test_loop_import_fence.py`

**Why (corrected — see Decision #6):** The spec §7 and the first plan draft both wrongly assumed the arm/poll graph is already broker-free. It is NOT: `paper_monitor.py:52` does `from autotrader_live.broker import OrderIntent, ReviewResult`, and `paper_loop` imports `paper_monitor`, so `run_paper_book.py` / `validate_raw.py` / `compute_signal_date.py` all pull `autotrader_live.broker` (and thus `LiveBroker`) into `sys.modules` today (verified). A naive "loop never imports broker" test would FAIL on first run. The fix is to make the fence true: move the **inert** order dataclasses (no MCP calls) into a pure `order_types.py`, leaving `LiveBroker` + the placement surface alone in `broker.py`, which then exits the loop graph.

- [ ] **Step 1: Write the failing fence test**

```python
# tests/live/test_loop_import_fence.py
"""The autonomous-loop scripts must never import autotrader_live.broker — the module
that holds LiveBroker (the funded-phase order-executing stub) + PaperBroker's
placement surface. The inert order dataclasses live in order_types.py and may be
reached. Run each script in a subprocess so the check is not polluted by other
tests importing broker into this process."""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_LOOP_SCRIPTS = [
    "scripts/run_paper_book.py",
    "scripts/validate_raw.py",
    "scripts/compute_signal_date.py",
    "scripts/heartbeat_check.py",
    "scripts/eod_audit.py",
]


@pytest.mark.parametrize("script", _LOOP_SCRIPTS)
def test_loop_script_does_not_import_broker(script):
    code = textwrap.dedent(f"""
        import importlib.util, sys
        from pathlib import Path
        repo = Path({str(_REPO)!r})
        sys.path.insert(0, str(repo / "src"))
        spec = importlib.util.spec_from_file_location("m", repo / {script!r})
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert "autotrader_live.broker" not in sys.modules, (
            "{script} pulled autotrader_live.broker (LiveBroker/placement) into the loop graph")
        print("OK")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"{script}: {r.stderr}"
```

- [ ] **Step 2: Run to verify it FAILS (proves the hazard is real today)**

Run: `.venv/bin/python -m pytest tests/live/test_loop_import_fence.py -q`
Expected: FAIL on `run_paper_book.py`, `validate_raw.py`, `compute_signal_date.py` (they pull broker via `paper_monitor`); PASS on `heartbeat_check.py`, `eod_audit.py`.

- [ ] **Step 3: Create `order_types.py` and move the inert dataclasses into it**

Move `OrderIntent`, `ReviewResult`, and `normalize_review_response` **verbatim** from `broker.py` into a new `src/autotrader_live/order_types.py` (keep their full bodies — frozen dataclasses + the pure parser; they make NO MCP call). The module header:

```python
# src/autotrader_live/order_types.py
"""Inert order dataclasses — OrderIntent / ReviewResult / normalize_review_response.

Pure data + a pure parser. NO MCP calls, NO LiveBroker, NO placement. Split out of
broker.py so the autonomous loop graph (paper_loop -> paper_monitor) can reference
the order *types* without importing broker.py (LiveBroker / PaperBroker placement
surface). The no-place source-scan still applies to this file.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date
from typing import Callable  # if needed by the moved bodies
# ... moved OrderIntent, ReviewResult, normalize_review_response verbatim ...
```

- [ ] **Step 4: In `broker.py`, import the moved names from `order_types` and re-export**

Replace the removed definitions in `broker.py` with a re-export so existing importers (`tests/live/test_broker.py`, `scripts/legacy/run_paper_monitor_live.py`) keep working unchanged:

```python
# src/autotrader_live/broker.py — near the top, after the module docstring
from autotrader_live.order_types import (  # re-export for back-compat
    OrderIntent, ReviewResult, normalize_review_response,
)
# Broker protocol, NoPlaceInPaperError, PaperBroker, LiveBroker remain here.
```

- [ ] **Step 5: Repoint `paper_monitor.py:52` at `order_types`**

```python
# src/autotrader_live/paper_monitor.py:52
from autotrader_live.order_types import OrderIntent, ReviewResult
```

- [ ] **Step 6: Run the fence test + broker tests + the FULL live suite**

Run: `.venv/bin/python -m pytest tests/live/test_loop_import_fence.py tests/live/test_broker.py tests/live/test_no_place_invariant.py tests/live/ -q`
Expected: ALL PASS — the fence test now green for all five scripts (broker.py left the loop graph), `test_broker.py` unchanged (re-export), the no-place package scan still green (order_types.py contains none of the six tokens — it uses `intent_type`/`order_type`, not `place_*`). If any LIVE-01 import broke, fix the importer to use `order_types` or rely on the broker re-export — do NOT move `LiveBroker` into `order_types`.

- [ ] **Step 7: Commit**

```bash
git add src/autotrader_live/order_types.py src/autotrader_live/broker.py \
        src/autotrader_live/paper_monitor.py tests/live/test_loop_import_fence.py
git commit -m "refactor(live03): fence LiveBroker out of the loop graph via order_types.py"
```

---

### Task 9: Add `review_option_order` to the package no-place scan

**Files:**
- Modify: `tests/live/test_no_place_invariant.py` (forbidden set only)

**Why:** Close the one gap in the existing package scan — `review_option_order` was missing from `_FORBIDDEN_TOKENS`. (Scanning loop scripts + prompts lands in Task 12, after the prompts exist.)

- [ ] **Step 1: Extend the forbidden set**

```python
# tests/live/test_no_place_invariant.py
_FORBIDDEN_TOKENS: list[str] = [
    "place_equity_order",
    "place_option_order",
    "cancel_equity_order",
    "cancel_option_order",
    "review_equity_order",
    "review_option_order",   # added: the sixth order-surface tool
    "mcp__",                 # added: any raw MCP tool id (uniform with the loop-surface scan)
]
```

- [ ] **Step 2: Run the package scan green**

Run: `.venv/bin/python -m pytest tests/live/test_no_place_invariant.py -q`
Expected: PASS — the count grows (now N files × 7 tokens); `src/autotrader_live/` contains none of them (verify `schedule_state.py` and `order_types.py` are clean too). The package references broker tools in prose as `get_equity_*` words, not `mcp__…` ids — if a docstring does contain a literal `mcp__` prefix, reword the docstring; do NOT weaken the scan.

- [ ] **Step 3: Commit**

```bash
git add tests/live/test_no_place_invariant.py
git commit -m "test(live03): forbid review_option_order in the no-place package scan"
```

---

## Phase C — automation prompts + ops docs

### Task 10: ARM task prompt

**Files:**
- Create: `automation/prompts/arm_task.md`

The prompt is the headless ARM agent's instructions. It must be self-contained, name the six forbidden tools as NEVER-call, and call the tested helpers. No unit test (it is a prompt); it is validated by Task 13's scan + the Task 14 dry-run.

- [ ] **Step 1: Write the ARM prompt**

```markdown
# LIVE-03 ARM (headless, recurring weekdays ~07:40 MT / ~09:40 ET)

You are a headless scheduled agent. PAPER ONLY. The broker is READ-ONLY.
NEVER call any order-surface tool — none of the six order tools in the broker MCP:
order placement, cancellation, or review, for equities or options
(place / cancel / review × equity / option). If you are ever asked or tempted to,
STOP and push "ABORT: order-surface call requested". (This prohibition is worded
without the literal tool-name tokens so the no-place source scan stays strict.)

Account: all account-scoped reads use 987654321 (non-order-capable). Interpreter:
.venv/bin/python. Use
absolute paths. Repo: <worktree>

Steps:
0. ARM LOCK: if data/live/paper_book/raw/.arm_inprogress exists and is < 10 min old,
   another ARM (or a SELF_HEAL) is mid-fetch -> exit silently (don't double-fetch /
   double-clear raw/). Otherwise write raw/.arm_inprogress with the current time.
   ALWAYS delete the lock on completion or abort.
1. FRESH START: move any existing data/live/paper_book/raw/* (except the lock) to
   raw/_prev_<date>/ (prevents reuse of a prior day's dumps), then recreate raw/.
2. GATE: get_equity_quotes(["SPY"]); require quote.state=="active". Also confirm
   session_clock says RTH (run: run_paper_book.py status is not enough — check the
   clock). If not active/RTH -> remove the lock and exit silently.
3. IDEMPOTENCY: run `run_paper_book.py status`; if last_arm_date == today -> remove
   the lock and exit.
4. SIGNAL DATE: get_equity_historicals("SPY", interval=day, span=year,
   end_time="<today>T00:00:00Z") -> save raw/hist_SPY.json verbatim; run
   compute_signal_date.py; confirm GUARD_OK=True.
5. UNIVERSE: run_scan (scan <your-scan-id>) -> raw/scan_fetch.json verbatim. Then fetch,
   writing each response VERBATIM (no normalization — central ingest does that):
   get_equity_tradability (account 987654321, batched) -> raw/trad_b*.json;
   get_equity_fundamentals (batched) -> raw/fund_b*.json;
   get_equity_quotes (<=20/call) -> raw/quotes_b*.json;
   get_earnings_calendar -> raw/earnings.json;
   get_equity_historicals per symbol with end_time="<today>T00:00:00Z" ->
   raw/hist_<SYM>.json (one symbol per call so results[0] is unambiguous).
6. CENTRAL INGEST + VALIDATE: run
   `validate_raw.py <signal_date> 30 <today>`. It trims/merges/normalizes and
   enforces hist symbol/bars/last-date/guard/close-cross-check, coverage (every
   top-30 in trad+fund+quotes) and freshness (today's mtime). On non-zero exit:
   ABORT ARM, do NOT mutate the book, remove the lock, push "ARM ABORTED: <stderr tail>".
   Proceed only if raw/arm_complete exists.
7. ARM: run_paper_book.py arm <signal_date> <today>.
8. NOTIFY + UNLOCK: remove raw/.arm_inprogress, then push "ARMED N: [syms]" (or the
   abort). Always include the honesty label: "simulator-only, orders forbidden;
   neg-to-breakeven after costs".

DEATH SIGNAL: any broker auth/unreachable error at any step -> remove the lock,
push "re-auth needed (ARM)", and stop. Do NOT add or honor any wall-clock token guard.
```

- [ ] **Step 2: Commit**

```bash
git add automation/prompts/arm_task.md
git commit -m "feat(live03): ARM headless task prompt"
```

---

### Task 11: POLL task prompt

**Files:**
- Create: `automation/prompts/poll_task.md`

- [ ] **Step 1: Write the POLL prompt**

```markdown
# LIVE-03 POLL (headless, recurring ~every 15 min, RTH window starting AT/after ARM)

Same hard guardrails as ARM: PAPER ONLY, broker READ-ONLY, NEVER call the six
order-surface tools, account 987654321, venv python, absolute paths.

1. DECIDE: action = `run_paper_book.py poll-decide <now_utc_iso>`. Branch:
   - EXIT          -> stop silently.
   - SELF_HEAL_ARM -> run the full ARM prompt now (a failed ARM must not kill the
                      trading day), then continue to a NORMAL poll. (The ARM prompt's
                      step-0 .arm_inprogress lock makes this safe if the scheduled
                      ARM is still mid-fetch — SELF_HEAL exits rather than double-clear.)
   - NORMAL        -> proceed to step 2.
   - FINAL         -> proceed to step 2, then step 4.
2. QUOTES: load held+armed symbols from `run_paper_book.py status`; fetch their
   get_equity_quotes (<=20/batch) -> data/live/paper_book/raw/quotes.json verbatim.
3. POLL: run_paper_book.py poll <now_utc_iso>. (poll_day is derived in ET.)
4. FINAL only: run_paper_book.py mark-eod <today>. (The EOD task pushes the summary.)
5. NOTIFY ON EVENTS ONLY: if this poll produced fills or stop-outs, push a one-line
   summary with the honesty label. Otherwise stay silent (no "nothing happened").

The schedule includes a dedicated 15:55 ET fire (schedule_state.POLL_SLOTS_ET) so a
close-bell breakout (the AMAT-15:59 case) is captured by a NORMAL in-session poll,
not only the FINAL boundary fire. DEATH SIGNAL: broker auth/unreachable -> push
"<your re-auth message>" and stop. Missing/bad quote for a name -> the driver marks
it at cost and the resting stop stands; NEVER force-sell on bad data. (Half-day
13:00 ET closes have no dedicated close-bell fire — covered by FINAL; known limit.)
```

- [ ] **Step 2: Commit**

```bash
git add automation/prompts/poll_task.md
git commit -m "feat(live03): POLL headless task prompt (decide/self-heal/final)"
```

---

### Task 12: HEARTBEAT + EOD task prompts + automation README

**Files:**
- Create: `automation/prompts/heartbeat_task.md`, `automation/prompts/eod_task.md`, `automation/README.md`

- [ ] **Step 1: Write the HEARTBEAT prompt**

```markdown
# LIVE-03 HEARTBEAT (recurring, every ~11 min, RTH local-hour window)

MCP-FREE. Do not call the broker at all.
1. Run `heartbeat_check.py`.
2. If stdout starts with STALE or NOT_ARMED -> push the line to the operator.
3. Otherwise (SILENT/OK) -> do nothing.
Re-enable this task when arming autonomous mode; PAUSE it on an intentional stop
(else it false-STALEs on the next un-armed RTH morning... which the armed-today
fix now downgrades to a NOT_ARMED alert — still pause on intentional stop).
```

- [ ] **Step 2: Write the EOD prompt**

```markdown
# LIVE-03 EOD (recurring, weekdays ~16:45 ET, single fire — after the FINAL poll)

MCP-FREE. 1. Run `eod_audit.py`. 2. Push ONE EOD summary: total_equity,
realized_pnl_cum, slots expected/actual/missing, and the honesty label
"simulator-only, orders forbidden; neg-to-breakeven after costs". The audit also
snapshots book.json + equity_curve.jsonl + fills.jsonl to the PRIVATE GitHub
`paper-book-data` branch (off-machine record). Scheduled at 16:45 ET so it always
runs AFTER the 15:55 ET close-bell FINAL poll (no ordering race); re-running is
harmless (idempotent — a no-change EOD commits nothing).
```

- [ ] **Step 3: Write `automation/README.md`** — index the four prompts and give the exact recurring-cron specs in **local-hour** form (DST-safe), plus the kill-switch:

```markdown
# LIVE-03 automation — scheduled-poll autonomy

Four RECURRING scheduled-tasks (never one-shot; one-shots are flaky on this
platform) over the pure run_paper_book.py driver. Crons are local-hour (your timezone),
DST-safe (MT and ET share US DST dates all year — verified — so a local-hour cron
keeps RTH alignment; RTH is invariant in local wall-clock).

POLL fire times are the SINGLE SOURCE OF TRUTH in schedule_state.POLL_SLOTS_ET
(first fire 09:45 ET just after the 09:40 ET ARM; every 15 min to 15:45 ET; plus a
dedicated 15:55 ET close-bell fire). The cron below is hand-derived to match it; if
you change one, change both (expected_rth_slots reads POLL_SLOTS_ET, so a mismatch
would make the EOD slot-gap audit report phantom gaps).

| Task      | Prompt                    | Cron (local MT)                          | ET coverage |
|-----------|---------------------------|------------------------------------------|-------------|
| ARM       | prompts/arm_task.md       | `<schedule>`                           | 09:40 ET, once/day after open |
| POLL      | prompts/poll_task.md      | `<schedule>` + `<schedule>` + `<schedule>` | 09:45 ET, then :00/:15/:30/:45 to 15:45 ET, plus 15:55 ET close-bell |
| HEARTBEAT | prompts/heartbeat_task.md | `<schedule>`            | liveness watchdog (MCP-free), RTH |
| EOD       | prompts/eod_task.md       | `<schedule>`                         | 16:45 ET, after the FINAL poll |

Pre-open POLL fires (none scheduled before 09:45 ET) and post-close fires resolve
to EXIT/FINAL via poll-decide, so a slightly-wide window is harmless.

KNOWN LIMITATION (half-days, 2026-11-27 / 2026-12-24, 13:00 ET close): there is no
dedicated 12:55 ET close-bell fire; the half-day close bell is covered only by the
FINAL boundary poll. Post-close POLL fires that day EXIT harmlessly. Accepted.

KILL-SWITCH (one gesture): disable all four recurring tasks (scheduled-tasks MCP)
AND revoke the broker tool approval. Rehearse in the dry-run (RUNBOOK gating).

DO NOT ARM FOR REAL until the RUNBOOK "Autonomous mode gating" checklist passes.
```

- [ ] **Step 4: Commit**

```bash
git add automation/prompts/heartbeat_task.md automation/prompts/eod_task.md automation/README.md
git commit -m "feat(live03): HEARTBEAT + EOD task prompts + automation README/crons"
```

---

### Task 12b: Quarantine the superseded LIVE-01 monitor to `scripts/legacy/`

**Files:**
- Move: `scripts/run_paper_monitor_live.py` → `scripts/legacy/run_paper_monitor_live.py`
- Modify: `RUNBOOK_LIVE_PAPER_RUN.md` (update the path reference)

**Why (Decision #2, fail-closed):** `run_paper_monitor_live.py` is the superseded LIVE-01 supervised review-monitor — it names `review_equity_order` and hard-codes the order-capable account `123456789`. It is NOT one of the autonomous tasks and is dead under account-lockdown, but it is the one committed script pairing an order tool with the order-capable account. Relocating it to `scripts/legacy/` lets Task 13 blanket-scan all of `scripts/*.py` fail-closed (any future order-touching script trips by default) while excluding the documented dead path. This retires the residual hazard reviewer A flagged.

- [ ] **Step 1: Relocate + header**

```bash
git mv scripts/run_paper_monitor_live.py scripts/legacy/run_paper_monitor_live.py
```
Add a header line to the moved file: `# SUPERSEDED — LIVE-01 supervised review-monitor. NOT an autonomous task; dead under account-lockdown (agentic off on 123456789). Kept for reference; do not schedule.` Grep the repo for any reference to the old path (`grep -rn run_paper_monitor_live`) and update `RUNBOOK_LIVE_PAPER_RUN.md` to the new path. Confirm no test imports it by path (`tests/live/` — verify none reference `scripts/run_paper_monitor_live.py`).

- [ ] **Step 2: Confirm the live suite is still green**

Run: `.venv/bin/python -m pytest tests/live/ -q`
Expected: PASS (the move touches no imported module; `broker.py` is unaffected).

- [ ] **Step 3: Commit**

```bash
git add -A scripts/legacy/run_paper_monitor_live.py RUNBOOK_LIVE_PAPER_RUN.md
git commit -m "chore(live03): quarantine superseded LIVE-01 monitor to scripts/legacy"
```

---

### Task 13: Widen the no-place invariant to ALL of `scripts/` + task prompts (fail-closed)

**Files:**
- Modify: `tests/live/test_no_place_invariant.py`

**Why (Decision #2, REVISED to fail-closed):** scan **every** `scripts/*.py` (top-level, excluding `scripts/legacy/`) plus `automation/prompts/*.md`, so a future order-touching script added under `scripts/` trips the scan by default rather than silently escaping an allowlist. The forbidden set (Task 9) already includes all six order tools + `mcp__`. Prerequisite: Task 12b (legacy quarantine) and Tasks 10/11/12 (token-free prompts) must land first.

- [ ] **Step 1: Write the failing test (blanket scan)**

```python
# tests/live/test_no_place_invariant.py — add a fail-closed scripts/ + prompts scan
_LOOP_SURFACE = (
    [p for p in sorted((_REPO_ROOT / "scripts").glob("*.py"))]   # top-level scripts only
    + sorted((_REPO_ROOT / "automation" / "prompts").glob("*.md"))
)
# scripts/legacy/ is intentionally excluded: it holds the documented, superseded
# LIVE-01 monitor (see Task 12b). The glob is non-recursive, so legacy/ is skipped.


def test_loop_surface_has_no_order_tokens():
    """Every top-level scripts/*.py + every task prompt must contain none of the
    forbidden order-surface tokens (incl. the raw mcp__ prefix). Fail-closed: a new
    order-touching script under scripts/ trips this by default."""
    for path in _LOOP_SURFACE:
        src = path.read_text(encoding="utf-8")
        for tok in _FORBIDDEN_TOKENS:   # already includes the six tools + mcp__ (Task 9)
            assert tok not in src, (
                f"NO-PLACE INVARIANT VIOLATED\n  file: "
                f"{path.relative_to(_REPO_ROOT)}\n  token: {tok!r}")
```

- [ ] **Step 2: Run — expect PASS** (all top-level scripts + prompts are token-free; the one offender moved to `scripts/legacy/` in Task 12b)

Run: `.venv/bin/python -m pytest tests/live/test_no_place_invariant.py -q`
Expected: PASS. If any script/prompt trips it, FIX the file (reword a prompt to avoid the literal token; relocate a genuinely order-touching script) — do NOT weaken the scan. **Teeth-check:** temporarily inject `place_equity_order` into a prompt, confirm FAIL, revert. (Reconciles spec §5, which said prompts "name all six forbidden tools": the prohibition is preserved in prose without the literal token strings, so the scan stays strict.)

- [ ] **Step 3: Commit**

```bash
git add tests/live/test_no_place_invariant.py
git commit -m "test(live03): widen no-place invariant to loop scripts + task prompts"
```

---

### Task 14: RUNBOOK autonomous-mode section + gating checklist + dropped-guard note

**Files:**
- Modify: `RUNBOOK_PAPER_BOOK.md`

This is the operational contract. It also documents Decision #1 (dropped wall-clock guard) and the **arm-for-real gate**.

- [ ] **Step 1: Append an "Autonomous (scheduled-poll) mode" section** covering, verbatim where possible:

```markdown
## Autonomous (scheduled-poll) mode — LIVE-03

Four RECURRING scheduled tasks over the pure driver (see automation/README.md):
ARM, POLL, HEARTBEAT, EOD. Tier-0 only: runs while the Mac is awake + the app is
open; missed slots (sleep/close) are caught by the EOD slot-gap audit + heartbeat,
not replayed.

### Safety — real orders are structurally impossible
- PRIMARY (account lockdown): the loop records and reads account 987654321
  (agentic_allowed=false). A place_* against it is broker-rejected. OPERATOR
  ACTION REQUIRED before arming: also set agentic_allowed=false on 123456789 so
  NO agent-actionable account exists.
- SECONDARY (defense-in-depth): zero order-surface tokens in the package + loop
  scripts + prompts (test_no_place_invariant, widened); the driver makes no MCP
  calls; prompts name all six forbidden tools; per-task approval covers only the
  read tools; the loop graph cannot import OrderIntent/LiveBroker (import fence
  test); runtime push if a stalled order is ever attempted.

### Death signal (the dropped wall-clock guard)
There is NO wall-clock OAuth-token guard. The death signal is a broker
auth/unreachable error at ARM or POLL -> "re-auth needed" push + stop; a dead
token also stales the curve, which the heartbeat catches. (The old ~95h guard
anchored to an unobservable value; it is dropped. An optional ~90h soft morning
warning is intentionally NOT built — YAGNI.)

### Mode coexistence
Keep the supervised ScheduleWakeup RUNBOOK loop (above) as a maintained, tested
fallback until the scheduled path banks >= 5 clean autonomous days.

### Autonomous-mode GATING (DO NOT ARM FOR REAL until all pass)
1. Account lockdown: operator sets agentic_allowed=false on 123456789. Confirm
   the full loop's reads still work on 987654321 AND a place_* call is rejected.
2. Per-task approval scope (adversarial, AUTO-fired): approve only the ~7 read
   tools; in a throwaway recurring task with ONLY reads approved, have it attempt a
   named order call (e.g. a place_equity_order on a throwaway intent) and confirm it
   STALLS (no order placed) — approval is strictly per-tool, not broad. Run this in
   an actually AUTO-fired task (not just Run-now), since auto-fire is the mode the
   loop runs in and the untested axis. If a broad approval would let it through ->
   STOP, do not arm. Delete the throwaway task afterward.
3. Full read surface AUTO-fires: confirm an actually auto-fired (not Run-now)
   scheduled task reaches the full read surface (scan/quotes/tradability/
   historicals/fundamentals/earnings), not just get_accounts.
4. Kill-switch rehearsed: disable the four tasks + revoke broker approval in one
   pass; confirm the next fire does nothing.
Only after 1-4 pass: enable the four recurring tasks + re-enable HEARTBEAT.
```

- [ ] **Step 2: Verify the doc references match reality** — script names, cron table, account numbers, the four task names. (No test; a careful read.)

- [ ] **Step 3: Commit**

```bash
git add RUNBOOK_PAPER_BOOK.md
git commit -m "docs(live03): autonomous-mode runbook, gating checklist, dropped-guard note"
```

---

### Task 15: Full-suite gate + doc-sync (memory) + firewall check

**Files:**
- Modify: the auto-memory note (`live02-paper-book-plan.md` update or a new `live03-autonomy-built.md`) + `MEMORY.md` index.

- [ ] **Step 1: Full live suite green**

Run: `.venv/bin/python -m pytest tests/live/ -q`
Expected: PASS. Count = 653 baseline + the new tests (schedule_state ~16, heartbeat ~1, validate_raw 3, run_paper_book ~5, paper_loop 1, eod_audit 1, import_fence 5, no-place loop-surface 1, plus the grown package×token parametrization from `review_option_order`).

- [ ] **Step 2: Firewall check (protected offline engine untouched)**

Run: `git -C <worktree> diff --stat origin/main -- src/autotrader/`
Expected: **EMPTY** (no diff under `src/autotrader/`). Also confirm `tests/live/fixtures/golden_paper_book/` is unchanged.

- [ ] **Step 3: Update the auto-memory note** with the built state (modules added, the four tasks, the gating gate that blocks arm-for-real, the open Decisions #1-5 and how they resolved). Add a `MEMORY.md` index line. Commit.

```bash
git add <agent session notes>
git commit -m "docs(live03): memory sync — autonomy harness built, arm-for-real gated"
```

- [ ] **Step 4: Open the PR** (branch off `main`, never stacked):

```bash
git -C <worktree> push -u origin claude/live-03-autonomy
gh pr create --repo <your-org>/<your-repo> --base main --head claude/live-03-autonomy \
  --title "LIVE-03: scheduled-poll autonomy harness (paper-only, orders structurally impossible)" \
  --body "<summary + the gating gate that must pass before arming for real>"
```

---

## Self-review (run before serving)

**Spec coverage map** (every spec element → task):
- Headless ARM + POLL recurring tasks → Tasks 10, 11 (prompts) over Tasks 1, 3, 5, 6 (helpers/driver).
- Account-lockdown firewall (loop on 987654321; operator disables agentic on 123456789) → Task 4 (recorded account) + Task 14 (operator action + gating).
- Partial-ARM coverage/freshness + `arm_complete` sentinel in validate_raw → Task 3.
- Heartbeat armed-today fix → Task 2.
- Drop wall-clock token guard → Decision #1 + Task 14 (doc) + auth-failure-as-death in Tasks 10/11.
- Close-bell final-poll state machine + poll self-heal → Task 1 (`poll_action`) + Task 6 (`poll-decide`/`mark-eod`) + Task 11 (prompt).
- Widen test_no_place_invariant (scripts/ + prompts; add review_option_order + mcp__) → Tasks 9 + 13; fail-closed blanket `scripts/` scan + Task 12b legacy quarantine.
- Fence LiveBroker/OrderIntent stub → Task 8 (relocate inert dataclasses to `order_types.py` so `broker.py`/`LiveBroker` leaves the loop graph — the real fence, not a regression-lock on a false premise).
- Keep ScheduleWakeup supervised loop as fallback → Task 14 (mode coexistence).
- Dry-run gating (per-tool approval auto-fired; full read surface auto-fires) → Task 14 (gating checklist).
- EOD slot-gap audit + off-machine book copy → Task 7 (`POLL_SLOTS_ET` single-source grid; EOD at 16:45 ET, idempotent).

**Task order:** 1→2→3→4→5→6→7 (helpers/driver/scripts) → 8, 9 (firewall) → 10, 11, 12 (prompts) → 12b (legacy quarantine) → 13 (fail-closed scan, needs 10/11/12/12b) → 14 (runbook/gating) → 15 (full-suite gate + doc-sync + PR). Forward deps checked: Task 5 needs `poll_day_et` (Task 1 ✓); Task 13 needs the prompts + 12b ✓; Task 8 needs `eod_audit.py` (Task 7 ✓).

**Placeholder scan:** every code step shows full code; no TBD/"add validation"/"similar to" (Task 5's test is now concrete; Task 3's sentinel is computed from live `RAW`).
**Type consistency:** `PollAction` + `poll_day_et`/`poll_action`/`heartbeat_status`/`POLL_SLOTS_ET`/`expected_rth_slots`/`slot_gap` referenced identically across Tasks 1, 2, 5, 6, 7; the `eod_done_<date>` marker, `arm_complete` sentinel, `.arm_inprogress` lock, and `order_types.py` names are consistent across Tasks 3, 6, 7, 8, 10, 11.
**Protected artifacts:** Task 5 is the only touch of a golden-covered path and is hard-gated on "golden unchanged or STOP" (a reviewer verified RTH UTC-date == ET-date, so it stays byte-equal); Task 8 moves inert dataclasses but adds no `PaperBook` field and changes no serialized output (run the full suite); `src/autotrader/**` is never touched (firewall check in Task 15).
