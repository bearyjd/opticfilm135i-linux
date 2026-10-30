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


def _int(row: dict, key: str, default: int = 0) -> int:
    v = row.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"field {key!r} must be an integer, got {v!r}")
    return v


def _parse_row(r) -> dict | None:
    """One decoded row -> neutral event (None = dropped). Raises ValueError
    for a row that is not in the documented format."""
    if not isinstance(r, dict):
        raise ValueError("row is not a JSON object")
    ep, status = _int(r, "endpoint"), _int(r, "status")
    setup = r.get("setup")
    if setup:
        if not (isinstance(setup, list) and len(setup) == 5
                and all(isinstance(x, int) and not isinstance(x, bool) for x in setup)):
            raise ValueError(f"'setup' must be 5 integers, got {setup!r}")
        bm, br, wv, wi, wl = setup
        data = bytes.fromhex(r.get("payload_hex", ""))
        if bm & 0x60 != 0x40 or status < 0:
            return None
        return ctrl_event(bm, br, wv, wi, wl, data)
    if ep == EP_INT_IN:
        data = bytes.fromhex(r.get("payload_hex", ""))
        return event("int", "in", data=data) if data else None   # empty = idle timeout
    if ep == EP_BULK_OUT:
        return event("bulk", "out", len=_int(r, "payload_length"),
                     sha=str(r.get("payload_sha256", "")))
    if ep & 0x80:
        return event("bulk", "in", len=_int(r, "returned"))
    return None


def load_capture(path: str | Path) -> list[dict]:
    """Parse a capture file; a malformed row raises ValueError naming its
    1-based line number ("row N")."""
    parsed = []
    for n, line in enumerate(Path(path).read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            ev = _parse_row(row)
            order = _int(row, "submit_packet") if isinstance(row, dict) else 0
        except (ValueError, TypeError) as e:
            raise ValueError(f"{path}: row {n}: {e}") from e
        if ev is not None:
            parsed.append((order, n, ev))
    parsed.sort(key=lambda t: (t[0], t[1]))
    return [ev for _, _, ev in parsed]
