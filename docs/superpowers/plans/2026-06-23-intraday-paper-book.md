# Intraday Forward Paper Book — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a forward paper-trading simulator that runs the existing single-name trend/trailing-stop strategy on real live Robinhood quotes, fills virtual orders, and keeps a running JSON P&L/equity book — driven by an agent-self-paced, session-bound intraday loop.

**Architecture:** New pure-Python modules in `src/autotrader_live/` (no MCP calls; the agent fetches, the package computes). A `PaperBook` holds state and is the atomic source of truth; a `fill_engine` prices virtual fills against live `Quote`s; a `paper_loop` orchestrates a once-daily *arm* step and a 15-min *advance_poll* step; a `session_clock` provides ET market-hours timing. Reuses `universe`/`strategy_trend`/`sizing`/`exits`/`cost_tier`/`mcp_live` unchanged. The offline `src/autotrader` harness and golden fixtures are never edited (read-only imports only).

**Tech Stack:** Python 3, pandas, pytest, stdlib `zoneinfo`/`json`/`dataclasses`. Spec: `docs/superpowers/specs/2026-06-23-intraday-paper-book-design.md`.

**Ratified knobs:** `$2,000` start; `slippage_bps=3`; `near_threshold=0.90`, `f=0.01`, `k=3.0`, `per_name_cap_frac=0.15`, `top_n=10`, `m=2.0`, `blackout_days=5`; 15-min polls; sampled-intraday ratchet; instant cash; JSON only; `MIN_NOTIONAL=$50`.

---

## File structure

| File | Responsibility |
|---|---|
| `src/autotrader_live/paper_book.py` | `Fill`, `PaperPosition`, `ArmedEntry`, `BookSnapshot`, `PaperBook` (state + atomic persist/load + reconcile + mark-to-market + apply_entry/exit/replace). Source of truth. |
| `src/autotrader_live/fill_engine.py` | Pure fill logic: `quote_is_fillable`, `entry_reference`, `should_trigger_entry`, `entry_fill`, `stop_fill`, `ratchet`. |
| `src/autotrader_live/session_clock.py` | ET market-hours timing: `is_regular_session`, `minutes_to_close`, early-close table. |
| `src/autotrader_live/paper_loop.py` | Orchestrator: `arm_day` (once/day), `advance_poll` (each poll) + JSONL log writers. |
| `scripts/run_paper_book.py` | Agent-invoked driver: normalize raw MCP JSON → arm/poll/status. |
| `RUNBOOK_PAPER_BOOK.md` | The agent-loop procedure (wake/heartbeat/timeout/expiry wiring). |
| `tests/live/test_paper_book.py` | Book state, persistence, reconcile, mark-to-market. |
| `tests/live/test_fill_engine.py` | All fill-engine functions, hand-worked. |
| `tests/live/test_session_clock.py` | Session timing incl. half-day. |
| `tests/live/test_paper_loop.py` | arm/advance/idempotency/no-double-enter. |
| `tests/live/test_golden_paper_book.py` + `tests/live/fixtures/golden_paper_book/` | Deterministic multi-poll replay lock. |

**Module dependency direction:** `fill_engine` → imports `paper_book` (types), `mcp_live` (Quote), `strategy_trend` (TrendDecision), `exits`. `paper_loop` → imports `paper_book`, `fill_engine`, `session_clock`, `universe`, `sizing`, `exits`, `paper_monitor` (for `completed_bar_guard`). No cycles (`paper_monitor` does not import `paper_loop`).

**Per-task gate (applies to EVERY task that creates/edits a `src/autotrader_live/*.py`):** the Step-4 pytest run must also include `pytest tests/live/test_no_place_invariant.py -q` so the no-order-surface invariant (no `place_*`/`cancel_*`/`review_equity_order` token anywhere in the package) is enforced per-commit, not only at P5.2. The test auto-discovers new files via rglob.

---

## Task P0 (GATING): Auth / wake spike — run BEFORE any code

This is an operational spike, not TDD. It must pass before P1–P5 are built; if it fails, the loop cannot run as designed and we escalate to the operator (supervised-only fallback or the token-relay project).

- [ ] **Step 1: Confirm `ScheduleWakeup` re-invokes within a session.** In an interactive session, call `ScheduleWakeup(delaySeconds=120, ...)`, let it fire, confirm the agent is re-invoked with the broker MCP still reachable (probe `get_accounts` → `123456789 agentic_allowed=true`).
- [ ] **Step 2: Confirm a poll-interval round-trip.** Fetch `get_equity_quotes(["SPY"])`, confirm a normalized `Quote` (bid/ask/last/state). Record the observed `quote.state` string(s) for the fillability gate (P2.1 `_ALLOWED_STATES`).
- [ ] **Step 3: Confirm the external heartbeat path.** Create a tiny `mcp__scheduled-tasks`/`CronCreate` job that reads a file mtime and sends a `PushNotification` — confirm the operator receives it. (No MCP/broker needed; pure file read.)
- [ ] **Step 4: Nightly-boundary note.** Document explicitly whether `ScheduleWakeup` survives a long (overnight) sleep with the app open, or whether the nightly pause must be a session boundary (the operator restarts). Record the answer in `RUNBOOK_PAPER_BOOK.md` (P4.2).
- [ ] **Step 5: Decision gate.** If Steps 1–3 pass → proceed to P1. If the wake/heartbeat cannot re-reach the MCP (the LIVE-01 probe stalled), STOP and escalate; do not build P1–P5 against a loop that can't run.

---

## Task P1.1: `PaperPosition` with guards

**Files:** Create `src/autotrader_live/paper_book.py`; Test `tests/live/test_paper_book.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/live/test_paper_book.py
import dataclasses
import datetime as dt
import json
import pytest
from autotrader_live.paper_book import PaperPosition


def _pos(**kw):
    base = dict(symbol="AMD", shares=2.0, entry_price=100.0, entry_ts="2026-06-23T14:00:00Z",
               atr_at_entry=5.0, current_stop=90.0, highest_high_since_entry=100.0,
               ratchet_seq=0, cost_tier_bps=20.0)
    base.update(kw)
    return PaperPosition(**base)


def test_paper_position_valid():
    p = _pos()
    assert p.symbol == "AMD" and p.shares == 2.0 and p.current_stop == 90.0


@pytest.mark.parametrize("field,bad", [
    ("shares", 0.0), ("shares", -1.0), ("entry_price", 0.0),
    ("current_stop", 0.0), ("current_stop", -5.0), ("atr_at_entry", 0.0),
])
def test_paper_position_guards(field, bad):
    with pytest.raises(ValueError):
        _pos(**{field: bad})
```

- [ ] **Step 2: Run, expect FAIL** — `pytest tests/live/test_paper_book.py -q` → ImportError/fails.

- [ ] **Step 3: Implement**

```python
# src/autotrader_live/paper_book.py
"""Paper-book state + atomic persistence for LIVE-02 (the forward paper simulator).

PURE module: NO MCP calls. book.json is the complete source of truth (positions,
cash, realized P&L, and the full fills list). fills.jsonl / equity_curve.jsonl are
DERIVED append-only logs, reconciled from book.json on load.

NO-PLACE INVARIANT: contains none of the forbidden broker-mutation tokens.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class PaperPosition:
    symbol: str
    shares: float
    entry_price: float
    entry_ts: str
    atr_at_entry: float
    current_stop: float
    highest_high_since_entry: float
    ratchet_seq: int
    cost_tier_bps: float

    def __post_init__(self) -> None:
        if self.shares <= 0:
            raise ValueError(f"shares must be > 0, got {self.shares!r} for {self.symbol!r}")
        if self.entry_price <= 0:
            raise ValueError(f"entry_price must be > 0, got {self.entry_price!r} for {self.symbol!r}")
        if self.atr_at_entry <= 0:
            raise ValueError(f"atr_at_entry must be > 0, got {self.atr_at_entry!r} for {self.symbol!r}")
        if self.current_stop <= 0:
            raise ValueError(f"current_stop must be > 0, got {self.current_stop!r} for {self.symbol!r}")
```

- [ ] **Step 4: Run, expect PASS** — `pytest tests/live/test_paper_book.py -q`

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(live): PaperPosition with guards — P1.1"`

---

## Task P1.2: `ArmedEntry` + `Fill` records

**Files:** Modify `src/autotrader_live/paper_book.py`; Test `tests/live/test_paper_book.py`

- [ ] **Step 1: Add the failing test**

```python
from autotrader_live.paper_book import ArmedEntry, Fill

def test_armed_entry_and_fill():
    a = ArmedEntry(symbol="AMD", entry_ref=101.0, ref_basis="breakout",
                   target_notional=200.0, atr_at_arm=5.0, cost_tier_bps=20.0,
                   arm_date="2026-06-23")
    assert a.ref_basis == "breakout"
    f = Fill(fill_id="AMD:entry:2026-06-23", ts="2026-06-23T14:00:00Z", symbol="AMD",
             side="buy", intent_type="entry", price=101.5, shares=1.97, notional=200.0,
             entry_ref=101.0, ref_basis="breakout", bid=101.4, ask=101.6,
             last_trade_price=101.5, previous_close=100.0, spread=0.2,
             cost_tier_bps=20.0, realized_pnl_delta=0.0)
    assert f.fill_id == "AMD:entry:2026-06-23" and f.realized_pnl_delta == 0.0
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement (append to `paper_book.py`)**

```python
@dataclass(frozen=True)
class ArmedEntry:
    symbol: str
    entry_ref: float
    ref_basis: str          # 'breakout' | 'near_high'
    target_notional: float
    atr_at_arm: float
    cost_tier_bps: float
    arm_date: str           # ISO date (the run day)


