#!/usr/bin/env python3
"""Turn a usbmon pcap (or a vendor trace.json.gz) into a GL126 reply script.

The reply script is what tests/gl126_magazine_probe.cpp's "replay" command
answers the backend's control-IN transfers from, so the magazine load can
be iterated against a recording of a real unit with no scanner attached
(tests/test_sane_replay.py). It is plain text, one transfer per line, in
the device's submit order, so two recordings diff line by line.

Format (version 1):

    # ...                                   comment / header
    GAP <ms>                                the device was idle this long
                                            (previous completion to next
                                            submit) before the next entry;
                                            only written for >= 50 ms
    <idx> CI <bm> <req> <wValue> <wIndex> <wLength> <reply> [xN [every=G]] [st=S]
    <idx> CO <bm> <req> <wValue> <wIndex> <wLength> <data>  [xN [every=G]] [st=S]
    <idx> BI|BO|II|IO <ep> <length> <data>                   [xN [every=G]] [st=S]

  idx      position of the transfer among the device's transfers (0-based,
           submit order); a run "xN" covers idx .. idx+N-1
  CI/CO    control IN / OUT; BI/BO bulk, II/IO interrupt
  bm, req  bmRequestType and bRequest, 2 hex digits
  wValue, wIndex  4 hex digits; wLength decimal
  reply    for a control IN, the bytes the device returned (completion
           data); for an OUT, the bytes the host sent (submit data);
           unbroken hex, "-" when empty. A control IN reply carries every
           byte, the 0x55 link-status byte of a GL126 register read
           included. A bulk IN (the scan's image data) NEVER carries data:
           it is always "-", only its requested length is kept. A bulk
           OUT (the tables written to the scanner, the shading tables
           computed from the calibration frames among them) is always
           "~": its bytes are not stored either, only its sent length; a
           replay matches a bulk OUT by length and position alone.
  ep       endpoint (hex, direction bit included); "--" when unknown
           (vendor traces do not record it)
  length   bulk/interrupt: the requested length (IN) or sent length (OUT)
  xN       the same transfer, byte for byte, N times in a row (a poll);
           never written for N = 1. Back to back unless every=G: then
           paced, about G ms idle before each repeat (G is the first
           repeat's gap, rounded; the others are within PACE_JITTER_MS of
           it, a change of pace starts a new entry)
  st=S     usbmon status when non-zero (e.g. -2: an interrupt listen the
           host cancelled)

Timestamps are usbmon's kernel submit/completion times. Only long idle
gaps are kept: the host's sleeps, not transfer latency.

Vendor traces (tools/compile_trace.py output) are converted too, with one
loss: a "poll" entry records only its read count and the DISTINCT replies,
not the per-read order, so it becomes each distinct reply once, the last
one repeated to fill the count, after a "# lossy poll" comment.

Usage:
    python3 tests/pcap_to_replay.py <in.pcap|in.trace.json.gz> [device] [--session N] > out.txt

`device` is the usbmon device number to keep (default: the device with
the most transfers). A capture of several runs (one process each, the
host idle for SESSION_GAP_MS or more between them) is cut by --session N
(0-based) into that run's transfers alone, keeping the capture's indices;
a run of fewer than SESSION_MIN_TRANSFERS transfers (the kernel's
enumeration) belongs to the run after it. stdlib only.
"""

from __future__ import annotations

import gzip
import json
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

GAP_MIN_MS = 50
# A paced poll's gaps wobble by a ms or so (100.35 ms mean in R1's edge
# wait); within this they are one pace.
PACE_JITTER_MS = 2
# Sessions of a multi-process capture: cut at an idle gap this long.
SESSION_GAP_MS = 10000
SESSION_MIN_TRANSFERS = 4
# A bulk OUT's payload column: its bytes are not stored (see the docstring).
BULK_OUT_MARKER = "~"

_PCAP_HDR = struct.Struct("<IHHiIII")
_REC_HDR = struct.Struct("<IIII")
# usbmon binary header, the first 48 bytes (linktype 189 has just these;
# 220, the mmapped form, adds 16 more: interval, start_frame, flags, ndesc).
_USBMON = struct.Struct("<QcBBBHccqiiII8s")
_HDR_LEN = {189: 48, 220: 64}
_XFER = {1: "I", 2: "C", 3: "B"}


@dataclass
class Transfer:
    kind: str                 # CI CO BI BO II IO
    head: str                 # the setup / endpoint columns, preformatted
    data: bytes
    status: int = 0
    submit: float = 0.0
    complete: float = 0.0


@dataclass
class Entry:
    """One parsed script line (a run counts as one entry)."""
    index: int
    kind: str
    fields: list
    data: bytes
    count: int = 1
    gap_ms: int = 0           # GAP before this entry
    status: int = 0
    every_ms: int = 0         # a run's pace (0: back to back)


