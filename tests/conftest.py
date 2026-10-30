"""Shared test setup.

Every suite except test_cal_wait_scanstop.py runs with the calibration stop
gate pinned off (see _gate_pin.py for why). test_cal_wait_scanstop.py owns
the default, enforce-failure and off-equivalence checks and runs on the
real default; test_gate_pin_parity.py tests the pin itself. Suites run as scripts are pinned by run_suite.py instead.
"""
from __future__ import annotations

import pytest

from _gate_pin import gate_off

_OWNS_THE_GATE = ("test_cal_wait_scanstop", "test_gate_pin_parity")


@pytest.fixture(autouse=True)
def _gate_off_outside_its_own_suite(request):
    if request.module.__name__.rpartition(".")[2] in _OWNS_THE_GATE:
        yield
    else:
        with gate_off():
            yield
