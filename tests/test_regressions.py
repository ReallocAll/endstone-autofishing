"""Run the original standalone regression checks in isolated interpreters.

Each check installs its own fake Endstone modules in sys.modules; isolated
processes prevent fixture/module ordering from changing their semantics.
"""

import subprocess
import sys
from pathlib import Path

import pytest


CHECKS = ("test_protocol.py", "test_registration.py", "test_state_machine.py", "test_hot_path.py")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("filename", CHECKS)
def test_regression_check(filename: str) -> None:
    script = ROOT / "tests" / "checks" / filename
    subprocess.run([sys.executable, str(script)], cwd=ROOT, check=True, timeout=30)
