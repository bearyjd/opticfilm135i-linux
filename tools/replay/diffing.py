"""Minimal order/value diff of two transfer-event lists (A = recorded
capture, B = what the driver sent). Register-level, no register map.

Normalisation: register-write bursts are flattened to one (reg, value) per
pair; consecutive reads of one register collapse to a run (first/last value);
write-acknowledge reads (bRequest 0x0C) and bulk-IN data are ignored; other
control transfers and bulk-OUT keep their identity. For names, field
decoding and timing use an external annotated-trace tool on the compiled
trace the runner also writes (``--compiled-out``); this module has none.
"""
from __future__ import annotations

import difflib

WRITE_WVALUES = (0x83, 0x183)
READ_WVALUES = (0x8E, 0x18E)
ACK_REQUEST = 0x0C


def _reg(wv: int, wi: int) -> int:
    return ((wi >> 8) & 0xFF) | (wv & 0x100)


def normalize(events: list[dict]) -> list[dict]:
    ops: list[dict] = []
    for e in events:
        if e["kind"] == "ctrl":
            op = _ctrl_ops(e)
        elif e["kind"] == "bulk" and e["dir"] == "out":
            op = [{"sig": ("bo", e["len"]), "val": e["sha"]}]
        else:
            op = []
        for o in op:
            prev = ops[-1] if ops else None
            if o["sig"][0] == "r" and prev and prev["sig"] == o["sig"]:
                prev["last"] = o["val"]
            else:
                if o["sig"][0] == "r":
                    o["last"] = o["val"]
                ops.append(o)
    return ops


def _ctrl_ops(e: dict) -> list[dict]:
    wv, wi, data = e["wv"], e["wi"], e["data"]
    if e["dir"] == "out" and wv in WRITE_WVALUES:
        hi = wv & 0x100
        return [{"sig": ("w", data[i] | hi), "val": data[i + 1]}
                for i in range(0, len(data) - 1, 2)]
    if e["dir"] == "in":
        if e["br"] == ACK_REQUEST:
            return []
        if wv in READ_WVALUES and data:
            return [{"sig": ("r", _reg(wv, wi)), "val": data[0]}]
    return [{"sig": ("c", e["dir"], e["bm"], e["br"], wv, wi), "val": data.hex()}]


def _name(sig: tuple) -> str:
    if sig[0] in ("w", "r"):
        return f"{'WRITE' if sig[0] == 'w' else 'READ '} reg 0x{sig[1]:03x}"
    if sig[0] == "bo":
        return f"BULK-OUT {sig[1]} B"
    return "CTRL-" + sig[1].upper() + f" br=0x{sig[3]:02x} wv=0x{sig[4]:04x} wi=0x{sig[5]:04x}"


def diff(a_events: list[dict], b_events: list[dict]) -> list[str]:
    a, b = normalize(a_events), normalize(b_events)
    sm = difflib.SequenceMatcher(None, [o["sig"] for o in a], [o["sig"] for o in b],
                                 autojunk=False)
    lines, order, value = [], 0, 0
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            for i, j in zip(range(i1, i2), range(j1, j2), strict=True):
                x, y = a[i], b[j]
                if x["val"] != y["val"]:
                    lines.append(f"VALUE  #{i} {_name(x['sig'])}: "
                                 f"A={_fmt(x['val'])} B={_fmt(y['val'])}")
                    value += 1
                elif x.get("last", x["val"]) != y.get("last", y["val"]):
                    lines.append(f"VALUE  #{i} {_name(x['sig'])} last: "
                                 f"A={_fmt(x['last'])} B={_fmt(y['last'])}")
                    value += 1
            continue
        order += 1
        lines.append(f"ORDER  block: A ops {i1}..{i2 - 1} vs B ops {j1}..{j2 - 1}")
        lines += [f"         A only: #{i} {_name(a[i]['sig'])}" for i in range(i1, i2)]
        lines += [f"         B only: #{j} {_name(b[j]['sig'])}" for j in range(j1, j2)]
    lines.append(f"# A={len(a)} ops, B={len(b)} ops; order blocks={order} value diffs={value}")
    return lines


def _fmt(v) -> str:
    return f"{v:02x}" if isinstance(v, int) else str(v)[:16]


def is_empty(lines: list[str]) -> bool:
    return all(ln.startswith("#") for ln in lines)
