"""Opt-in transfer recorder (diagnostic, pure observer).

When ``OF135I_TRACE=<path>`` is set, every control and bulk transfer that
passes through ``safety.GuardedDevice`` is appended to ``<path>`` as one
JSON line, so a native run can be compared offline with another capture
(``tools/trace_to_compiled.py`` converts the file to the compiled trace
format the offline diff tools load).

Contract:

- Default (variable unset or empty): ``from_env()`` returns ``None`` and
  the transfer path runs the bare call -- no file, no clock read, no
  extra transfer.
- Observer only: the recorder reads what the transfer already produced.
  It never issues, delays, retries or alters a transfer. It uses
  ``time.perf_counter`` (not ``time.monotonic``/``time.sleep``, which the
  driver's poll loops use for their deadlines).
- Failures are never fatal: any error opening, encoding or writing logs
  ONE warning, disables recording and lets scanner I/O continue.
- The file is created exclusively (``O_EXCL``: an existing file or a
  symlink is refused, never overwritten) with mode 0600, and every record
  is one ``os.write`` of a complete line, so an interrupted run leaves a
  valid prefix.
- Data: control transfers (both directions) and bulk-OUT keep the first
  ``DATA_CAP`` bytes plus the full length; bulk-IN (image data) and the
  interrupt endpoint record only the length.
"""

from __future__ import annotations

import json
import logging
import os
import time

log = logging.getLogger("of135i")

TRACE_ENV = "OF135I_TRACE"
DATA_CAP = 64
FORMAT = "of135i-trace/1"


class TraceRecorder:
    """Appends one JSON line per transfer to an exclusively created file."""

    def __init__(self, path: str):
        self._fd: int | None = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        self._seq = 0
        self._t0 = time.perf_counter()
        self._emit({"format": FORMAT})

    @property
    def active(self) -> bool:
        return self._fd is not None

    def run(self, kind, direction, ep, setup, out_data, call):
        """Perform ``call()`` and record it; the transfer's result or
        exception passes through untouched. ``setup`` is (bm, br, wv, wi,
        wlen) for control transfers, else None; ``out_data`` the bytes
        sent for an OUT transfer."""
        if self._fd is None:
            return call()
        t0 = time.perf_counter()
        try:
            result = call()
        except BaseException as e:
            self._record(kind, direction, ep, setup, out_data, None, t0,
                         f"error:{type(e).__name__}")
            raise
        status = "ok"
        if direction == "out" and out_data is not None:
            try:
                if int(result) != len(out_data):
                    status = f"short:{int(result)}"
            except (TypeError, ValueError):
                status = "short:?"
        self._record(kind, direction, ep, setup, out_data, result, t0, status)
        return result

    def _record(self, kind, direction, ep, setup, out_data, result, t0, status):
        try:
            dur = time.perf_counter() - t0
            rec = {"seq": self._seq + 1, "t": round(t0 - self._t0, 6),
                   "dur": round(dur, 6), "kind": kind, "dir": direction,
                   "status": status}
            if ep is not None:
                rec["ep"] = ep
            if setup is not None:
                rec["bm"], rec["br"], rec["wv"], rec["wi"], rec["wlen"] = setup
            payload = b""
            if direction == "out":
                payload = bytes(out_data) if out_data is not None else b""
                rec["len"] = len(payload)
            elif result is not None:
                payload = bytes(result)
                rec["len"] = len(payload)
            if kind == "ctrl" or (kind == "bulk" and direction == "out"):
                rec["data"] = payload[:DATA_CAP].hex()
            self._emit(rec)
        except Exception as e:      # recording must never reach scanner I/O
            self._disable(e)

    def _emit(self, rec: dict) -> None:
        line = (json.dumps(rec, separators=(",", ":")) + "\n").encode()
        fd = self._fd
        if fd is None:
            return
        try:
            view = memoryview(line)
            while view:
                view = view[os.write(fd, view):]
        except Exception as e:
            self._disable(e)
            return
        self._seq = rec.get("seq", self._seq)

    def _disable(self, err: BaseException) -> None:
        fd, self._fd = self._fd, None
        log.warning("transfer trace disabled (%s: %s); scanning continues unrecorded",
                    type(err).__name__, err)
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    def close(self) -> None:
        fd, self._fd = self._fd, None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass


def from_env() -> "TraceRecorder | None":
    """A recorder when ``OF135I_TRACE`` names a path, else None. A path
    that cannot be created exclusively logs one warning and gives None."""
    path = os.environ.get(TRACE_ENV)
    if not path:
        return None
    try:
        rec = TraceRecorder(path)
    except Exception as e:
        log.warning("transfer trace not started (%s: %s); scanning continues unrecorded",
                    type(e).__name__, e)
        return None
    return rec if rec.active else None