@dataclass(frozen=True)
class Fill:
    fill_id: str            # 'SYM:entry:<arm_date>' | 'SYM:stop:<ratchet_seq>'
    ts: str                 # ISO-8601 UTC, injected by caller
    symbol: str
    side: str               # 'buy' | 'sell'
    intent_type: str        # 'entry' | 'stop'
    price: float            # actual fill price (post-slippage)
    shares: float
    notional: float         # price * shares
    entry_ref: float | None
    ref_basis: str | None
    bid: float | None
    ask: float | None
    last_trade_price: float
    previous_close: float
    spread: float | None
    cost_tier_bps: float    # comparison-only metadata
    realized_pnl_delta: float  # 0.0 for entries; (price - entry_price)*shares for stops
```

- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat(live): ArmedEntry + Fill records — P1.2"`

---

## Task P1.3: `PaperBook` + atomic save/load round-trip

**Files:** Modify `src/autotrader_live/paper_book.py`; Test `tests/live/test_paper_book.py`

- [ ] **Step 1: Add the failing test**

```python
from autotrader_live.paper_book import PaperBook

def test_book_new_and_roundtrip(tmp_path):
    book = PaperBook.new(start_capital=2000.0, start_ts="2026-06-23T13:30:00Z",
                         token_issue_ts="2026-06-23T13:00:00Z")
    assert book.cash == 2000.0 and book.realized_pnl == 0.0
    book.save(tmp_path)
    assert (tmp_path / "book.json").exists()
    reloaded = PaperBook.load(tmp_path)
    assert reloaded.cash == 2000.0
    assert reloaded.start_capital == 2000.0
    assert reloaded.data_source == "robinhood_mcp_live"

def test_book_load_missing_returns_none(tmp_path):
    assert PaperBook.load(tmp_path) is None

def test_book_save_is_deterministic(tmp_path):
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    book.save(tmp_path)
    first = (tmp_path / "book.json").read_text()
    book2 = PaperBook.load(tmp_path)
    book2.save(tmp_path)
    assert (tmp_path / "book.json").read_text() == first
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement (append; mirror `paper_monitor` atomic + 10dp + sort_keys discipline)**

```python
def _round(obj: Any) -> Any:
    if isinstance(obj, float):
        return round(obj, 10)
    if isinstance(obj, dict):
        return {k: _round(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round(x) for x in obj]
    return obj


def _atomic_write_json(path: Path, obj: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(_round(obj), indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


@dataclass
class PaperBook:
    cash: float
    start_capital: float
    start_ts: str
    token_issue_ts: str | None
    positions: dict[str, PaperPosition] = field(default_factory=dict)
    armed: dict[str, ArmedEntry] = field(default_factory=dict)
    filled_today: set[str] = field(default_factory=set)
    realized_pnl: float = 0.0
    last_arm_date: str | None = None
    last_poll_ts: str | None = None
    fills: list[Fill] = field(default_factory=list)
    data_source: str = "robinhood_mcp_live"

    @classmethod
    def new(cls, *, start_capital: float, start_ts: str, token_issue_ts: str | None) -> "PaperBook":
        if start_capital <= 0:
            raise ValueError(f"start_capital must be > 0, got {start_capital!r}")
        return cls(cash=start_capital, start_capital=start_capital, start_ts=start_ts,
                   token_issue_ts=token_issue_ts)

    @property
    def applied_fill_ids(self) -> set[str]:
        return {f.fill_id for f in self.fills}

    def to_dict(self) -> dict:
        return {
            "cash": self.cash,
            "start_capital": self.start_capital,
            "start_ts": self.start_ts,
            "token_issue_ts": self.token_issue_ts,
            "positions": {s: dataclasses.asdict(p) for s, p in self.positions.items()},
            "armed": {s: dataclasses.asdict(a) for s, a in self.armed.items()},
            "filled_today": sorted(self.filled_today),
            "realized_pnl": self.realized_pnl,
            "last_arm_date": self.last_arm_date,
            "last_poll_ts": self.last_poll_ts,
            "fills": [dataclasses.asdict(f) for f in self.fills],
            "data_source": self.data_source,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PaperBook":
        return cls(
            cash=float(d["cash"]),
            start_capital=float(d["start_capital"]),
            start_ts=d["start_ts"],
            token_issue_ts=d.get("token_issue_ts"),
            positions={s: PaperPosition(**p) for s, p in d.get("positions", {}).items()},
            armed={s: ArmedEntry(**a) for s, a in d.get("armed", {}).items()},
            filled_today=set(d.get("filled_today", [])),
            realized_pnl=float(d.get("realized_pnl", 0.0)),
            last_arm_date=d.get("last_arm_date"),
            last_poll_ts=d.get("last_poll_ts"),
            fills=[Fill(**f) for f in d.get("fills", [])],
            data_source=d.get("data_source", "robinhood_mcp_live"),
        )

    def save(self, state_dir: str | Path) -> None:
        state_dir = Path(state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(state_dir / "book.json", self.to_dict())
        _reconcile_logs(state_dir, self.fills)

    @classmethod
    def load(cls, state_dir: str | Path) -> "PaperBook | None":
        path = Path(state_dir) / "book.json"
        if not path.exists():
            return None
        book = cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        _reconcile_logs(Path(state_dir), book.fills)  # self-heal fills.jsonl
        return book
```

- [ ] **Step 4: Run, expect PASS** (note: `_reconcile_logs` is defined in P1.6; add a temporary `def _reconcile_logs(*a, **k): pass` now, replaced in P1.6).
- [ ] **Step 5: Commit** — `git commit -am "feat(live): PaperBook state + atomic JSON roundtrip — P1.3"`

---

## Task P1.4: `mark_to_market` + `BookSnapshot`

**Files:** Modify `src/autotrader_live/paper_book.py`; Test `tests/live/test_paper_book.py`

- [ ] **Step 1: Add the failing test**

```python
from autotrader_live.paper_book import BookSnapshot
from autotrader_live.mcp_live import Quote
import datetime as dt

def _quote(sym, last):
    return Quote(symbol=sym, settled_close=last, settled_close_date=dt.date(2026,6,22),
                 settled_close_interpolated=False, settled_close_source="x",
                 bid=last-0.05, ask=last+0.05, last_trade_price=last,
                 previous_close=last, has_traded=True, state="active")

def test_mark_to_market():
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    book.cash = 1800.0
    book.positions["AMD"] = PaperPosition(symbol="AMD", shares=2.0, entry_price=100.0,
        entry_ts="t", atr_at_entry=5.0, current_stop=90.0,
        highest_high_since_entry=100.0, ratchet_seq=0, cost_tier_bps=20.0)
    snap = book.mark_to_market({"AMD": _quote("AMD", 110.0)})
    assert snap.positions_mv == pytest.approx(220.0)        # 2 * 110
    assert snap.total_equity == pytest.approx(2020.0)       # 1800 + 220
    assert snap.unrealized_pnl == pytest.approx(20.0)       # 2 * (110 - 100)
    assert snap.n_positions == 1
    assert snap.n_marked_at_cost == 0

def test_mark_to_market_missing_quote_marks_at_cost():
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    book.cash = 1800.0
    book.positions["AMD"] = PaperPosition(symbol="AMD", shares=2.0, entry_price=100.0,
        entry_ts="t", atr_at_entry=5.0, current_stop=90.0,
        highest_high_since_entry=100.0, ratchet_seq=0, cost_tier_bps=20.0)
    snap = book.mark_to_market({})  # no quote
    assert snap.positions_mv == pytest.approx(200.0)        # marked at cost
    assert snap.unrealized_pnl == pytest.approx(0.0)
    assert snap.n_marked_at_cost == 1                       # outage is auditable, not hidden
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement (append)**

```python
@dataclass(frozen=True)
class BookSnapshot:
    ts: str
    cash: float
    positions_mv: float
    total_equity: float
    n_positions: int
    realized_pnl_cum: float
    unrealized_pnl: float
    n_marked_at_cost: int   # held names with no usable quote this poll (marked at cost)


# (add to PaperBook)
    def mark_to_market(self, quotes: dict, ts: str = "") -> BookSnapshot:
        mv = 0.0
        unrealized = 0.0
        n_marked_at_cost = 0
        for sym, pos in self.positions.items():
            q = quotes.get(sym)
            if q is not None and q.last_trade_price > 0:
                mark = q.last_trade_price
            else:
                mark = pos.entry_price          # data outage -> mark at cost (provisional)
                n_marked_at_cost += 1
            mv += pos.shares * mark
            unrealized += pos.shares * (mark - pos.entry_price)
        return BookSnapshot(ts=ts, cash=self.cash, positions_mv=mv,
                            total_equity=self.cash + mv, n_positions=len(self.positions),
                            realized_pnl_cum=self.realized_pnl, unrealized_pnl=unrealized,
                            n_marked_at_cost=n_marked_at_cost)

    def equity_at_cost(self) -> float:
        """Sizing basis: cash + open positions marked at COST (not live mark)."""
        return self.cash + sum(p.shares * p.entry_price for p in self.positions.values())
```

- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat(live): mark_to_market + equity_at_cost + BookSnapshot — P1.4"`

---

## Task P1.5: `apply_entry` / `apply_exit` / `replace_position`

**Files:** Modify `src/autotrader_live/paper_book.py`; Test `tests/live/test_paper_book.py`

- [ ] **Step 1: Add the failing test**

```python
def _fill(fill_id, side, intent, price, shares, realized=0.0, sym="AMD"):
    return Fill(fill_id=fill_id, ts="t", symbol=sym, side=side, intent_type=intent,
                price=price, shares=shares, notional=price*shares, entry_ref=None,
                ref_basis=None, bid=price, ask=price, last_trade_price=price,
                previous_close=price, spread=0.0, cost_tier_bps=20.0,
                realized_pnl_delta=realized)

def test_apply_entry_then_exit():
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    book.armed["AMD"] = ArmedEntry("AMD", 99.0, "near_high", 200.0, 5.0, 20.0, "2026-06-23")
    pos = PaperPosition(symbol="AMD", shares=2.0, entry_price=100.0, entry_ts="t",
        atr_at_entry=5.0, current_stop=90.0, highest_high_since_entry=100.0,
        ratchet_seq=0, cost_tier_bps=20.0)
    book.apply_entry(pos, _fill("AMD:entry:2026-06-23", "buy", "entry", 100.0, 2.0))
    assert book.cash == pytest.approx(1800.0)
    assert "AMD" in book.positions and "AMD" in book.filled_today
    assert "AMD" not in book.armed
    # idempotent: re-applying the same fill_id is a no-op
    book.apply_entry(pos, _fill("AMD:entry:2026-06-23", "buy", "entry", 100.0, 2.0))
    assert book.cash == pytest.approx(1800.0)
    # exit at 110 -> realized +20, cash back
    book.apply_exit(_fill("AMD:stop:0", "sell", "stop", 110.0, 2.0, realized=20.0))
    assert book.cash == pytest.approx(2020.0)
    assert book.realized_pnl == pytest.approx(20.0)
    assert "AMD" not in book.positions

def test_replace_position_ratchet():
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    pos = PaperPosition("AMD", 2.0, 100.0, "t", 5.0, 90.0, 100.0, 0, 20.0)
    book.positions["AMD"] = pos
    higher = dataclasses.replace(pos, current_stop=95.0, ratchet_seq=1, highest_high_since_entry=110.0)
    book.replace_position(higher)
    assert book.positions["AMD"].current_stop == 95.0
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement (add to PaperBook)**

```python
    def apply_entry(self, position: PaperPosition, fill: Fill) -> None:
        if fill.fill_id in self.applied_fill_ids:
            return
        self.cash -= fill.notional
        self.positions[position.symbol] = position
        self.filled_today.add(position.symbol)
        self.armed.pop(position.symbol, None)
        self.fills.append(fill)

    def apply_exit(self, fill: Fill) -> None:
        if fill.fill_id in self.applied_fill_ids:
            return
        self.cash += fill.notional
        self.realized_pnl += fill.realized_pnl_delta
        self.positions.pop(fill.symbol, None)
        self.fills.append(fill)

    def replace_position(self, position: PaperPosition) -> None:
        # ratchet stop move — no fill, no cash change
        self.positions[position.symbol] = position
```

- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat(live): apply_entry/apply_exit/replace_position (idempotent) — P1.5"`

---

## Task P1.6: Derived-log reconcile (`fills.jsonl` self-heal)

**Files:** Modify `src/autotrader_live/paper_book.py`; Test `tests/live/test_paper_book.py`

- [ ] **Step 1: Add the failing test**

```python
def test_reconcile_logs_appends_missing(tmp_path):
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    pos = PaperPosition("AMD", 2.0, 100.0, "t", 5.0, 90.0, 100.0, 0, 20.0)
    book.apply_entry(pos, _fill("AMD:entry:2026-06-23", "buy", "entry", 100.0, 2.0))
    book.save(tmp_path)  # writes book.json AND fills.jsonl
    lines = (tmp_path / "fills.jsonl").read_text().strip().splitlines()
    assert len(lines) == 1
    # Simulate a crash where fills.jsonl was truncated but book.json kept the fill
    (tmp_path / "fills.jsonl").write_text("")
    reloaded = PaperBook.load(tmp_path)              # load reconciles
    lines2 = (tmp_path / "fills.jsonl").read_text().strip().splitlines()
    assert len(lines2) == 1                          # re-appended from book.json
    assert json.loads(lines2[0])["fill_id"] == "AMD:entry:2026-06-23"
```

- [ ] **Step 2: Run, expect FAIL** (current `_reconcile_logs` is the no-op stub)

- [ ] **Step 3: Replace the stub with the real implementation**

```python
def _reconcile_logs(state_dir: Path, fills: list[Fill]) -> None:
    """Ensure fills.jsonl contains exactly the committed book fills (self-heal).

    book.json is the source of truth. Any committed fill missing from fills.jsonl
    is appended; logged-but-uncommitted ids are ignored (we rewrite the file from
    the book's fills in order). equity_curve.jsonl is append-only and NOT rewritten
    (a missing row is a poll-gap, detected by the calendar-vs-rows audit).
    """
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "fills.jsonl"
    existing_ids: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                existing_ids.add(json.loads(line)["fill_id"])
    missing = [f for f in fills if f.fill_id not in existing_ids]
    if missing:
        with path.open("a", encoding="utf-8") as fh:
            for f in missing:
                fh.write(json.dumps(_round(dataclasses.asdict(f)), sort_keys=True) + "\n")
```

- [ ] **Step 4: Run, expect PASS** — `pytest tests/live/test_paper_book.py -q`
- [ ] **Step 5: Commit** — `git commit -am "feat(live): fills.jsonl reconcile/self-heal from book.json — P1.6"`

---

## Task P2.1: `fill_engine.quote_is_fillable`

**Files:** Create `src/autotrader_live/fill_engine.py`; Test `tests/live/test_fill_engine.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/live/test_fill_engine.py
import datetime as dt
import pytest
from autotrader_live.mcp_live import Quote
from autotrader_live import fill_engine as fe

def _q(**kw):
    base = dict(symbol="AMD", settled_close=100.0, settled_close_date=dt.date(2026,6,22),
                settled_close_interpolated=False, settled_close_source="x",
                bid=99.9, ask=100.1, last_trade_price=100.0, previous_close=100.0,
                has_traded=True, state="active")
    base.update(kw)
    return Quote(**base)

def test_fillable_happy():
    assert fe.quote_is_fillable(_q()) is True

@pytest.mark.parametrize("kw", [
    dict(last_trade_price=0.0),
    dict(bid=None),
    dict(ask=None),
    dict(bid=100.2, ask=100.1),          # crossed
    dict(has_traded=False),
    dict(state="closed"),
    dict(last_trade_price=200.0),        # >50% vs previous_close=100
])
def test_not_fillable(kw):
    assert fe.quote_is_fillable(_q(**kw)) is False
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement**

```python
# src/autotrader_live/fill_engine.py
"""Pure virtual-fill logic for LIVE-02. NO MCP calls; NO order placement.

Fills cross the real observed spread on both sides (buy@ask, sell@bid) plus a
slippage_bps adverse-drift haircut. The cost_tier estimate is comparison-only
metadata (never deducted). All functions are pure given (book/position/quote).
"""
from __future__ import annotations

import dataclasses

from autotrader_live import exits
from autotrader_live.mcp_live import Quote
from autotrader_live.paper_book import ArmedEntry, Fill, PaperPosition
from autotrader_live.strategy_trend import TrendDecision

# Observed live quote states that count as a tradeable regular session.
# VERIFY the exact live string(s) in the P0 spike and extend if needed.
_ALLOWED_STATES = {"active"}
MIN_NOTIONAL: float = 50.0
_MAX_MOVE_FRAC: float = 0.50  # reject a last more than 50% from previous_close


def quote_is_fillable(quote: Quote) -> bool:
    if quote.last_trade_price <= 0:
        return False
    if quote.bid is None or quote.ask is None:
        return False
    if quote.bid > quote.ask:
        return False
    if not quote.has_traded:
        return False
    if quote.state not in _ALLOWED_STATES:
        return False
    if quote.previous_close > 0 and abs(quote.last_trade_price / quote.previous_close - 1.0) > _MAX_MOVE_FRAC:
        return False
    return True
```

- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat(live): fill_engine.quote_is_fillable sanity gate — P2.1"`

---

## Task P2.2: `entry_reference` + `should_trigger_entry`

**Files:** Modify `src/autotrader_live/fill_engine.py`; Test `tests/live/test_fill_engine.py`

- [ ] **Step 1: Add the failing test**

```python
from autotrader_live.strategy_trend import TrendDecision

def _dec(**kw):
    base = dict(signal_date=dt.date(2026,6,22), symbol="AMD", close=100.0, sma200=80.0,
                trend_ok=True, mom_252=0.3, momentum_ok=True, nearness=0.97, near_high=True,
                prior_donch_upper=105.0, breakout_55=False, atr14=5.0, entry=True, reason="ok")
    base.update(kw)
    return TrendDecision(**base)

def test_entry_reference_near_high():
    ref, basis = fe.entry_reference(_dec(breakout_55=False))
    assert ref == 100.0 and basis == "near_high"          # uses close

def test_entry_reference_breakout():
    ref, basis = fe.entry_reference(_dec(breakout_55=True))
    assert ref == 105.0 and basis == "breakout"           # uses prior_donch_upper

def test_should_trigger_strict_gt():
    assert fe.should_trigger_entry(105.01, 105.0) is True
    assert fe.should_trigger_entry(105.0, 105.0) is False  # strict >
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement (append)**

```python
def entry_reference(decision: TrendDecision) -> tuple[float, str]:
    """Intraday entry reference level + its basis.

    Breakout-qualifiers must clear the prior 55-day HIGH intraday (a real
    breakout confirmation); near-high-qualifiers need only clear the prior
    settled close. `prior_donch_upper` and `close` are frozen on the decision —
    never recomputed here.
    """
    if decision.breakout_55:
        return (decision.prior_donch_upper, "breakout")
    return (decision.close, "near_high")


def should_trigger_entry(last_trade_price: float, entry_ref: float) -> bool:
    return last_trade_price > entry_ref  # strict, matches decide()'s close_t > prior_donch_upper
```

- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat(live): entry_reference + should_trigger_entry — P2.2"`

---

## Task P2.3: `entry_fill`

**Files:** Modify `src/autotrader_live/fill_engine.py`; Test `tests/live/test_fill_engine.py`

- [ ] **Step 1: Add the failing test**

```python
from autotrader_live.paper_book import ArmedEntry

def _armed(**kw):
    base = dict(symbol="AMD", entry_ref=99.0, ref_basis="near_high", target_notional=200.0,
                atr_at_arm=5.0, cost_tier_bps=20.0, arm_date="2026-06-23")
    base.update(kw)
    return ArmedEntry(**base)

def test_entry_fill_at_ask_plus_slippage():
    q = _q(ask=100.10, bid=99.90, last_trade_price=100.0)
    f = fe.entry_fill(q, _armed(target_notional=200.0), available_cash=2000.0,
                      ts="2026-06-23T14:00:00Z", slippage_bps=3.0)
    assert f is not None
    assert f.price == pytest.approx(100.10 * (1 + 3/1e4))   # ask + 3bps
    assert f.notional == pytest.approx(200.0)               # capped by target
    assert f.shares == pytest.approx(200.0 / f.price)
    assert f.fill_id == "AMD:entry:2026-06-23" and f.intent_type == "entry"
    assert f.realized_pnl_delta == 0.0

def test_entry_fill_capped_by_cash():
    f = fe.entry_fill(_q(), _armed(target_notional=200.0), available_cash=120.0,
                      ts="t", slippage_bps=3.0)
    assert f.notional == pytest.approx(120.0)

def test_entry_fill_skips_dust():
    assert fe.entry_fill(_q(), _armed(target_notional=200.0), available_cash=40.0,
                         ts="t", slippage_bps=3.0) is None   # below MIN_NOTIONAL

def test_entry_fill_ask_none_falls_back_to_last():
    q = _q(ask=None, bid=99.9, last_trade_price=100.0)
    # quote_is_fillable would reject ask=None, but entry_fill defends anyway:
    f = fe.entry_fill(q, _armed(), available_cash=2000.0, ts="t", slippage_bps=0.0)
    assert f.price == pytest.approx(100.0)
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement (append)**

```python
def entry_fill(quote: Quote, armed: ArmedEntry, available_cash: float, ts: str,
               *, slippage_bps: float = 3.0) -> Fill | None:
    """Price a virtual BUY for an armed name. Caller has already confirmed the
    quote is fillable and the trigger fired. Returns None on a dust/affordability
    skip."""
    base = quote.ask if quote.ask is not None else quote.last_trade_price
    fill_price = base * (1.0 + slippage_bps / 1e4)
    notional = min(armed.target_notional, available_cash)
    if notional < MIN_NOTIONAL or available_cash < MIN_NOTIONAL:
        return None
    shares = notional / fill_price
    return Fill(
        fill_id=f"{armed.symbol}:entry:{armed.arm_date}", ts=ts, symbol=armed.symbol,
        side="buy", intent_type="entry", price=fill_price, shares=shares,
        notional=fill_price * shares, entry_ref=armed.entry_ref, ref_basis=armed.ref_basis,
        bid=quote.bid, ask=quote.ask, last_trade_price=quote.last_trade_price,
        previous_close=quote.previous_close, spread=quote.spread,
        cost_tier_bps=armed.cost_tier_bps, realized_pnl_delta=0.0)
```

- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat(live): entry_fill (ask + slippage, dust/cash cap) — P2.3"`

---

## Task P2.4: `stop_fill` (bid-confirmed, spread-crossing, gap-through)

**Files:** Modify `src/autotrader_live/fill_engine.py`; Test `tests/live/test_fill_engine.py`

- [ ] **Step 1: Add the failing test**

```python
def _pos(**kw):
    base = dict(symbol="AMD", shares=2.0, entry_price=100.0, entry_ts="t", atr_at_entry=5.0,
                current_stop=95.0, highest_high_since_entry=100.0, ratchet_seq=0, cost_tier_bps=20.0)
    base.update(kw)
    return PaperPosition(**base)

from autotrader_live.paper_book import PaperPosition

def test_stop_no_trigger_when_bid_above_stop():
    q = _q(bid=96.0, ask=96.1, last_trade_price=96.05)   # bid 96 > stop 95
    assert fe.stop_fill(_pos(current_stop=95.0), q, ts="t", slippage_bps=3.0) is None

def test_stop_touch_fills_at_bid_below_stop():
    # bid touches the stop: a non-gap touch still crosses the spread (fills < stop)
    q = _q(bid=95.0, ask=95.2, last_trade_price=95.1)
    f = fe.stop_fill(_pos(current_stop=95.0), q, ts="t", slippage_bps=3.0)
    assert f is not None and f.intent_type == "stop" and f.side == "sell"
    assert f.price == pytest.approx(95.0 * (1 - 3/1e4))
    assert f.price < 95.0                                  # realizes LESS than the stop
    assert f.realized_pnl_delta == pytest.approx((f.price - 100.0) * 2.0)
    assert f.fill_id == "AMD:stop:t:0"   # date-namespaced by entry_ts[:10] ("t" in this fixture)

def test_stop_gap_through_fills_at_low_bid():
    q = _q(bid=80.0, ask=80.3, last_trade_price=80.1)      # gapped well below stop
    f = fe.stop_fill(_pos(current_stop=95.0), q, ts="t", slippage_bps=3.0)
    assert f.price == pytest.approx(80.0 * (1 - 3/1e4))    # loss bounded by sizing, not stop

def test_stop_bid_none_skips():
    q = _q(bid=None, ask=95.0, last_trade_price=94.0)
    assert fe.stop_fill(_pos(current_stop=95.0), q, ts="t", slippage_bps=3.0) is None
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement (append)**

```python
def stop_fill(position: PaperPosition, quote: Quote, ts: str,
              *, slippage_bps: float = 3.0) -> Fill | None:
    """Price a virtual SELL when the NBBO bid is at/below the stop.

    Trigger uses the BID (not last_trade) to avoid a stale-print wick. Fill at the
    bid minus slippage, so a gentle touch crosses the sell-side spread (fills below
    the stop) and a gap-through fills at the low bid. Returns None if bid is missing
    (never force-sell on missing data) or the stop is not breached."""
    if quote.bid is None or quote.bid > position.current_stop:
        return None
    fill_price = quote.bid * (1.0 - slippage_bps / 1e4)
    shares = position.shares
    realized = (fill_price - position.entry_price) * shares
    return Fill(
        # DATE-NAMESPACED by the entry date (entry_ts[:10]) so a name that opens and
        # stops out flat (ratchet_seq=0) on two DIFFERENT days yields distinct fill_ids
        # — otherwise the second exit dedups against the first in applied_fill_ids and
        # the stop-out is silently dropped (cash never credited, position never closed).
        fill_id=f"{position.symbol}:stop:{position.entry_ts[:10]}:{position.ratchet_seq}", ts=ts,
        symbol=position.symbol, side="sell", intent_type="stop", price=fill_price,
        shares=shares, notional=fill_price * shares, entry_ref=None, ref_basis=None,
        bid=quote.bid, ask=quote.ask, last_trade_price=quote.last_trade_price,
        previous_close=quote.previous_close, spread=quote.spread,
        cost_tier_bps=position.cost_tier_bps, realized_pnl_delta=realized)
```

- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat(live): stop_fill (bid-confirmed, spread-crossing, gap-through) — P2.4"`

---

## Task P2.5: `ratchet` (monotonic-up, sampled intraday high)

**Files:** Modify `src/autotrader_live/fill_engine.py`; Test `tests/live/test_fill_engine.py`

- [ ] **Step 1: Add the failing test**

```python
def test_ratchet_raises_stop_on_new_high():
    pos = _pos(current_stop=95.0, highest_high_since_entry=110.0, atr_at_entry=5.0, ratchet_seq=0)
    # new sampled high 120 -> chandelier 120 - 3*5 = 105 > 95 -> stop rises, seq++
    out = fe.ratchet(pos, last_trade_price=120.0, k=3.0)
    assert out.highest_high_since_entry == 120.0
    assert out.current_stop == pytest.approx(105.0)
    assert out.ratchet_seq == 1

def test_ratchet_monotonic_no_lower():
    pos = _pos(current_stop=105.0, highest_high_since_entry=120.0, atr_at_entry=5.0, ratchet_seq=1)
    out = fe.ratchet(pos, last_trade_price=100.0, k=3.0)   # lower price
    assert out.current_stop == 105.0                       # never falls
    assert out.highest_high_since_entry == 120.0           # high unchanged
    assert out.ratchet_seq == 1                            # no increment
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement (append)**

```python
def ratchet(position: PaperPosition, last_trade_price: float, *, k: float = 3.0) -> PaperPosition:
    """Monotonic-up chandelier ratchet on the highest SAMPLED poll price.

    `highest_high_since_entry` updates to include this poll's last_trade_price; the
    new stop = max(current_stop, hh - k*atr_at_entry). ratchet_seq increments only
    when the stop actually rises (so each stop_fill fill_id is unique)."""
    hh = max(position.highest_high_since_entry, last_trade_price)
    new_stop = exits.update_trailing_stop(position.current_stop, hh, position.atr_at_entry, k=k)
    seq = position.ratchet_seq + (1 if new_stop > position.current_stop else 0)
    return dataclasses.replace(position, highest_high_since_entry=hh,
                               current_stop=new_stop, ratchet_seq=seq)
```

- [ ] **Step 4: Run, expect PASS** — `pytest tests/live/test_fill_engine.py -q`
- [ ] **Step 5: Commit** — `git commit -am "feat(live): ratchet (monotonic-up, sampled high) — P2.5"`

---

## Task P2.6: `session_clock` (ET hours + half-days)

**Files:** Create `src/autotrader_live/session_clock.py`; Test `tests/live/test_session_clock.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/live/test_session_clock.py
import datetime as dt
from zoneinfo import ZoneInfo
from autotrader_live import session_clock as sc

ET = ZoneInfo("America/New_York")

def test_regular_session_open_midday():
    assert sc.is_regular_session(dt.datetime(2026, 6, 23, 11, 0, tzinfo=ET)) is True

def test_before_open_and_after_close():
    assert sc.is_regular_session(dt.datetime(2026, 6, 23, 9, 0, tzinfo=ET)) is False
    assert sc.is_regular_session(dt.datetime(2026, 6, 23, 16, 30, tzinfo=ET)) is False

def test_weekend_closed():
    assert sc.is_regular_session(dt.datetime(2026, 6, 27, 11, 0, tzinfo=ET)) is False  # Saturday

def test_half_day_early_close():
    # 2026-11-27 (day after Thanksgiving) closes 13:00 ET
    assert sc.is_regular_session(dt.datetime(2026, 11, 27, 12, 30, tzinfo=ET)) is True
    assert sc.is_regular_session(dt.datetime(2026, 11, 27, 13, 30, tzinfo=ET)) is False

def test_minutes_to_close():
    now = dt.datetime(2026, 6, 23, 15, 30, tzinfo=ET)
    assert sc.minutes_to_close(now) == 30
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement**

```python
# src/autotrader_live/session_clock.py
"""ET market-session timing for the LIVE-02 loop (the date-only TradingCalendar
has no hours). The AUTHORITATIVE open/halt signal at runtime is the broker quote
`state` (covers unscheduled halts); this clock paces sleeps and the nightly pause.
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
    d = now_et.date()
    if d.weekday() >= 5 or d in _HOLIDAYS:
        return False
    return _OPEN <= now_et.timetz().replace(tzinfo=None) < _close_time(d)


def minutes_to_close(now_et: dt.datetime) -> int:
    close_dt = dt.datetime.combine(now_et.date(), _close_time(now_et.date()), tzinfo=ET)
    return max(0, int((close_dt - now_et).total_seconds() // 60))
```

- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat(live): session_clock ET hours + half-days — P2.6"`

---

## Task P3.1: `paper_loop.arm_day`

**Files:** Create `src/autotrader_live/paper_loop.py`; Test `tests/live/test_paper_loop.py`

- [ ] **Step 1: Write the failing test** (uses a small `StaticMarketData` built from helpers; reuses the real `build_universe`)

```python
# tests/live/test_paper_loop.py
import datetime as dt
import pandas as pd
import pytest
from autotrader_live.paper_book import PaperBook
from autotrader_live import paper_loop
from autotrader_live.mcp_live import (StaticMarketData, ScanRow, Quote, Tradability,
                                      Fundamentals)

def _bars_ending(signal_date, n=300, start=10.0, step=0.5):
    # strictly rising closes => trend_ok/momentum_ok/breakout all true; the LAST bar
    # is dated EXACTLY signal_date so completed_bar_guard(bars, signal_date) passes
    # (consecutive calendar days are fine — the guard checks ascending + last==signal_date,
    #  not trading-day-ness).
    rows = []
    px = start
    for i in range(n):
        d = signal_date - dt.timedelta(days=(n - 1 - i))
        rows.append((d, px, px + 0.2, px - 0.2, px, 1_000_000))
        px += step
    return pd.DataFrame(rows, columns=["date", "open", "high", "low", "close", "volume"])

def _md(signal_date, *, bars=None):
    bars = _bars_ending(signal_date) if bars is None else bars
    last = float(bars["close"].iloc[-1])
    scan = [ScanRow("AMD", "id", "EQUITY", "AMD", last, last, 1e11, 9e6, 1.2, 65.0, 1.0)]
    quotes = {"AMD": Quote("AMD", last, signal_date, False, "x", last-0.05, last+0.05,
                           last, last, True, "active")}
    trad = {"AMD": Tradability("AMD", True, "active", True, False)}
    fund = {"AMD": Fundamentals("AMD", 1e11, 9e6, 9e6, last*1.1, "Tech", "Semis")}
    return StaticMarketData(scan_rows=scan, historicals={"AMD": bars}, quotes=quotes,
                            tradability=trad, fundamentals=fund, earnings={})

def test_arm_day_arms_qualifier_and_freezes_notional():
    signal_date = dt.date(2026, 6, 22)
    today = dt.date(2026, 6, 23)
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    paper_loop.arm_day(book, _md(signal_date), signal_date=signal_date, today=today)
    assert "AMD" in book.armed
    a = book.armed["AMD"]
    assert a.target_notional == pytest.approx(min(0.15*2000.0, a.target_notional))
    assert a.target_notional <= 300.0 + 1e-9          # per_name_cap_frac=0.15 binds
    assert book.last_arm_date == "2026-06-23"
    assert book.filled_today == set()

def test_arm_day_idempotent_same_day():
    signal_date, today = dt.date(2026, 6, 22), dt.date(2026, 6, 23)
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    paper_loop.arm_day(book, _md(signal_date), signal_date=signal_date, today=today)
    book.armed["AMD"] = book.armed["AMD"]
    paper_loop.arm_day(book, _md(signal_date), signal_date=signal_date, today=today)  # no-op
    assert book.last_arm_date == "2026-06-23"

def test_arm_day_skips_lookahead_bar():
    # historicals whose LAST bar is dated AFTER signal_date (an unsettled today-bar)
    # must be skipped by the completed_bar_guard — never armed off look-ahead data.
    signal_date, today = dt.date(2026, 6, 22), dt.date(2026, 6, 23)
    lookahead_bars = _bars_ending(dt.date(2026, 6, 23))   # last bar = 2026-06-23 > signal_date
    md = _md(signal_date, bars=lookahead_bars)
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    paper_loop.arm_day(book, md, signal_date=signal_date, today=today)
    assert "AMD" not in book.armed                          # guard refused the look-ahead bar
    assert book.last_arm_date == "2026-06-23"               # day still marked armed (no re-arm loop)
```

**`signal_date` provenance:** `arm_day` does NOT consult the calendar. `signal_date` (the last settled trading day) is computed UPSTREAM by the driver/agent (P4.1) from fresh SPY history via `autotrader.calendar_nyse.TradingCalendar` + `paper_monitor.resolve_signal_date`, then passed in. `arm_day`'s `completed_bar_guard` is the defense-in-depth that the per-name bars actually match that settled date.

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement**

```python
# src/autotrader_live/paper_loop.py
"""Orchestrator for the LIVE-02 paper book: once-daily arm + per-poll advance.

PURE over a MarketData provider + quotes dict; the agent does all MCP I/O.
Writes book.json (atomic, via PaperBook.save) FIRST, then appends the equity row.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from autotrader_live import exits, fill_engine, paper_monitor
from autotrader_live.mcp_live import MarketData, Quote
from autotrader_live.paper_book import ArmedEntry, PaperBook, PaperPosition
from autotrader_live.paper_monitor import LookAheadError, StaleDataError
from autotrader_live.sizing import size
from autotrader_live.strategy_trend import TrendDecision
from autotrader_live.universe import build_universe

# Ratified knobs (single source).
NEAR_THRESHOLD = 0.90
F = 0.01
K = 3.0
PER_NAME_CAP_FRAC = 0.15
TOP_N = 10
M = 2.0
BLACKOUT_DAYS = 5
SLIPPAGE_BPS = 3.0


def arm_day(book: PaperBook, market_data: MarketData, *,
            signal_date: dt.date, today: dt.date) -> None:
    """Build the day's armed entry set (once per calendar day). Idempotent: a
    second call on the same `today` is a no-op."""
    today_iso = today.isoformat()
    if book.last_arm_date == today_iso:
        return

    uni = build_universe(market_data, signal_date=signal_date,
                         near_threshold=NEAR_THRESHOLD, blackout_days=BLACKOUT_DAYS, top_n=TOP_N)

    equity_basis = book.equity_at_cost()
    armed: dict[str, ArmedEntry] = {}
    for cand in uni.selected:
        sym = cand.symbol
        if sym in book.positions:
            continue
        # LOOK-AHEAD GUARD (spec §1.5/§7): never arm off an unsettled bar. decide()
        # trusts the caller to pass settled bars; enforce it here, mirroring
        # paper_monitor.py's per-name guard. A today-dated (in-progress) bar -> skip.
        try:
            paper_monitor.completed_bar_guard(market_data.historicals(sym), signal_date)
        except (LookAheadError, StaleDataError, KeyError, ValueError):
            continue
        dec: TrendDecision = cand.decision  # non-None for selected
        ref, basis = fill_engine.entry_reference(dec)
        sz = size(equity_basis, dec.atr14, dec.close, f=F, k=K, per_name_cap_frac=PER_NAME_CAP_FRAC)
        armed[sym] = ArmedEntry(symbol=sym, entry_ref=ref, ref_basis=basis,
                                target_notional=sz["notional"], atr_at_arm=dec.atr14,
                                cost_tier_bps=cand.cost_tier.roundtrip_bps, arm_date=today_iso)

    book.armed = armed
    book.filled_today = set()
    book.last_arm_date = today_iso
```

- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat(live): paper_loop.arm_day — P3.1"`

---

## Task P3.2: `advance_poll` — entries

**Files:** Modify `src/autotrader_live/paper_loop.py`; Test `tests/live/test_paper_loop.py`

- [ ] **Step 1: Add the failing test**

```python
def test_advance_poll_fills_triggered_entry(tmp_path):
    signal_date, today = dt.date(2026, 6, 22), dt.date(2026, 6, 23)
    md = _md(signal_date)
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    paper_loop.arm_day(book, md, signal_date=signal_date, today=today)
    a = book.armed["AMD"]
    # quote whose last is strictly above the entry_ref -> trigger
    q = {"AMD": Quote("AMD", a.entry_ref, signal_date, False, "x",
                      a.entry_ref+0.9, a.entry_ref+1.1, a.entry_ref + 1.0,
                      a.entry_ref, True, "active")}
    paper_loop.advance_poll(book, q, md, ts="2026-06-23T14:00:00Z", state_dir=tmp_path)
    assert "AMD" in book.positions
    assert "AMD" in book.filled_today and "AMD" not in book.armed
    pos = book.positions["AMD"]
    assert pos.current_stop == pytest.approx(pos.entry_price - M * pos.atr_at_entry)
    assert book.cash < 2000.0
    # equity row written
    assert (tmp_path / "equity_curve.jsonl").exists()

def test_advance_poll_no_trigger_leaves_armed(tmp_path):
    signal_date, today = dt.date(2026, 6, 22), dt.date(2026, 6, 23)
    md = _md(signal_date)
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    paper_loop.arm_day(book, md, signal_date=signal_date, today=today)
    a = book.armed["AMD"]
    q = {"AMD": Quote("AMD", a.entry_ref, signal_date, False, "x",
                      a.entry_ref-1.1, a.entry_ref-0.9, a.entry_ref - 1.0,
                      a.entry_ref, True, "active")}  # last below ref
    paper_loop.advance_poll(book, q, md, ts="t2", state_dir=tmp_path)
    assert "AMD" in book.armed and "AMD" not in book.positions
```

- [ ] **Step 2: Run, expect FAIL**

- [ ] **Step 3: Implement (append to `paper_loop.py`)**

```python
def _append_jsonl(path: Path, row: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


def advance_poll(book: PaperBook, quotes: dict[str, Quote], market_data: MarketData, *,
                 ts: str, state_dir: str | Path, slippage_bps: float = SLIPPAGE_BPS) -> None:
    """One poll: fill triggered entries, fill/ratchet open positions, mark-to-market,
    checkpoint (book.json atomic FIRST), then append the equity row."""
    state_dir = Path(state_dir)

    # ── Arm-ordering guard ─────────────────────────────────────────────────────
    # Only take entries if TODAY's arm ran. If a poll fires before arm_day on a new
    # day (overnight wake / restart), armed[] + filled_today are STALE — manage open
    # positions only. The stale-arm skip is visible in the equity row (entries_taken).
    poll_day = ts[:10]
    take_entries = (book.last_arm_date == poll_day)

    # ── Entries: armed names with a fillable, triggered quote ──────────────────
    for sym in (sorted(book.armed.keys()) if take_entries else []):
        if sym in book.filled_today or sym in book.positions:
            continue
        q = quotes.get(sym)
        if q is None or not fill_engine.quote_is_fillable(q):
            continue
        armed = book.armed[sym]
        if not fill_engine.should_trigger_entry(q.last_trade_price, armed.entry_ref):
            continue
        fill = fill_engine.entry_fill(q, armed, book.cash, ts, slippage_bps=slippage_bps)
        if fill is None:
            continue
        try:
            stop_px = exits.initial_catastrophe_stop(fill.price, armed.atr_at_arm, m=M)
        except ValueError:
            continue  # ATR too wide for a valid stop — skip this entry
        position = PaperPosition(symbol=sym, shares=fill.shares, entry_price=fill.price,
                                 entry_ts=ts, atr_at_entry=armed.atr_at_arm, current_stop=stop_px,
                                 highest_high_since_entry=fill.price, ratchet_seq=0,
                                 cost_tier_bps=armed.cost_tier_bps)
        book.apply_entry(position, fill)

    # ── Open positions: stop-fill else ratchet ────────────────────────────────
    for sym in sorted(book.positions.keys()):
        q = quotes.get(sym)
        if q is None or not fill_engine.quote_is_fillable(q):
            continue  # never act on missing/bad data; resting stop stands
        pos = book.positions[sym]
        stop = fill_engine.stop_fill(pos, q, ts, slippage_bps=slippage_bps)
        if stop is not None:
            book.apply_exit(stop)
        else:
            book.replace_position(fill_engine.ratchet(pos, q.last_trade_price, k=K))

    # ── Checkpoint (book.json FIRST) + equity row ─────────────────────────────
    book.last_poll_ts = ts
    book.save(state_dir)  # atomic book.json + fills.jsonl reconcile
    snap = book.mark_to_market(quotes, ts=ts)
    _append_jsonl(state_dir / "equity_curve.jsonl", {
        "ts": snap.ts, "cash": round(snap.cash, 10), "positions_mv": round(snap.positions_mv, 10),
        "total_equity": round(snap.total_equity, 10), "n_positions": snap.n_positions,
        "realized_pnl_cum": round(snap.realized_pnl_cum, 10),
        "unrealized_pnl": round(snap.unrealized_pnl, 10),
        "n_marked_at_cost": snap.n_marked_at_cost,   # >0 => provisional equity (data outage)
        "entries_taken": take_entries})              # False => poll ran before today's arm (stale skip)
```

- [ ] **Step 4: Run, expect PASS**
- [ ] **Step 5: Commit** — `git commit -am "feat(live): advance_poll entries + checkpoint + equity row — P3.2"`

---

## Task P3.3: `advance_poll` — exits + ratchet (behavioral test)

**Files:** Test only — `tests/live/test_paper_loop.py` (exit/ratchet code already in P3.2)

- [ ] **Step 1: Add the failing-then-passing behavioral test**

```python
def test_advance_poll_stops_out_and_ratchets(tmp_path):
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    book.cash = 1800.0
    book.positions["AMD"] = PaperPosition("AMD", 2.0, 100.0, "t", 5.0, 90.0, 100.0, 0, 20.0)
    md = _md(dt.date(2026, 6, 22))
    # Poll 1: price rallies to 120 -> ratchet stop up to 120-15=105 (no fill)
    q1 = {"AMD": Quote("AMD", 100.0, dt.date(2026,6,22), False, "x", 119.9, 120.1, 120.0, 100.0, True, "active")}
    paper_loop.advance_poll(book, q1, md, ts="t1", state_dir=tmp_path)
    assert book.positions["AMD"].current_stop == pytest.approx(105.0)
    assert "AMD" in book.positions
    # Poll 2: bid drops to 104 (<=105) -> stop-out at bid - slippage
    q2 = {"AMD": Quote("AMD", 100.0, dt.date(2026,6,22), False, "x", 104.0, 104.2, 104.1, 100.0, True, "active")}
    paper_loop.advance_poll(book, q2, md, ts="t2", state_dir=tmp_path)
    assert "AMD" not in book.positions
    assert book.realized_pnl == pytest.approx((104.0*(1-3/1e4) - 100.0) * 2.0)

def test_advance_poll_never_force_sells_on_missing_quote(tmp_path):
    # spec §1.6/§7: an open position with NO usable quote survives the poll un-sold.
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    book.cash = 1800.0
    book.positions["AMD"] = PaperPosition("AMD", 2.0, 100.0, "2026-06-23T14:00:00Z", 5.0,
                                          95.0, 100.0, 0, 20.0)
    n_fills_before = len(book.fills)
    paper_loop.advance_poll(book, {}, _md(dt.date(2026, 6, 22)), ts="2026-06-23T15:00:00Z",
                            state_dir=tmp_path)            # empty quotes => data outage
    assert "AMD" in book.positions                         # NOT force-sold
    assert book.positions["AMD"].current_stop == 95.0      # stop unchanged
    assert len(book.fills) == n_fills_before               # no exit fill appended
    row = json.loads((tmp_path / "equity_curve.jsonl").read_text().splitlines()[-1])
    assert row["n_marked_at_cost"] == 1                    # outage flagged in the curve
```

- [ ] **Step 2: Run** — `pytest tests/live/test_paper_loop.py -q` → PASS (exit/ratchet logic landed in P3.2; add `import json` to the test module)
- [ ] **Step 3: Commit** — `git commit -am "test(live): advance_poll stop-out, ratchet, never-force-sell — P3.3"`

---

## Task P3.4: No-double-enter across a crash boundary

**Files:** Test only — `tests/live/test_paper_loop.py`

- [ ] **Step 1: Add the failing-then-passing test**

```python
def test_no_double_enter_after_crash(tmp_path):
    signal_date, today = dt.date(2026, 6, 22), dt.date(2026, 6, 23)
    md = _md(signal_date)
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    paper_loop.arm_day(book, md, signal_date=signal_date, today=today)
    a = book.armed["AMD"]
    q = {"AMD": Quote("AMD", a.entry_ref, signal_date, False, "x",
                      a.entry_ref+0.9, a.entry_ref+1.1, a.entry_ref + 1.0, a.entry_ref, True, "active")}
    paper_loop.advance_poll(book, q, md, ts="t1", state_dir=tmp_path)
    cash_after = book.cash
    # Simulate crash+restart: reload from disk, re-arm (idempotent), re-poll same day
    reloaded = PaperBook.load(tmp_path)
    paper_loop.arm_day(reloaded, md, signal_date=signal_date, today=today)  # no-op (same day)
    paper_loop.advance_poll(reloaded, q, md, ts="t2", state_dir=tmp_path)
    assert reloaded.cash == pytest.approx(cash_after)        # no second buy
    assert len([f for f in reloaded.fills if f.fill_id == "AMD:entry:2026-06-23"]) == 1

def _arm_open_stop(book, md, signal_date, today, tmp_path):
    """Arm + trigger an entry (poll 1) + force a stop-out (poll 2) for AMD on `today`."""
    paper_loop.arm_day(book, md, signal_date=signal_date, today=today)
    a = book.armed["AMD"]
    day = today.isoformat()
    q1 = {"AMD": Quote("AMD", a.entry_ref, signal_date, False, "x",
                       a.entry_ref+0.9, a.entry_ref+1.1, a.entry_ref+1.0, a.entry_ref, True, "active")}
    paper_loop.advance_poll(book, q1, md, ts=f"{day}T14:00:00Z", state_dir=tmp_path)
    stop = book.positions["AMD"].current_stop
    prev = book.positions["AMD"].entry_price
    # bid below the stop but a small, fillable move (passes the <=50% sanity gate)
    q2 = {"AMD": Quote("AMD", prev, signal_date, False, "x",
                       stop-0.10, stop+0.10, stop-0.05, prev, True, "active")}
    paper_loop.advance_poll(book, q2, md, ts=f"{day}T15:00:00Z", state_dir=tmp_path)

def test_cross_day_stop_out_books_both(tmp_path):
    # The HIGH fix: a name that opens and stops out flat (ratchet_seq=0) on TWO
    # different days must book BOTH exits (distinct date-namespaced stop fill_ids),
    # not silently dedup the second.
    book = PaperBook.new(start_capital=2000.0, start_ts="t", token_issue_ts="t")
    _arm_open_stop(book, _md(dt.date(2026, 6, 22)), dt.date(2026, 6, 22), dt.date(2026, 6, 23), tmp_path)
    assert "AMD" not in book.positions
    realized_after_day1 = book.realized_pnl
    assert realized_after_day1 < 0                                   # flat stop-out is a small loss
    _arm_open_stop(book, _md(dt.date(2026, 6, 23)), dt.date(2026, 6, 23), dt.date(2026, 6, 24), tmp_path)
    assert "AMD" not in book.positions
    assert book.realized_pnl < realized_after_day1                   # SECOND loss booked too
    stop_fills = [f for f in book.fills if f.intent_type == "stop"]
    assert len(stop_fills) == 2
    assert {f.fill_id for f in stop_fills} == {"AMD:stop:2026-06-23:0", "AMD:stop:2026-06-24:0"}
```

- [ ] **Step 2: Run, expect PASS** (no-double-enter via `filled_today` + `applied_fill_ids`; cross-day exits distinct via date-namespaced stop `fill_id`)
- [ ] **Step 3: Commit** — `git commit -am "test(live): no double-enter + cross-day stop-out both book — P3.4"`

---

## Task P3.5: Golden replay determinism lock

**Files:** Create `tests/live/test_golden_paper_book.py`, `tests/live/fixtures/golden_paper_book/` (script + expected); helper `scripts/regen_paper_book_golden.py`

- [ ] **Step 1: Write the replay test (fails until the golden is frozen)**

```python
# tests/live/test_golden_paper_book.py
import importlib.util
import json
import math
from pathlib import Path
from autotrader_live.paper_book import PaperBook

FIX = Path(__file__).parent / "fixtures" / "golden_paper_book"

def _load_replay():
    """Load replay() from the sibling scenario module by PATH (not package import),
    matching the repo convention of resolving fixtures relative to the test file
    (commit 2f3a7b2) — robust regardless of pytest rootdir / __init__ presence."""
    path = Path(__file__).parent / "_paper_book_scenario.py"
    spec = importlib.util.spec_from_file_location("_paper_book_scenario", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.replay

def test_golden_book_byte_match(tmp_path):
    """Replay the scripted poll sequence; book.json must byte-match the frozen golden."""
    _load_replay()(tmp_path)
    produced = (tmp_path / "book.json").read_text()
    expected = (FIX / "book.json").read_text()
    assert produced == expected, "book.json drifted from the frozen golden"

def test_golden_equity_curve_structural(tmp_path):
    _load_replay()(tmp_path)
    prod = [json.loads(l) for l in (tmp_path / "equity_curve.jsonl").read_text().splitlines() if l.strip()]
    exp = [json.loads(l) for l in (FIX / "equity_curve.jsonl").read_text().splitlines() if l.strip()]
    assert len(prod) == len(exp)
    for p, e in zip(prod, exp):
        assert math.isclose(p["total_equity"], e["total_equity"], rel_tol=0, abs_tol=1e-9)

def test_golden_includes_winning_ratchet_stop_exit(tmp_path):
    """Spec §7: a winner exiting on a ratchet-stop touch realizes BELOW the stop
    (sell-side spread crossed). Asserts the scenario actually exercises that path."""
    _load_replay()(tmp_path)
    book = PaperBook.load(tmp_path)
    stop_fills = [f for f in book.fills if f.intent_type == "stop"]
    assert stop_fills, "scenario must include at least one stop exit"
    # the scenario's winning exit books a positive realized_pnl_delta on a ratcheted stop
    assert any(f.realized_pnl_delta > 0 for f in stop_fills), "scenario must include a winning ratchet-stop exit"
    # a winning exit's fill_id carries a non-zero ratchet_seq (the stop was raised before the touch)
    assert any(f.fill_id.rsplit(":", 1)[-1] != "0" for f in stop_fills if f.realized_pnl_delta > 0)
```

- [ ] **Step 2: Write the shared scenario** `tests/live/_paper_book_scenario.py` exposing `replay(state_dir)` — a deterministic, MCP-free `arm` + N-poll sequence using `StaticMarketData` + scripted `Quote` snapshots and a FIXED `ts` per poll (never wall-clock). **`replay()` MUST clear `state_dir` at the top** (unlink `book.json`/`fills.jsonl`/`equity_curve.jsonl` if present) so `equity_curve.jsonl` — which is append-only and never rewritten — cannot double-append on a re-run in a dirty dir and false-pass. The scenario MUST exercise: (a) a triggered entry, (b) a ratchet that RAISES the stop on a new high, (c) a **winning exit** where price pulls back to the raised stop → `stop_fill` books a positive `realized_pnl_delta` (so `test_golden_includes_winning_ratchet_stop_exit` passes), and (d) at least one poll with a missing quote (so `n_marked_at_cost` is exercised in the curve).

- [ ] **Step 3: Freeze the golden** — write `scripts/regen_paper_book_golden.py` that `shutil.rmtree`s + recreates `tests/live/fixtures/golden_paper_book/`, runs `replay()` into it, and leaves `book.json` + `equity_curve.jsonl` there. (The rmtree is REQUIRED — without it a second regen double-appends the equity curve.) Run it once; eyeball the numbers against hand math; commit the fixtures.

- [ ] **Step 4: Run, expect PASS** — `pytest tests/live/test_golden_paper_book.py -q`
- [ ] **Step 5: Commit** — `git commit -am "test(live): golden paper-book replay determinism lock — P3.5"`

---

## Task P4.1: Driver `scripts/run_paper_book.py`

**Files:** Create `scripts/run_paper_book.py`

- [ ] **Step 1: Implement the driver** (modes `arm` / `poll` / `status`; reads the agent's raw MCP JSON from `data/live/paper_book/raw/`, normalizes centrally via `mcp_live`, builds `StaticMarketData`, calls `arm_day`/`advance_poll`, prints a compact summary). Mirrors `scripts/run_paper_monitor_live.py` structure: `REPO`/`sys.path` insert, `_load(name)`, central normalize, keyword-only `StaticMarketData(...)`, `ACCOUNT="123456789"`, `EQUITY`→`start_capital=2000.0`. Timestamps are passed in as argv (never computed in-module), e.g. `python scripts/run_paper_book.py poll 2026-06-23T14:00:00Z`.

- [ ] **Step 2: Smoke-run against the committed fixtures** — point `raw/` at copied `tests/live/fixtures/mcp_samples/` shapes; run `arm` then `poll`; confirm `book.json` + `equity_curve.jsonl` are written and the summary prints.

- [ ] **Step 3: Commit** — `git commit -am "feat(live): run_paper_book.py driver (arm/poll/status) — P4.1"`

---

## Task P4.2: `RUNBOOK_PAPER_BOOK.md` (the agent loop)

**Files:** Create `RUNBOOK_PAPER_BOOK.md`

- [ ] **Step 1: Write the runbook**, covering exactly:
  - **Preconditions:** interactive session w/ Robinhood MCP; `get_accounts` → `123456789 agentic_allowed`; refresh SPY calendar forward each morning; P0 spike passed.
  - **Daily arm (once, at/after open per `session_clock` + quote `state`):** scan → tradability → enrich (foreground subagents fetch large historicals → main loop normalizes centrally) → `run_paper_book.py arm <ts>`.
  - **Poll loop (every ~15 min while `is_regular_session` and quote `state` open):** `get_equity_quotes(held + armed)` → write raw → `run_paper_book.py poll <ts>` → `ScheduleWakeup(~15m)`.
  - **Self-pacing:** the concrete `ScheduleWakeup` call + the nightly-boundary behavior decided in P0 Step 4 (long sleep vs session boundary).
  - **External heartbeat:** the `CronCreate`/`scheduled-tasks` job that reads `equity_curve.jsonl` mtime/last-ts and `PushNotification`s the operator if stale > ~20 min during RTH.
  - **Hang timeout:** if a poll produces no `book.json` ts advance within N seconds, next wake records a gap note + pings.
  - **Token expiry:** refuse to `arm` past `token_issue_ts + ~90h`; ping the morning before; treat expiry as a planned stop.
  - **Loud death / quiet pause:** `PushNotification` on death (best-effort); the durable audit = calendar trading days vs `equity_curve.jsonl` rows; nightly close = pause.
  - **Off-machine copy:** periodically copy `book.json` off the laptop.
  - **Hard guardrails:** NO order-surface calls at all (not even `review_equity_order`); broker is read-only quotes/positions; never force-sell on missing data; pin `adjustment_type='split'` on historicals.

- [ ] **Step 2: Commit** — `git commit -am "docs(live): RUNBOOK_PAPER_BOOK.md — the agent loop procedure — P4.2"`

---

## Task P5: Offline dry-run → first supervised live session

**Files:** none (operational)

- [ ] **Step 1: Full offline dry-run** — run the entire `arm`→multi-`poll` flow against `StaticMarketData` scripted quotes (the golden scenario extended with a multi-day arc); confirm the book, fills, and equity curve are coherent and hand-checkable.
- [ ] **Step 2: Full test suite green + invariants** — `pytest -q` (all `tests/live/` incl. `test_no_place_invariant.py`; its source-scan auto-parametrizes over every `src/autotrader_live/*.py` × 5 forbidden tokens, so the count grows from 55 to ~75 as this phase's 4 new modules land — all must contain none of the tokens, incl. `review_equity_order`); confirm `git diff <base> -- src/autotrader/` is EMPTY (firewall).
- [ ] **Step 3: First SUPERVISED live session** — the operator present; run one real `arm` + a few `poll`s against the live MCP during RTH; review the produced `book.json`/`equity_curve.jsonl` together; confirm fills look sane vs the live tape.
- [ ] **Step 4: Decision** — the operator decides whether to let the loop run continuously (session-bound) from here.

---

## Self-review (run against the spec)

**Spec coverage:** §1 firewall/no-place → P1/P2 module placement + reuse of existing `test_no_place_invariant` (P5.2). §2.1 PaperBook → P1.1–P1.6. §2.2 fill_engine → P2.1–P2.5 (entry@ask+slippage, exit@bid spread-crossing, gap-through, sampled ratchet, sanity gate, dust floor). §2.3 orchestrator (arm off morning cost-equity; advance_poll) → P3.1–P3.3. §2.4 session_clock → P2.6. §2.5 driver → P4.1. §2.6/§5 run model (ScheduleWakeup session-bound, P0 spike, heartbeat, timeout, token-expiry, instant-cash) → P0 + P4.2 (RUNBOOK). §1.5 idempotency/crash → P3.4 + P1.6. §7 testing (hand-worked, crash, golden, half-day) → P2/P3.4/P3.5/P2.6. §9 honesty caveat → P4.2 runbook + spec.

**Placeholder scan:** none — every code step has full code; the only non-code tasks (P0, P5) are operational by nature with explicit acceptance steps.

**Type consistency:** `Fill`/`PaperPosition`/`ArmedEntry`/`BookSnapshot` fields are defined once (P1.1/P1.2/P1.4) and used consistently in `fill_engine` (P2.3/P2.4) and `paper_loop` (P3.1/P3.2). `fill_id` format (`SYM:entry:<arm_date>`, `SYM:stop:<ratchet_seq>`) is identical in `entry_fill`/`stop_fill` and the reconcile/idempotency tests. Reused signatures (`size`, `initial_catastrophe_stop`, `update_trailing_stop`, `build_universe`, `Candidate.cost_tier.roundtrip_bps`, `Quote` fields, `StaticMarketData(**kwargs)`) match the verified source.

**Plan-review fixes folded in (3 cold reviewers, 2026-06-23):** (HIGH) stop `fill_id` date-namespaced (`SYM:stop:{entry_date}:{seq}`) so cross-day flat stop-outs don't dedup-drop (P2.4 + P3.4 regression); (HIGH) `completed_bar_guard` added to `arm_day` with a look-ahead-skip test, and the `_bars_ending` fixture fixed to end at `signal_date` (P3.1). (MEDIUM) poll-before-arm ordering guard so a stale `armed[]` can't fire after an overnight wake/restart (P3.2); `n_marked_at_cost` + `entries_taken` in the equity row so data-outage / stale-arm polls are auditable, not silently flattering (P1.4/P3.2); orchestrator-level never-force-sell test (P3.3); robust path-based golden scenario import + regen-clears-dir + winning-ratchet-stop assertion (P3.5); per-commit no-place scan (Per-task gate note).

**Known follow-on (out of scope, recorded):** verify the live `quote.state` string in P0 and extend `_ALLOWED_STATES`; the per-name 15% cap binds before the vol-target on a $2k book (cosmetic vol-target — documented in spec §9); the dust-floor freeze tail (equity-at-cost < ~$333 → all targets dust) should emit a distinct logged reason vs "no qualifiers" (low-pri operational note).
