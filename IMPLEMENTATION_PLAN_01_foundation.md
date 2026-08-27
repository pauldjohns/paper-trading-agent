# Foundation & Canary — Implementation Plan (Plan 01 of 4) — v2

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build and golden-test the trustworthy foundation of the backtest harness — data ingest/cache, NYSE calendar, and the execution simulator (cost model, T+1 settled-cash ledger, daily-bar stop fills) — so every later result rests on a verified base.

**Architecture:** A small Python package (`autotrader`). The live MCP is NOT called from Python; the agent saves raw `get_equity_historicals` JSON to `data/raw/`, a tested ingester normalizes it to parquet in `data/cache/`, and a `DataStore` serves bars. The execution simulator is pure, deterministic, and frozen behind a golden fixture — which exercises a **buy, a gap-through stop, T+1 settlement, and a GFV-blocked redeploy** — before any strategy is built (`STRATEGY_TESTING_SPEC.md` §7; PROJECT_CONTEXT.md "pin behavior with a canary first").

**Tech Stack:** Python 3.10+, pandas, numpy, pyarrow (parquet), pytest. No network in tests — everything runs against fixtures.

**Reads:** [`STRATEGY_TESTING_SPEC.md`](STRATEGY_TESTING_SPEC.md) (§3.1-§3.7, §7) and [`COST_MODEL.md`](COST_MODEL.md).

