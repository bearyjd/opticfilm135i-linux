#!/usr/bin/env python3
"""List the narrow dips the white-measurement guard finds in exported
calibration data (`<stem>.cal.npz`, written by `scan`/`digitize`).

For each file, the visible white (odd lines of `verify_meas`, table A)
and, on dual exports, the IR white (even lines, table B) are run through
`calibrate.guard_white_dips()`; every dip is printed with its channel,
column span and depth, plus the per-column correction factor a host-side
fix would apply to the frame's raw output at those columns:
raw_output * (white_raw / white_guarded), i.e. divide out the extra gain.
Frames scanned BEFORE the guard existed carry the over-gain in their
`fN.tiff` / `fN-ir.tiff`; frames scanned with it do not (their dips are
listed here for the record only). Column indices are sensor pixels and
map 1:1 to the columns of the 5184-wide dual outputs.

Usage: cal_dips.py [--json] <stem>.cal.npz [...]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from of135i import calibrate  # noqa: E402


def dips_for(path: Path) -> dict:
    z = np.load(path)
    verify = z["verify_meas"]
    dual = bool(z["dual"]) if "dual" in z.files else verify.shape[1] == 5184
    tables = {"a": verify[1::2], "b": verify[0::2]} if dual else {"a": verify}
    out = {"file": str(path), "dual": dual, "tables": {}}
    for name, meas in tables.items():
        raw = meas.astype(np.float64).mean(axis=0)
        guarded, dips = calibrate.guard_white_dips(meas)
        entries = []
        for d in dips:
            cols = slice(d.start, d.stop + 1)
            factor = (raw[cols, d.channel] / guarded[0, cols, d.channel]).round(5).tolist()
            entries.append({**d.as_dict(), "correction_factor": factor})
        out["tables"][name] = entries
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--json", action="store_true", help="one JSON object per file")
    args = ap.parse_args(argv)
    for path in args.files:
        rec = dips_for(path)
        if args.json:
            print(json.dumps(rec))
            continue
        for name, entries in rec["tables"].items():
            label = {"a": "visible", "b": "IR"}[name] if rec["dual"] else "visible"
            if not entries:
                print(f"{path}: table {name} ({label}): no dips")
            for e in entries:
                print(f"{path}: table {name} ({label}) ch{e['channel']} cols {e['start']}-{e['stop']} "
                      f"depth {100 * e['depth']:.1f} %  min factor {min(e['correction_factor']):.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
