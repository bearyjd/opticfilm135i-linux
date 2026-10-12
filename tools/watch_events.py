#!/usr/bin/env python3
"""Read-only observer for button / magazine events (SA-002).

Purpose: check the hypothesis in docs/static-analysis.md (SA-002) that the
bare 0x48 interrupt on EP 0x83 is told apart by register state. Every
--interval seconds it reads WATCH_REGS (control-IN register reads only)
and the interrupt endpoint EP 0x83 (short timeout), and prints a line only
when an interrupt arrives or a watched value changes. Reg 0x101 is decoded
as latched / present / motor bits.

STRICTLY READ-ONLY: the session is opened readonly (the safety layer
refuses every OUT transfer), takes the normal process lock and refuses if
it is busy. It writes no register and sends no motor command. It does NOT
eject or load anything: the vendor application commands those; with this
tool nothing moves.

Operator procedure: see docs/static-analysis.md SA-002.

Usage:
    .venv/bin/python tools/watch_events.py [--interval 0.1] [--duration 120]
                                           [--log FILE]
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from of135i import vendor_derived as vd
from of135i.usbio import InterruptOverflowError, UsbIo


def decode101(v: int) -> str:
    parts = [
        "latched" if v & vd.REG101_LATCHED_BIT else "loose",
        "present" if v & vd.REG101_PRESENT_BIT else "absent",
        "motor" if v & vd.REG101_MOTOR_BIT else "idle",
    ]
    return "/".join(parts)


def _name(reg: int) -> str:
    return f"0x{reg:03x}" if reg > 0xFF else f"0x{reg:02x}"


def read_watch(io, reg: int) -> int:
    return io.read_ext_reg(reg) if reg > 0xFF else io.read_reg(reg)


def run(io, interval: float, duration: float, emit, clock=time.monotonic,
        sleep=time.sleep) -> None:
    t0 = clock()

    def stamp() -> str:
        return f"{clock() - t0:8.3f}s"

    last = {r: read_watch(io, r) for r in vd.WATCH_REGS}
    emit(f"{stamp()} snapshot " + " ".join(
        f"{_name(r)}=0x{v:02x}" for r, v in last.items())
        + f"  reg0x101: {decode101(last[0x101])}")
    while clock() - t0 < duration:
        try:
            ev = io.read_button(timeout_ms=50)
        except InterruptOverflowError:
            ev = None
            emit(f"{stamp()} interrupt EP 0x83: overflow (unreadable)")
        cur = {r: read_watch(io, r) for r in vd.WATCH_REGS}
        if ev is not None:
            emit(f"{stamp()} INTERRUPT 0x{ev:02x}  state: " + " ".join(
                f"{_name(r)}=0x{v:02x}" for r, v in cur.items())
                + f"  reg0x101: {decode101(cur[0x101])}")
        for r, v in cur.items():
            if v != last[r]:
                extra = ""
                if r == 0x101:
                    extra = f"  ({decode101(last[r])} -> {decode101(v)})"
                elif r == 0x31:
                    gone = last[r] & ~v & vd.REG31_INSERT_MARK_MASK
                    if gone:
                        extra = f"  (insert-mark bits cleared: 0x{gone:02x})"
                emit(f"{stamp()} change {_name(r)}: "
                     f"0x{last[r]:02x} -> 0x{v:02x}{extra}")
        last = cur
        sleep(interval)


def main(argv=None, opener=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--interval", type=float, default=0.1)
    ap.add_argument("--duration", type=float, default=120.0)
    ap.add_argument("--log", default=None, help="append lines to FILE")
    args = ap.parse_args(argv)

    logf = open(args.log, "a") if args.log else None

    def emit(line: str) -> None:
        print(line, flush=True)
        if logf:
            logf.write(line + "\n")
            logf.flush()

    opener = opener or (lambda: UsbIo.open(readonly=True))
    try:
        io = opener()
        try:
            run(io, args.interval, args.duration, emit)
        finally:
            io.close()
    except KeyboardInterrupt:
        emit("interrupted, exiting")
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    finally:
        if logf:
            logf.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