**v2 note:** revised after three independent reviews (which compiled and ran the code). Fixes: real Corwin-Schultz test (was a `0==0` tautology); stops integrated into the simulator + golden (was orphaned); S3 strategy-level cost floor encoded (was unrepresentable); Python pin lowered to 3.10; `git init` / data-dir / provenance-manifest setup added. The `adjustment_type='all'` @ daily MCP assumption was **verified live 2026-06-16** (works; today's bar returns `interpolated:true` and is filtered).

**This plan is the canary. Plans 02-04 are NOT started until every task here is green.**

---

## File structure (locked here)

```
<repo-root>/
  pyproject.toml
  .gitignore
  src/autotrader/{__init__,config,ingest,datastore,calendar_nyse,costs,ledger,stops,simulator}.py
  tests/{test_ingest,test_datastore,test_calendar_nyse,test_costs,test_ledger,test_stops,test_simulator}.py
  tests/fixtures/{historicals_aapl_2026ytd.json, golden_simulator_sequence.json}
  data/raw/.gitkeep      data/cache/.gitkeep      data/manifest.csv   (manifest IS tracked)
```

---

## Task 0: Project scaffold + environment gate

**Files:** Create `pyproject.toml`, `src/autotrader/__init__.py`, `.gitignore`, data dirs.

- [ ] **Step 0: Preconditions (gate — do not skip).**

Run: `python3 --version`
Expected: `Python 3.10.x` or higher. If lower, install 3.11 (`brew install python@3.11`) and use `python3.11` in Step 4.
Run: `git rev-parse --is-inside-work-tree 2>/dev/null || git init`
Run: `git config user.email && git config user.name`
Expected: both print non-empty values. If empty, set them before any commit (per PROJECT_CONTEXT.md Git workflow).

- [ ] **Step 1: Create `pyproject.toml`**

```toml
[project]
name = "autotrader"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["pandas>=2.1", "numpy>=1.26", "pyarrow>=15.0"]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
```

- [ ] **Step 2: Create `src/autotrader/__init__.py`**

```python
"""autotrader: backtest + paper-monitor harness for the Robinhood Agentic MCP."""
```

- [ ] **Step 3: Create `.gitignore`** (raw pulls are ignored, but the provenance manifest is tracked)

```
__pycache__/
*.pyc
.pytest_cache/
.venv/
data/raw/*
data/cache/*
!data/raw/.gitkeep
!data/cache/.gitkeep
!data/manifest.csv
```

- [ ] **Step 4: Create dirs + env, verify import**

```bash
mkdir -p data/raw data/cache
touch data/raw/.gitkeep data/cache/.gitkeep
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
python -c "import autotrader; print('ok')"
```
Expected: `ok`. (All later `python`/`pytest` commands assume this venv is active and the **repo root** is the CWD — fixture paths are repo-root-relative.)

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml src/autotrader/__init__.py .gitignore data/raw/.gitkeep data/cache/.gitkeep
git commit -m "chore: project scaffold + env gate for autotrader harness"
```

---

## Task 1: Real MCP fixture + config (incl. S3 cost floor)

**Files:** Create `tests/fixtures/historicals_aapl_2026ytd.json`, `src/autotrader/config.py`.

- [ ] **Step 1: Save the real MCP fixture** `tests/fixtures/historicals_aapl_2026ytd.json`:

```json
{"data":{"results":[{"symbol":"AAPL","interval":"day","bounds":"regular","bars":[
{"begins_at":"2026-01-02T00:00:00Z","open_price":"272.255000","close_price":"271.010000","high_price":"277.840000","low_price":"269.000000","volume":37838054,"session":"reg"},
{"begins_at":"2026-01-05T00:00:00Z","open_price":"270.640000","close_price":"267.260000","high_price":"271.510000","low_price":"266.140000","volume":45647190,"session":"reg"},
{"begins_at":"2026-01-06T00:00:00Z","open_price":"267.000000","close_price":"262.360000","high_price":"267.550000","low_price":"262.120000","volume":52352090,"session":"reg"}
]}]}}
```

- [ ] **Step 2: Create `src/autotrader/config.py`**

```python
"""Static configuration: universes, liquidity tiers, fee constants, strategy cost floors."""

# Survivorship-clean, gating-grade momentum universe (9 original Select Sector SPDRs).
SECTOR_SPDRS = ["XLK", "XLF", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLU"]
INDEX_ETFS = ["SPY", "QQQ", "DIA", "IWM"]
BOND_ETFS = ["IEF", "AGG"]

# Liquidity tiers (axis A: instrument). Calm value = round-trip fraction of price.
TIER_INDEX_ETF = "index_etf"
TIER_SECTOR_SPDR = "sector_spdr"
TIER_MEGA_CAP = "mega_cap"
TIER_OTHER = "other"

TIER_CALM_ROUNDTRIP = {
    TIER_INDEX_ETF: 0.0010,    # 0.10%
    TIER_SECTOR_SPDR: 0.0015,  # 0.15%
    TIER_MEGA_CAP: 0.0020,     # 0.20%
    TIER_OTHER: 0.0050,        # 0.50%
}
TIER_MAX_STRESS = {
    TIER_INDEX_ETF: 5.0, TIER_SECTOR_SPDR: 5.0, TIER_MEGA_CAP: 5.0, TIER_OTHER: 5.0,
}

# Axis B: strategy-level punitive cost floor (COST_MODEL.md section 4). S3 dip-buys when
# spreads are widest, so it is charged a floor REGARDLESS of its (cheap) ETF instrument tier.
# Cost = max(instrument_tier_calm, strategy_floor) x stress.
S3_COST_FLOOR = 0.0045   # 0.45% round-trip floor for the mean-reversion null-test.

# Regulatory pass-throughs (SELL side only). Verified 2026-06-16 (COST_MODEL.md section 6).
SEC_SECTION31_RATE = 0.0000206   # $20.60 per $1,000,000 of sell proceeds.
FINRA_TAF_PER_SHARE = 0.000166   # per share sold.
FINRA_TAF_MAX = 8.30             # per-trade cap.
```

- [ ] **Step 3: Commit**

```bash
git add tests/fixtures/historicals_aapl_2026ytd.json src/autotrader/config.py
git commit -m "feat: real MCP fixture + config incl. S3 strategy cost floor"
```

---

## Task 2: Ingest — raw MCP JSON → normalized DataFrame (filters interpolated bars)

**Files:** Create `src/autotrader/ingest.py`, `tests/test_ingest.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ingest.py
import json, pytest, pandas as pd
from autotrader.ingest import parse_historicals

def test_parse_historicals_from_real_fixture():
    with open("tests/fixtures/historicals_aapl_2026ytd.json") as f:
        df = parse_historicals(json.load(f))
    assert list(df.columns) == ["date", "open", "high", "low", "close", "volume"]
    assert len(df) == 3
    assert str(df.iloc[0]["date"]) == "2026-01-02"
    assert str(df.iloc[-1]["date"]) == "2026-01-06"
    assert df.iloc[0]["open"] == 272.255
    assert df.iloc[0]["close"] == 271.010
    assert df.iloc[0]["volume"] == 37838054

def test_parse_drops_interpolated_bars():
    raw = {"data": {"results": [{"bars": [
        {"begins_at": "2026-01-02T00:00:00Z", "open_price": "1", "close_price": "1",
         "high_price": "1", "low_price": "1", "volume": 10, "session": "reg"},
        {"begins_at": "2026-01-03T00:00:00Z", "open_price": "1", "close_price": "1",
         "high_price": "1", "low_price": "1", "volume": 0, "session": "reg", "interpolated": True},
    ]}]}}
    df = parse_historicals(raw)
    assert len(df) == 1  # interpolated gap-fill bar dropped

def test_parse_historicals_rejects_empty():
    with pytest.raises(ValueError):
        parse_historicals({"data": {"results": []}})
```

- [ ] **Step 2: Run test to verify it fails** — `pytest tests/test_ingest.py -v` → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/ingest.py
"""Normalize raw get_equity_historicals JSON into a clean OHLCV DataFrame."""
import pandas as pd


def parse_historicals(raw: dict) -> pd.DataFrame:
    """One get_equity_historicals response -> sorted OHLCV DataFrame.

    Columns: date (datetime.date), open/high/low/close (float), volume (int).
    Drops interpolated=true gap-fill bars (per MCP guidance). Raises ValueError if empty.
    """
    results = raw.get("data", {}).get("results", [])
    if not results or not results[0].get("bars"):
        raise ValueError("no bars in historicals response")
    rows = []
    for b in results[0]["bars"]:
        if b.get("interpolated"):
            continue
        rows.append({
            "date": pd.to_datetime(b["begins_at"]).date(),
            "open": float(b["open_price"]), "high": float(b["high_price"]),
            "low": float(b["low_price"]), "close": float(b["close_price"]),
            "volume": int(b["volume"]),
        })
    if not rows:
        raise ValueError("no non-interpolated bars")
    df = pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])
    return df.sort_values("date").reset_index(drop=True)
```

- [ ] **Step 4: Run** — `pytest tests/test_ingest.py -v` → PASS (3 passed)
- [ ] **Step 5: Commit** — `git add src/autotrader/ingest.py tests/test_ingest.py && git commit -m "feat: ingest MCP historicals, drop interpolated bars"`

---

## Task 3: DataStore — parquet cache read/write

**Files:** Create `src/autotrader/datastore.py`, `tests/test_datastore.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_datastore.py
import json, pytest, pandas as pd
from autotrader.ingest import parse_historicals
from autotrader.datastore import DataStore

def test_write_then_load_roundtrip(tmp_path):
    with open("tests/fixtures/historicals_aapl_2026ytd.json") as f:
        df = parse_historicals(json.load(f))
    store = DataStore(cache_dir=tmp_path)
    store.write("AAPL", "day", "all", df)
    pd.testing.assert_frame_equal(store.load("AAPL", "day", "all"), df)

def test_load_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        DataStore(cache_dir=tmp_path).load("ZZZZ", "day", "all")

def test_write_rejects_unsorted_or_dupe_dates(tmp_path):
    store = DataStore(cache_dir=tmp_path)
    bad = pd.DataFrame({
        "date": [pd.Timestamp("2026-01-02").date(), pd.Timestamp("2026-01-02").date()],
        "open": [1.0, 1.0], "high": [1.0, 1.0], "low": [1.0, 1.0],
        "close": [1.0, 1.0], "volume": [1, 1]})
    with pytest.raises(ValueError):
        store.write("AAPL", "day", "all", bad)
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/datastore.py
"""Local parquet cache for normalized OHLCV bars."""
from pathlib import Path
import pandas as pd

_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


class DataStore:
    def __init__(self, cache_dir):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, symbol, interval, adjustment):
        return self.cache_dir / f"{symbol}_{interval}_{adjustment}.parquet"

    def write(self, symbol, interval, adjustment, df):
        if list(df.columns) != _COLUMNS:
            raise ValueError(f"unexpected columns: {list(df.columns)}")
        dates = df["date"].tolist()
        if dates != sorted(dates):
            raise ValueError("dates must be sorted ascending")
        if len(set(dates)) != len(dates):
            raise ValueError("duplicate dates not allowed")
        df.to_parquet(self._path(symbol, interval, adjustment), index=False)

    def load(self, symbol, interval, adjustment):
        path = self._path(symbol, interval, adjustment)
        if not path.exists():
            raise FileNotFoundError(str(path))
        return pd.read_parquet(path)[_COLUMNS].reset_index(drop=True)
```

- [ ] **Step 4: Run** → PASS (3 passed)
- [ ] **Step 5: Commit** — `git add src/autotrader/datastore.py tests/test_datastore.py && git commit -m "feat: parquet DataStore with date validation"`

---

## Task 4: NYSE trading calendar (derived from SPY bar dates)

**Files:** Create `src/autotrader/calendar_nyse.py`, `tests/test_calendar_nyse.py`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_calendar_nyse.py
import datetime as dt, pytest
from autotrader.calendar_nyse import TradingCalendar

DAYS = [dt.date(2026, 1, 2), dt.date(2026, 1, 5), dt.date(2026, 1, 6), dt.date(2026, 1, 7)]

def test_is_trading_day():
    cal = TradingCalendar(DAYS)
    assert cal.is_trading_day(dt.date(2026, 1, 5)) is True
    assert cal.is_trading_day(dt.date(2026, 1, 3)) is False  # Saturday

def test_next_trading_day_skips_weekend():
    assert TradingCalendar(DAYS).next_trading_day(dt.date(2026, 1, 2)) == dt.date(2026, 1, 5)

def test_next_trading_day_from_nontrading_day():
    assert TradingCalendar(DAYS).next_trading_day(dt.date(2026, 1, 3)) == dt.date(2026, 1, 5)

def test_next_trading_day_past_end_raises():
    with pytest.raises(ValueError):
        TradingCalendar(DAYS).next_trading_day(dt.date(2026, 1, 7))

def test_add_trading_days():
    cal = TradingCalendar(DAYS)
    assert cal.add_trading_days(dt.date(2026, 1, 2), 2) == dt.date(2026, 1, 6)
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/calendar_nyse.py
"""Trading calendar derived from the dates present in cached SPY bars."""
import bisect
import datetime as dt


class TradingCalendar:
    def __init__(self, trading_days):
        self._days = sorted(set(trading_days))
        self._set = set(self._days)

    @classmethod
    def from_datastore(cls, store, symbol="SPY", interval="day", adjustment="all"):
        return cls(store.load(symbol, interval, adjustment)["date"].tolist())

    def is_trading_day(self, d: dt.date) -> bool:
        return d in self._set

    def next_trading_day(self, d: dt.date) -> dt.date:
        i = bisect.bisect_right(self._days, d)
        if i >= len(self._days):
            raise ValueError(f"no trading day after {d} within calendar range")
        return self._days[i]

    def add_trading_days(self, d: dt.date, n: int) -> dt.date:
        cur = d
        for _ in range(n):
            cur = self.next_trading_day(cur)
        return cur
```

- [ ] **Step 4: Run** → PASS (5 passed)
- [ ] **Step 5: Commit** — `git add src/autotrader/calendar_nyse.py tests/test_calendar_nyse.py && git commit -m "feat: NYSE calendar from SPY bar dates"`

---

## Task 5: Cost model (fees + Corwin-Schultz + tier×stress + strategy floor)

**Files:** Create `src/autotrader/costs.py`, `tests/test_costs.py`.

- [ ] **Step 1: Write the failing test** (CS tested on a **positive** spread, not the clamp; plus the S3 floor)

```python
# tests/test_costs.py
import math
from autotrader.costs import (corwin_schultz_spread, average_cs_spread,
                              regulatory_sell_fees, effective_roundtrip_cost)
from autotrader import config

def test_corwin_schultz_positive_case_real_value():
    # Wide ranges -> a genuinely POSITIVE spread (NOT the clamp-to-zero path).
    s = corwin_schultz_spread(high_t=110, low_t=90, high_t1=112, low_t1=92)
    assert s > 0.0                      # kills the 0==0 tautology
    assert 0.13 < s < 0.17              # band catches a sign-flip/swap in alpha or beta/gamma

def test_corwin_schultz_zero_clamp_on_negative_estimate():
    # Narrow, non-trending ranges produce a negative raw estimate -> clamped to 0.
    s = corwin_schultz_spread(high_t=101, low_t=99, high_t1=102, low_t1=100)
    assert s == 0.0

def test_corwin_schultz_zero_when_no_range():
    assert corwin_schultz_spread(100, 100, 100, 100) == 0.0

def test_average_cs_spread_over_window():
    bars = [{"high": 110, "low": 90}, {"high": 112, "low": 92}, {"high": 111, "low": 91}]
    avg = average_cs_spread(bars)
    assert avg > 0.0  # mean of the per-pair estimates

def test_regulatory_sell_fees_small():
    fees = regulatory_sell_fees(proceeds=1000.0, shares=4.0)
    assert abs(fees - (1000.0 * config.SEC_SECTION31_RATE + 4.0 * config.FINRA_TAF_PER_SHARE)) < 1e-9
    assert fees < 0.05

def test_taf_cap_applied():
    fees = regulatory_sell_fees(proceeds=10_000.0, shares=10_000_000.0)
    assert fees == 10_000.0 * config.SEC_SECTION31_RATE + config.FINRA_TAF_MAX

def test_effective_roundtrip_tier_only():
    assert effective_roundtrip_cost(config.TIER_INDEX_ETF) == config.TIER_CALM_ROUNDTRIP[config.TIER_INDEX_ETF]

def test_effective_roundtrip_strategy_floor_overrides_cheap_tier():
    # S3 on an index ETF: cheap 0.10% tier must be lifted to the 0.45% floor.
    c = effective_roundtrip_cost(config.TIER_INDEX_ETF, floor=config.S3_COST_FLOOR)
    assert c == config.S3_COST_FLOOR

def test_effective_roundtrip_stress_scales_and_clamps():
    base = effective_roundtrip_cost(config.TIER_INDEX_ETF)
    assert effective_roundtrip_cost(config.TIER_INDEX_ETF, stress=3.0) == base * 3.0
    assert effective_roundtrip_cost(config.TIER_INDEX_ETF, stress=99.0) == base * config.TIER_MAX_STRESS[config.TIER_INDEX_ETF]
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/costs.py
"""Cost model per COST_MODEL.md.

Default cost path = effective_roundtrip_cost (per-tier x stress, with an optional
strategy-level floor). Corwin-Schultz is the spread-estimation REFINEMENT (used later to
calibrate/replace the tier baseline). NOTE: corwin_schultz_spread is the raw 2-day
estimator; the overnight-gap adjustment (COST_MODEL.md section 6) is deferred to Plan 03 —
do not use the single-pair value directly for per-bar cost; use average_cs_spread.
"""
import math
from autotrader import config

_K = 3 - 2 * math.sqrt(2)


def corwin_schultz_spread(high_t, low_t, high_t1, low_t1) -> float:
    """Raw two-day Corwin-Schultz (2012) proportional spread. Negative -> clamped to 0."""
    if min(low_t, low_t1) <= 0:
        raise ValueError("prices must be positive")
    beta = math.log(high_t / low_t) ** 2 + math.log(high_t1 / low_t1) ** 2
    gamma = math.log(max(high_t, high_t1) / min(low_t, low_t1)) ** 2
    alpha = (math.sqrt(2 * beta) - math.sqrt(beta)) / _K - math.sqrt(gamma / _K)
    return max(2 * (math.exp(alpha) - 1) / (1 + math.exp(alpha)), 0.0)


def average_cs_spread(bars) -> float:
    """Mean of per-consecutive-pair Corwin-Schultz estimates over a list of OHLC bars.

    bars: list of dicts with 'high' and 'low'. Requires >= 2 bars.
    (Overnight-gap adjustment deferred to Plan 03 per COST_MODEL.md section 6.)
    """
    if len(bars) < 2:
        raise ValueError("need >= 2 bars")
    vals = [corwin_schultz_spread(bars[i]["high"], bars[i]["low"],
                                  bars[i + 1]["high"], bars[i + 1]["low"])
            for i in range(len(bars) - 1)]
    return sum(vals) / len(vals)


def regulatory_sell_fees(proceeds: float, shares: float) -> float:
    """SEC Section 31 (on gross proceeds) + FINRA TAF (per share, capped). Sell side only."""
    return proceeds * config.SEC_SECTION31_RATE + min(
        shares * config.FINRA_TAF_PER_SHARE, config.FINRA_TAF_MAX)


def effective_roundtrip_cost(tier: str, floor: float = None, stress: float = 1.0) -> float:
    """Round-trip cost fraction = max(instrument-tier calm, strategy floor) x clamped stress."""
    base = config.TIER_CALM_ROUNDTRIP[tier]
    if floor is not None:
        base = max(base, floor)
    stress = max(1.0, min(stress, config.TIER_MAX_STRESS[tier]))
    return base * stress
```

- [ ] **Step 4: Run** → PASS (9 passed). If `test_corwin_schultz_positive_case_real_value` fails the band, STOP — the estimator has a sign/transposition bug (the whole point of this test).
- [ ] **Step 5: Commit** — `git add src/autotrader/costs.py tests/test_costs.py && git commit -m "feat: cost model (CS spread tested positive, reg fees, tier×stress + S3 floor)"`

---

## Task 6: T+1 shared settled-cash ledger

**Files:** Create `src/autotrader/ledger.py`, `tests/test_ledger.py`. (Unchanged from v1 — already correct per review.)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_ledger.py
import datetime as dt, pytest
from autotrader.calendar_nyse import TradingCalendar
from autotrader.ledger import SettledCashLedger

DAYS = [dt.date(2026, 1, 2), dt.date(2026, 1, 5), dt.date(2026, 1, 6), dt.date(2026, 1, 7)]
def make(): return SettledCashLedger(calendar=TradingCalendar(DAYS))

def test_deposit_is_immediately_settled():
    led = make(); led.deposit(1000.0, on=dt.date(2026, 1, 2))
    assert led.settled_cash(dt.date(2026, 1, 2)) == 1000.0

def test_sale_proceeds_settle_next_trading_day():
    led = make(); led.deposit(1000.0, on=dt.date(2026, 1, 2))
    led.execute_buy(1000.0, on=dt.date(2026, 1, 2))
    led.record_sale(1050.0, trade_date=dt.date(2026, 1, 5))
    assert led.settled_cash(dt.date(2026, 1, 5)) == 0.0
    assert led.settled_cash(dt.date(2026, 1, 6)) == 1050.0

def test_buy_with_unsettled_funds_is_refused_gfv_guard():
    led = make(); led.deposit(1000.0, on=dt.date(2026, 1, 2))
    led.execute_buy(1000.0, on=dt.date(2026, 1, 2))
    led.record_sale(1000.0, trade_date=dt.date(2026, 1, 5))
    with pytest.raises(ValueError):
        led.execute_buy(500.0, on=dt.date(2026, 1, 5))
    led.execute_buy(500.0, on=dt.date(2026, 1, 6))
    assert led.settled_cash(dt.date(2026, 1, 6)) == 500.0

def test_can_buy_reflects_settled_only():
    led = make(); led.deposit(300.0, on=dt.date(2026, 1, 2))
    assert led.can_buy(300.0, on=dt.date(2026, 1, 2)) is True
    assert led.can_buy(300.01, on=dt.date(2026, 1, 2)) is False
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/ledger.py
"""Single shared settled-cash ledger with T+1 dated tranches (conservative GFV proxy).

A sale's proceeds become spendable only on the next trading day. A buy may draw ONLY
settled cash; spending unsettled proceeds is refused (conservative proxy for the live
Good-Faith-Violation rule, spec section 3.6). One pool, shared across all sleeves.
"""
import datetime as dt
from dataclasses import dataclass, field


@dataclass
class _Tranche:
    amount: float
    settle_date: dt.date


@dataclass
class SettledCashLedger:
    calendar: object
    _tranches: list = field(default_factory=list)

    def deposit(self, amount: float, on: dt.date) -> None:
        self._tranches.append(_Tranche(amount, on))

    def settled_cash(self, as_of: dt.date) -> float:
        return round(sum(t.amount for t in self._tranches if t.settle_date <= as_of), 10)

    def can_buy(self, amount: float, on: dt.date) -> bool:
        return amount <= self.settled_cash(on) + 1e-9

    def execute_buy(self, amount: float, on: dt.date) -> None:
        if not self.can_buy(amount, on):
            raise ValueError(
                f"insufficient settled cash on {on}: need {amount}, "
                f"have {self.settled_cash(on)} (unsettled proceeds cannot fund a buy)")
        remaining = amount
        for t in sorted(self._tranches, key=lambda x: x.settle_date):
            if t.settle_date > on or remaining <= 0:
                continue
            take = min(t.amount, remaining)
            t.amount -= take
            remaining -= take
        self._tranches = [t for t in self._tranches if t.amount > 1e-12]

    def record_sale(self, proceeds: float, trade_date: dt.date) -> None:
        self._tranches.append(_Tranche(proceeds, self.calendar.next_trading_day(trade_date)))
```

- [ ] **Step 4: Run** → PASS (4 passed)
- [ ] **Step 5: Commit** — `git add src/autotrader/ledger.py tests/test_ledger.py && git commit -m "feat: T+1 shared settled-cash ledger with GFV-guard"`

---

## Task 7: Daily-bar stop-fill rules

**Files:** Create `src/autotrader/stops.py`, `tests/test_stops.py`.

- [ ] **Step 1: Write the failing test** (note: test #2 name now correctly says **minus** slippage)

```python
# tests/test_stops.py
from autotrader.stops import stop_fill_price

def test_gap_through_fills_at_open_worse_than_stop():
    bar = {"open": 90.0, "high": 92.0, "low": 88.0, "close": 91.0}
    assert stop_fill_price(bar, stop_price=95.0, slippage_frac=0.0) == 90.0  # open, NOT 95

def test_intrabar_pierce_fills_at_stop_minus_slippage():
    bar = {"open": 100.0, "high": 101.0, "low": 96.0, "close": 99.0}
    fill = stop_fill_price(bar, stop_price=97.0, slippage_frac=0.001)
    assert abs(fill - 97.0 * (1 - 0.001)) < 1e-9  # sell fills worse = lower

def test_no_trigger_returns_none():
    bar = {"open": 100.0, "high": 101.0, "low": 98.0, "close": 99.5}
    assert stop_fill_price(bar, stop_price=97.0, slippage_frac=0.001) is None
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/stops.py
"""Daily-bar stop-fill modeling for protective SELL stops (spec section 3.5).

- Gap-through (open <= stop): fill at the OPEN (worse than stop; the gapped open already
  embeds the adverse move, so no extra slippage is layered on).
- Intrabar pierce (low <= stop < open): fill at stop_price minus slippage (a stop_market
  becomes a market sell on trigger).
- No touch (low > stop): no fill (None).
A SELL stop never fills ABOVE the stop, so this never fabricates downside protection.
"""


def stop_fill_price(bar: dict, stop_price: float, slippage_frac: float):
    if bar["open"] <= stop_price:
        return bar["open"]
    if bar["low"] <= stop_price:
        return stop_price * (1 - slippage_frac)
    return None
```

- [ ] **Step 4: Run** → PASS (3 passed)
- [ ] **Step 5: Commit** — `git add src/autotrader/stops.py tests/test_stops.py && git commit -m "feat: daily-bar stop-fill rules"`

---

## Task 8: Execution simulator (stops INTEGRATED) + GOLDEN FIXTURE (the canary)

**Files:** Create `src/autotrader/simulator.py`, `tests/test_simulator.py`, `tests/fixtures/golden_simulator_sequence.json`.

The simulator integrates costs + ledger + stops. The golden freezes a full sequence: **buy → place stop → hold → gap-through stop fill → T+1 settlement → GFV-blocked redeploy.** This is the gate for the whole project.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_simulator.py
import datetime as dt, json
from autotrader.calendar_nyse import TradingCalendar
from autotrader.simulator import Simulator
from autotrader import config

DAYS = [dt.date(2026,1,2), dt.date(2026,1,5), dt.date(2026,1,6),
        dt.date(2026,1,7), dt.date(2026,1,8), dt.date(2026,1,9)]
BARS = {"XLK": {
    dt.date(2026,1,2): {"open":100.0,"high":101.0,"low":99.0,"close":100.0},
    dt.date(2026,1,5): {"open":100.0,"high":102.0,"low":100.0,"close":101.0},  # buy fills here
    dt.date(2026,1,6): {"open":101.0,"high":103.0,"low":100.0,"close":102.0},  # stop low 100 > 95: no fill
    dt.date(2026,1,7): {"open":102.0,"high":103.0,"low":101.0,"close":102.5},  # no fill
    dt.date(2026,1,8): {"open":90.0,"high":91.0,"low":88.0,"close":89.0},      # GAP-THROUGH stop@95 -> fill @90
    dt.date(2026,1,9): {"open":89.0,"high":90.0,"low":88.0,"close":89.0},      # settlement day
}}

def test_buy_next_open_with_cost():
    sim = Simulator(calendar=TradingCalendar(DAYS), bars=BARS, slippage_frac=0.0)
    sim.deposit(1000.0, on=dt.date(2026,1,2))
    buy = sim.submit_buy("XLK", signal_date=dt.date(2026,1,2),
                         dollar_amount=1000.0, tier=config.TIER_SECTOR_SPDR)
    assert buy.date == dt.date(2026,1,5)
    assert buy.price == 100.0
    half = config.TIER_CALM_ROUNDTRIP[config.TIER_SECTOR_SPDR] / 2
    assert abs(buy.cost - 1000.0 * half) < 1e-9
    assert abs(buy.shares - (1000.0 - buy.cost) / 100.0) < 1e-9

def test_full_sequence_with_stop_and_gfv_matches_golden():
    sim = Simulator(calendar=TradingCalendar(DAYS), bars=BARS, slippage_frac=0.0)
    sim.deposit(1000.0, on=dt.date(2026,1,2))
    buy = sim.submit_buy("XLK", signal_date=dt.date(2026,1,2),
                         dollar_amount=1000.0, tier=config.TIER_SECTOR_SPDR)
    sim.place_stop("XLK", stop_price=95.0)
    assert sim.evaluate_stops(dt.date(2026,1,6)) == []   # not triggered
    assert sim.evaluate_stops(dt.date(2026,1,7)) == []
    fills = sim.evaluate_stops(dt.date(2026,1,8))        # gap-through trigger
    assert len(fills) == 1
    stop_exit = fills[0]
    assert stop_exit.price == 90.0                       # filled at gapped-down open

    # GFV guard in the integrated sim: stop proceeds (sold 1/8) are unsettled until 1/9.
    assert sim.ledger.can_buy(100.0, on=dt.date(2026,1,8)) is False
    assert sim.ledger.can_buy(100.0, on=dt.date(2026,1,9)) is True

    result = {
        "buy": {"date": str(buy.date), "price": buy.price,
                "shares": round(buy.shares,6), "cost": round(buy.cost,6)},
        "stop_exit": {"date": str(stop_exit.date), "price": stop_exit.price,
                      "shares": round(stop_exit.shares,6),
                      "proceeds": round(stop_exit.proceeds,6), "cost": round(stop_exit.cost,6)},
        "settled_on_exit_day": round(sim.ledger.settled_cash(dt.date(2026,1,8)),6),
        "settled_next_trading_day": round(sim.ledger.settled_cash(dt.date(2026,1,9)),6),
    }
    with open("tests/fixtures/golden_simulator_sequence.json") as f:
        assert result == json.load(f)
```

- [ ] **Step 2: Run** → FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Write minimal implementation**

```python
# src/autotrader/simulator.py
"""Deterministic execution simulator: next-open fills, per-tier+floor costs, T+1 ledger,
integrated daily-bar stops.

Entry/exit each pay HALF the effective round-trip cost. Signal-based buys/sells fill at the
NEXT trading day's open; resting stops fill SAME-DAY at the stop-fill price. The ledger
enforces T+1 settlement. Position tracking here is minimal — only what's needed to evaluate
stops; full holdings/oversell accounting lives in the backtest engine (Plan 03). Frozen
behind the Task-8 golden fixture before any strategy is built.
"""
import datetime as dt
from dataclasses import dataclass
from autotrader.calendar_nyse import TradingCalendar
from autotrader.ledger import SettledCashLedger
from autotrader.costs import effective_roundtrip_cost, regulatory_sell_fees
from autotrader.stops import stop_fill_price


@dataclass
class BuyFill:
    symbol: str; date: dt.date; price: float; shares: float; cost: float


@dataclass
class SellFill:
    symbol: str; date: dt.date; price: float; shares: float; proceeds: float; cost: float


@dataclass
class _Position:
    symbol: str; shares: float; tier: str; cost_floor: float; stop_price: float = None


class Simulator:
    def __init__(self, calendar: TradingCalendar, bars: dict, slippage_frac: float = 0.0):
        self.calendar = calendar
        self.bars = bars
        self.slippage_frac = slippage_frac
        self.ledger = SettledCashLedger(calendar=calendar)
        self.positions = {}

    def deposit(self, amount, on): self.ledger.deposit(amount, on)

    def submit_buy(self, symbol, signal_date, dollar_amount, tier,
                   cost_floor=None, stress=1.0) -> BuyFill:
        fill_date = self.calendar.next_trading_day(signal_date)
        price = self.bars[symbol][fill_date]["open"]
        if not self.ledger.can_buy(dollar_amount, fill_date):
            raise ValueError(f"buy refused: insufficient settled cash on {fill_date}")
        cost = dollar_amount * effective_roundtrip_cost(tier, cost_floor, stress) / 2
        shares = (dollar_amount - cost) / price
        self.ledger.execute_buy(dollar_amount, on=fill_date)
        self.positions[symbol] = _Position(symbol, shares, tier, cost_floor)
        return BuyFill(symbol, fill_date, price, shares, cost)

    def place_stop(self, symbol, stop_price):
        self.positions[symbol].stop_price = stop_price

    def _book_sell(self, pos, trade_date, price, stress=1.0) -> SellFill:
        gross = pos.shares * price
        spread_cost = gross * effective_roundtrip_cost(pos.tier, pos.cost_floor, stress) / 2
        cost = spread_cost + regulatory_sell_fees(proceeds=gross, shares=pos.shares)
        proceeds = gross - cost
        self.ledger.record_sale(proceeds, trade_date=trade_date)
        return SellFill(pos.symbol, trade_date, price, pos.shares, proceeds, cost)

    def submit_sell(self, symbol, signal_date, stress=1.0) -> SellFill:
        pos = self.positions.pop(symbol)
        fill_date = self.calendar.next_trading_day(signal_date)
        return self._book_sell(pos, fill_date, self.bars[symbol][fill_date]["open"], stress)

    def evaluate_stops(self, date, stress=1.0):
        fills = []
        for symbol in list(self.positions):
            pos = self.positions[symbol]
            if pos.stop_price is None:
                continue
            bar = self.bars[symbol].get(date)
            if bar is None:
                continue
            fp = stop_fill_price(bar, pos.stop_price, self.slippage_frac)
            if fp is None:
                continue
            fills.append(self._book_sell(pos, date, fp, stress))
            del self.positions[symbol]
        return fills
```

- [ ] **Step 4: Create the golden fixture.** Run this from the **repo root** with the venv active, hand-verify, then paste into `tests/fixtures/golden_simulator_sequence.json`:

Run: `./.venv/bin/python - <<'PY'`
```python
import datetime as dt, json
from autotrader.calendar_nyse import TradingCalendar
from autotrader.simulator import Simulator
from autotrader import config
DAYS=[dt.date(2026,1,2),dt.date(2026,1,5),dt.date(2026,1,6),dt.date(2026,1,7),dt.date(2026,1,8),dt.date(2026,1,9)]
BARS={"XLK":{dt.date(2026,1,2):{"open":100.0,"high":101.0,"low":99.0,"close":100.0},
 dt.date(2026,1,5):{"open":100.0,"high":102.0,"low":100.0,"close":101.0},
 dt.date(2026,1,6):{"open":101.0,"high":103.0,"low":100.0,"close":102.0},
 dt.date(2026,1,7):{"open":102.0,"high":103.0,"low":101.0,"close":102.5},
 dt.date(2026,1,8):{"open":90.0,"high":91.0,"low":88.0,"close":89.0},
 dt.date(2026,1,9):{"open":89.0,"high":90.0,"low":88.0,"close":89.0}}}
sim=Simulator(calendar=TradingCalendar(DAYS),bars=BARS,slippage_frac=0.0)
sim.deposit(1000.0,on=dt.date(2026,1,2))
buy=sim.submit_buy("XLK",signal_date=dt.date(2026,1,2),dollar_amount=1000.0,tier=config.TIER_SECTOR_SPDR)
sim.place_stop("XLK",stop_price=95.0)
sim.evaluate_stops(dt.date(2026,1,6)); sim.evaluate_stops(dt.date(2026,1,7))
ex=sim.evaluate_stops(dt.date(2026,1,8))[0]
out={"buy":{"date":str(buy.date),"price":buy.price,"shares":round(buy.shares,6),"cost":round(buy.cost,6)},
 "stop_exit":{"date":str(ex.date),"price":ex.price,"shares":round(ex.shares,6),"proceeds":round(ex.proceeds,6),"cost":round(ex.cost,6)},
 "settled_on_exit_day":round(sim.ledger.settled_cash(dt.date(2026,1,8)),6),
 "settled_next_trading_day":round(sim.ledger.settled_cash(dt.date(2026,1,9)),6)}
print(json.dumps(out,indent=2))
PY
```

**Hand-check before pasting (STOP and fix the code if these don't match):** buy fills 1/5 @100; entry cost = 1000×0.0015/2 = **0.75**; shares = 999.25/100 = **9.9925**. Stop @95 doesn't trigger 1/6 (low 100) or 1/7 (low 101); on 1/8 the open (90) ≤ 95 → gap-through fill **@90**. Exit gross = 9.9925×90 = 899.325; exit spread = 899.325×0.00075 = 0.674494; reg fees = 899.325×0.0000206 + 9.9925×0.000166 = 0.020185; exit cost ≈ **0.694679**; proceeds ≈ **898.630321**, settling 1/9. So `settled_on_exit_day` (1/8) = **0.0**, `settled_next_trading_day` (1/9) = **898.630321**.

- [ ] **Step 5: Run the full golden test** — `pytest tests/test_simulator.py -v` → PASS (2 passed)
- [ ] **Step 6: Commit the canary**

```bash
git add src/autotrader/simulator.py tests/test_simulator.py tests/fixtures/golden_simulator_sequence.json
git commit -m "feat: execution simulator w/ integrated stops + golden canary (buy, gap-stop, T+1, GFV)"
```

---

## Task 9: adjustment_type='all' @ daily — record the verified MCP gate

**Files:** Create `data/manifest.csv` (tracked provenance), append a note to `STRATEGY_TESTING_SPEC.md` §6.

- [ ] **Step 1:** The blocking assumption (dividend-adjusted daily bars exist) was **verified live 2026-06-16**: `get_equity_historicals(["SPY"], interval="day", adjustment_type="all")` returned daily bars (today's bar flagged `interpolated:true`, filtered by Task 2). Record provenance by creating `data/manifest.csv`:

```csv
symbol,interval,adjustment,verified_date,note
SPY,day,all,2026-06-16,"adjustment_type=all confirmed at interval=day; interpolated last bar filtered on ingest"
```

- [ ] **Step 2:** When Plan 02 populates the real cache, it appends one manifest row per fetched series (symbol, interval, adjustment, fetched_at, start, end, n_rows, sha256) so every backtest verdict is reproducible from version control.
- [ ] **Step 3: Commit** — `git add data/manifest.csv && git commit -m "chore: record verified MCP data-source gate (adjustment=all @ daily)"`

---

## Task 10: Full green + foundation tag

- [ ] **Step 1: Run the entire suite** — `pytest -v` from repo root.
Expected: ALL PASS (ingest 3, datastore 3, calendar 5, costs 9, ledger 4, stops 3, simulator 2 = **29 passed**).
- [ ] **Step 2: Tag** — `git tag foundation-v1 -m "Canary foundation: data, calendar, cost/T+1/stop simulator — golden green"`

---

## Self-review against the spec (completed by plan author)

- **§3.1 data/cache + `adjustment_type='all'`:** ingest (drops interpolated) + DataStore (keyed by symbol/interval/adjustment). The `'all'`@daily gate is **verified live** (Task 9), not deferred. Bulk paging/population + per-series manifest rows = Plan 02.
- **§3.2 calendar:** Task 4, from SPY bar dates; `add_trading_days` now tested.
- **§3.3 + COST_MODEL:** Task 5 — `effective_roundtrip_cost(tier, floor, stress)` is the default cost path and **encodes the two axes** (instrument tier + S3 strategy floor); CS is tested on a **positive** value (tautology fixed) with `average_cs_spread`; overnight-gap adjustment explicitly deferred to Plan 03 with an in-code warning.
- **§3.5 stop fills (incl. gap-through):** Task 7 unit + **integrated into the simulator and exercised end-to-end in the golden** (Task 8).
- **§3.6 shared T+1 ledger / GFV proxy:** Task 6 unit + **GFV-blocked redeploy asserted in the integrated golden** (Task 8).
- **§7 build order + canary-first:** matches; golden now covers the buy, gap-through stop, T+1 settlement, and GFV block the spec named.
- **Provenance:** `data/manifest.csv` is tracked (raw pulls gitignored, manifest is not).
- **Deferred (correctly): ** indicators, the four strategies, backtest engine, metrics (deflated Sharpe / PBO / bootstrap CIs), benchmarks, robustness runner, paper-monitor, CS overnight-gap adjustment + stress-from-data wiring.

No placeholders; types consistent across tasks (`BuyFill`/`SellFill`/`_Position`, `effective_roundtrip_cost`, `SettledCashLedger`, `TradingCalendar`, `stop_fill_price`).

---

## Roadmap — remaining plans (written after this is green)

- **Plan 02 — Indicators & Strategies:** data-population runbook (agent fetches the universe via MCP, writes manifest) → indicator library (SMA, RSI(2) Wilder, CumRSI, rolling-252 high, returns) → the four strategy modules (S1 trend, S2 sector-momentum, S3 mean-reversion null-test wired to `S3_COST_FLOOR`, S4 trend-gated-momentum blend).
- **Plan 03 — Backtest Engine, Metrics & Benchmarks:** walk-forward engine; CS overnight-gap adjustment + stress-from-data; metrics (CAGR, Sharpe, **deflated Sharpe**, **PBO/CSCV**, **bootstrap CIs**, Sortino, max-DD, Calmar); the four benchmarks; report.
- **Plan 04 — Robustness Runner & Paper-Monitor:** plateau scan, stress folds (2008/2020/2022), rebalance-day dispersion, random-selection placebo; paper-monitor (live data, no orders, `review_equity_order` telemetry, measured-spread log).
