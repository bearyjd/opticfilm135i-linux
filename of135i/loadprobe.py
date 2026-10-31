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
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

from . import diag

SCHEMA = "of135i-load-probe/1"

#: Exactly the registers `doctor` reads (diag.known_read_regs(): 0x01-0x120,
#: every one the vendor driver is observed reading), so each probe read has
#: doctor precedent on hardware. Not a copy: derived by the same function.
PROBE_REGS = diag.known_read_regs()

#: A snapshot stops its register loop after this many failed reads in a row,
#: or once SNAPSHOT_BUDGET_S seconds have passed since it started, so stalled
#: reads cannot hold up the operator's load for minutes. The partial data
#: and the reason ("aborted: ...") are recorded in the snapshot; the load goes
#: on unchanged. (The status word and reg 0x01 are read first, one read each.)
MAX_CONSECUTIVE_READ_ERRORS = 3
SNAPSHOT_BUDGET_S = 2.0
_clock = time.perf_counter


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

    started = _clock()
    aborted: list[str] = []
    guarded("status_word", lambda: f"0x{io.read_status_word():04x}")
    guarded("reg01", lambda: f"0x{io.read_reg(0x01):02x}")
    guarded("regs", lambda: diag._collect_regs(
        io, PROBE_REGS, max_consecutive_errors=MAX_CONSECUTIVE_READ_ERRORS,
        deadline=started + SNAPSHOT_BUDGET_S, clock=_clock, aborted=aborted))
    if aborted:
        snap["regs_aborted"] = aborted[0]
        snap["magazine_present"] = {"error": "skipped: " + aborted[0]}
    else:
        guarded("magazine_present", lambda: bool(io.read_ext_reg(0x101) & 0x08))
    return snap


def prepare_dir(path: str) -> str:
    """Create ``path`` with mode 0700 (parents this call creates are 0700
    too). An existing directory is reused untouched only if it is a real
    directory (not a symlink) with no group/other access; otherwise this
    refuses rather than chmod it. Raises OSError."""
    p = Path(path)
    missing = []
    for anc in p.parent.parents:
        if anc.exists():
            break
        missing.append(anc)
    for anc in (*reversed(missing), p.parent):
        try:
            os.mkdir(anc, 0o700)       # makedirs' mode ignores intermediate dirs
        except FileExistsError:
            pass
    try:
        os.mkdir(p, 0o700)
        return str(p)
    except FileExistsError:
        pass
    st = os.lstat(p)
    if stat.S_ISLNK(st.st_mode):
        raise OSError(f"{p} is a symlink; give a real directory")
    if not stat.S_ISDIR(st.st_mode):
        raise OSError(f"{p} exists and is not a directory")
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise OSError(f"{p} exists with mode {stat.S_IMODE(st.st_mode):04o}, wider than "
                      "0700; chmod 700 it or choose another directory")
    return str(p)


def write_snapshot(directory: str, label: str, snap: dict) -> str:
    """Write ``snap`` as a new 0600 file named by timestamp and label.
    Written to a temp name in the same directory, then hard-linked to the
    final name (so an existing file is never replaced; a collision gets a
    numeric suffix) -- an interrupted write leaves no partial final file."""
    stem = f"probe-{_stamp()}-{label}"
    fd = -1
    for n in range(1000):
        tmp = os.path.join(directory, f".{stem}.{os.getpid()}.{n}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            continue
    if fd < 0:
        raise FileExistsError(f"no free temp name for {stem}")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(snap, f, indent=2, sort_keys=True, default=diag._json_default)
        for n in range(1000):
            final = os.path.join(directory, f"{stem}{'' if n == 0 else f'-{n}'}.json")
            try:
                os.link(tmp, final)
            except FileExistsError:
                continue
            return final
        raise FileExistsError(f"no free probe file name for {stem}")
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


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
    if isinstance(v, int) and not isinstance(v, bool):
        return f"0x{v:02x}"
    return "<absent>" if v is None else f"<{v!r}>"


def _regs_of(snap: dict, name: str) -> dict:
    regs = snap.get("regs") or {}
    if not isinstance(regs, dict):
        raise ValueError(f"{name}: 'regs' must be an object of register values")
    for key in regs:
        try:
            int(key, 16)
        except (TypeError, ValueError):
            raise ValueError(f"{name}: register key {key!r} is not hex like '0x10'") from None
    return regs


def probe_diff(a: dict, b: dict, ignore_written: bool = False) -> list[str]:
    """Lines describing what differs between two snapshots. Raises
    ValueError for a malformed ``regs`` section."""
    ra, rb = _regs_of(a, "A"), _regs_of(b, "B")
    lines = []
    for key in ("status_word", "reg01", "magazine_present"):
        if a.get(key) != b.get(key):
            lines.append(f"{key}: {a.get(key)} -> {b.get(key)}")
    written = written_registers() if ignore_written else frozenset()
    for key in sorted(set(ra) | set(rb), key=lambda k: int(k, 16)):
        if ra.get(key) == rb.get(key) or int(key, 16) in written:
            continue
        lines.append(f"reg {key}: {_reg_value(ra.get(key))} -> {_reg_value(rb.get(key))}")
    return lines
