# src/autotrader/walkforward.py
"""Anchored walk-forward (D3): the engine runs ONCE over full history; this module slices that
single daily-return series into reporting periods. Parameters are literature-locked (never refit),
so the expanding windows are for OUT-OF-SAMPLE metric reporting, not re-optimization; the forced
stress folds (spec §3.8) make the only deep-bear (2008-09) + 2020 + 2022 separately reported."""
import datetime as dt
import pandas as pd

STRESS_PERIODS = {
    "2008-09": (dt.date(2008, 1, 1), dt.date(2009, 12, 31)),
    "2020": (dt.date(2020, 1, 1), dt.date(2020, 12, 31)),
    "2022": (dt.date(2022, 1, 1), dt.date(2022, 12, 31)),
}


def expanding_windows(returns: pd.Series):
    """Expanding windows anchored at the series start, ending at each calendar year-end present in
    the data, plus the full series. Each is a slice of the same continuous run (no reset)."""
    r = pd.Series(returns)
    dates = list(r.index)
    start = dates[0]
    years = sorted({d.year for d in dates})
    wins = []
    for y in years:
        ye = dt.date(y, 12, 31)
        sl = r[[d <= ye for d in dates]]
        if len(sl) and sl.index[-1] != start:
            wins.append(sl)
    if not wins or wins[-1].index[-1] != dates[-1]:
        wins.append(r)
    # dedupe by end-date, keep order
    seen, out = set(), []
    for w in wins:
        key = w.index[-1]
        if key not in seen:
            seen.add(key); out.append(w)
    return out


def stress_folds(returns: pd.Series):
    """Extract the forced 2008-09 / 2020 / 2022 windows as slices (empty if absent in the data)."""
    r = pd.Series(returns)
    dates = list(r.index)
    out = {}
    for name, (lo, hi) in STRESS_PERIODS.items():
        sl = r[[lo <= d <= hi for d in dates]]
        out[name] = sl
    return out
