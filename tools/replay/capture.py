"""Load a decoded capture (one JSON object per USB transfer) into
neutral transfer events.

Accepted row fields (the decoded-capture jsonl format):
  endpoint (int), setup ([bmRequestType, bRequest, wValue, wIndex, wLength]
  for control transfers), payload_hex (control payload), payload_length,
  payload_sha256 (bulk OUT), returned (bulk IN length), status, submit_packet.

Standard and class requests (bmRequestType bits 5-6 != vendor) are dropped:
the driver never issues them.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

EP_BULK_OUT = 0x02
EP_INT_IN = 0x83


def event(kind: str, direction: str, **kw) -> dict:
    """The neutral event shape shared by captures, the fake device log and
    the diff: kind in {ctrl, bulk, int}, direction in {in, out}."""
    return {"kind": kind, "dir": direction, **kw}


def ctrl_event(bm: int, br: int, wv: int, wi: int, wl: int, data: bytes) -> dict:
    return event("ctrl", "in" if bm & 0x80 else "out", bm=bm, br=br, wv=wv, wi=wi,
                 wl=wl, data=bytes(data))


def bulk_out_event(data: bytes) -> dict:
    return event("bulk", "out", len=len(data), sha=hashlib.sha256(data).hexdigest())


def load_capture(path: str | Path) -> list[dict]:
    rows = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError as e:
            raise ValueError(f"{path}:{n}: not a JSON object per line ({e})") from e
    rows.sort(key=lambda r: r.get("submit_packet", 0))
    out: list[dict] = []
    for r in rows:
        ep, status = r.get("endpoint", 0), r.get("status", 0)
        setup = r.get("setup")
        if setup:
            bm, br, wv, wi, wl = setup
            if bm & 0x60 != 0x40 or status < 0:
                continue
            out.append(ctrl_event(bm, br, wv, wi, wl, bytes.fromhex(r.get("payload_hex", ""))))
        elif ep == EP_INT_IN:
            data = bytes.fromhex(r.get("payload_hex", ""))
            if data:                       # empty = the usual idle timeout
                out.append(event("int", "in", data=data))
        elif ep == EP_BULK_OUT:
            out.append(event("bulk", "out", len=r.get("payload_length", 0),
                             sha=r.get("payload_sha256", "")))
        elif ep & 0x80:
            out.append(event("bulk", "in", len=r.get("returned", 0)))
    return out
