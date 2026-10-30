"""Offline tests for the capture-replay harness (tools/replay_harness.py).

Every capture here is a SYNTHETIC hand-written mini-capture with arbitrary
register numbers and values; nothing is derived from a real recording.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest
import usb.core

_HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_HERE, ".."))
sys.path.insert(0, os.path.join(_HERE, "..", "tools"))

import replay_harness as rh  # noqa: E402
from of135i.usbio import UsbIo  # noqa: E402
from replay import diffing  # noqa: E402
from replay.capture import bulk_out_event, ctrl_event, load_capture  # noqa: E402
from replay.fakedev import FakeVendorDevice, no_real_usb  # noqa: E402


def rd(reg, val, ack=0x55):
    return ctrl_event(0xC0, 4, 0x8E, (reg << 8) | 0x22, 2, bytes([val, ack]))


def st(val, low=0x55):
    return ctrl_event(0xC0, 4, 0x18E, 0x0122, 2, bytes([val, low]))


def wr(*pairs):
    return ctrl_event(0x40, 4, 0x83, 0, 2 * len(pairs), bytes(b for p in pairs for b in p))


def driver_seq(s, dpi=3600):
    """The synthetic 'operation': what the driver sends in the matching case."""
    s.check_start_state()
    s.io.write_regs([(0x10, 0xAA), (0x11, 0xBB)])
    s.io.read_status()
    s.io.read_status()
    s.io.buf_write(0x1000, b"\x01\x02\x03\x04")


RECORDED = [
    rd(0x01, 0x22),
    wr((0x10, 0xAA), (0x11, 0xBB)),
    st(0xD1), st(0xF8),
    None,                                        # buffer descriptor, set below
    bulk_out_event(b"\x01\x02\x03\x04"),
]
# the real descriptor the driver sends: addr 0x1000, length 4
RECORDED[4] = ctrl_event(0x40, 4, 0x82, 1, 8,
                         (0x1000).to_bytes(4, "little") + (4).to_bytes(4, "little"))


def make_io(recorded, **kw):
    fake = FakeVendorDevice(recorded, **kw)
    io = UsbIo(fake)
    io.session.arm(0x22)         # tests below drive UsbIo directly
    return io, fake


def kinds(fake):
    return [d["what"] for d in fake.divergences]


# ------------------------------------------------------------ capture format
def test_load_capture_parses_and_drops_standard_requests(tmp_path):
    rows = [
        {"endpoint": 128, "setup": [0x80, 6, 0x100, 0, 18], "payload_hex": "aa" * 18,
         "status": 0, "submit_packet": 1},
        {"endpoint": 128, "setup": [0xC0, 4, 0x8E, 0x1022, 2], "payload_hex": "3455",
         "status": 0, "submit_packet": 5},
        {"endpoint": 0, "setup": [0x40, 4, 0x83, 0, 2], "payload_hex": "1011",
         "status": 0, "submit_packet": 3},
        {"endpoint": 2, "payload_length": 512, "payload_sha256": "ab" * 32,
         "status": 0, "submit_packet": 7},
        {"endpoint": 0x83, "payload_length": 0, "status": -2, "submit_packet": 9},
    ]
    p = tmp_path / "c.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    ev = load_capture(p)
    assert [(e["kind"], e["dir"]) for e in ev] == [("ctrl", "out"), ("ctrl", "in"), ("bulk", "out")]
    assert ev[1]["data"] == bytes.fromhex("3455")      # sorted by submit order


def test_load_capture_rejects_garbage(tmp_path):
    p = tmp_path / "bad.jsonl"
    p.write_text("not json\n")
    with pytest.raises(ValueError):
        load_capture(p)


# ----------------------------------------------------------- reply matching
def test_replies_matched_in_recorded_order():
    io, fake = make_io([st(0xD1), st(0xD9), st(0xF8)])
    assert [io.read_status() for _ in range(3)] == [0xD1, 0xD9, 0xF8]
    assert fake.divergences == []


def test_exhausted_replies_serve_last_value_and_log_divergence():
    io, fake = make_io([st(0xD1), st(0xF8)])
    got = [io.read_status() for _ in range(4)]
    assert got == [0xD1, 0xF8, 0xF8, 0xF8]
    assert kinds(fake) == ["read_unanswered", "read_unanswered"]


def test_unseen_request_gets_idle_value_and_divergence():
    io, fake = make_io([], idle={(0x8E, 0x0122): bytes([0x22, 0x55])})
    assert io.read_reg(0x01) == 0x22
    assert io.read_reg(0x40) == 0x00           # no idle entry: zero value, ack byte kept
    assert kinds(fake) == ["read_unanswered"] * 2


def test_read_does_not_cross_a_recorded_write():
    io, fake = make_io([wr((0x10, 1)), st(0xF8)])
    assert io.read_status() == 0x00            # reply lies behind a write the driver has not made
    io.write_regs([(0x10, 1)])
    assert io.read_status() == 0xF8
    assert kinds(fake) == ["read_unanswered"]


def test_write_value_difference_is_logged_and_still_advances():
    io, fake = make_io([wr((0x10, 1)), st(0xF8)])
    io.write_regs([(0x10, 2)])
    assert kinds(fake) == ["write_data_differs"]
    assert io.read_status() == 0xF8


def test_unmatched_write_and_bulk_out_logged():
    io, fake = make_io([])
    io.write_regs([(0x10, 2)])
    io.dev.write(0x02, b"xyz")
    assert kinds(fake) == ["write_unmatched", "bulk_out_unmatched"]


def test_bulk_out_content_difference_logged():
    io, fake = make_io([bulk_out_event(b"abc")])
    io.dev.write(0x02, b"abd")
    assert kinds(fake) == ["bulk_out_differs"]


def test_bulk_in_is_zeros_never_invented():
    io, fake = make_io([])
    assert io.dev.read(0x81, 4096) == bytes(4096)
    assert kinds(fake) == ["bulk_in_zeros"]


def test_interrupt_events_replayed_then_timeout():
    ev = {"kind": "int", "dir": "in", "data": b"\x48"}
    io, _ = make_io([ev])
    assert io.read_button() == 0x48
    assert io.read_button() is None


# ------------------------------------------------------------------- guard
def test_guard_blocks_real_usb():
    real_find = usb.core.find
    with no_real_usb():
        with pytest.raises(AssertionError):
            usb.core.find(idVendor=0x07B3)
        with pytest.raises(AssertionError):
            UsbIo.open()
    assert usb.core.find is real_find


def test_run_executes_under_guard_and_restores_state():
    real = (usb.core.find, time.sleep, time.monotonic)
    seen = []

    def op(s, dpi):
        with pytest.raises(AssertionError):
            usb.core.find()
        seen.append(True)

    res = rh.run(RECORDED, op)
    assert seen and res["halted"] is None
    assert (usb.core.find, time.sleep, time.monotonic) == real


def test_virtual_time_makes_poll_timeouts_instant():
    io, _ = make_io([st(0x00)])
    real = time.perf_counter()
    with rh._virtual_time():
        v0 = time.perf_counter()
        io.poll_status_word(0xFFFF, 0xF855, timeout=3600.0)
        assert time.perf_counter() - v0 > 3600      # virtual hours ...
    assert time.perf_counter() - real < 60          # ... not real ones


# ---------------------------------------------------- round trip with the diff
def test_round_trip_equal_sequence_has_empty_diff():
    res = rh.run(RECORDED, driver_seq)
    assert res["halted"] is None
    assert res["divergences"] == []
    assert diffing.is_empty(res["diff"]), res["diff"]


def test_mutating_a_recorded_write_value_gives_value_diff():
    mutated = list(RECORDED)
    mutated[1] = wr((0x10, 0xAA), (0x11, 0xBC))
    res = rh.run(mutated, driver_seq)
    assert not diffing.is_empty(res["diff"])
    assert any(ln.startswith("VALUE") and "reg 0x011" in ln for ln in res["diff"])
    assert [d["what"] for d in res["divergences"]] == ["write_data_differs"]


def test_extra_driver_op_gives_order_diff():
    def extra(s, dpi):
        driver_seq(s, dpi)
        s.io.write_regs([(0x30, 1)])

    res = rh.run(RECORDED, extra)
    assert any(ln.startswith("ORDER") for ln in res["diff"])
    assert any("B only" in ln and "reg 0x030" in ln for ln in res["diff"])


def test_missing_driver_op_gives_order_diff():
    def short(s, dpi):
        s.io.read_reg(0x01, strict=True)

    res = rh.run(RECORDED, short)
    assert any("A only" in ln for ln in res["diff"])


# ------------------------------------------------------------- runner & CLI
def test_driver_error_is_reported_not_raised():
    def boom(s, dpi):
        raise RuntimeError("synthetic failure")

    res = rh.run(RECORDED, boom)
    assert res["halted"].startswith("RuntimeError")


def test_unknown_op_rejected():
    with pytest.raises(ValueError):
        rh.run(RECORDED, "nope")


def test_cli_writes_outputs_and_refuses_existing_dir(tmp_path):
    cap = tmp_path / "c.jsonl"
    cap.write_text(json.dumps({"endpoint": 128, "setup": [0xC0, 4, 0x8E, 0x0122, 2],
                               "payload_hex": "2255", "status": 0, "submit_packet": 1}) + "\n")
    out = tmp_path / "out"
    assert rh.main(["x", str(cap), "--op", "cold_init", "--out", str(out)]) == 0
    assert (out / "diff.txt").exists() and (out / "divergences.jsonl").exists()
    assert (out / "trace.jsonl").exists() and (out / "compiled.trace.json").exists()
    with pytest.raises(FileExistsError):
        rh.main(["x", str(cap), "--op", "cold_init", "--out", str(out)])
