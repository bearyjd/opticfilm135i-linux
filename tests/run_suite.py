"""Run a suite as a script with the calibration stop gate pinned off.

    python tests/run_suite.py test_safety

Same as `python tests/test_safety.py`, except the gate pin that conftest.py
gives pytest also applies (the script-run suites never see conftest.py).
"""
from __future__ import annotations

from contextlib import nullcontext
import os
import runpy
import sys

# pytest-style suites (no __main__ of their own): run through pytest and print
# the "N tests passed" line tools/release_check.py classifies.
PYTEST_SUITES = ("test_cold_homing_rounds", "test_diag_readback_regs",
                 "test_load_probe", "test_replay_harness")

_HERE = os.path.dirname(os.path.abspath(__file__))
# Run as a script, only tests/ is on sys.path; _gate_pin imports of135i from the repo root.
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from _gate_pin import gate_off  # noqa: E402


class _Count:
    def __init__(self) -> None:
        self.passed = self.skipped = 0

    def pytest_runtest_logreport(self, report) -> None:
        if report.when == "call" and report.passed:
            self.passed += 1
        elif report.skipped:
            self.skipped += 1


def run_pytest(path: str) -> int:
    import pytest
    count = _Count()
    rc = pytest.main(["-q", "-p", "no:cacheprovider", path], plugins=[count])
    print(f"{count.passed} tests passed" + (f", {count.skipped} skipped" if count.skipped else ""))
    return int(rc)


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].startswith("test_"):
        print("usage: run_suite.py test_<name>", file=sys.stderr)
        return 2
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), argv[1] + ".py")
    with (nullcontext() if argv[1] in ("test_cal_wait_scanstop", "test_gate_pin_parity",
                                         "test_trace_recorder")
          else gate_off()):
        if argv[1] in PYTEST_SUITES:
            return run_pytest(path)
        try:
            runpy.run_path(path, run_name="__main__")
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
