# tests/live/test_risk_invariants.py
"""Pin the risk-invariant coupling between sizing.py and exits.py.

Two invariants are enforced here by reading the ACTUAL function-signature defaults
via inspect.signature, not by hard-coding expected values.  Any silent divergence
between the modules (e.g. someone bumps k in sizing.py but forgets to bump it in
exits.py, or accidentally sets m > k) will be caught immediately.

Invariant 1 — sizing k >= catastrophe-stop m
---------------------------------------------
    sizing.size(k=...)   >=   exits.initial_catastrophe_stop(m=...)

WHY: the per-trade loss bound is:
    loss = m * ATR * shares = (m / k) * dollars_at_risk = (m / k) * f * equity

When k >= m:   loss <= f * equity   (stays within the risk budget)
When k < m:    loss > f * equity    (the stop is placed WIDER than the sizing
               assumed — even a clean-fill at the stop exceeds the budget)

Note: a gap-through fills BELOW the stop, so the ACTUAL loss can exceed
f * equity regardless.  The sizing k is what bounds the expected-case loss;
k >= m keeps even the expected-case loss within budget.

Invariant 2 — sizing k == chandelier k
---------------------------------------
    sizing.size(k=...)   ==   exits.chandelier_level(k=...)
                         ==   exits.update_trailing_stop(k=...)

WHY (spec FLAG D "same k" claim):
    - sizing computes risk_per_share = k * ATR.
    - the chandelier ratchet computes level = highest_high - k * ATR.
    - these should use the SAME k so the trail distance equals the initial
      stop distance.  A divergence means the ratchet would be calibrated to
      a different ATR multiple than what sizing assumed, making the stop
      semantics inconsistent.
"""
import inspect
import sys
from pathlib import Path

import pytest

# ── path shim ────────────────────────────────────────────────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from autotrader_live import sizing, exits


def _get_default(func, param_name: str):
    """Return the default value of `param_name` in `func`'s signature.

    Raises AssertionError if the parameter has no default (catches missing-param
    bugs at test collection time).
    """
    sig = inspect.signature(func)
    param = sig.parameters.get(param_name)
    assert param is not None, (
        f"{func.__name__}: parameter {param_name!r} not found in signature. "
        f"Available params: {list(sig.parameters)}"
    )
    assert param.default is not inspect.Parameter.empty, (
        f"{func.__name__}: parameter {param_name!r} has no default value. "
        "This test reads defaults to catch silent divergence — the parameter "
        "must have a default."
    )
    return param.default


