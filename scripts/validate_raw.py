#!/usr/bin/env python3
"""Central raw-ingest for the LIVE-02 daily ARM: trim + merge + normalize +
VALIDATE the agent-saved raw MCP dumps into the canonical files run_paper_book.py
expects. ONE command replaces the ad-hoc steps the agent ran by hand on
2026-06-24.

Per [[subagent-ingest-centrally]]: subagents only DUMP raw MCP JSON verbatim;
this script (the central, guarded ingest) does ALL transformation + validation.

Inputs in data/live/paper_book/raw/ (agent-saved, verbatim):
  scan_fetch.json        full run_scan dump        -> trimmed to scan.json (top-N by mcap)
  trad_b*.json           get_equity_tradability    -> merged to tradability.json
  fund_b*.json           get_equity_fundamentals   -> merged to fundamentals.json
  quotes_b*.json         get_equity_quotes (arm)   -> merged to quotes.json
  earnings.json          get_earnings_calendar     (used as-is)
  hist_<SYM>.json        get_equity_historicals    (one file per symbol; validated)

Validation (fails LOUD, exit 2): every normalize_* parses; and for each of the
top-N symbols the hist file is the RIGHT symbol (results[0].symbol == filename),
has >= 253 bars, last bar == signal_date, passes completed_bar_guard, its
signal-date close matches the scan's settled Close (content-based transposition
check), and no two hist files share identical bar content.

Usage: validate_raw.py <signal_date_iso> [top_n=30]
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from autotrader_live import mcp_live, paper_monitor  # noqa: E402

RAW = REPO / "data" / "live" / "paper_book" / "raw"


def _today() -> dt.date:
    return dt.datetime.now(ZoneInfo("America/New_York")).date()


def _merge(files: list[Path], out: str) -> int:
    results: list = []
    for f in sorted(files):
        results.extend(json.loads(f.read_text())["data"]["results"])
    (RAW / out).write_text(json.dumps({"data": {"results": results}}))
    return len(results)


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        raise SystemExit("usage: validate_raw.py <signal_date_iso> [top_n=30] [today_iso]")
    signal_date = dt.date.fromisoformat(argv[1])
    top_n = int(argv[2]) if len(argv) > 2 else 30
    today = dt.date.fromisoformat(argv[3]) if len(argv) > 3 else _today()
    # Compute the sentinel path from the LIVE RAW (not a module-level constant) so
    # tests that monkeypatch `RAW` write/check the same path. Success-only artifact:
    # clear any stale one up front.
    sentinel = RAW / "arm_complete"
    if sentinel.exists():
        sentinel.unlink()
    problems: list[str] = []

    # ── 1. Trim the full scan to the top-N by market cap -> scan.json ───────────
    full = json.loads((RAW / "scan_fetch.json").read_text())
    rows = full["data"]["result"]["results"]
    rows_sorted = sorted(rows, key=lambda r: float(r["columns"]["Market cap"]), reverse=True)
    top = rows_sorted[:top_n]
    (RAW / "scan.json").write_text(json.dumps({"data": {"result": {"results": top}}}))
    top_syms = [r["columns"]["Symbol"] for r in top]
    scan_close = {r["columns"]["Symbol"]: float(r["columns"]["Close"]) for r in top}
    print(f"scan: {len(rows)} matches -> top {len(top)} by mcap -> scan.json")

    # ── 2. Merge batch dumps -> canonical files ────────────────────────────────
    n_trad = _merge(list(RAW.glob("trad_b*.json")), "tradability.json")
    n_fund = _merge(list(RAW.glob("fund_b*.json")), "fundamentals.json")
    n_quot = _merge(list(RAW.glob("quotes_b*.json")), "quotes.json")
    print(f"merged: tradability={n_trad} fundamentals={n_fund} quotes={n_quot}")

    # ── 3. Central normalize (guarded — raises loudly on malformed input) ───────
    trad = mcp_live.normalize_tradability(json.loads((RAW / "tradability.json").read_text()))
    fund = mcp_live.normalize_fundamentals(json.loads((RAW / "fundamentals.json").read_text()))
    quotes = mcp_live.normalize_quotes(json.loads((RAW / "quotes.json").read_text()))
    earn = mcp_live.normalize_earnings(json.loads((RAW / "earnings.json").read_text()))
    print(f"normalized OK: trad={len(trad)} fund={len(fund)} quotes={len(quotes)} earnings={len(earn)}")

    tradeable = [s for s in top_syms if s in trad and trad[s].tradeable]
    print(f"tradeable: {len(tradeable)}/{len(top_syms)}  "
          f"(not: {[s for s in top_syms if s not in tradeable] or 'none'})")

    # earnings blackout window [signal_date, signal_date+5]
    deadline = signal_date + dt.timedelta(days=5)
    black = sorted(s for s, e in earn.items()
                   if s in top_syms and not e.reported and signal_date <= e.report_date <= deadline)
    print(f"earnings-blackout in {signal_date}..{deadline}: {black or 'none'}")

    # ── 4. Per-symbol historicals validation (the transposition / look-ahead gate)
    bar_hashes: dict[str, list[str]] = {}
    print(f"\nhist validation (signal_date={signal_date}):")
    for s in top_syms:
        f = RAW / f"hist_{s}.json"
        if not f.exists():
            problems.append(f"{s}: MISSING hist file")
            print(f"  {s:6s} MISSING"); continue
        raw = json.loads(f.read_text())
        r0 = raw["data"]["results"][0]
        r0sym = r0.get("symbol", "(none)")
        try:
            df = mcp_live.normalize_bars(raw)
        except Exception as e:  # noqa: BLE001
            problems.append(f"{s}: normalize_bars failed: {e}")
            print(f"  {s:6s} normalize FAIL"); continue
        n, last, hc = len(df), df["date"].iloc[-1], float(df["close"].iloc[-1])
        sc = scan_close.get(s, float("nan"))
        try:
            paper_monitor.completed_bar_guard(df, signal_date); guard = "OK"
        except Exception as e:  # noqa: BLE001
            guard = "FAIL"; problems.append(f"{s}: guard {type(e).__name__}")
        match = abs(hc - sc) <= max(0.05, 0.01 * sc)
        if r0sym not in (s, "(none)"): problems.append(f"{s}: results[0].symbol={r0sym} != filename")
        if n < 253: problems.append(f"{s}: {n} bars (<253)")
        if last != signal_date: problems.append(f"{s}: last bar {last} != signal_date")
        if not match: problems.append(f"{s}: close {hc} != scan {sc} (transposition?)")
        bars = raw["data"]["results"][0]["bars"]
        bar_hashes.setdefault(hashlib.md5(json.dumps(bars, sort_keys=True).encode()).hexdigest(), []).append(s)
        print(f"  {s:6s} sym={str(r0sym):5s} bars={n:>4d} last={last} "
              f"close={hc:>9.2f} scan={sc:>9.2f} guard={guard} {'ok' if match else 'MISMATCH'}")

    dups = {h: v for h, v in bar_hashes.items() if len(v) > 1}
    if dups:
        problems.append(f"duplicate bar-content (transposition!): {dups}")

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

    print()
    if problems:
        print(f"VALIDATION FAILED ({len(problems)} problem(s)):")
        for p in problems:
            print("  -", p)
        return 2
    sentinel.write_text(f"arm_complete signal_date={signal_date} today={today}\n")
    print(f"VALIDATION PASSED: coverage+freshness OK for {len(top_syms)} symbols; "
          f"sentinel -> {sentinel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
