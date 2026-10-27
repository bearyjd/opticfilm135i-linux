"""Wire-sequence regression: host-side diagnostics must not change traffic.

Each scenario drives a full driver flow against the offline FakeUsbDevice
and hashes the ordered host->device side of every transfer it saw
(``FakeUsbDevice.wire_log``: control writes with their data, control
reads with their length, bulk writes, bulk reads). The digests below were
recorded from the driver BEFORE the status-word decoder, the eject
interpretation text and the travel-margin diagnostic were added; those
changes are bookkeeping only, so with default settings every digest must
stay identical. A digest change means a transfer was added, removed,
reordered or altered -- that is a behaviour change, not a diagnostic.

Pure offline logic: no USB, no scanner.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

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


SCENARIOS = {
    "plain_3600_frame1": _plain_scan,
    "dual_ir_frame1": _dual_ir_scan,
    "slide4_1200": _slide4_scan,
    "eject_completes": _eject_completes,
    "eject_times_out": _eject_times_out,
    "eject_sensor_clear": _eject_sensor_clear,
    "cold_move_times_out": _cold_move_times_out,
}

#: Recorded from the unmodified driver (fork master 2da5158).
BASELINE = {
    'cold_move_times_out': ('6f464b5fdeae465e9c8cebd6ed0bdaa32a7810710f9c031e5b58faddcef580f7', 49),
    'dual_ir_frame1': ('521ac284f6538a1b4de1316f17014b3b2a18d599e6130927d2f2daaa08fe3500', 24164),
    'eject_completes': ('076cdba7eb651025b055d4e0e1cb0685e583e669127bec6dbe2032b718e0ab84', 16),
    'eject_sensor_clear': ('4b8b9196a550c8201b36667abe79c9d73ca24ecb4f29c167cbdda9d32579efab', 2),
    'eject_times_out': ('076cdba7eb651025b055d4e0e1cb0685e583e669127bec6dbe2032b718e0ab84', 16),
    'plain_3600_frame1': ('bdb210aa37a02eed88fcd9fdc5db34ffef504e2c84e0135fefc3837331659e06', 9789),
    'slide4_1200': ('4b8c1dc6b3accc93590c7036c1c0b0af81ac2cdcc290809e773cb5a9e260a0cb', 4737),
}


def _record(name: str) -> tuple[str, int]:
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    with fast_time():
        SCENARIOS[name](fake)
    return _digest(fake.wire_log), len(fake.wire_log)


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_default_wire_sequence_matches_pre_change_recording(name):
    digest, n = _record(name)
    assert n > 0, name
    assert digest == _record(name)[0], f"{name}: recording is not deterministic"
    assert BASELINE[name] == (digest, n), (name, digest, n)


if __name__ == "__main__":
    for name in sorted(SCENARIOS):
        print(f"    {name!r}: {_record(name)!r},")
