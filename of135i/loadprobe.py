"""Read-only register snapshots for `of135i load --probe DIR`, and the
offline `probe-diff` comparison.

HARD RULE: ``collect_snapshot`` performs ONLY the control READS the
``doctor`` command performs (the status word, reg 0x01, the register
read helper ``diag._collect_regs`` and the loader-sensor bit), through
the session's own ``UsbIo`` and therefore through the safety layer's
``GuardedDevice``. It writes nothing, drains nothing from EP 0x83 (the
events the load flow already read are passed in and copied), and never
raises for a failed read: the error is recorded in the snapshot.
``probe_diff`` / ``written_registers`` are pure offline code: they open
no device.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from . import diag

SCHEMA = "of135i-load-probe/1"

#: Every register 0x00-0x120, plain reads below 0x100, extended above.
PROBE_REGS = tuple(range(0x121))


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def collect_snapshot(io, label: str, events) -> dict:
    """One snapshot. Each item is read independently; a failing read is
    recorded as {"error": ...} and the others still run. ``events`` is
    what the load flow itself already drained from EP 0x83 (copied)."""
    snap: dict = {
        "schema": SCHEMA,
        "label": label,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "interrupt_events_seen_by_flow": list(events) if isinstance(events, list) else events,
    }

    def guarded(key, fn):
        try:
            snap[key] = fn()
        except Exception as e:
            snap[key] = {"error": str(e)}

    guarded("status_word", lambda: f"0x{io.read_status_word():04x}")
    guarded("reg01", lambda: f"0x{io.read_reg(0x01):02x}")
    guarded("regs", lambda: diag._collect_regs(io, PROBE_REGS))
    guarded("magazine_present", lambda: bool(io.read_ext_reg(0x101) & 0x08))
    return snap


def prepare_dir(path: str) -> str:
    """Create ``path`` with mode 0700 (an existing directory is reused
    untouched). Raises OSError."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.mkdir(p, 0o700)
    except FileExistsError:
        if not p.is_dir():
            raise
    return str(p)


def write_snapshot(directory: str, label: str, snap: dict) -> str:
    """Write ``snap`` as a new 0600 file named by timestamp and label.
    Never overwrites: a name collision gets a numeric suffix."""
    stem = f"probe-{_stamp()}-{label}"
    for n in range(1000):
        path = os.path.join(directory, f"{stem}{'' if n == 0 else f'-{n}'}.json")
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        with os.fdopen(fd, "w") as f:
            json.dump(snap, f, indent=2, sort_keys=True, default=diag._json_default)
        return path
    raise FileExistsError(f"no free probe file name for {stem}")


# --------------------------------------------------------- offline compare


def written_registers() -> frozenset[int]:
    """Registers the driver's own write tables set: the (reg, value)
    pairs of every register-batch op (wValue 0x83) in every phase table
    module, and the *_PAIRS lists of tables_base. Inline writes in
    device.py are not included."""
    import importlib
    from . import tables_base
    from .tables import Phase

    regs: set[int] = set()
    for name in ("tables", "tables_ir", "tables_load", "tables_startup",
                 "tables_dpi600", "tables_dpi1200", "tables_dpi2400", "tables_dpi7200"):
        mod = importlib.import_module(f".{name}", __package__)
        for phase in vars(mod).values():
            if not isinstance(phase, Phase):
                continue
            for op in phase.ops:
                if op.kind == "cw" and op.wv == 0x0083 and op.data:
                    regs.update(op.data[0::2])
    for attr, pairs in vars(tables_base).items():
        if attr.endswith("_PAIRS"):
            regs.update(reg for reg, _ in pairs)
    return frozenset(regs)


def _reg_value(v) -> str:
    if isinstance(v, dict):
        return f"<error: {v.get('error', '?')}>"
    return "<absent>" if v is None else f"0x{v:02x}"


def probe_diff(a: dict, b: dict, ignore_written: bool = False) -> list[str]:
    """Lines describing what differs between two snapshots."""
    lines = []
    for key in ("status_word", "reg01", "magazine_present"):
        if a.get(key) != b.get(key):
            lines.append(f"{key}: {a.get(key)} -> {b.get(key)}")
    ra, rb = a.get("regs") or {}, b.get("regs") or {}
    written = written_registers() if ignore_written else frozenset()
    for key in sorted(set(ra) | set(rb), key=lambda k: int(k, 16)):
        if ra.get(key) == rb.get(key) or int(key, 16) in written:
            continue
        lines.append(f"reg {key}: {_reg_value(ra.get(key))} -> {_reg_value(rb.get(key))}")
    return lines
