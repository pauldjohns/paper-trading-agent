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

One-time off-machine backup setup (use a private repo) — create the orphan data branch +
worktree once so the EOD task can commit/push the book off the laptop:

```bash
git checkout --orphan paper-book-data && git rm -rf . && \
  git commit --allow-empty -m "init paper-book-data" && git push -u origin paper-book-data && \
  git checkout claude/live-03-autonomy
git worktree add ~/<repo>-book-data <data-branch>
```

KILL-SWITCH (one gesture): disable all four recurring tasks (scheduled-tasks MCP)
AND revoke the broker tool approval. Rehearse in the dry-run (RUNBOOK gating).

DO NOT ARM FOR REAL until the RUNBOOK "Autonomous mode gating" checklist passes.
