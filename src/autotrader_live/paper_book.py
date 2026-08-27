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

    def mark_to_market(self, quotes: dict, ts: str = "") -> BookSnapshot:
        # Local import to break the paper_book <-> fill_engine import cycle.
        from autotrader_live.fill_engine import quote_is_fillable
        mv = 0.0
        unrealized = 0.0
        n_marked_at_cost = 0
        for sym, pos in self.positions.items():
            q = quotes.get(sym)
            if q is not None and quote_is_fillable(q):
                mark = q.last_trade_price
            else:
                # missing OR unfillable quote (halted / crossed / >50% move / not
                # active / not traded) -> mark at cost; never bank a fabricated gain.
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
