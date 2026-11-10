import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_dashboard_logic():
    r = subprocess.run(["node", str(ROOT / "tests" / "frontend_logic.test.js")], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
