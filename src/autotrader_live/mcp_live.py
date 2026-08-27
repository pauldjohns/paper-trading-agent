# src/autotrader_live/mcp_live.py
"""Pure normalizers: raw Robinhood MCP JSON → typed shapes.

Architecture reality
--------------------
Python cannot invoke MCP tools directly in this environment.  The agent's main
loop fetches live data via MCP and passes the raw JSON dicts into these PURE
normalizer functions.  This module never touches the network.

Roles:
  1. ``normalize_*`` functions — stateless converters: raw dict → typed shape.
  2. ``MarketData`` — a ``typing.Protocol`` defining the read interface the live
     loop depends on.  All methods are read-only; no placement / cancellation.
  3. ``StaticMarketData`` — concrete ``MarketData`` impl constructed from
     already-normalized data.  Serves as BOTH the test fake AND the live
     "prefetched" provider the agent populates after each MCP fetch cycle.

NO-PLACE INVARIANT (§2.5): this module contains NONE of the forbidden broker
mutation tokens.  The source-scan enforcer in
``tests/live/test_no_place_invariant.py`` will catch any violation.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import pandas as pd

# ── typing helpers ─────────────────────────────────────────────────────────────

_BARS_COLUMNS = ["date", "open", "high", "low", "close", "volume"]


def _to_float(value: object, context: str) -> float:
    """Convert *value* to float; raise ValueError with *context* on failure."""
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Expected a numeric string for {context!r}, got {value!r}"
        ) from exc
    if f != f:  # NaN check
        raise ValueError(f"Numeric conversion produced NaN for {context!r}")
    return f


def _parse_date(s: str, context: str) -> dt.date:
    """Parse ``YYYY-MM-DD`` or ``YYYY-MM-DDThh:mm:ssZ`` → ``datetime.date``."""
    try:
        return dt.date.fromisoformat(s[:10])
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Cannot parse date for {context!r}: {s!r}") from exc


# =============================================================================
# Typed shapes (frozen dataclasses)
# =============================================================================


@dataclass(frozen=True)
class ScanRow:
    """One row from a Robinhood scan result."""

    symbol: str
    instrument_id: str
    instrument_type: str
    name: str
    price: float         # columns["Close"]
    last: float          # columns["Last"]
    market_cap: float    # columns["Market cap"]
    volume: float        # columns["Volume"]
    relative_volume: float  # columns["Relative volume"]
    rsi: float           # columns["RSI"]
    pct_change: float    # columns["% Change"]


@dataclass(frozen=True)
class Quote:
    """Settled-close + live bid/ask snapshot for one symbol."""

    symbol: str
    settled_close: float
    settled_close_date: dt.date
    settled_close_interpolated: bool
    settled_close_source: str
    bid: float | None
    ask: float | None
    last_trade_price: float
    previous_close: float
    has_traded: bool
    state: str

    @property
    def spread(self) -> float | None:
        """Bid-ask spread; None if either side is absent."""
        if self.bid is None or self.ask is None:
            return None
        return self.ask - self.bid


@dataclass(frozen=True)
class Tradability:
    """Computed trade-gate plus raw tradability fields for one symbol."""

    symbol: str
    tradeable: bool   # computed gate (see normalize_tradability)
    state: str
    fractional: bool
    short_selling: bool


@dataclass(frozen=True)
class Fundamentals:
    """Key fundamental metrics for one symbol (TODAY-only snapshot).

    WARNING: ``high_52_weeks`` is a TODAY-only field (its date matches today).
    Callers MUST NOT use it for signals; derive the 52-week high from
    ``normalize_bars`` historicals instead.
    """

    symbol: str
    market_cap: float
    average_volume: float     # average_volume field (2-week average from Robinhood)
    avg_volume_30d: float     # average_volume_30_days field
    high_52_weeks: float      # TODAY-ONLY — forbidden in signal path
    sector: str
    industry: str


@dataclass(frozen=True)
class EarningsEvent:
    """Next upcoming earnings event for one symbol."""

    symbol: str
    report_date: dt.date
    timing: str    # "am" | "pm"
    verified: bool
    reported: bool  # True iff eps.actual is not None


# =============================================================================
# Normalizers
# =============================================================================


def normalize_bars(raw_historicals: dict) -> pd.DataFrame:
    """Parse ``get_equity_historicals`` response → OHLCV DataFrame.

    Drops any bar where ``interpolated == True`` (today-placeholder).
    Columns: ``["date", "open", "high", "low", "close", "volume"]`` with
    ``date`` as ``datetime.date``, OHLC as ``float``, ``volume`` as ``int``.
    Sorted ascending; duplicate dates are rejected.

    Raises
    ------
    ValueError
        If the result is empty after dropping interpolated bars, or if
        duplicate dates are present.
    KeyError
        If required fields are missing in the raw payload.
    """
    results = raw_historicals["data"]["results"]
    bars_raw = results[0]["bars"]

    rows = []
    for bar in bars_raw:
        if bar.get("interpolated", False):
            continue
        date_val = _parse_date(bar["begins_at"], "begins_at")
        open_  = _to_float(bar["open_price"],  "open_price")
        high   = _to_float(bar["high_price"],  "high_price")
        low    = _to_float(bar["low_price"],   "low_price")
        close  = _to_float(bar["close_price"], "close_price")
        volume = int(float(bar["volume"]))
        rows.append((date_val, open_, high, low, close, volume))

    if not rows:
        raise ValueError(
            "normalize_bars: no settled bars remain after dropping interpolated entries"
        )

    df = pd.DataFrame(rows, columns=_BARS_COLUMNS)
    df = df.sort_values("date").reset_index(drop=True)

    # Integrity checks
    dates = df["date"].tolist()
    if len(set(dates)) != len(dates):
        raise ValueError("normalize_bars: duplicate dates in historicals response")

    return df


def normalize_scan(raw_scan: dict) -> list[ScanRow]:
    """Parse a scan response (two shapes supported) → list of ScanRow.

    Accepts:
      - ``run_scan`` shape: ``{data: {result: {results: [...]}}}``
      - Trimmed fixture shape: top-level ``{results: [...]}``

    All column values arrive as strings and are cast to float; a non-numeric
    value raises ``ValueError``.
    """
    # Determine results list
    if "data" in raw_scan and "result" in raw_scan.get("data", {}):
        results = raw_scan["data"]["result"]["results"]
    elif "results" in raw_scan:
        results = raw_scan["results"]
    else:
        raise KeyError("normalize_scan: cannot locate 'results' in scan payload")

    rows: list[ScanRow] = []
    for item in results:
        cols = item["columns"]
        symbol = cols.get("Symbol") or item.get("ticker") or item.get("symbol")
        if symbol is None:
            raise KeyError("normalize_scan: missing symbol in scan row")

        rows.append(
            ScanRow(
                symbol=str(symbol),
                instrument_id=str(item["instrument_id"]),
                instrument_type=str(item["instrument_type"]),
                name=str(cols["Name"]),
                price=_to_float(cols["Close"], f"{symbol}.Close"),
                last=_to_float(cols["Last"], f"{symbol}.Last"),
                market_cap=_to_float(cols["Market cap"], f"{symbol}.Market cap"),
                volume=_to_float(cols["Volume"], f"{symbol}.Volume"),
                relative_volume=_to_float(cols["Relative volume"], f"{symbol}.Relative volume"),
                rsi=_to_float(cols["RSI"], f"{symbol}.RSI"),
                pct_change=_to_float(cols["% Change"], f"{symbol}.% Change"),
            )
        )

    return rows


def normalize_quotes(raw_quotes: dict) -> dict[str, Quote]:
    """Parse ``get_equity_quotes`` response → ``{symbol: Quote}``.

    ``bid`` and ``ask`` are ``None`` when the raw value is ``"0.000000"`` or
    absent.  ``spread`` is computed as a property on ``Quote``.
    """
    results = raw_quotes["data"]["results"]
    out: dict[str, Quote] = {}

    for item in results:
        q = item["quote"]
        c = item["close"]
        symbol = str(q["symbol"])

        bid_raw = q.get("bid_price", "0")
        ask_raw = q.get("ask_price", "0")
        bid_f = _to_float(bid_raw, f"{symbol}.bid_price")
        ask_f = _to_float(ask_raw, f"{symbol}.ask_price")

        out[symbol] = Quote(
            symbol=symbol,
            settled_close=_to_float(c["price"], f"{symbol}.close.price"),
            settled_close_date=_parse_date(c["date"], f"{symbol}.close.date"),
            settled_close_interpolated=bool(c.get("interpolated", False)),
            settled_close_source=str(c.get("source", "")),
            bid=bid_f if bid_f != 0.0 else None,
            ask=ask_f if ask_f != 0.0 else None,
            last_trade_price=_to_float(q["last_trade_price"], f"{symbol}.last_trade_price"),
            previous_close=_to_float(q["adjusted_previous_close"], f"{symbol}.previous_close"),
            has_traded=bool(q.get("has_traded", False)),
            state=str(q.get("state", "")),
        )

    return out


def normalize_tradability(
    raw: dict,
    account_type: str = "individual",
) -> dict[str, Tradability]:
    """Parse ``get_equity_tradability`` response → ``{symbol: Tradability}``.

    The computed ``tradeable`` gate requires ALL four clauses to be true:
      1. ``tradeable == True``
      2. ``state == "active"``
      3. ``fractional_tradability == "tradable"``
      4. The ``account_type_tradabilities`` entry matching *account_type* has
         ``account_type_tradability == "tradable"``

    If no entry matches *account_type*, the gate fails (``tradeable=False``).
    """
    results = raw["data"]["results"]
    out: dict[str, Tradability] = {}

    for item in results:
        symbol = str(item["symbol"])
        raw_tradeable: bool = bool(item.get("tradeable", False))
        state: str = str(item.get("state", ""))
        frac: str = str(item.get("fractional_tradability", ""))
        short_sell_str: str = str(item.get("short_selling_tradability", "untradable"))

        # Resolve account-type tradability
        acct_entries: list[dict] = item.get("account_type_tradabilities", [])
        acct_tradable: bool = False
        for entry in acct_entries:
            if str(entry.get("account_type", "")) == account_type:
                acct_tradable = str(entry.get("account_type_tradability", "")) == "tradable"
                break

        computed_tradeable = (
            raw_tradeable
            and state == "active"
            and frac == "tradable"
            and acct_tradable
        )

        out[symbol] = Tradability(
            symbol=symbol,
            tradeable=computed_tradeable,
            state=state,
            fractional=frac == "tradable",
            short_selling=short_sell_str == "tradable",
        )

    return out


def normalize_fundamentals(raw: dict) -> dict[str, Fundamentals]:
    """Parse ``get_equity_fundamentals`` response → ``{symbol: Fundamentals}``.

    NOTE: ``high_52_weeks`` is a TODAY-only value.  Callers must NOT use it in
    the signal path; derive the 52-week high from ``normalize_bars`` instead.
    """
    results = raw["data"]["results"]
    out: dict[str, Fundamentals] = {}

    for item in results:
        symbol = str(item["symbol"])
        out[symbol] = Fundamentals(
            symbol=symbol,
            market_cap=_to_float(item["market_cap"], f"{symbol}.market_cap"),
            average_volume=_to_float(item["average_volume"], f"{symbol}.average_volume"),
            avg_volume_30d=_to_float(item["average_volume_30_days"], f"{symbol}.avg_volume_30d"),
            high_52_weeks=_to_float(item["high_52_weeks"], f"{symbol}.high_52_weeks"),
            sector=str(item.get("sector", "")),
            industry=str(item.get("industry", "")),
        )

    return out


def normalize_earnings(raw: dict) -> dict[str, EarningsEvent]:
    """Parse ``get_earnings_calendar`` response → ``{symbol: EarningsEvent}``.

    For each symbol, keeps the earliest upcoming event where ``reported=False``.
    If all are reported, keeps the earliest overall.
    """
    results = raw["data"]["results"]

    # Group rows by symbol
    by_symbol: dict[str, list[dict]] = {}
    for item in results:
        sym = str(item["symbol"])
        by_symbol.setdefault(sym, []).append(item)

    out: dict[str, EarningsEvent] = {}

    for sym, rows in by_symbol.items():
        # Parse all rows
        parsed: list[tuple[dt.date, str, bool, bool, dict]] = []
        for item in rows:
            rep = item["report"]
            rdate = _parse_date(rep["date"], f"{sym}.report.date")
            timing = str(rep.get("timing", ""))
            verified = bool(rep.get("verified", False))
            reported = item.get("eps", {}).get("actual") is not None
            parsed.append((rdate, timing, verified, reported, item))

        # Prefer earliest with reported=False; fall back to earliest overall
        upcoming = [(d, t, v, r, i) for d, t, v, r, i in parsed if not r]
        if upcoming:
            best = min(upcoming, key=lambda x: x[0])
        else:
            best = min(parsed, key=lambda x: x[0])

        rdate, timing, verified, reported, _ = best
        out[sym] = EarningsEvent(
            symbol=sym,
            report_date=rdate,
            timing=timing,
            verified=verified,
            reported=reported,
        )

    return out


# =============================================================================
# MarketData protocol + StaticMarketData
# =============================================================================


@runtime_checkable
class MarketData(Protocol):
    """Read-only market data interface consumed by the live agent loop.

    The agent populates a ``StaticMarketData`` instance after each MCP fetch
    cycle and passes it into the decision logic.  Tests supply a
    ``StaticMarketData`` built from fixture-normalized data.

    All methods are read-only.  This protocol contains NO placement or
    cancellation methods (§2.5 no-place invariant).
    """

    def scan(self) -> list[ScanRow]:
        """Return the current scan universe."""
        ...

    def historicals(self, symbol: str) -> pd.DataFrame:
        """Return settled OHLCV bars for *symbol*."""
        ...

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        """Return settled-close + live quote snapshot for *symbols*."""
        ...

    def tradability(self, symbols: list[str]) -> dict[str, Tradability]:
        """Return computed trade-gate for *symbols*."""
        ...

    def fundamentals(self, symbols: list[str]) -> dict[str, Fundamentals]:
        """Return fundamental metrics for *symbols*."""
        ...

    def earnings(self) -> dict[str, EarningsEvent]:
        """Return next upcoming earnings event per symbol."""
        ...


class StaticMarketData:
    """Concrete ``MarketData`` implementation backed by pre-normalized data.

    Used as the test fake AND the agent-populated live provider.  The agent
    calls each ``normalize_*`` function after fetching from MCP, then wraps
    all results in a ``StaticMarketData`` for the decision pass.

    No MCP calls are made here.
    """

    def __init__(
        self,
        *,
        scan_rows: list[ScanRow],
        historicals: dict[str, pd.DataFrame],
        quotes: dict[str, Quote],
        tradability: dict[str, Tradability],
        fundamentals: dict[str, Fundamentals],
        earnings: dict[str, EarningsEvent],
    ) -> None:
        self._scan_rows = scan_rows
        self._historicals = historicals
        self._quotes = quotes
        self._tradability = tradability
        self._fundamentals = fundamentals
        self._earnings = earnings

    def scan(self) -> list[ScanRow]:
        return list(self._scan_rows)

    def historicals(self, symbol: str) -> pd.DataFrame:
        return self._historicals[symbol]  # KeyError if absent — intentional

    def quotes(self, symbols: list[str]) -> dict[str, Quote]:
        return {s: self._quotes[s] for s in symbols if s in self._quotes}

    def tradability(self, symbols: list[str]) -> dict[str, Tradability]:
        return {s: self._tradability[s] for s in symbols if s in self._tradability}

    def fundamentals(self, symbols: list[str]) -> dict[str, Fundamentals]:
        return {s: self._fundamentals[s] for s in symbols if s in self._fundamentals}

    def earnings(self) -> dict[str, EarningsEvent]:
        return dict(self._earnings)
