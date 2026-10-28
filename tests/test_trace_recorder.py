"""Opt-in transfer recorder (OF135I_TRACE): pure observer.

Offline only -- the FakeUsbDevice stands in for the scanner. The recorder
must (a) record exactly the transfers the fake saw, (b) never store
bulk-IN image data, (c) leave a valid prefix after an interrupted run,
(d) create its file 0600 and refuse to overwrite, (e) be invisible when
the variable is unset (no file, identical wire fingerprints) and (f)
never let a recording failure reach scanner I/O.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import sys
import time

import pytest
import usb.core

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_safety import FakeUsbDevice, _cal_buffers, fast_time, make_scanner  # noqa: E402
from test_wire_baseline import BASELINE, SCENARIOS, _record  # noqa: E402

from of135i import trace  # noqa: E402
from of135i.usbio import UsbIo  # noqa: E402


def _io(fake) -> UsbIo:
    io = UsbIo(fake)
    io.session.arm(0x22)        # an IDLE-verified session, no transfer
    return io


def _records(path) -> list[dict]:
    lines = open(path, "rb").read().decode().splitlines()
    return [json.loads(ln) for ln in lines]


def _xfers(path) -> list[dict]:
    return [r for r in _records(path) if "kind" in r]


@pytest.fixture
def tpath(tmp_path, monkeypatch):
    p = tmp_path / "run.trace.jsonl"
    monkeypatch.setenv(trace.TRACE_ENV, str(p))
    return p


def test_env_name():
    assert trace.TRACE_ENV == "OF135I_TRACE"


def test_records_match_scripted_transfers(tpath):
    fake = FakeUsbDevice()
    dev = _io(fake).dev
    dev.ctrl_transfer(0x40, 0x04, 0x0083, 0x0000, bytes([0x03, 0x30]))
    got = bytes(dev.ctrl_transfer(0xC0, 0x04, 0x008E, 0x0122, 2))
    dev.write(0x02, b"\x01\x02\x03")
    dev.read(0x81, 100)
    x = _xfers(tpath)
    assert [(r["kind"], r["dir"]) for r in x] == [
        ("ctrl", "out"), ("ctrl", "in"), ("bulk", "out"), ("bulk", "in")]
    cw, cr, bo, bi = x
    assert (cw["bm"], cw["br"], cw["wv"], cw["wi"], cw["wlen"]) == (0x40, 0x04, 0x83, 0, 2)
    assert cw["data"] == "0330" and cw["status"] == "ok"
    assert (cr["bm"], cr["br"], cr["wv"], cr["wi"], cr["wlen"]) == (0xC0, 0x04, 0x8E, 0x122, 2)
    assert cr["data"] == got.hex() and cr["len"] == 2
    assert bo["ep"] == 0x02 and bo["data"] == "010203" and bo["len"] == 3
    assert bi["ep"] == 0x81 and bi["len"] == 100
    ts = [r["t"] for r in x]
    assert ts == sorted(ts) and all(isinstance(t, float) for t in ts)
    # one record per transfer the device saw
    assert len(x) == len(fake.wire_log)
    assert [r["seq"] for r in x] == [1, 2, 3, 4]


def test_bulk_in_data_not_stored(tpath):
    fake = FakeUsbDevice()
    dev = _io(fake).dev
    payload = bytes(dev.read(0x81, 64))
    assert payload == (b"\x00\x70" * 32)
    x = _xfers(tpath)[0]
    assert x["len"] == 64 and "data" not in x
    assert payload.hex()[:16] not in open(tpath).read()


def test_data_capped_at_64_bytes_with_full_length(tpath):
    fake = FakeUsbDevice()
    dev = _io(fake).dev
    big = bytes(range(200))
    dev.write(0x02, big)
    dev.ctrl_transfer(0x40, 0x04, 0x0083, 0, big[:128])
    bo, cw = _xfers(tpath)
    assert bo["data"] == big[:64].hex() and bo["len"] == 200
    assert cw["data"] == big[:64].hex() and cw["wlen"] == 128


def test_failed_transfer_recorded_and_still_raises(tpath):
    def fault(ev):
        if ev["kind"] == "ctrl_in":
            return usb.core.USBTimeoutError("boom", -7, 110)
    dev = _io(FakeUsbDevice(fault=fault)).dev
    with pytest.raises(usb.core.USBTimeoutError):
        dev.ctrl_transfer(0xC0, 0x04, 0x008E, 0x0122, 2)
    r = _xfers(tpath)[0]
    assert r["status"].startswith("error:") and "USBTimeoutError" in r["status"]
    assert "data" not in r or r["data"] == ""


def test_interrupted_run_leaves_valid_prefix(tpath):
    seen = {"n": 0}

    def fault(ev):
        seen["n"] += 1
        if seen["n"] == 40:
            return KeyboardInterrupt()
    fake = FakeUsbDevice(fault=fault, cal_buffers=_cal_buffers())
    with fast_time(), pytest.raises(KeyboardInterrupt):
        s = make_scanner(fake)
        s.initialize()
        s.scan(frame=1)
    # No close/flush by the driver: the file must already be complete lines.
    raw = open(tpath, "rb").read()
    assert raw.endswith(b"\n")
    x = _xfers(tpath)
    assert len(fake.wire_log) == 39
    assert len(x) == 40                      # 39 completed + the interrupted one
    assert [r["status"] for r in x[:39]] == ["ok"] * 39
    assert x[39]["status"] == "error:KeyboardInterrupt"
    assert [r["seq"] for r in x] == list(range(1, 41))


def test_truncated_final_line_is_the_only_possible_damage(tpath):
    _io(FakeUsbDevice()).dev.write(0x02, b"x")
    raw = open(tpath, "rb").read()
    for line in raw.splitlines():
        json.loads(line)


def test_file_mode_is_0600_even_with_open_umask(tmp_path, monkeypatch):
    p = tmp_path / "t.jsonl"
    monkeypatch.setenv(trace.TRACE_ENV, str(p))
    old = os.umask(0)
    try:
        UsbIo(FakeUsbDevice())
    finally:
        os.umask(old)
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o600


def test_refuses_overwrite_warns_and_io_continues(tpath, caplog):
    tpath.write_bytes(b"precious\n")
    fake = FakeUsbDevice()
    with caplog.at_level(logging.WARNING, logger="of135i"):
        dev = _io(fake).dev
        dev.write(0x02, b"abc")
        dev.write(0x02, b"abc")
    assert tpath.read_bytes() == b"precious\n"
    assert len(fake.wire_log) == 2
    warns = [r for r in caplog.records if "trace" in r.getMessage().lower()]
    assert len(warns) == 1 and warns[0].levelno == logging.WARNING


def test_refuses_symlink_target(tmp_path, monkeypatch):
    victim = tmp_path / "victim"
    victim.write_bytes(b"keep")
    link = tmp_path / "link.jsonl"
    link.symlink_to(victim)
    monkeypatch.setenv(trace.TRACE_ENV, str(link))
    _io(FakeUsbDevice()).dev.write(0x02, b"abc")
    assert victim.read_bytes() == b"keep"


def test_bad_path_warns_once_and_io_continues(tmp_path, monkeypatch, caplog):
    monkeypatch.setenv(trace.TRACE_ENV, str(tmp_path / "no" / "such" / "dir" / "t.jsonl"))
    fake = FakeUsbDevice()
    with caplog.at_level(logging.WARNING, logger="of135i"):
        dev = _io(fake).dev
        dev.write(0x02, b"abc")
    assert len(fake.wire_log) == 1
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_write_error_disables_recording_and_io_continues(tpath, monkeypatch, caplog):
    fake = FakeUsbDevice()
    io = _io(fake)
    io.dev.write(0x02, b"one")
    fd = io.dev._recorder._fd
    real_write = os.write
    calls = {"n": 0}

    def bad_write(f, data):
        if f == fd:
            calls["n"] += 1
            raise OSError(28, "No space left on device")
        return real_write(f, data)
    monkeypatch.setattr(os, "write", bad_write)
    with caplog.at_level(logging.WARNING, logger="of135i"):
        assert io.dev.write(0x02, b"two") == 3
        assert io.dev.write(0x02, b"three") == 5
        assert bytes(io.dev.ctrl_transfer(0xC0, 0x04, 0x008E, 0x0122, 2))
    assert calls["n"] == 1                         # disabled after the first failure
    assert len(fake.wire_log) == 4                 # I/O unaffected
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
    assert len(_xfers(tpath)) == 1                 # prefix intact


def test_unserialisable_state_error_never_reaches_io(tpath, monkeypatch):
    fake = FakeUsbDevice()
    io = _io(fake)
    monkeypatch.setattr(json, "dumps", lambda *a, **k: (_ for _ in ()).throw(ValueError("x")))
    assert io.dev.write(0x02, b"abc") == 3
    assert len(fake.wire_log) == 1


def test_close_releases_the_trace_file(tpath):
    io = _io(FakeUsbDevice())
    io.dev.write(0x02, b"abc")
    fd = io.dev._recorder._fd
    io.close()
    with pytest.raises(OSError):
        os.fstat(fd)
    assert len(_xfers(tpath)) == 1


def test_unset_and_empty_env_create_nothing(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG, logger="of135i")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(trace.TRACE_ENV, raising=False)
    dev = _io(FakeUsbDevice()).dev
    assert dev._recorder is None
    monkeypatch.setenv(trace.TRACE_ENV, "")
    dev = _io(FakeUsbDevice()).dev
    assert dev._recorder is None
    assert list(tmp_path.iterdir()) == []
    assert caplog.records == []          # silent: not even a warning


def test_recorder_makes_no_clock_or_sleep_call(tpath, monkeypatch):
    def forbidden(*a, **k):
        raise AssertionError("recorder touched the patched clock/sleep")
    fake = FakeUsbDevice()
    io = _io(fake)
    monkeypatch.setattr(time, "monotonic", forbidden)
    monkeypatch.setattr(time, "sleep", forbidden)
    io.dev.write(0x02, b"abc")
    io.dev.read(0x81, 8)
    io.dev.ctrl_transfer(0xC0, 0x04, 0x008E, 0x0122, 2)
    assert len(_xfers(tpath)) == 3


@pytest.mark.parametrize("name", ["plain_3600_frame1", "dual_ir_frame1",
                                  "eject_completes", "cold_move_times_out"])
def test_env_set_leaves_wire_fingerprints_identical(name, tpath):
    """Recording ON must not change a single transfer: same digest as the
    stored default-mode baseline, and one record per scripted transfer."""
    digest, n = _record(name)
    assert BASELINE[name] == (digest, n)
    assert name in SCENARIOS
    x = _xfers(tpath)
    assert len([r for r in x if r["kind"] != "intr"]) == n


def test_env_unset_baseline_unchanged_and_no_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(trace.TRACE_ENV, raising=False)
    digest, n = _record("plain_3600_frame1")
    assert BASELINE["plain_3600_frame1"] == (digest, n)
    assert list(tmp_path.iterdir()) == []


# ------------------------------------------------------------- converter

def _converter():
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
    import trace_to_compiled
    return trace_to_compiled


def test_converter_produces_compiled_ops(tpath):
    dev = _io(FakeUsbDevice()).dev
    dev.ctrl_transfer(0x40, 0x04, 0x0083, 0, bytes([0x03, 0x30]))
    dev.ctrl_transfer(0xC0, 0x04, 0x008E, 0x0122, 2)
    dev.write(0x02, bytes(range(100)))
    dev.read(0x81, 4096)
    tc = _converter()
    ops, skipped = tc.convert(tc.read_records(open(tpath).read()))
    assert skipped == 0
    assert [o["t"] for o in ops] == ["cw", "cr", "bo", "bi"]
    cw, cr, bo, bi = ops
    assert (cw["bm"], cw["br"], cw["wv"], cw["wi"], cw["data"]) == (0x40, 4, 0x83, 0, "0330")
    assert cr["len"] == 2 and cr["resp"] == "2255" and "dur" in cr
    assert len(bytes.fromhex(bo["data"])) == 100            # padded to the real length
    assert bytes.fromhex(bo["data"])[:64] == bytes(range(64))
    assert bi == {"t": "bi", "len": 4096, "dt": bi["dt"]}
    assert all(o["dt"] >= 0 for o in ops)


def test_converter_skips_failures_interrupt_and_partial_line(tpath):
    def fault(ev):
        if ev["kind"] == "bulk_in":
            return usb.core.USBTimeoutError("t", -7, 110)
    dev = _io(FakeUsbDevice(fault=fault)).dev
    dev.write(0x02, b"ab")
    with pytest.raises(usb.core.USBTimeoutError):
        dev.read(0x81, 8)
    with pytest.raises(usb.core.USBTimeoutError):
        dev.read(0x83, 1)          # fake times out on an idle interrupt endpoint
    text = open(tpath).read() + '{"seq": 9, "kind": "ct'     # killed mid-write
    tc = _converter()
    ops, skipped = tc.convert(tc.read_records(text))
    assert [o["t"] for o in ops] == ["bo"] and skipped == 2


def test_converter_cli_gz_and_refuses_overwrite(tpath, tmp_path):
    import gzip
    _io(FakeUsbDevice()).dev.write(0x02, b"ab")
    tc = _converter()
    out = tmp_path / "o.trace.json.gz"
    assert tc.main([str(tpath), "-o", str(out)]) == 0
    assert json.load(gzip.open(out, "rt"))[0]["t"] == "bo"
    before = out.read_bytes()
    assert tc.main([str(tpath), "-o", str(out)]) == 1
    assert out.read_bytes() == before
