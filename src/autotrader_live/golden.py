# src/autotrader_live/golden.py
"""Deterministic offline-replay golden snapshot builder — T1.5.

Builds a JSON-serialisable snapshot of the today-decision pipeline run over all
15 cached ETFs at their last bar.  The frozen fixture
``tests/live/fixtures/golden_trend_decisions.json`` is generated once (via the
``__main__`` block below) and then validated on every CI run by
``tests/live/test_golden_replay.py``.

CONTRACT
--------
- ``build_snapshot`` is PURE: same cache → same output, forever.
- All floats are rounded to ``_ROUND_DP`` (10) decimal places so the frozen
  file is platform-stable (IEEE-754 double arithmetic is deterministic for the
  same sequence of operations on the same data, but repr differences between
  platforms could shift trailing digits past dp 15).
- ``snapshot_json`` serialises with ``sort_keys=True`` and a trailing newline
  so the file is byte-for-byte reproducible regardless of dict-insertion order.
- NaN is explicitly rejected: if any float in the snapshot is NaN, ``build_snapshot``
  raises immediately (all 15 ETFs have 5396 bars → every sub-signal is non-NaN;
  a NaN signals a cache or indicator bug, not a normal "insufficient history"
  path).

FIREWALL — this module imports from src/autotrader/ and src/autotrader_live/
read-only.  It does NOT edit any file in either package.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Any

# ── make ``src/`` importable when run directly ────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from autotrader.datastore import DataStore
from autotrader_live.strategy_trend import decide
from autotrader_live.sizing import size
from autotrader_live.exits import initial_catastrophe_stop

# ── public constants (consumed by tests and the live loop) ───────────────────
GOLDEN_SYMBOLS: list[str] = [
    "AGG", "DIA", "IEF", "IWM", "QQQ", "SPY",
    "XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY",
]
GOLDEN_EQUITY: float = 100_000.0
GOLDEN_PARAMS: dict[str, float] = {
    "near_threshold": 0.90,
    "f": 0.01,
    "k": 3.0,
    "per_name_cap_frac": 0.15,
    "m": 2.0,
}
_ROUND_DP: int = 10


# ── helpers ───────────────────────────────────────────────────────────────────

def _round_float(v: float) -> float:
    """Round a float to _ROUND_DP decimal places for platform-stable serialisation."""
    return round(v, _ROUND_DP)


def _clean_decision(dec: Any, params: dict) -> dict:
    """Convert a TrendDecision to a JSON-safe dict with floats rounded.

    Raises ValueError if any float field contains NaN.
    """
    raw = dec.to_dict()
    out: dict[str, Any] = {}
    for key, val in raw.items():
        if key == "signal_date":
            # datetime.date → ISO string
            out[key] = val.isoformat() if hasattr(val, "isoformat") else str(val)
        elif isinstance(val, bool):
            out[key] = bool(val)
        elif isinstance(val, float):
            if math.isnan(val):
                raise ValueError(
                    f"NaN detected in decision field '{key}' — "
                    "cache or indicator bug; all 15 ETFs must have full history."
                )
            out[key] = _round_float(val)
        else:
            out[key] = val
    return out


def _clean_sizing(sz: dict) -> dict:
    """Round float fields in a sizing dict; leave capped (bool) intact."""
    return {
        "shares": _round_float(sz["shares"]),
        "notional": _round_float(sz["notional"]),
        "risk_per_share": _round_float(sz["risk_per_share"]),
        "dollars_at_risk": _round_float(sz["dollars_at_risk"]),
        "capped": bool(sz["capped"]),
    }


# ── public API ────────────────────────────────────────────────────────────────

def _build_row(
    bars,
    sym: str,
    *,
    equity: float,
    near_threshold: float,
    f: float,
    k: float,
    per_name_cap_frac: float,
    m: float,
) -> dict:
    """Build a single per-symbol row for the snapshot.

    When an entry name's ``initial_catastrophe_stop`` would raise ``ValueError``
    (i.e. ``close − m·ATR ≤ 0`` — ATR too wide for the price), the row is
    NOT propagated with an exception.  Instead the row is recorded with
    ``sizing: null``, ``initial_catastrophe_stop: null``, and an extra key
    ``"skip_reason": "infeasible_catastrophe_stop"``.  Normal rows (both entry
    and no-entry) never carry ``skip_reason``.

    Parameters
    ----------
    bars:
        DataFrame returned by ``DataStore.load(sym, "day", "split")``.
    sym:
        Ticker symbol string.
    equity, near_threshold, f, k, per_name_cap_frac, m:
        Strategy parameters (from the ``params`` dict).

    Returns
    -------
    dict
        One decision row ready for the ``"decisions"`` list.
    """
    dec = decide(bars, sym, near_threshold=near_threshold)
    dec_dict = _clean_decision(dec, {
        "near_threshold": near_threshold, "f": f, "k": k,
        "per_name_cap_frac": per_name_cap_frac, "m": m,
    })

    if dec.entry:
        close = dec.close
        atr14 = dec.atr14
        try:
            stop_val: float | None = _round_float(
                initial_catastrophe_stop(close, atr14, m)
            )
        except ValueError:
            # ATR too wide for the price: close − m·ATR ≤ 0.
            # Skip sizing and stop for this name rather than crashing the snapshot.
            return {
                "symbol": sym,
                "decision": dec_dict,
                "sizing": None,
                "initial_catastrophe_stop": None,
                "skip_reason": "infeasible_catastrophe_stop",
            }
        sz_raw = size(equity, atr14, close, f=f, k=k, per_name_cap_frac=per_name_cap_frac)
        sz_dict: dict | None = _clean_sizing(sz_raw)
    else:
        sz_dict = None
        stop_val = None

    return {
        "symbol": sym,
        "decision": dec_dict,
        "sizing": sz_dict,
        "initial_catastrophe_stop": stop_val,
    }


def build_snapshot(
    store: DataStore,
    symbols: list[str] = GOLDEN_SYMBOLS,
    *,
    equity: float = GOLDEN_EQUITY,
    params: dict = GOLDEN_PARAMS,
) -> dict:
    """Build a canonical, JSON-serialisable today-decision snapshot.

    Parameters
    ----------
    store:
        A ``DataStore`` instance pointing at the split-adjusted daily cache.
    symbols:
        Ordered list of symbols to process.  The output ``decisions`` list is
        sorted by symbol ascending (regardless of the order passed here) to
        guarantee determinism.
    equity:
        Account equity used for sizing.
    params:
        Strategy parameters dict.  Required keys: ``near_threshold``, ``f``,
        ``k``, ``per_name_cap_frac``, ``m``.

    Returns
    -------
    dict
        Schema-v1 snapshot ready for ``snapshot_json()``.

    Raises
    ------
    ValueError
        If any float in the snapshot is NaN (indicates a cache/indicator bug).
    """
    near_threshold = params["near_threshold"]
    f = params["f"]
    k = params["k"]
    per_name_cap_frac = params["per_name_cap_frac"]
    m = params["m"]

    decisions: list[dict] = []

    for sym in sorted(symbols):  # sort for determinism
        bars = store.load(sym, "day", "split")
        row = _build_row(
            bars, sym,
            equity=equity,
            near_threshold=near_threshold,
            f=f, k=k, per_name_cap_frac=per_name_cap_frac, m=m,
        )
        decisions.append(row)

    return {
        "schema_version": 1,
        "signal_basis": "split",
        "equity": equity,
        "params": {k: v for k, v in sorted(params.items())},
        "decisions": decisions,
    }


def snapshot_json(snapshot: dict) -> str:
    """Canonical serialisation: deterministic JSON with a trailing newline.

    Uses ``sort_keys=True`` so dict-insertion order can never affect the output.
    """
    return json.dumps(snapshot, indent=2, sort_keys=True) + "\n"


# ── generation entrypoint ─────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    # When invoked as ``python src/autotrader_live/golden.py`` from the repo root,
    # __file__ resolves correctly; when installed via pip/editable, _REPO_ROOT is
    # already correct.  Either way, the defaults below are sensible.
    _default_cache = str(_REPO_ROOT / "data" / "cache")
    _default_out = str(
        _REPO_ROOT / "tests" / "live" / "fixtures" / "golden_trend_decisions.json"
    )

    parser = argparse.ArgumentParser(
        description="Build and write the golden today-decision fixture."
    )
    parser.add_argument(
        "--cache",
        default=_default_cache,
        help="Path to the split-adjusted daily cache directory.",
    )
    parser.add_argument(
        "--out",
        default=_default_out,
        help="Output path for the frozen fixture JSON.",
    )
    args = parser.parse_args()

    store = DataStore(args.cache)
    snapshot = build_snapshot(store)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(snapshot_json(snapshot), encoding="utf-8")

    n_entry = sum(
        1 for d in snapshot["decisions"] if d["sizing"] is not None
    )
    print(f"Wrote {out_path}")
    print(f"  {len(snapshot['decisions'])} decisions, {n_entry} with entry=True")
    # Spot-check SPY
    spy = next(d for d in snapshot["decisions"] if d["symbol"] == "SPY")
    print(f"  SPY: entry={spy['decision']['entry']}, "
          f"sizing={spy['sizing'] is not None}, "
          f"stop={spy['initial_catastrophe_stop']}")
