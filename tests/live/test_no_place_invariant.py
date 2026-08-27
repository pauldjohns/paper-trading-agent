# tests/live/test_no_place_invariant.py
"""Source-scan enforcement of the §2.5 no-place invariant.

The offline package ``autotrader_live`` must never reach any MCP mutation /
broker-call token.  This test globs every ``*.py`` under ``src/autotrader_live/``
and asserts NONE contains the forbidden tokens.

WHY: the invariant guarantees that week-1 offline canary code is review-only and
cannot accidentally place or cancel real orders.

SCOPE: this is the §2.5 enforcement teeth for the OFFLINE package (source scan).
The runtime monkeypatch tripwire — patching ``place_equity_order`` to fail-on-call
across the PaperBroker import graph — lands with the broker seam at T2.3.
"""
from pathlib import Path

import pytest

# ── locate src/autotrader_live/ relative to this test file ────────────────────
_REPO_ROOT = Path(__file__).resolve().parents[2]
_LIVE_SRC = _REPO_ROOT / "src" / "autotrader_live"

# Tokens whose presence in any source file would violate the no-place invariant.
# These are the MCP mutation / broker-call surface that must never appear in the
# offline package.
_FORBIDDEN_TOKENS: list[str] = [
    "place_equity_order",
    "place_option_order",
    "cancel_equity_order",
    "cancel_option_order",
    "review_equity_order",
    "review_option_order",   # the sixth order-surface tool
    "mcp__",                 # any raw MCP tool id (uniform with the loop-surface scan)
]


def _collect_py_files() -> list[Path]:
    """Glob all *.py files under src/autotrader_live/ (recursive)."""
    return sorted(_LIVE_SRC.rglob("*.py"))


def pytest_generate_tests(metafunc):
    """Parametrize test_no_forbidden_token across every (file, token) pair."""
    if "py_file" in metafunc.fixturenames and "token" in metafunc.fixturenames:
        py_files = _collect_py_files()
        params = [
            pytest.param(f, t, id=f"{f.name}::{t}")
            for f in py_files
            for t in _FORBIDDEN_TOKENS
        ]
        metafunc.parametrize("py_file,token", params)


def test_no_forbidden_token(py_file: Path, token: str) -> None:
    """Assert that ``py_file`` does not contain ``token``.

    Failure message names the offending file and token so the developer knows
    exactly which file introduced a forbidden broker-call.
    """
    source = py_file.read_text(encoding="utf-8")
    assert token not in source, (
        f"NO-PLACE INVARIANT VIOLATED\n"
        f"  file : {py_file.relative_to(_REPO_ROOT)}\n"
        f"  token: {token!r}\n"
        f"\n"
        f"The offline autotrader_live package must never reach broker mutation "
        f"calls.  Remove or fence the forbidden token, or — if this is PaperBroker "
        f"code being added at T2.3 — add the runtime monkeypatch tripwire required "
        f"by §2.5 before landing the file."
    )


# ── LIVE-03 fail-closed scan: ALL top-level scripts/*.py + every task prompt ──────
# Fail-closed (not an allowlist): a future order-touching script added under
# scripts/ trips this by default. scripts/legacy/ is intentionally excluded — the
# glob is non-recursive — it holds the documented, superseded LIVE-01 monitor
# (quarantined; names the order-review tool against the order-capable account).
_LOOP_SURFACE = (
    sorted((_REPO_ROOT / "scripts").glob("*.py"))                 # top-level scripts only
    + sorted((_REPO_ROOT / "automation" / "prompts").glob("*.md"))
)


def test_loop_surface_has_no_order_tokens() -> None:
    """Every top-level scripts/*.py + every task prompt must contain none of the
    forbidden order-surface tokens (incl. the raw mcp__ prefix). Fail-closed."""
    for path in _LOOP_SURFACE:
        src = path.read_text(encoding="utf-8")
        for tok in _FORBIDDEN_TOKENS:   # the six order tools + mcp__ (Task 9)
            assert tok not in src, (
                f"NO-PLACE INVARIANT VIOLATED\n  file: "
                f"{path.relative_to(_REPO_ROOT)}\n  token: {tok!r}")
