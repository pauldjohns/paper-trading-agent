#!/usr/bin/env python3
"""Task-7 price-cache build tool (reproducible record of the cache build).

The agent fetches raw `get_equity_historicals` adjustment='split' JSON pages (gated, read-only,
≤10 symbols/call, paged) into data/raw/<SYM>_day_split_p<N>.json. This script does the rest,
CENTRALLY (Python never calls the MCP): parse via the tested parse_historicals, correct three
documented upstream data quirks, write the parquet cache via the tested DataStore, then run the
acceptance gate and (optionally) write the provenance manifest.

WHY this exists: the cache build is not unit-tested production code, but it must be reproducible.
verify_series checks gaps/dtype/coverage but NOT value sanity, so the >40% overnight discontinuity
scan is the canary that catches a mis-adjusted bar. The three quirks (see the data-foundation note
in PROJECT_CONTEXT.md / STRATEGY_TESTING_SPEC.md §3.1 and the session-memory files):

  1. Old splits not back-adjusted by Robinhood's 'split' series (only IWM 2:1 2005-06-09 in-window)
     -> uniform pre-split factor (KNOWN_SPLITS).
  2. Single mis-adjusted bars (the 2025-12-05 sector-split back-adjustment missed ~20 individual
     2005-08 bars, leaving them at exactly 2x neighbors) -> _repair scales any bar within ~7% of a
     clean split factor (0.5/2/3) of its centered 11-bar median.
  3. Dropped-interpolated gaps (parse_historicals drops interpolated bars; 8 early days had only an
     interpolated bar) -> _complete forward-fills to the SPY calendar so all symbols share one axis.

Run from the repo root:  ./.venv/bin/python scripts/build_price_cache.py [--write-manifest]
(omit --write-manifest to dry-run the gate; the manifest is written only if every symbol is clean.)
"""
import sys
import csv
import glob
import json
import hashlib
import datetime as dt
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "src"))

from autotrader.ingest import parse_historicals          # noqa: E402
from autotrader.datastore import DataStore               # noqa: E402
from autotrader.calendar_nyse import TradingCalendar     # noqa: E402
from autotrader.datacheck import verify_series           # noqa: E402

RAW, CACHE, MANIFEST = REPO / "data/raw", REPO / "data/cache", REPO / "data/manifest.csv"
UNIVERSE = ["SPY", "XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU",
            "QQQ", "DIA", "IWM", "IEF", "AGG"]
KNOWN_SPLITS = {"IWM": [(dt.date(2005, 6, 9), 0.5)]}     # un-back-adjusted old split: pre-split x0.5
_FACTORS = [(0.5, 2.0), (2.0, 0.5), (1.0 / 3.0, 3.0), (3.0, 1.0 / 3.0)]
DISCONTINUITY = 0.40
MIN_START, MIN_ROWS = dt.date(2005, 2, 1), 4000


def _correct_splits(symbol, df):
    notes = []
    for d, f in KNOWN_SPLITS.get(symbol, []):
        m = df["date"] < d
        if m.any():
            for col in ("open", "high", "low", "close"):
                df.loc[m, col] = df.loc[m, col] * f
            notes.append(f"{f}x to {int(m.sum())} pre-{d} bars (old-split back-adjust)")
    return df, notes


def _repair(df):
    """Scale a single bar back into line when its close is within ~7% of a clean split factor
    (0.5/2/3) of its centered 11-bar median. Real ETFs do not move 50%+ in a day and revert, so
    this only fires on upstream mis-adjustments; the 11-bar median is robust to short runs."""
    notes = []
    base = df["close"].rolling(11, center=True, min_periods=3).median()
    for i in range(len(df)):
        b = base.iloc[i]
        if not b or b <= 0:
            continue
        ratio = df["close"].iloc[i] / b
        for obs, rep in _FACTORS:
            if abs(ratio - obs) <= 0.07 * obs:
                for col in ("open", "high", "low", "close"):
                    df.iat[i, df.columns.get_loc(col)] = df.iat[i, df.columns.get_loc(col)] * rep
                notes.append(f"{df['date'].iloc[i]} x{rep:g} (ratio {ratio:.2f} to local median)")
                break
    return df, notes


