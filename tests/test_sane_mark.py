#!/usr/bin/env python3
"""Offline tests for the GL126 SANE backend's magazine mark.

sane/gl126_mark.{h,cpp} implements the small state file that lets a
magazine load span two processes (scanimage is a fresh process per
invocation). These tests cover the file itself (path, format, round trip,
hardening against symlinks, hard links and FIFOs) without building the
full SANE backend or touching hardware: gl126_mark.cpp is compiled
standalone (no genesys headers) together with a tiny probe program,
tests/gl126_mark_probe.cpp. One test documents the contract that the
backend takes no process lock: it still reads and writes the mark while the
Python driver's ProcessLock is held.

Run with:
    .venv/bin/python tests/test_sane_mark.py

Requires a C++ compiler (g++) on PATH; if none is found, every test
prints a SKIP line and passes trivially.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from of135i.safety import ProcessLock  # noqa: E402

SANE_DIR = REPO / "sane"
PROBE_SRC = Path(__file__).resolve().parent / "gl126_mark_probe.cpp"

_probe_bin: str | None = None
_build_attempted = False


def _build_probe() -> str | None:
    """Compile gl126_mark.cpp + the probe once; return the probe binary
    path, or None (having printed a SKIP line, once) if no g++ is on
    PATH."""
    global _probe_bin, _build_attempted
    if _build_attempted:
        return _probe_bin
    _build_attempted = True

    gxx = shutil.which("g++")
    if gxx is None:
        print("SKIP: g++ not found on PATH -- test_sane_mark.py needs a C++ "
              "compiler to build sane/gl126_mark.cpp standalone")
        return None

    tmpdir = tempfile.mkdtemp(prefix="gl126-mark-test-")
    binary = str(Path(tmpdir) / "probe")
    cmd = [gxx, "-std=c++11", "-Wall", "-Wextra", "-Werror",
           str(SANE_DIR / "gl126_mark.cpp"), str(PROBE_SRC),
           "-I", str(SANE_DIR), "-o", binary]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise AssertionError(
            f"failed to build the gl126_mark probe:\n"
            f"{' '.join(cmd)}\n{result.stdout}\n{result.stderr}")
    _probe_bin = binary
    return _probe_bin


def _probe_env(mark_path: str) -> dict:
    env = dict(os.environ)
    env["SANE_GL126_MAGAZINE_MARK"] = mark_path
    return env


def test_magazine_mark_round_trip():
    """docs/sane-wp4-magazine.md section 2.1: the "a release is pending"
    fact, written to a file so a SECOND process can see
    it -- `scanimage` loads in two invocations (press Load film, reseat,
    scan) and each one is a new process.

    It is only ever a hint: the backend re-reads the hardware before the
    load, and a power cycle both re-enumerates the unit under a new
    address and leaves reg 0x01 cold. What this test pins is the file
    itself -- where it lives, that it round-trips the device key, that a
    foreign key comes back as written (so the caller can compare and
    ignore it), and that clearing really removes it."""
    probe = _build_probe()
    if probe is None:
        print("test_magazine_mark_round_trip SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        mark_path = str(Path(td) / "mark")
        env = _probe_env(mark_path)

        def run(*args):
            return subprocess.run([probe, *args], capture_output=True,
                                  text=True, env=env)

        # SANE_GL126_MAGAZINE_MARK moves the mark -- a test, or a second
        # host user, cannot collide with the real one.
        r = run("mark-path")
        assert r.returncode == 0, r
        assert r.stdout.strip() == mark_path, r.stdout
        # Without the override, the default path is a fixed name in /tmp.
        env_default = {k: v for k, v in os.environ.items()
                       if k != "SANE_GL126_MAGAZINE_MARK"}
        r = subprocess.run([probe, "mark-path"], capture_output=True,
                           text=True, env=env_default)
        assert r.stdout.strip() == "/tmp/sane-gl126-07b3-1436.magazine", r.stdout
        assert not Path(mark_path).exists(), "no mark before anything is written"

        # No mark: "NONE", non-zero exit -- this is the ordinary case on
        # every scan that is not completing a load.
        r = run("mark-read")
        assert r.returncode == 1 and r.stdout.strip() == "NONE", r

        r = run("mark-write", "libusb:001:007")
        assert r.returncode == 0 and r.stdout.strip() == "WROTE", r
        r = run("mark-read")
        assert r.returncode == 0 and r.stdout.strip() == "KEY libusb:001:007", r

        # Readable by a person: `cat` on it says what it is, with the device key on its own line -- a SANE
        # device name can contain spaces, so it is never a field in a
        # space-delimited line.
        lines = Path(mark_path).read_text().splitlines()
        assert lines[0].startswith("released "), lines
        assert lines[0].endswith("(sane genesys gl126)"), lines
        assert lines[1] == "libusb:001:007", lines

        # A key WITH spaces round-trips whole (the backend's own test mode
        # names its device "test device:0x07b3:0x1436").
        r = run("mark-write", "test device:0x07b3:0x1436")
        assert r.returncode == 0, r
        r = run("mark-read")
        assert r.stdout.strip() == "KEY test device:0x07b3:0x1436", r
        r = run("mark-write", "libusb:001:007")
        assert r.returncode == 0, r

        # Writing again replaces rather than appends: one unit, at most
        # one magazine waiting.
        r = run("mark-write", "libusb:001:009")
        assert r.returncode == 0, r
        r = run("mark-read")
        assert r.stdout.strip() == "KEY libusb:001:009", r
        assert Path(mark_path).read_text().count("released") == 1
        assert Path(mark_path).read_text().splitlines()[1] == "libusb:001:009"

        r = run("mark-clear")
        assert r.returncode == 0, r
        assert not Path(mark_path).exists()
        r = run("mark-read")
        assert r.returncode == 1 and r.stdout.strip() == "NONE", r

        # A truncated or foreign file is "no mark", not a crash.
        Path(mark_path).write_text("garbage\n")
        r = run("mark-read")
        assert r.returncode == 1 and r.stdout.strip() == "NONE", r

    print("test_magazine_mark_round_trip OK "
          "(path, round trip, replace, clear, malformed)")


def test_magazine_mark_path_fifo_does_not_block_the_read():
    """A FIFO planted at the mark path must not hang magazine_mark_read()
    -- it is called before every load and must always return promptly.
    Before O_NONBLOCK this open() would block forever on a FIFO with no
    writer attached; a regression here must fail the test outright
    rather than hang the whole suite, hence the explicit subprocess
    timeout."""
    probe = _build_probe()
    if probe is None:
        print("test_magazine_mark_path_fifo_does_not_block_the_read SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        mark_path = Path(td) / "mark"
        lock_path = str(mark_path)
        os.mkfifo(str(mark_path))

        try:
            r = subprocess.run([probe, "mark-read"], capture_output=True, text=True,
                                env=_probe_env(lock_path), timeout=10)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                "magazine_mark_read() blocked on a FIFO at the mark path -- "
                "the O_NONBLOCK open regressed")
        assert r.returncode == 1 and r.stdout.strip() == "NONE", r
    print("test_magazine_mark_path_fifo_does_not_block_the_read OK")


def test_magazine_mark_path_hard_link_is_refused():
    """A hard link planted at the mark path must read as "no mark" (like
    a symlink or FIFO there) and must never be written through -- the
    victim's content comes out unchanged."""
    probe = _build_probe()
    if probe is None:
        print("test_magazine_mark_path_hard_link_is_refused SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        victim = Path(td) / "victim"
        victim.write_text("victim content\n")
        mark_path = Path(td) / "mark"
        lock_path = str(mark_path)
        os.link(str(victim), str(mark_path))

        r = subprocess.run([probe, "mark-read"], capture_output=True, text=True,
                            env=_probe_env(lock_path), timeout=10)
        assert r.returncode == 1 and r.stdout.strip() == "NONE", r
        assert victim.read_text() == "victim content\n"
    print("test_magazine_mark_path_hard_link_is_refused OK")


def test_magazine_mark_symlink_is_replaced_not_written_through():
    """A symlink planted at the mark path must never be written through
    in place: mark-write must either fail, or replace the symlink
    itself (via rename()) -- never touch what the symlink pointed at,
    and never leave a `.tmp.<pid>` file behind either way."""
    probe = _build_probe()
    if probe is None:
        print("test_magazine_mark_symlink_is_replaced_not_written_through SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        victim = Path(td) / "victim"
        victim.write_text("victim content\n")
        mark_path = Path(td) / "mark"
        lock_path = str(mark_path)
        mark_path.symlink_to(victim)

        r = subprocess.run([probe, "mark-write", "libusb:001:007"],
                            capture_output=True, text=True, env=_probe_env(lock_path))
        assert r.stdout.strip() in ("WROTE", "FAILED"), r.stdout

        # Whichever outcome: the symlink's old target is untouched, and no
        # temp file survives.
        assert victim.read_text() == "victim content\n"
        leftovers = [p.name for p in Path(td).iterdir() if ".tmp." in p.name]
        assert leftovers == [], leftovers

        if r.stdout.strip() == "WROTE":
            # Succeeded by replacing the symlink itself with a regular file.
            assert not mark_path.is_symlink(), "mark-write wrote through the symlink"
            r2 = subprocess.run([probe, "mark-read"], capture_output=True, text=True,
                                 env=_probe_env(lock_path))
            assert r2.stdout.strip() == "KEY libusb:001:007", r2.stdout
    print("test_magazine_mark_symlink_is_replaced_not_written_through OK")


def test_magazine_mark_empty_file_reads_as_no_mark():
    """An empty mark file (e.g. a crash mid-write, before the atomic
    rename existed) must read as "no mark", not a crash -- same rule as
    the truncated/garbage case already covered by
    test_magazine_mark_round_trip. Also checks that a normal write
    leaves no temp file behind."""
    probe = _build_probe()
    if probe is None:
        print("test_magazine_mark_empty_file_reads_as_no_mark SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        mark_path = Path(td) / "mark"
        env = _probe_env(str(mark_path))

        r = subprocess.run([probe, "mark-write", "libusb:001:007"],
                            capture_output=True, text=True, env=env)
        assert r.stdout.strip() == "WROTE", r
        assert mark_path.exists() and not mark_path.is_symlink()
        leftovers = [p.name for p in Path(td).iterdir() if ".tmp." in p.name]
        assert leftovers == [], leftovers

        mark_path.write_text("")
        r = subprocess.run([probe, "mark-read"], capture_output=True, text=True, env=env)
        assert r.returncode == 1 and r.stdout.strip() == "NONE", r
    print("test_magazine_mark_empty_file_reads_as_no_mark OK")


def test_magazine_mark_loaded_and_failed_kinds_round_trip():
    """WP-5 review finding E: the two kinds added after Ejected --
    "loaded" (a Load film press completed in a separate process) and
    "failed" (a magazine sequence failed, and that fact must survive the
    failing process's exit) -- go through the SAME two-argument
    magazine_mark_write()/magazine_mark_read() Ejected already used.
    Exercised here via the probe's mark-write-kind/mark-read-kind
    commands; the one-argument mark-write/mark-read commands (used by
    test_magazine_mark_round_trip) are UNCHANGED and stay Released-only,
    per docs/sane-wp4-magazine.md section 10.1."""
    probe = _build_probe()
    if probe is None:
        print("test_magazine_mark_loaded_and_failed_kinds_round_trip SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        mark_path = Path(td) / "mark"
        env = _probe_env(str(mark_path))

        def run(*args):
            return subprocess.run([probe, *args], capture_output=True, text=True, env=env)

        r = run("mark-write-kind", "failed", "test device:0x07b3:0x1436")
        assert r.returncode == 0 and r.stdout.strip() == "WROTE", r
        r = run("mark-read-kind")
        assert r.returncode == 0, r
        assert r.stdout.strip() == "KIND failed KEY test device:0x07b3:0x1436", r

        # The mark file's own first word is the kind name -- readable by
        # a person the same way "released"/"ejected" already are.
        lines = mark_path.read_text().splitlines()
        assert lines[0].startswith("failed "), lines

        r = run("mark-write-kind", "loaded", "libusb:001:007")
        assert r.returncode == 0 and r.stdout.strip() == "WROTE", r
        r = run("mark-read-kind")
        assert r.stdout.strip() == "KIND loaded KEY libusb:001:007", r
        lines = mark_path.read_text().splitlines()
        assert lines[0].startswith("loaded "), lines

        # The one-argument, Released-only forms are unaffected by, and
        # unaware of, the kind that was last written: a "loaded" mark
        # reads as "no mark" through mark-read (docs/sane-wp4-magazine.md
        # section 10.1's back-compat rule, now covering a third kind too).
        r = run("mark-read")
        assert r.returncode == 1 and r.stdout.strip() == "NONE", r

        r = run("mark-clear")
        assert r.returncode == 0, r
        r = run("mark-read-kind")
        assert r.returncode == 1 and r.stdout.strip() == "NONE", r
    print("test_magazine_mark_loaded_and_failed_kinds_round_trip OK "
          "(loaded/failed round-trip, one-argument forms stay Released-only)")


def test_mark_is_readable_while_python_driver_lock_is_held():
    """Contract since v8: the backend takes no process lock at all. With
    the Python driver's ProcessLock held (on its own lock file), the mark
    probe still writes, reads and clears the mark."""
    probe = _build_probe()
    if probe is None:
        print("test_mark_is_readable_while_python_driver_lock_is_held SKIPPED (no g++)")
        return "skipped"

    with tempfile.TemporaryDirectory() as td:
        lock_path = str(Path(td) / "driver.lock")
        mark_path = str(Path(td) / "mark")
        env = _probe_env(mark_path)
        env["OF135I_LOCK_FILE"] = lock_path  # what the driver honours; the backend ignores it

        lock = ProcessLock(lock_path)
        lock.acquire()
        try:
            def run(*args):
                return subprocess.run([probe, *args], capture_output=True,
                                      text=True, env=env, timeout=10)
            r = run("mark-write", "libusb:001:007")
            assert r.returncode == 0 and r.stdout.strip() == "WROTE", r
            r = run("mark-read")
            assert r.returncode == 0 and r.stdout.strip() == "KEY libusb:001:007", r
            r = run("mark-clear")
            assert r.returncode == 0, r
            assert lock.held
        finally:
            lock.release()
    print("test_mark_is_readable_while_python_driver_lock_is_held OK")


def main() -> int:
    tests = [
        test_magazine_mark_round_trip,
        test_magazine_mark_path_fifo_does_not_block_the_read,
        test_magazine_mark_path_hard_link_is_refused,
        test_magazine_mark_symlink_is_replaced_not_written_through,
        test_magazine_mark_empty_file_reads_as_no_mark,
        test_magazine_mark_loaded_and_failed_kinds_round_trip,
        test_mark_is_readable_while_python_driver_lock_is_held,
    ]
    passed = 0
    skipped = 0
    for t in tests:
        result = t()
        # Each test prints its own "SKIPPED (...)" line and returns the
        # string "skipped" in that case; a test that ran to completion
        # prints an "OK" line and returns None (the implicit return of a
        # plain `print(...)` as the last statement).
        if result == "skipped":
            skipped += 1
        else:
            passed += 1
    if skipped:
        print(f"\n{passed} tests passed, {skipped} skipped.")
    else:
        print(f"\n{passed} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
