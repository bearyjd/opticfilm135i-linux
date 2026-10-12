#!/usr/bin/env python3
"""Offline tests: facts from static analysis never reach the SANE side.

of135i/vendor_derived.py (policy: docs/static-analysis.md) may be used by
the standalone driver only. These tests check, in fresh isolated
interpreters, that tools/gen_sane_tables.py does not import it -- directly,
transitively, or lazily during generation -- and that nothing under sane/
or the generator mentions it.

Run with:
    .venv/bin/python tests/test_vendor_isolation.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MODULE = "of135i.vendor_derived"

PRELUDE = (
    "import sys\n"
    f"sys.path.insert(0, {str(REPO)!r})\n"
    f"sys.path.insert(0, {str(REPO / 'tools')!r})\n"
    "import gen_sane_tables\n"
)
REPORT = f"print('LEAK' if {MODULE!r} in sys.modules else 'CLEAN')\n"


def _run(code: str) -> str:
    proc = subprocess.run([sys.executable, "-I", "-c", code],
                          capture_output=True, text=True, cwd=str(REPO))
    assert proc.returncode == 0, (proc.returncode, proc.stdout, proc.stderr)
    return proc.stdout.strip().splitlines()[-1]


def test_import_of_generator_does_not_load_vendor_derived():
    assert _run(PRELUDE + REPORT) == "CLEAN", \
        "tools/gen_sane_tables.py imports of135i.vendor_derived (directly or transitively)"
    print("test_import_of_generator_does_not_load_vendor_derived OK")


def test_generator_check_does_not_load_vendor_derived():
    code = (PRELUDE
            + "rc = gen_sane_tables.main(['--check'])\n"
            + "print('RC', rc)\n"
            + REPORT)
    out = subprocess.run([sys.executable, "-I", "-c", code],
                         capture_output=True, text=True, cwd=str(REPO))
    lines = out.stdout.strip().splitlines()
    assert out.returncode == 0, (out.returncode, out.stdout, out.stderr)
    assert "RC 0" in lines, lines
    assert lines[-1] == "CLEAN", \
        "gen_sane_tables.main(['--check']) loaded of135i.vendor_derived"
    print("test_generator_check_does_not_load_vendor_derived OK")


def test_vendor_derived_imports_cleanly():
    code = (f"import sys; sys.path.insert(0, {str(REPO)!r})\n"
            f"import {MODULE}\nprint('IMPORTED')\n")
    assert _run(code) == "IMPORTED"
    print("test_vendor_derived_imports_cleanly OK")


def test_sane_side_never_mentions_vendor_derived():
    paths = [REPO / "tools" / "gen_sane_tables.py"]
    paths += [p for p in (REPO / "sane").rglob("*") if p.is_file()]
    assert len(paths) > 1, "sane/ not found"
    hits = []
    for p in paths:
        try:
            text = p.read_text(errors="ignore")
        except OSError:
            continue
        if "vendor_derived" in text:
            hits.append(str(p.relative_to(REPO)))
    assert not hits, f"vendor_derived referenced on the SANE side: {hits}"
    print("test_sane_side_never_mentions_vendor_derived OK")


def main() -> int:
    tests = [
        test_import_of_generator_does_not_load_vendor_derived,
        test_generator_check_does_not_load_vendor_derived,
        test_vendor_derived_imports_cleanly,
        test_sane_side_never_mentions_vendor_derived,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