def _complete(df, cal_dates):
    """Forward-fill the few early days a symbol lacks (only interpolated bars existed there, dropped
    by parse_historicals) onto the shared SPY calendar. Filled day: O=H=L=prior close, volume=0."""
    g = df.set_index("date").reindex(cal_dates)
    missing = int(g["close"].isna().sum())
    g["close"] = g["close"].ffill()
    for col in ("open", "high", "low"):
        g[col] = g[col].fillna(g["close"])
    g["volume"] = g["volume"].fillna(0).astype(int)
    g = g.reset_index().rename(columns={"index": "date"})
    return g[["date", "open", "high", "low", "close", "volume"]], missing


def _discontinuities(df):
    c = df["close"].tolist()
    return [(str(df["date"].iloc[i]), round(c[i] / c[i - 1] - 1.0, 4))
            for i in range(1, len(c)) if abs(c[i] / c[i - 1] - 1.0) > DISCONTINUITY]


def _ingest(symbol):
    files = sorted(glob.glob(str(RAW / f"{symbol}_day_split_p*.json")))
    if not files:
        raise FileNotFoundError(f"no raw split pages for {symbol} in {RAW}")
    frames = []
    for f in files:
        with open(f) as fh:
            frames.append(parse_historicals(json.load(fh)))
    df = (pd.concat(frames, ignore_index=True)
          .drop_duplicates(subset="date", keep="first").sort_values("date").reset_index(drop=True))
    df, n1 = _correct_splits(symbol, df)
    df, n2 = _repair(df)
    return df, n1 + n2


def main(write_manifest):
    ingested = {s: _ingest(s) for s in UNIVERSE}
    cal_dates = ingested["SPY"][0]["date"].tolist()
    store = DataStore(cache_dir=str(CACHE))
    fills = {}
    for s in UNIVERSE:
        df, nfill = _complete(ingested[s][0], cal_dates)
        fills[s] = nfill
        store.write(s, "day", "split", df)

    cal = TradingCalendar.from_datastore(store, "SPY", "day", "split")
    out, all_clean = {}, True
    for s in UNIVERSE:
        df = store.load(s, "day", "split")
        probs = verify_series(df, cal, min_start=MIN_START, min_rows=MIN_ROWS)
        d = _discontinuities(df)
        if d:
            probs = probs + [f"discontinuity>40%: {d}"]
        if probs:
            all_clean = False
        out[s] = {"n_rows": int(len(df)), "start": str(df["date"].iloc[0]), "end": str(df["date"].iloc[-1]),
                  "filled": fills[s], "repairs": ingested[s][1], "verify": probs}
    print(json.dumps({"all_clean": all_clean, "symbols": out}, indent=2))

    if write_manifest and all_clean:
        ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = []
        for s in UNIVERSE:
            df = store.load(s, "day", "split")
            with open(CACHE / f"{s}_day_split.parquet", "rb") as fh:
                sha = hashlib.sha256(fh.read()).hexdigest()
            rows.append([s, "day", "split", ts, str(df["date"].iloc[0]), str(df["date"].iloc[-1]), int(len(df)), sha])
        with open(MANIFEST, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["symbol", "interval", "adjustment", "fetched_at", "start", "end", "n_rows", "sha256"])
            w.writerows(rows)
        print(f"WROTE manifest: {len(rows)} rows -> {MANIFEST}")
    elif write_manifest:
        print("NOT writing manifest: gate not clean")
    return 0 if all_clean else 1


if __name__ == "__main__":
    sys.exit(main("--write-manifest" in sys.argv))