class TestRiskInvariantCoupling:
    """Enforce the two structural invariants between sizing and exits defaults."""

    def test_sizing_k_ge_catastrophe_stop_m(self):
        """sizing.size default k must be >= exits.initial_catastrophe_stop default m.

        Loss-at-stop = m * ATR * shares = (m/k) * f * equity.
        When k >= m, loss-at-stop <= f * equity (within the risk budget).
        When k < m, loss-at-stop > f * equity (a clean stop-fill exceeds budget).

        A gap-through always fills below the stop, so sizing bounds the
        expected-case loss, not the worst-case loss.  The k >= m bound keeps
        the expected-case loss within budget.
        """
        k_sizing = _get_default(sizing.size, "k")
        m_stop = _get_default(exits.initial_catastrophe_stop, "m")

        assert k_sizing >= m_stop, (
            f"RISK INVARIANT VIOLATED: sizing.size default k={k_sizing!r} < "
            f"exits.initial_catastrophe_stop default m={m_stop!r}.\n"
            f"\n"
            f"This means the initial stop is placed WIDER than the sizing assumed:\n"
            f"  loss-at-stop = m * ATR * shares = (m/k) * f * equity\n"
            f"  = ({m_stop}/{k_sizing}) * f * equity  > f * equity\n"
            f"\n"
            f"Fix: ensure sizing k >= catastrophe-stop m.  "
            f"Current: k={k_sizing}, m={m_stop}."
        )

    def test_sizing_k_eq_chandelier_level_k(self):
        """sizing.size default k must equal exits.chandelier_level default k.

        Spec FLAG D (STRATEGY_CANARY_SPEC.md §4): the chandelier ratchet uses
        the same ATR multiple as the sizing stop-distance so the trail calibration
        is consistent with the initial risk-per-share.

        A divergence means the chandelier ratchet would use a different ATR
        multiple than what sizing assumed, making the stop semantics inconsistent.
        """
        k_sizing = _get_default(sizing.size, "k")
        k_chandelier = _get_default(exits.chandelier_level, "k")

        assert k_sizing == k_chandelier, (
            f"RISK INVARIANT VIOLATED: sizing.size default k={k_sizing!r} != "
            f"exits.chandelier_level default k={k_chandelier!r}.\n"
            f"\n"
            f"Both must use the same ATR multiplier (spec FLAG D) so the chandelier "
            f"trail distance equals the initial stop distance.  "
            f"Fix: align k in sizing.py and exits.py."
        )

    def test_sizing_k_eq_update_trailing_stop_k(self):
        """sizing.size default k must equal exits.update_trailing_stop default k.

        update_trailing_stop is the daily-close ratchet driver.  It should use
        the same k as chandelier_level and sizing to maintain consistent trail
        calibration throughout the position's life.
        """
        k_sizing = _get_default(sizing.size, "k")
        k_update = _get_default(exits.update_trailing_stop, "k")

        assert k_sizing == k_update, (
            f"RISK INVARIANT VIOLATED: sizing.size default k={k_sizing!r} != "
            f"exits.update_trailing_stop default k={k_update!r}.\n"
            f"\n"
            f"The daily-close ratchet must use the same ATR multiplier as the "
            f"initial sizing to maintain consistent trail calibration.  "
            f"Fix: align k in sizing.py and exits.py."
        )

    def test_chandelier_k_eq_update_trailing_stop_k(self):
        """exits.chandelier_level and exits.update_trailing_stop must agree on k.

        update_trailing_stop delegates to chandelier_level internally, so these
        should always agree.  This test catches a copy-paste divergence if the
        two functions are ever refactored independently.
        """
        k_chandelier = _get_default(exits.chandelier_level, "k")
        k_update = _get_default(exits.update_trailing_stop, "k")

        assert k_chandelier == k_update, (
            f"exits.chandelier_level k={k_chandelier!r} != "
            f"exits.update_trailing_stop k={k_update!r} — "
            "these must always agree."
        )

    def test_report_actual_defaults(self):
        """Report the actual defaults; always passes — used for CI output visibility."""
        k_sizing = _get_default(sizing.size, "k")
        m_stop = _get_default(exits.initial_catastrophe_stop, "m")
        k_chandelier = _get_default(exits.chandelier_level, "k")
        k_update = _get_default(exits.update_trailing_stop, "k")
        f_sizing = _get_default(sizing.size, "f")
        cap_sizing = _get_default(sizing.size, "per_name_cap_frac")

        print(
            f"\n[Risk invariants] sizing.size: k={k_sizing}, f={f_sizing}, "
            f"per_name_cap_frac={cap_sizing}\n"
            f"[Risk invariants] exits.initial_catastrophe_stop: m={m_stop}\n"
            f"[Risk invariants] exits.chandelier_level: k={k_chandelier}\n"
            f"[Risk invariants] exits.update_trailing_stop: k={k_update}\n"
            f"[Risk invariants] k_sizing >= m_stop: {k_sizing >= m_stop} "
            f"(loss-at-stop = (m/k)*f*equity = ({m_stop}/{k_sizing})*{f_sizing}*equity "
            f"= {m_stop / k_sizing * f_sizing * 100:.2f}% per trade)\n"
            f"[Risk invariants] k_sizing == k_chandelier: {k_sizing == k_chandelier}"
        )
        assert True  # always passes — visibility only
