#!/usr/bin/env python3
"""Offline tests for tools/watch_events.py -- no hardware.

Run with:
    .venv/bin/python tests/test_watch_events.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tools"))

import watch_events as we


class FakeIo:
    """Read-only fake. Any write-capable method records a call."""

    def __init__(self, script, events, fail_at=None):
        self.script = script          # list of {reg: value} per round
        self.events = list(events)
        self.round = 0
        self.reads = 0
        self.closed = False
        self.write_calls = []
        self.fail_at = fail_at

    def _vals(self):
        return self.script[min(self.round, len(self.script) - 1)]

    def read_reg(self, reg):
        self.reads += 1
        return self._vals()[reg]

    def read_ext_reg(self, reg):
        self.reads += 1
        return self._vals()[reg]

    def read_button(self, timeout_ms=100):
        if self.fail_at is not None and self.round >= self.fail_at:
            raise RuntimeError("boom")
        ev = self.events.pop(0) if self.events else None
        self.round += 1
        return ev

    def write_regs(self, pairs):
        self.write_calls.append(("write_regs", pairs))

    def end_access(self, *a, **k):
        self.write_calls.append(("end_access", a))

    def buf_write(self, *a, **k):
        self.write_calls.append(("buf_write", a))

    def close(self):
        self.closed = True


BASE = {0x31: 0xFE, 0x32: 0x1D, 0x35: 0x00, 0x37: 0x00, 0x100: 0x00, 0x101: 0xD8}


def _script():
    ins = {**BASE, 0x31: 0xB6}
    lat = {**ins, 0x101: 0xF8}
    return [BASE, BASE, ins, lat, lat]


def _clock():
    t = [0.0]

    def clock():
        t[0] += 0.01
        return t[0]
    return clock


def test_changes_and_interrupt_printed_no_writes():
    io = FakeIo(_script(), [None, None, 0x48, None, None])
    lines = []
    we.run(io, 0.0, 0.5, lines.append, clock=_clock(), sleep=lambda s: None)
    text = "\n".join(lines)
    assert "snapshot" in lines[0]
    assert "INTERRUPT 0x48" in text, text
    assert "change 0x31: 0xfe -> 0xb6" in text, text
    assert "change 0x101: 0xd8 -> 0xf8" in text and "latched" in text, text
    assert io.write_calls == [], io.write_calls
    print("test_changes_and_interrupt_printed_no_writes OK")


def test_lock_released_after_exception():
    io = FakeIo(_script(), [], fail_at=1)
    rc = we.main(["--duration", "5", "--interval", "0"], opener=lambda: io)
    assert rc == 1
    assert io.closed, "device/lock must be released after an exception"
    io2 = FakeIo(_script(), [])
    assert we.main(["--duration", "0.0", "--interval", "0"], opener=lambda: io2) == 0
    assert io2.closed
    print("test_lock_released_after_exception OK")


def main() -> int:
    tests = [test_changes_and_interrupt_printed_no_writes,
             test_lock_released_after_exception]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
