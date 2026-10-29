"""Shared test setup.

The calibration stop gate (Scanner.cal_wait_scanstop) defaults to "enforce".
Every suite except test_cal_wait_scanstop.py exercises other behaviour
against fakes that script their own status replies and clocks, and
test_wire_baseline.py pins the transfer sequence without the gate's status
reads. Those suites run with the gate off (class default and CLI
parser default) so their expectations keep their meaning; test_cal_wait_scanstop.py owns the default, enforce-failure and
off-equivalence checks and runs on the real default.
"""
from __future__ import annotations

import pytest

from of135i import cli
from of135i.device import Scanner

_OWNS_THE_GATE = "test_cal_wait_scanstop"


@pytest.fixture(autouse=True)
def _gate_off_outside_its_own_suite(request, monkeypatch):
    if request.module.__name__.rpartition(".")[2] == _OWNS_THE_GATE:
        return
    monkeypatch.setattr(Scanner, "_cal_wait_scanstop", "off")
    build_parser = cli.build_parser

    def gate_off_parser(*a, **k):
        parser = build_parser(*a, **k)
        for action in parser._actions:
            for sub in getattr(action, "choices", None) or {}:
                sp = action.choices[sub]
                if hasattr(sp, "set_defaults") and any(
                        "--cal-wait-scanstop" in x.option_strings for x in sp._actions):
                    sp.set_defaults(cal_wait_scanstop="off")
        return parser

    monkeypatch.setattr(cli, "build_parser", gate_off_parser)