@dataclass
class Script:
    source: str
    device: int
    bus: int
    transfers: list = field(default_factory=list)
    notes: dict = field(default_factory=dict)   # transfer index -> comment
    first_index: int = 0      # the capture index of transfers[0] (a session)

    def sessions(self):
        """The capture's runs, one Script each, in order."""
        cuts = [0]
        for i in range(1, len(self.transfers)):
            gap = (self.transfers[i].submit - self.transfers[i - 1].complete) * 1000.0
            if gap >= SESSION_GAP_MS:
                cuts.append(i)
        cuts.append(len(self.transfers))
        runs = [(a, b) for a, b in zip(cuts, cuts[1:])]
        merged = []
        for a, b in runs:
            if merged and merged[-1][2]:
                merged[-1] = (merged[-1][0], b, False)
            else:
                merged.append((a, b, b - a < SESSION_MIN_TRANSFERS))
        return [Script(self.source, self.device, self.bus, self.transfers[a:b],
                       first_index=self.first_index + a) for a, b, _ in merged]

    def body(self):
        out = []
        i = 0
        n = len(self.transfers)
        while i < n:
            t = self.transfers[i]
            if i > 0:
                gap = (t.submit - self.transfers[i - 1].complete) * 1000.0
                if gap >= GAP_MIN_MS:
                    out.append(f"GAP {int(round(gap))}")
            if i in self.notes:
                out.append(f"# {self.notes[i]}")
            # A run: the same transfer again and again, back to back, or
            # (a paced poll) with the same long gap, to the ms, each time.
            j = i + 1
            every = None
            while j < n and j not in self.notes and _same(self.transfers[j], t):
                g = _gap_class(self.transfers[j - 1], self.transfers[j])
                if every is None:
                    every = g
                elif (g == 0) != (every == 0) or abs(g - every) > PACE_JITTER_MS:
                    break
                j += 1
            payload = BULK_OUT_MARKER if t.kind == "BO" else t.data.hex() or "-"
            line = f"{self.first_index + i} {t.kind} {t.head} {payload}"
            if j - i > 1:
                line += f" x{j - i}"
                if every:
                    line += f" every={every}"
            if t.status:
                line += f" st={t.status}"
            out.append(line)
            i = j
        return out

    def text(self):
        head = ["# gl126 reply script v1",
                f"# source: {self.source}, bus {self.bus} device {self.device}, "
                f"{len(self.transfers)} transfers"
                + (f" from #{self.first_index}" if self.first_index else ""),
                "# format: tests/pcap_to_replay.py docstring"]
        return "\n".join(head + self.body()) + "\n"


def _gap_class(prev, cur):
    """0 for back to back (< GAP_MIN_MS), else the idle gap in whole ms."""
    gap = (cur.submit - prev.complete) * 1000.0
    return int(round(gap)) if gap >= GAP_MIN_MS else 0


def _same(a, b):
    return (a.kind, a.head, a.data, a.status) == (b.kind, b.head, b.data, b.status)


def _ts(sec, usec):
    return sec + usec / 1e6


def convert_pcap(path, device=None):
    path = Path(path)
    blob = path.read_bytes()
    magic, _, _, _, _, _, linktype = _PCAP_HDR.unpack_from(blob, 0)
    if magic != 0xA1B2C3D4:
        raise ValueError(f"{path.name}: not a little-endian microsecond pcap")
    if linktype not in _HDR_LEN:
        raise ValueError(f"{path.name}: linktype {linktype}, want usbmon (189 or 220)")
    hdr_len = _HDR_LEN[linktype]
    pos = _PCAP_HDR.size
    pending = {}          # (bus, dev, urb id) -> Transfer awaiting its completion
    done = {}             # (bus, dev) -> [Transfer] in submit order
    while pos + _REC_HDR.size <= len(blob):
        _, _, incl, _ = _REC_HDR.unpack_from(blob, pos)
        pkt = blob[pos + _REC_HDR.size: pos + _REC_HDR.size + incl]
        pos += _REC_HDR.size + incl
        (urb, kind, xfer, ep, dev, bus, flag_setup, _, sec, usec, status,
         length, data_len, setup) = _USBMON.unpack_from(pkt, 0)
        data = pkt[hdr_len: hdr_len + data_len]
        key = (bus, dev, urb)
        if xfer not in _XFER:
            continue
        if kind == b"S":
            d = "I" if ep & 0x80 else "O"
            if xfer == 2:
                bm, req, wv, wi, wl = struct.unpack("<BBHHH", setup)
                d = "I" if bm & 0x80 else "O"
                head = f"{bm:02x} {req:02x} {wv:04x} {wi:04x} {wl}"
                payload = data if d == "O" else b""
            else:
                head = f"{ep:02x} {length}"
                # A bulk OUT carries the tables written to the scanner,
                # the shading tables among them (computed from the
                # calibration frames): never kept, only its length.
                payload = b""
            t = Transfer(_XFER[xfer] + d, head, payload, submit=_ts(sec, usec))
            pending[key] = t
            done.setdefault((bus, dev), []).append(t)
        elif kind in (b"C", b"E") and key in pending:
            t = pending.pop(key)
            t.complete = _ts(sec, usec)
            t.status = status
            # A bulk IN completion carries the scan's image data: never
            # kept, only its length (the request's, as the script's key).
            if t.kind[1] == "I" and t.kind != "BI":
                t.data = data
    if not done:
        return Script(path.name + f" (usbmon linktype {linktype})", device or 0, 0)
    if device is None:
        bus, device = max(done, key=lambda k: len(done[k]))
    else:
        matches = [k for k in done if k[1] == device]
        if not matches:
            raise ValueError(f"{path.name}: no transfers for device {device}")
        bus = matches[0][0]
    xfers = done[(bus, device)]
    for t in xfers:
        if not t.complete:
            t.complete = t.submit
    return Script(f"{path.name} (usbmon linktype {linktype})", device, bus, xfers)


