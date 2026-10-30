"""CI parity for the calibration stop-gate pin.

conftest.py pins the gate off for pytest, but suites run as scripts
(`python tests/$t.py`) never load conftest.py: they reach the default
"enforce" gate on fake clocks and fail. They must go through run_suite.py,
which applies the same pin (_gate_pin.gate_off).
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(__file__))

from _gate_pin import gate_off  # noqa: E402

from of135i import cli  # noqa: E402
from of135i.device import Scanner  # noqa: E402

ROOT = os.path.join(os.path.dirname(__file__), "..")
BARE_SCRIPT = re.compile(r"python\s+tests/(?:\$t|test_\w+)\.py")


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_workflow_runs_no_suite_as_a_bare_script():
    wf = _read(".github/workflows/offline-checks.yml")
    assert not BARE_SCRIPT.findall(wf), "script-run suites must use tests/run_suite.py"
    assert "python tests/run_suite.py $t" in wf


def test_documented_script_loops_use_the_runner():
    assert "python tests/$t.py" not in _read("docs/offline-checks.md")


def test_gate_off_pins_class_and_cli_defaults_then_restores():
    before = Scanner._cal_wait_scanstop
    assert before == "enforce"
    with gate_off():
        assert Scanner._cal_wait_scanstop == "off"
        for cmd in (["digitize", "-o", "x"], ["scan", "--frame", "1", "-o", "f.tiff"]):
            assert cli.build_parser().parse_args(cmd).cal_wait_scanstop == "off"
    assert Scanner._cal_wait_scanstop == "enforce"
    assert cli.build_parser().parse_args(["digitize", "-o", "x"]).cal_wait_scanstop == "enforce"
