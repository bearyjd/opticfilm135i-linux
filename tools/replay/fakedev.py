"""A pyusb-device stand-in that answers the driver from recorded replies.

Matching (see docs/replay-harness.md for the rationale):

* A cursor walks the recorded events. A driver control READ is answered by
  the next recorded reply with the same (bmRequestType, bRequest, wValue,
  wIndex) at or after the cursor, searching no further than the next
  recorded state-changing event (control OUT / bulk OUT). The cursor moves
  past the reply used.
* A driver control OUT or bulk OUT is matched against the next recorded
  event of the same shape skipping at most ``window`` recorded OUT events: an exact data match is
  silent; the same request with other data logs ``write_data_differs``;
  no match logs ``write_unmatched``. The cursor moves past a match.
* A read with no recorded reply (the vendor never asked, or only asked
  earlier / after a boundary) gets the LAST value served for that request
  (else an idle value) and logs a ``divergence`` event.
* Bulk IN (image data) is never invented: zeros of the requested size.

Every transfer the driver makes is appended to ``log`` as a neutral event
(full payloads), so the diff does not depend on the recorder's 64-byte cap.
"""
from __future__ import annotations

from contextlib import contextmanager

import usb.core

from .capture import bulk_out_event, ctrl_event

REG_READ_WVALUES = (0x008E, 0x018E)
ACK = 0x55                      # second byte of a 2-byte register-read reply


class VirtualClock:
    """Replaces time.sleep / monotonic / perf_counter so the driver's poll
    loops run to their deadlines instantly. sleep() advances virtual time."""

    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += max(0.0, float(s))

    def now(self):
        self.t += 0.001
        return self.t


class FakeVendorDevice:
    """Duck-typed ``usb.core.Device`` for ``UsbIo`` / ``GuardedDevice``."""

    class _Ctx:
        def dispose(self, device):
            device.disposed = True

    idVendor, idProduct, bus, address = 0x07B3, 0x1436, 0, 0

    def __init__(self, recorded: list[dict], idle: dict | None = None, window: int = 256):
        self._ctx = self._Ctx()
        self.disposed = False
        self.recorded = recorded
        self.window = window
        self.cursor = 0
        self.idle = dict(idle or {})        # (wv, wi) -> bytes served when nothing is known
        self.last: dict[tuple, bytes] = {}  # request key -> last reply served
        self.log: list[dict] = []           # what the driver did
        self.divergences: list[dict] = []

    # ------------------------------------------------------------ helpers
    def _diverge(self, what: str, **detail) -> None:
        self.divergences.append({"seq": len(self.log), "what": what, **detail})

    @staticmethod
    def _key(bm, br, wv, wi) -> tuple:
        return (bm, br, wv, wi)

    def _is_boundary(self, ev: dict) -> bool:
        return ev["dir"] == "out"

    def _find_reply(self, pred):
        i = self.cursor
        while i < len(self.recorded):
            ev = self.recorded[i]
            if pred(ev):
                return i
            if self._is_boundary(ev):
                return None
            i += 1
        return None

    def _find_write(self, pred):
        outs = 0
        for i in range(self.cursor, len(self.recorded)):
            ev = self.recorded[i]
            if pred(ev):
                return i
            if self._is_boundary(ev):
                outs += 1
                if outs >= self.window:
                    break
        return None

    # ---------------------------------------------------------- transfers
    def ctrl_transfer(self, bm, br, wv=0, wi=0, data_or_wlen=None, timeout=None):
        if bm & 0x80:
            return self._ctrl_in(bm, br, wv, wi, int(data_or_wlen or 0))
        data = bytes(data_or_wlen) if data_or_wlen is not None else b""
        ev = ctrl_event(bm, br, wv, wi, len(data), data)
        self.log.append(ev)
        key = self._key(bm, br, wv, wi)
        def same(e):
            return (e["kind"] == "ctrl" and e["dir"] == "out"
                    and self._key(e["bm"], e["br"], e["wv"], e["wi"]) == key)

        exact = self._find_write(lambda e: same(e) and e["data"] == data)
        if exact is not None:
            self.cursor = exact + 1
            return len(data)
        loose = self._find_write(same)
        if loose is not None:
            self.cursor = loose + 1
            self._diverge("write_data_differs", req=list(key), len=len(data))
        else:
            self._diverge("write_unmatched", req=list(key), len=len(data))
        return len(data)

    def _ctrl_in(self, bm, br, wv, wi, wl):
        key = self._key(bm, br, wv, wi)
        i = self._find_reply(lambda e: e["kind"] == "ctrl" and e["dir"] == "in"
                             and self._key(e["bm"], e["br"], e["wv"], e["wi"]) == key)
        if i is not None:
            self.cursor = i + 1
            reply = self.recorded[i]["data"][:wl] if wl else self.recorded[i]["data"]
        else:
            reply = self.last.get(key) or self.idle.get((wv, wi)) or self._default(wv, wl)
            self._diverge("read_unanswered", req=list(key), served="last/idle")
        self.last[key] = reply
        self.log.append(ctrl_event(bm, br, wv, wi, wl, reply))
        return reply

    @staticmethod
    def _default(wv: int, wl: int) -> bytes:
        if wv in REG_READ_WVALUES and wl == 2:
            return bytes([0, ACK])
        return bytes(wl)

    def write(self, endpoint, data, timeout=None):
        data = bytes(data)
        ev = bulk_out_event(data)
        self.log.append(ev)
        i = self._find_write(lambda e: e["kind"] == "bulk" and e["dir"] == "out")
        if i is None:
            self._diverge("bulk_out_unmatched", len=len(data))
        else:
            self.cursor = i + 1
            rec = self.recorded[i]
            if (rec["len"], rec["sha"]) != (ev["len"], ev["sha"]):
                self._diverge("bulk_out_differs", len=len(data), recorded_len=rec["len"])
        return len(data)

    def read(self, endpoint, size, timeout=None):
        if endpoint == 0x83:
            i = self._find_reply(lambda e: e["kind"] == "int")
            if i is None:
                raise usb.core.USBTimeoutError("no recorded interrupt event", errno=110)
            self.cursor = i + 1
            return self.recorded[i]["data"][:int(size)]
        n = int(size)
        self.log.append({"kind": "bulk", "dir": "in", "len": n})
        self._diverge("bulk_in_zeros", len=n)
        return bytes(n)

    # state-changing pyusb calls are blocked by GuardedDevice before reaching here
    def is_kernel_driver_active(self, *_):
        raise AssertionError("replay: no real device is opened")


@contextmanager
def no_real_usb():
    """Hard guard: any attempt to find or open a real device raises."""
    def refuse(*a, **k):
        raise AssertionError("replay harness: opening a real USB device is forbidden")

    import usb.backend.libusb1 as libusb1
    import usb.core as core
    saved = (core.find, core.Device.__init__, libusb1.get_backend)
    core.find, core.Device.__init__, libusb1.get_backend = refuse, refuse, refuse
    try:
        yield
    finally:
        core.find, core.Device.__init__, libusb1.get_backend = saved
