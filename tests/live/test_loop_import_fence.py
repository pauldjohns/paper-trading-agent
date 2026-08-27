"""The autonomous-loop scripts must never import autotrader_live.broker — the module
that holds LiveBroker (the funded-phase order-executing stub) + PaperBroker's
placement surface. The inert order dataclasses live in order_types.py and may be
reached. Run each script in a subprocess so the check is not polluted by other
tests importing broker into this process."""
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_LOOP_SCRIPTS = [
    "scripts/run_paper_book.py",
    "scripts/validate_raw.py",
    "scripts/compute_signal_date.py",
    "scripts/heartbeat_check.py",
    "scripts/eod_audit.py",
]


@pytest.mark.parametrize("script", _LOOP_SCRIPTS)
def test_loop_script_does_not_import_broker(script):
    code = textwrap.dedent(f"""
        import importlib.util, sys
        from pathlib import Path
        repo = Path({str(_REPO)!r})
        sys.path.insert(0, str(repo / "src"))
        spec = importlib.util.spec_from_file_location("m", repo / {script!r})
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        assert "autotrader_live.broker" not in sys.modules, (
            "{script} pulled autotrader_live.broker (LiveBroker/placement) into the loop graph")
        print("OK")
    """)
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, f"{script}: {r.stderr}"
