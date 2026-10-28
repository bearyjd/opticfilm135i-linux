"""Failure wording when a cold-start homing round's FINAL jog (move 3/3)
ends with the motor stopped and the loader sensor clear (outcome
ended-off-sensor).

On a LATCHED holder that is the expected end state: the move releases the
holder. The report must say so, ask for a hand check (never force) and a
power cycle, and must not use the generic "new observation"/unknown-state
wording. It is reporting only: the session still ends FAILED, the exit code
stays 1, nothing more is sent, and every other outcome / move keeps the
generic wording. Pure offline logic against fake devices.
"""
from __future__ import annotations

import hashlib
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_cold_move_outcome import sim_clock  # noqa: E402
from test_safety import (FakeUsbDevice, SessionState, _cli, cli_over,  # noqa: E402
                         fast_time, make_scanner)

from of135i import loadflow, safety  # noqa: E402

# status-word high byte served for every read after execute pulse #N
BUSY, IDLE_OFF_SENSOR = 0xD9, 0xF0


def _fail_at(fake, pulse, history):
    """Moves before pulse ``pulse`` complete (f855); after it the reads
    are ``history`` (list of high bytes, the last one repeating)."""
    seen = {"n": 0}

    def high(f):
        if f.pulses == 0:
            return 0x48                       # power-on: opening poll, best effort
        if f.pulses < pulse:
            return 0xF8
        i = min(seen["n"], len(history) - 1)
        seen["n"] += 1
        return history[i]
    fake.status_high = high


def _cold(pulse, history, early=False):
    fake = FakeUsbDevice(reg01=0x00)
    _fail_at(fake, pulse, history)
    scanner = make_scanner(fake)
    scanner.cold_move_early_fail = early
    with sim_clock():
        try:
            scanner.cold_init()
        except safety.StrictPollTimeoutError as e:
            return fake, scanner, e
    raise AssertionError("expected a strict poll timeout")


OFF = [BUSY, IDLE_OFF_SENSOR]
GENERIC = "new observation"


def _released_wording(text):
    low = text.lower()
    assert "expected end state" in low, text
    assert "latched" in low, text
    assert "probably been released" in low, text
    assert "by hand" in low and "never force" in low, text
    assert "power" in low and safety.POWER_CYCLE_INSTRUCTION in text, text
    assert GENERIC not in low, text
    assert "unknown" not in low, text
    assert "1-2 s" not in text, text
    # the technical cause line stays
    assert "outcome=ended-off-sensor" in text and "status word f055" in text, text
    assert "want f855" in text, text


@pytest.mark.parametrize("pulse", [3, 6, 9])      # final jog of rounds 1, 2, 3
@pytest.mark.parametrize("early", [False, True])
def test_final_jog_ended_off_sensor_says_released(pulse, early):
    fake, scanner, e = _cold(pulse, OFF, early)
    _released_wording(str(e))
    assert "move 3/3" in str(e)
    assert e.outcome == "ended-off-sensor" and e.want.hex() == "f855"
    assert scanner.session.state is SessionState.FAILED
    assert not scanner.session.cold_init_done
    assert fake.pulses == pulse                   # nothing more started
    _released_wording(scanner.session.describe_failure())
    n = fake.out_count
    with fast_time(), pytest.raises(safety.SessionFailedError):
        scanner.cold_init()
    assert fake.out_count == n


@pytest.mark.parametrize("pulse", [1, 2, 4, 5])   # moves 1/3 and 2/3
def test_earlier_moves_keep_generic_wording(pulse):
    _fake, scanner, e = _cold(pulse, OFF)
    low = str(e).lower()
    assert GENERIC in low and "probably been released" not in low
    assert "1-2 s" not in str(e)
    assert "probably been released" not in scanner.session.describe_failure()
    assert "UNKNOWN" in scanner.session.describe_failure()


@pytest.mark.parametrize("history,outcome", [
    ([BUSY], "still-busy"),
    ([IDLE_OFF_SENSOR], "no-busy-observed"),
    ([BUSY, 0xE8], "other"),
])
def test_final_jog_other_outcomes_keep_generic_wording(history, outcome):
    _fake, scanner, e = _cold(3, history)
    assert e.outcome == outcome
    low = str(e).lower()
    assert GENERIC in low and "probably been released" not in low
    assert "1-2 s" not in str(e)
    assert "UNKNOWN" in scanner.session.describe_failure()


def test_final_jog_malformed_keeps_generic_wording():
    fake = FakeUsbDevice(reg01=0x00)
    _fail_at(fake, 3, [BUSY])
    real = fake.ctrl_transfer

    def ctrl(bm, br, wv=0, wi=0, data=None, timeout=None):
        got = real(bm, br, wv, wi, data, timeout)
        if fake.pulses >= 3 and (bm & 0x80) and br == 0x04 and wv == 0x018E \
                and (wi >> 8) == 0x01 and len(got) == 2 and got[0] & 0x01 == 0:
            return b"\xf0"
        return got
    fake.ctrl_transfer = ctrl
    _fail_at(fake, 3, [BUSY, 0xF0])
    scanner = make_scanner(fake)
    with sim_clock():
        with pytest.raises(safety.StrictPollTimeoutError) as ei:
            scanner.cold_init()
    assert ei.value.outcome == "malformed"
    assert GENERIC in str(ei.value).lower()
    assert "probably been released" not in str(ei.value)


# Fingerprints (count, sha256 prefix of repr(out_log)) of the wire traffic of
# these failures, recorded from the unmodified driver (fork/master before this
# change): the wording change must leave them byte-identical.
BASELINE_WIRE = {
    3: (55, "3624009a34919cf9"),
    6: (108, "668daad64b99fa78"),
}


@pytest.mark.parametrize("pulse", [3, 6])
@pytest.mark.parametrize("history", [OFF, [BUSY]])
def test_wire_traffic_matches_the_unmodified_driver(pulse, history):
    fake = _cold(pulse, history)[0]
    got = (len(fake.out_log), hashlib.sha256(repr(fake.out_log).encode()).hexdigest()[:16])
    assert got == BASELINE_WIRE[pulse]
    # and nothing at all is written after the failing move's execute pulse
    assert fake.out_log[-1]["data"] == bytes([0x0F, 0x01])


def _error_log(pulse, history):
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Grab(level=logging.ERROR)
    prev_level = safety.log.level
    safety.log.setLevel(logging.ERROR)
    safety.log.addHandler(handler)
    try:
        _cold(pulse, history)
    finally:
        safety.log.removeHandler(handler)
        safety.log.setLevel(prev_level)
    return " ".join(records)


def test_failure_log_does_not_claim_unknown_state():
    text = _error_log(3, OFF)
    assert "probably been released" in text and "UNKNOWN" not in text
    assert "UNKNOWN" in _error_log(3, [BUSY])


def test_load_release_cli_reports_released_and_exits_1(capsys):
    fake = FakeUsbDevice(reg01=0x00)
    fake.magazine = True
    _fail_at(fake, 3, OFF)
    with cli_over(fake), sim_clock():
        rc = loadflow.run(release_only=True)
    err = capsys.readouterr().err
    assert rc == 1
    _released_wording(err)
    assert fake.pulses == 3


def test_cold_eject_cli_reports_released_and_exits_1():
    fake = FakeUsbDevice(reg01=0x00)
    _fail_at(fake, 3, OFF)
    with cli_over(fake), sim_clock():
        code, _out, err = _cli(["eject"])
    assert code == 1
    _released_wording(err)
    assert fake.pulses == 3
