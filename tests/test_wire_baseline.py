"""Wire-sequence regression: host-side diagnostics must not change traffic.

Each scenario drives a full driver flow against the offline FakeUsbDevice
and hashes the ordered host->device side of every transfer it saw
(``FakeUsbDevice.wire_log``: control writes with their data, control
reads with their length, bulk writes, bulk reads). The digests below were
recorded from the driver BEFORE the status-word decoder, the eject
interpretation text and the travel-margin diagnostic were added; those
changes are bookkeeping only, so with default settings every digest must
stay identical. The fast_time() scenarios expire every mismatched poll
after one read; the MULTI_READ scenarios use a small per-call clock tick
so busy -> done waits and timed-out polls read many times, which pins
the read count of every poll loop as well. A digest change means a transfer was added, removed,
reordered or altered -- that is a behaviour change, not a diagnostic.

Pure offline logic: no USB, no scanner.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_safety import FakeUsbDevice, _cal_buffers, fast_time, make_scanner  # noqa: E402

from of135i import safety  # noqa: E402


def _digest(wire_log: list[dict]) -> str:
    canon = [{k: (v.hex() if isinstance(v, (bytes, bytearray)) else v)
              for k, v in sorted(e.items())} for e in wire_log]
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()


def _plain_scan(fake):
    s = make_scanner(fake)
    s.initialize()
    s.scan(frame=1)


def _dual_ir_scan(fake):
    s = make_scanner(fake)
    s.initialize(ir=True)
    s.scan(frame=1, ir=True)


def _slide4_scan(fake):
    s = make_scanner(fake)
    s.initialize(dpi=1200)
    s.scan(frame=4, dpi=1200, slide=True)


def _eject_completes(fake):
    # The fake answers idle (f855) on the first post-GO read.
    assert make_scanner(fake).eject() == "completed"


def _eject_times_out(fake):
    fake.status_high = lambda f: 0xF9 if f.pulses else None
    with pytest.raises(safety.EjectIncompleteError):
        make_scanner(fake).eject()


def _eject_sensor_clear(fake):
    fake.magazine = False
    make_scanner(fake).eject()


def _cold_move_times_out(fake):
    # Cold start (reg 0x01 = 0x00): the first loader move never reads done.
    fake.regs[0x01] = 0x00
    fake.status_high = lambda f: 0xD9 if f.pulses else None
    with pytest.raises(safety.StrictPollTimeoutError):
        make_scanner(fake).cold_init()


@contextmanager
def tick_time(step: float = 0.01):
    """time.monotonic() advancing ``step`` per call, sleep a no-op: every
    poll loop reads many times before a match or its deadline, and the
    number of reads depends on how many clock calls the loop makes -- so
    an added clock call or sleep inside a loop changes the digest."""
    state = {"t": 0.0}

    def mono():
        state["t"] += step
        return state["t"]
    real_sleep, real_mono = time.sleep, time.monotonic
    time.sleep, time.monotonic = (lambda s: None), mono
    try:
        yield
    finally:
        time.sleep, time.monotonic = real_sleep, real_mono


def _after_each_pulse(fake, busy_reads: int, busy: int, done: int):
    """Status high byte: ``busy`` for the first ``busy_reads`` status reads
    after every execute pulse, then ``done``."""
    seen = {"pulse": 0, "reads": 0}

    def high(f):
        if f.pulses != seen["pulse"]:
            seen["pulse"], seen["reads"] = f.pulses, 0
        seen["reads"] += 1
        return busy if f.pulses and seen["reads"] <= busy_reads else done
    fake.status_high = high


def _multi_eject_busy_then_idle(fake):
    _after_each_pulse(fake, 5, 0xD9, 0xF8)
    with tick_time(0.001):
        assert make_scanner(fake).eject() == "completed"


def _multi_eject_times_out(fake):
    fake.status_high = lambda f: 0xF9 if f.pulses else None
    with tick_time(0.01), pytest.raises(safety.EjectIncompleteError):
        make_scanner(fake).eject()


def _multi_cold_init_busy_then_done(fake):
    fake.regs[0x01] = 0x00
    _after_each_pulse(fake, 7, 0xD9, 0xF8)
    with tick_time(0.01):
        make_scanner(fake).cold_init()


def _multi_cold_move_times_out(fake):
    fake.regs[0x01] = 0x00
    fake.status_high = lambda f: 0xD9 if f.pulses else None
    with tick_time(0.05), pytest.raises(safety.StrictPollTimeoutError):
        make_scanner(fake).cold_init()


def _multi_plain_scan(fake):
    with tick_time(0.01):
        _plain_scan(fake)


MULTI_READ = {
    "multi_eject_busy_then_idle": _multi_eject_busy_then_idle,
    "multi_eject_times_out": _multi_eject_times_out,
    "multi_cold_init_busy_then_done": _multi_cold_init_busy_then_done,
    "multi_cold_move_times_out": _multi_cold_move_times_out,
    "multi_plain_3600_frame1": _multi_plain_scan,
}

SCENARIOS = {
    "plain_3600_frame1": _plain_scan,
    "dual_ir_frame1": _dual_ir_scan,
    "slide4_1200": _slide4_scan,
    "eject_completes": _eject_completes,
    "eject_times_out": _eject_times_out,
    "eject_sensor_clear": _eject_sensor_clear,
    "cold_move_times_out": _cold_move_times_out,
}

#: Recorded from the unmodified driver (fork master 2da5158), including
#: the MULTI_READ entries (recorded by running this file against a clean
#: export of that commit).
BASELINE = {
    'cold_move_times_out': ('6f464b5fdeae465e9c8cebd6ed0bdaa32a7810710f9c031e5b58faddcef580f7', 49),
    'dual_ir_frame1': ('521ac284f6538a1b4de1316f17014b3b2a18d599e6130927d2f2daaa08fe3500', 24164),
    'eject_completes': ('076cdba7eb651025b055d4e0e1cb0685e583e669127bec6dbe2032b718e0ab84', 16),
    'eject_sensor_clear': ('4b8b9196a550c8201b36667abe79c9d73ca24ecb4f29c167cbdda9d32579efab', 2),
    'eject_times_out': ('076cdba7eb651025b055d4e0e1cb0685e583e669127bec6dbe2032b718e0ab84', 16),
    'plain_3600_frame1': ('bdb210aa37a02eed88fcd9fdc5db34ffef504e2c84e0135fefc3837331659e06', 9789),
    'slide4_1200': ('4b8c1dc6b3accc93590c7036c1c0b0af81ac2cdcc290809e773cb5a9e260a0cb', 4737),
    'multi_cold_init_busy_then_done': ('b4cd53d161b3b91276f10d698b3b1e98482ad32c33ecdfe4d8c8c186a5e99521', 289),
    'multi_cold_move_times_out': ('3ceb72235f16c610b528a82e8076956c61ec3582812d50f1445a04431218b226', 648),
    'multi_eject_busy_then_idle': ('59f1e392fbffb45f660d8c5dfe8efe4cb2e779d297838dd0b4f55b0211f2a654', 21),
    'multi_eject_times_out': ('17177cdffe85589a53702bc54d35e2cec403eb585c46adbbebe757ce87c1cea8', 515),
    'multi_plain_3600_frame1': ('bade2e018362ceeb7b30c63a24abd5c9339c6e777dbf58123089e843a1dae689', 10204),
}


def _record(name: str) -> tuple[str, int]:
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    if name in MULTI_READ:
        MULTI_READ[name](fake)
    else:
        with fast_time():
            SCENARIOS[name](fake)
    return _digest(fake.wire_log), len(fake.wire_log)


@pytest.mark.parametrize("name", sorted(SCENARIOS) + sorted(MULTI_READ))
def test_default_wire_sequence_matches_pre_change_recording(name):
    digest, n = _record(name)
    assert n > 0, name
    assert digest == _record(name)[0], f"{name}: recording is not deterministic"
    assert BASELINE[name] == (digest, n), (name, digest, n)


if __name__ == "__main__":
    for name in sorted(SCENARIOS) + sorted(MULTI_READ):
        print(f"    {name!r}: {_record(name)!r},")