def convert_vendor_trace(path):
    path = Path(path)
    with gzip.open(path, "rt") as f:
        trace = json.load(f)
    xfers = []
    notes = {}
    clock = 0.0
    for op in trace:
        clock += float(op.get("dt", 0.0))
        t = op["t"]
        if t in ("cr", "cw", "poll"):
            head = f"{op['bm']:02x} {op['br']:02x} {op['wv']:04x} {op['wi']:04x} "
        if t == "cw":
            data = bytes.fromhex(op.get("data", ""))
            reps = [("CO", head + str(len(data)), data)]
        elif t == "cr":
            reps = [("CI", head + str(op["len"]), bytes.fromhex(op.get("resp", "")))]
        elif t == "poll":
            resps = [bytes.fromhex(r) for r in op["resps"]]
            n = int(op["n"])
            fill = resps[:-1] + [resps[-1]] * max(1, n - len(resps) + 1)
            reps = [("CI", head + str(op["len"]), r) for r in fill[:max(n, 1)]]
            notes[len(xfers)] = (f"lossy poll: {n} reads, distinct replies "
                                 f"{' '.join(r or '-' for r in op['resps'])} "
                                 "(per-read order not recorded)")
        elif t == "bo":
            reps = [("BO", f"-- {len(bytes.fromhex(op.get('data', '')))}", b"")]
        elif t == "bi":
            reps = [("BI", f"-- {op.get('len', 0)}", b"")]
        else:
            raise ValueError(f"{path.name}: unknown trace entry type {t!r}")
        for k, (kind, h, data) in enumerate(reps):
            # A poll's reads are back to back; its own duration is not
            # an idle gap.
            xfers.append(Transfer(kind, h, data, submit=clock, complete=clock))
    return Script(f"{path.name} (vendor trace)", 0, 0, xfers, notes)


def parse_script(text):
    """The script's lines as Entry objects (comments dropped, GAP folded
    into the entry after it)."""
    entries = []
    gap = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        tok = line.split()
        if tok[0] == "GAP":
            gap = int(tok[1])
            continue
        count, status, every = 1, 0, 0
        while tok[-1].startswith(("x", "st=", "every=")):
            last = tok.pop()
            if last.startswith("st="):
                status = int(last[3:])
            elif last.startswith("every="):
                every = int(last[6:])
            else:
                count = int(last[1:])
        index, kind = int(tok[0]), tok[1]
        data = b"" if tok[-1] in ("-", BULK_OUT_MARKER) else bytes.fromhex(tok[-1])
        entries.append(Entry(index, kind, tok[2:-1], data, count, gap, status, every))
        gap = 0
    return entries


def main(argv):
    session = None
    if "--session" in argv:
        k = argv.index("--session")
        session = int(argv[k + 1])
        argv = argv[:k] + argv[k + 2:]
    if len(argv) not in (2, 3):
        print(__doc__.split("Usage:")[1], file=sys.stderr)
        return 2
    src = Path(argv[1])
    if src.name.endswith(".json.gz"):
        script = convert_vendor_trace(src)
    else:
        script = convert_pcap(src, int(argv[2]) if len(argv) == 3 else None)
    if session is not None:
        runs = script.sessions()
        if not 0 <= session < len(runs):
            print(f"{src.name}: session {session} of {len(runs)}", file=sys.stderr)
            return 2
        script = runs[session]
    sys.stdout.write(script.text())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
