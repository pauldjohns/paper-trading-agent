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
