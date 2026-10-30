"""The one place that pins the calibration stop gate off for legacy suites.

Scanner.cal_wait_scanstop defaults to "enforce". Suites other than
test_cal_wait_scanstop.py use fakes that script their own status replies
and clocks (and test_wire_baseline.py pins the sequence without the gate's
status reads), so they run with the gate off: the Scanner class default and
the CLI parser default. Used by conftest.py (pytest) and run_suite.py
(suites run as scripts, which conftest never reaches).
"""
from __future__ import annotations

from contextlib import contextmanager

from of135i import cli
from of135i.device import Scanner


def _gate_off_parser(build_parser):
    def build(*a, **k):
        parser = build_parser(*a, **k)
        for action in parser._actions:
            for sp in (getattr(action, "choices", None) or {}).values():
                if hasattr(sp, "set_defaults") and any(
                        "--cal-wait-scanstop" in x.option_strings for x in sp._actions):
                    sp.set_defaults(cal_wait_scanstop="off")
        return parser
    return build


@contextmanager
def gate_off():
    scanner_default, build_parser = Scanner._cal_wait_scanstop, cli.build_parser
    Scanner._cal_wait_scanstop = "off"
    cli.build_parser = _gate_off_parser(build_parser)
    try:
        yield
    finally:
        Scanner._cal_wait_scanstop, cli.build_parser = scanner_default, build_parser
