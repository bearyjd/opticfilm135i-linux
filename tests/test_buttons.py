#!/usr/bin/env python3
"""Offline tests for of135i.buttons, Scanner.poll_buttons, the `buttons`
and `watch` CLI commands -- fake io, no hardware.

Run with:
    .venv/bin/python tests/test_buttons.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from of135i import buttons, cli
from of135i.device import Scanner


def test_decode_tables():
    assert buttons.BUTTON_CODES == {0x48: "eject", 0x20: "positive",
                                    0x40: "negative", 0x28: "custom"}
    for code, name in buttons.BUTTON_CODES.items():
        assert buttons.decode_interrupt(code) == name
        assert buttons.decode_reg31(0xFE & ~code | 0x00) is not None
    assert buttons.decode_reg31(0xFE) is None
    assert buttons.decode_reg31(0xB6) == "eject"      # Test 97 values
    assert buttons.decode_reg31(0xDE) == "positive"
    assert buttons.decode_reg31(0xBE) == "negative"
    assert buttons.decode_reg31(0xD6) == "custom"
    assert buttons.decode_reg31(0xFF) is None
    assert buttons.decode_interrupt(0x04) == "unknown-0x04"
    assert buttons.decode_reg31(0x9E) == "unknown-0x60"
    print("test_decode_tables OK")


class FakeIo:
    """Scripted fake: per poll, a list of interrupt bytes and a reg 0x31
    value. Any write-capable method records a call."""

    def __init__(self, polls, magazine=True):
        self.polls = list(polls)
        self.cur = (0xFE, [])
        self.write_calls = []
        self.magazine = magazine
        self.closed = False

    def drain_events(self, max_events=8, timeout_ms=50):
        if self.polls:
            self.cur = self.polls.pop(0)
        else:
            raise KeyboardInterrupt
        return list(self.cur[1])

    def read_reg(self, reg, strict=False):
        assert reg == 0x31, reg
        return self.cur[0]

    def read_ext_reg(self, reg):
        return 0x08 if self.magazine else 0x00

    def write_regs(self, *a, **k):
        self.write_calls.append("write_regs")

    def end_access(self, *a, **k):
        self.write_calls.append("end_access")

    def buf_write(self, *a, **k):
        self.write_calls.append("buf_write")

    def close(self):
        self.closed = True


def test_poll_buttons():
    io = FakeIo([(0xFE, []), (0xB6, [0x48]), (0xDE, []), (0xFE, [0x20, 0x20]),
                 (0xBE, [0x40, 0x04])])
    sc = Scanner(io)
    assert sc.poll_buttons() == []
    assert sc.poll_buttons() == ["eject"]            # interrupt + held, dedup
    assert sc.poll_buttons() == ["positive"]         # reg 0x31 only (cold)
    assert sc.poll_buttons() == ["positive"]         # interrupt only, dedup
    assert sc.poll_buttons() == ["negative", "unknown-0x04"]
    assert io.write_calls == [], io.write_calls
    print("test_poll_buttons OK")


class OverflowIo(FakeIo):
    def drain_events(self, max_events=8, timeout_ms=50):
        self.cur = (0xD6, [])
        return "overflow"


def test_poll_overflow_falls_back_to_reg31():
    assert Scanner(OverflowIo([])).poll_buttons() == ["custom"]
    from of135i.usbio import InterruptOverflowError
    try:
        Scanner(OverflowIo([])).poll_buttons(strict_interrupts=True)
        raise AssertionError("expected InterruptOverflowError")
    except InterruptOverflowError:
        pass
    print("test_poll_overflow_falls_back_to_reg31 OK")


class Args:
    duration = None


def _run_buttons(io, capsys_lines):
    real_open = cli.UsbIo.open
    cli.UsbIo.open = classmethod(lambda cls, readonly=False: (
        capsys_lines.append(readonly) or io))
    try:
        return cli._cmd_buttons(Args())
    finally:
        cli.UsbIo.open = real_open


def test_cli_buttons_readonly():
    io = FakeIo([(0xFE, []), (0xB6, [0x48]), (0xB6, []), (0xFE, [])])
    seen = []
    rc = _run_buttons(io, seen)
    assert rc == 0
    assert seen == [True], "must open the device read-only"
    assert io.write_calls == []
    assert io.closed
    print("test_cli_buttons_readonly OK")


def _run_watch(io, monkey_start=True):
    ejects = []

    class W(Scanner):
        def check_start_state(self):
            return None

        def eject(self):
            ejects.append(1)

    real = cli.Scanner.open
    cli.Scanner.open = classmethod(lambda cls: W(io))
    try:
        rc = cli._cmd_watch(Args())
    finally:
        cli.Scanner.open = real
    return rc, ejects


def test_watch_ejects_only_for_eject_with_magazine():
    # Eject held across three polls (interrupt then reg 0x31): ONE eject.
    io = FakeIo([(0xFE, []), (0xB6, [0x48]), (0xB6, []), (0xB6, []), (0xFE, [])])
    rc, ejects = _run_watch(io)
    assert rc == 0 and ejects == [1], (rc, ejects)
    # No magazine: no eject.
    io = FakeIo([(0xB6, [0x48]), (0xFE, [])], magazine=False)
    rc, ejects = _run_watch(io)
    assert rc == 0 and ejects == []
    # Other buttons and unknown interrupts never eject.
    io = FakeIo([(0xDE, [0x20]), (0xFE, []), (0xBE, [0x40]), (0xFE, []),
                 (0xD6, [0x28]), (0xFE, [0x04]), (0xFE, [])])
    rc, ejects = _run_watch(io)
    assert rc == 0 and ejects == [] and io.write_calls == []
    # Two separate eject presses: two ejects.
    io = FakeIo([(0xB6, [0x48]), (0xFE, []), (0xB6, [0x48]), (0xFE, [])])
    rc, ejects = _run_watch(io)
    assert ejects == [1, 1]
    print("test_watch_ejects_only_for_eject_with_magazine OK")


def main() -> int:
    tests = [test_decode_tables, test_poll_buttons,
             test_poll_overflow_falls_back_to_reg31,
             test_cli_buttons_readonly,
             test_watch_ejects_only_for_eject_with_magazine]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
