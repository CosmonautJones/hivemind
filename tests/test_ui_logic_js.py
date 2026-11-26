"""Runs the UI's pure-logic tests (tests/js/*.test.mjs, node:test) when node is installed; skipped otherwise."""
import shutil
import subprocess
from pathlib import Path

import pytest

JS_DIR = Path(__file__).resolve().parent / "js"


def test_ui_pure_logic_with_node():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    files = sorted(str(p) for p in JS_DIR.glob("*.test.mjs"))
    assert files, "no JS test files found"
    r = subprocess.run([node, "--test", *files], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stdout[-4000:] + r.stderr[-2000:]
