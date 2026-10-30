"""Run a suite as a script with the calibration stop gate pinned off.

    python tests/run_suite.py test_safety

Same as `python tests/test_safety.py`, except the gate pin that conftest.py
gives pytest also applies (the script-run suites never see conftest.py).
"""
from __future__ import annotations

import os
import runpy
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
# Run as a script, only tests/ is on sys.path; _gate_pin imports of135i from the repo root.
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from _gate_pin import gate_off  # noqa: E402


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not argv[1].startswith("test_"):
        print("usage: run_suite.py test_<name>", file=sys.stderr)
        return 2
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), argv[1] + ".py")
    with gate_off():
        try:
            runpy.run_path(path, run_name="__main__")
        except SystemExit as e:
            return e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
