# LIVE-03 HEARTBEAT (recurring, every ~11 min, RTH local-hour window)

MCP-FREE. Do not call the broker at all.
1. Run `heartbeat_check.py`.
2. If stdout starts with STALE or NOT_ARMED -> push the line to the operator.
3. Otherwise (SILENT/OK) -> do nothing.
Re-enable this task when arming autonomous mode; PAUSE it on an intentional stop
(else it false-STALEs on the next un-armed RTH morning... which the armed-today
fix now downgrades to a NOT_ARMED alert — still pause on intentional stop).
