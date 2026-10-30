#!/usr/bin/env python3
"""Replay a recorded capture's device side into one driver operation, offline.

The driver talks to a FakeVendorDevice that answers its reads with the
replies in a decoded capture; what the driver sends is diffed against what
the recorded host sent. No real USB device is ever opened (a guard makes
any attempt raise). See docs/replay-harness.md for usage and limits.

Usage:
  python3 -I tools/replay_harness.py CAPTURE.jsonl --op scan --dpi 600 --out OUTDIR

OUTDIR must not exist. It receives: divergences.jsonl, diff.txt, and (with
the recorder) trace.jsonl in of135i-trace/1 format plus compiled.trace.json.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from contextlib import contextmanager
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from of135i import safety  # noqa: E402
from of135i.device import Scanner  # noqa: E402
from of135i.usbio import UsbIo  # noqa: E402
from replay import diffing  # noqa: E402
from replay.capture import load_capture  # noqa: E402
from replay.fakedev import FakeVendorDevice, VirtualClock, no_real_usb, require_fake  # noqa: E402


def _op_initialize(s: Scanner, dpi: int) -> None:
    s.initialize(dpi=dpi)


def _op_scan(s: Scanner, dpi: int) -> None:
    """initialize + scan: the calibration phases run first. The load-flow
    precondition is waived (the replay is not a physical session)."""
    s.initialize(dpi=dpi)
    s._loaded_this_session = True
    s.scan(frame=1, dpi=dpi)


OPERATIONS = {
    "cold_init": lambda s, dpi: s.cold_init(),
    "initialize": _op_initialize,
    "vendor_startup": lambda s, dpi: s.vendor_startup(),
    "load_magazine": lambda s, dpi: s.load_magazine(),
    "scan": _op_scan,
}


#: Halts that are the documented outcome of a measurement check: the replay
#: serves zeros for image data, so a step that inspects a measurement fails.
#: Anything else that stops a run is unexpected and makes the CLI exit nonzero.
EXPECTED_HALT_TYPES = frozenset({"LampWarmupError", "CalibrationError"})


def halt_is_expected(halt_type: str | None) -> bool:
    return halt_type in EXPECTED_HALT_TYPES


@contextmanager
def _virtual_time():
    clock = VirtualClock()
    saved = (time.sleep, time.monotonic, time.perf_counter)
    time.sleep, time.monotonic, time.perf_counter = clock.sleep, clock.now, clock.now
    try:
        yield
    finally:
        time.sleep, time.monotonic, time.perf_counter = saved


@contextmanager
def _env(name: str, value: str | None):
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    try:
        yield
    finally:
        if old is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = old


def run(recorded: list[dict], op, dpi: int = 3600, window: int = 256,
        start_reg01: int = safety.REG01_IDLE,
        trace_path: str | None = None, device: FakeVendorDevice | None = None) -> dict:
    """Drive operation ``op`` (a name from OPERATIONS or a callable
    ``(scanner, dpi)``) against the recorded replies. Never raises for a
    driver error: the first exception ends the run and is reported in
    ``halted`` (the transfers up to it are still returned)."""
    if not callable(op) and op not in OPERATIONS:
        raise ValueError(f"unknown op {op!r}; choose from {sorted(OPERATIONS)}")
    action = op if callable(op) else OPERATIONS[op]
    idle = {(0x8E, 0x0122): bytes([start_reg01, 0x55])}
    fake = require_fake(device if device is not None
                        else FakeVendorDevice(recorded, idle=idle, window=window))
    halted = halt_type = halt_message = None
    logger = logging.getLogger("of135i")
    level = logger.level
    logger.setLevel(logging.ERROR)
    with no_real_usb(), _virtual_time(), _env("OF135I_TRACE", trace_path):
        try:
            with Scanner(UsbIo(fake)) as s:
                _check_device(s, fake)
                action(s, dpi)
                _check_device(s, fake)
        except BaseException as e:      # reported, not hidden; the replay ends here
            if isinstance(e, KeyboardInterrupt):
                raise
            halt_type, halt_message = type(e).__name__, str(e)[:300]
            halted = f"{halt_type}: {halt_message}"
        finally:
            logger.setLevel(level)
    return {"log": fake.log, "divergences": fake.divergences, "halted": halted,
            "halt_type": halt_type, "halt_message": halt_message,
            "diff": diffing.diff(recorded, fake.log)}


def _check_device(s: Scanner, fake: FakeVendorDevice) -> None:
    """The driver must hold exactly our fake. If something else was swapped in,
    put the fake back first (so close() never disposes a foreign device), then
    refuse."""
    held = s.io.dev._GuardedDevice__raw
    if held is not fake:
        object.__setattr__(s.io.dev, "_GuardedDevice__raw", fake)
        require_fake(held)
        raise TypeError("replay harness: the driver's device is not this run's fake")


def _counts(divergences: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in divergences:
        counts[d["what"]] = counts.get(d["what"], 0) + 1
    return counts


def _summary(res: dict) -> str:
    counts = _counts(res["divergences"])
    return (f"driver transfers: {len(res['log'])}; divergences: {counts or 'none'}; "
            f"halted: {res['halted'] or 'no'}")


def _write_new(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("capture")
    ap.add_argument("--op", choices=sorted(OPERATIONS), default="scan")
    ap.add_argument("--dpi", type=int, default=3600)
    ap.add_argument("--window", type=int, default=256,
                    help="write look-ahead, in recorded OUT events")
    ap.add_argument("--start-reg01", type=lambda x: int(x, 0), default=safety.REG01_IDLE,
                    help="value served for the start-state read when the capture has none")
    ap.add_argument("--out", required=True, help="new output directory")
    args = ap.parse_args(argv[1:])
    recorded = load_capture(args.capture)        # ValueError names a malformed row
    out = Path(args.out)
    out.mkdir(parents=True)
    trace = out / "trace.jsonl"
    res = run(recorded, args.op, args.dpi, args.window, args.start_reg01, str(trace))
    if res["halted"]:
        res["diff"].append(f"# HALTED {res['halt_type']}: {res['halt_message']}")
    _write_new(out / "divergences.jsonl",
               "".join(json.dumps(d) + "\n" for d in res["divergences"]))
    _write_new(out / "diff.txt", "\n".join(res["diff"]) + "\n")
    if trace.exists():
        import trace_to_compiled as ttc
        ops, _ = ttc.convert(ttc.read_records(trace.read_text()))
        _write_new(out / "compiled.trace.json", json.dumps(ops))
    expected = res["halted"] is None or halt_is_expected(res["halt_type"])
    _write_new(out / "summary.json", json.dumps({
        "halted": res["halted"] is not None, "halt_type": res["halt_type"],
        "halt_message": res["halt_message"], "expected_halt": bool(res["halted"]) and expected,
        "transfers": len(res["log"]), "divergences": _counts(res["divergences"]),
    }, indent=1) + "\n")
    print(_summary(res))  # noqa: T201
    print(res["diff"][-1])  # noqa: T201
    return 0 if expected else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
