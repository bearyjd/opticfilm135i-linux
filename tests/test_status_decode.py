"""Status-word decoder, honest eject interpretation, travel-margin diagnostic.

Host-side bookkeeping only: none of these changes adds, removes or
reorders a transfer (tests/test_wire_baseline.py pins that). The status
word read with wValue 0x018e / wIndex 0x0122 carries register 0x101 in
its high byte and the ack 0x55 in its low byte. Bit meanings, in plain
words, as observed in hardware logs and vendor-app traffic:

    0x01 motor enabled (the engine is driving the motor)
    0x04 lamp on
    0x08 loader/home sensor
    0x40 read buffer empty
    0x80 power bit
    0x02, 0x10, 0x20 recorded raw (meaning not established)

Pure offline logic against fake devices: no USB, no scanner.
"""
from __future__ import annotations

import math
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_poll import _reg32_poll, _scanner, _status_poll  # noqa: E402
from test_safety import (FakeUsbDevice, _cal_buffers, _cli, cli_over,  # noqa: E402
                         fast_time, make_scanner)

from of135i import device, holder, safety  # noqa: E402
from of135i.usbio import UsbIo  # noqa: E402

# ------------------------------------------------------------ F1 decoder

_ALL_KEYS = {"motor_enabled", "bit_0x02", "lamp_on", "loader_sensor",
             "bit_0x10", "bit_0x20", "buffer_empty", "power_bit"}


@pytest.mark.parametrize("reply,expected", [
    ("ad55", dict(motor_enabled=1, buffer_empty=0, loader_sensor=1, lamp_on=1,
                  power_bit=1, bit_0x20=1, bit_0x10=0, bit_0x02=0)),
    ("ec55", dict(motor_enabled=0, buffer_empty=1, loader_sensor=1, bit_0x20=1,
                  bit_0x10=0)),
    ("9c55", dict(motor_enabled=0, buffer_empty=0, loader_sensor=1, bit_0x10=1)),
    ("f955", dict(motor_enabled=1, buffer_empty=1, loader_sensor=1)),
    ("0255", dict(bit_0x02=1, motor_enabled=0, power_bit=0, lamp_on=0)),
])
def test_decoder_names_each_bit(reply, expected):
    bits = device.decode_status_word(bytes.fromhex(reply))
    assert set(bits) == _ALL_KEYS
    for k, v in expected.items():
        assert bits[k] == v, (reply, k)
    assert all(v in (0, 1) for v in bits.values())


@pytest.mark.parametrize("reply", ["ec00", "ec54", "ec", "", "ec5500"])
def test_decoder_rejects_malformed_reply(reply):
    assert device.decode_status_word(bytes.fromhex(reply)) is None


def test_nonstrict_timeout_settle_entry_carries_named_bits():
    s = _scanner(["ec55"])
    s._poll_one(_status_poll("ad55"))
    e = s._diag_poll_settle[-1]
    assert e["how"] == "timeout"
    assert e["bits"]["motor_enabled"] == 0 and e["bits"]["buffer_empty"] == 1
    # The timeout-details record keeps its exact historical shape.
    assert s._diag_poll_timeout_details == [
        {"wv": "0x018e", "wi": "0x0122", "last": "ec55", "want": "ad55"}]


def test_settled_status_poll_also_records_bits():
    s = _scanner(["bd55"])
    s._poll_one(_status_poll("bd55"))
    assert s._diag_poll_settle[-1]["bits"]["motor_enabled"] == 1


@pytest.mark.parametrize("reply", ["ec00", "ec", ""])
def test_malformed_reply_in_poll_is_recorded_not_raised(reply):
    s = _scanner([reply])
    s._poll_one(_status_poll("ad55"))     # must not raise: non-strict continues
    e = s._diag_poll_settle[-1]
    assert e["how"] == "timeout" and e["bits"] is None


def test_bits_only_for_the_extended_status_word():
    s = _scanner(["8d55"])
    s._poll_one(_reg32_poll("9555"))
    assert "bits" not in s._diag_poll_settle[-1]
    s = _scanner(["f855"])
    s._poll_one(_status_poll("f855", wi=0x0022))
    assert "bits" not in s._diag_poll_settle[-1]


def test_eject_completion_record_carries_last_bits(monkeypatch):
    import time
    ticks = iter(i * 0.001 for i in range(10000))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    fake = FakeUsbDevice(reg01=0x22)
    words = iter((0xD9, 0xF9, 0xF8))
    fake.status_high = lambda f: next(words) if f.pulses else None
    scanner = make_scanner(fake)
    assert scanner.eject() == device.EJECT_COMPLETED
    bits = scanner.last_eject["completion"]["last_bits"]
    assert bits["motor_enabled"] == 0 and bits["loader_sensor"] == 1


def test_unconfirmed_eject_record_carries_busy_bits():
    fake = FakeUsbDevice(reg01=0x22)
    fake.status_high = lambda f: 0xF9 if f.pulses else None
    scanner = make_scanner(fake)
    with fast_time(), pytest.raises(safety.EjectIncompleteError):
        scanner.eject()
    assert scanner.last_eject["completion"]["last_bits"]["motor_enabled"] == 1


def test_eject_status_read_error_records_no_bits():
    import usb.core
    fake = FakeUsbDevice(reg01=0x22)
    scanner = make_scanner(fake)

    def fault(ev):
        if ev["kind"] == "ctrl_in" and ev["wv"] == 0x018E and ev["pulses_so_far"]:
            return usb.core.USBError("simulated read failure")
        return None
    fake.fault = fault
    with fast_time(), pytest.raises(usb.core.USBError):
        scanner.eject()
    assert scanner.last_eject["completion"]["last_bits"] is None


def test_cold_move_timeout_message_names_the_bits():
    fake = FakeUsbDevice(reg01=0x00)
    fake.status_high = lambda f: 0xD9 if f.pulses else None
    scanner = make_scanner(fake)
    with fast_time(), pytest.raises(safety.StrictPollTimeoutError) as ei:
        scanner.cold_init()
    msg = str(ei.value)
    assert "d955" in msg and "motor_enabled=1" in msg and "loader_sensor=1" in msg
    assert ei.value.last == bytes.fromhex("d955")


def test_poll_comment_names_bit0_as_motor_enabled():
    import inspect
    src = inspect.getsource(device.Scanner._poll_one)
    assert "motor-enabled bit" in src

# ------------------------------------------------------------ F5 eject text


def test_clear_sensor_before_initialize_is_interpreted_not_claimed():
    fake = FakeUsbDevice(reg01=0x22)
    fake.status_high = 0xE0
    scanner = make_scanner(fake)
    with fast_time():
        assert scanner.eject() == device.EJECT_SKIPPED_NO_MAGAZINE
    rec = scanner.last_eject
    assert rec["interpretation"] == device.EJECT_SENSOR_CLEAR_INTERPRETATION
    assert "fed past the sensor" in rec["interpretation"]
    assert rec["sensor_after_initialize"] is False
    assert fake.out_count == 0


def test_clear_sensor_after_initialize_is_interpreted_and_flagged():
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    scanner = make_scanner(fake)
    with fast_time():
        scanner.initialize()
        fake.status_high = 0xD0
        n = fake.out_count
        assert scanner.eject() == device.EJECT_SKIPPED_NO_MAGAZINE
    assert scanner.last_eject["sensor_after_initialize"] is True
    assert "interpretation" in scanner.last_eject
    assert fake.out_count == n


def test_present_sensor_cold_eject_has_no_interpretation():
    fake = FakeUsbDevice(reg01=0x00)
    fake.status_high = lambda f: 0x48 if f.pulses == 0 else None
    scanner = make_scanner(fake)
    with fast_time():
        assert scanner.eject() == device.EJECT_COMPLETED
    assert "interpretation" not in scanner.last_eject
    assert fake.pulses > 1        # cold_init moves, then the eject move


def test_readonly_session_refuses_eject_before_any_read():
    fake = FakeUsbDevice(reg01=0x22)
    scanner = device.Scanner(UsbIo(fake, readonly=True))
    with fast_time(), pytest.raises(safety.ReadOnlySessionError):
        scanner.eject()
    assert fake.out_count == 0 and fake.in_count == 0


def test_cli_prints_interpretation_instead_of_no_magazine():
    fake = FakeUsbDevice(reg01=0x22, magazine=False)
    with cli_over(fake):
        code, stdout, stderr = _cli(["eject"])
    assert code == 0
    assert "eject NOT run" in stdout
    assert device.EJECT_SENSOR_CLEAR_INTERPRETATION in stdout
    assert "reads no magazine" not in stdout
    assert "ejected" not in stdout
    assert fake.out_count == 0


def test_cli_warning_path_keeps_stderr_and_text(capsys):
    from of135i import cli
    cli._report_eject(device.EJECT_SKIPPED_NO_MAGAZINE)
    out = capsys.readouterr()
    assert out.out == ""
    assert out.err.startswith("warning: eject NOT run")
    assert device.EJECT_SENSOR_CLEAR_INTERPRETATION in out.err

# ------------------------------------------------------------ F6 margin


def test_feedl_guard_is_unchanged():
    assert holder.FEEDL_CEILING == 71490
    assert holder.check_feedl(71490) == 71490
    with pytest.raises(safety.FeedlOutOfRangeError):
        holder.check_feedl(71491)


@pytest.mark.parametrize("end,margin", [(71315, 175), (71490, 0), (71491, -1), (1, 71489)])
def test_travel_margin(end, margin):
    assert holder.travel_margin(end) == margin


@pytest.mark.parametrize("end", [71489, 71490, 71491, 71492])
def test_negative_margin_exactly_when_guard_refuses(end):
    refused = False
    try:
        holder.check_feedl(end)
    except safety.FeedlOutOfRangeError:
        refused = True
    assert refused == (holder.travel_margin(end) < 0)


def test_ceiling_docstring_states_provenance():
    import inspect
    src = inspect.getsource(holder)
    i = src.index("FEEDL_CEILING = 71490")
    doc = " ".join(src[max(0, i - 1500):i].replace("#:", " ").split())
    assert "reverse" in doc and "not proof of forward travel" in doc


def test_slide_travel_end_matches_guard_arithmetic():
    from of135i import image as image_mod
    from of135i.tables_dpi1200 import DEFAULT_LINES, LINES_PER_CHUNK
    geom, wire = holder.slide_dual_geometry(
        4, dpi=1200, lines_per_chunk=LINES_PER_CHUNK,
        colour_crop_lines=image_mod.align_shift(1200), default_wire_lines=DEFAULT_LINES)
    end = holder.slide_travel_end(geom.feedl, wire, 1200)
    assert end == math.ceil(geom.feedl + wire * (7200 // 1200) / 2)
    assert holder.travel_margin(end) >= 175


def _scan_diag(**kw):
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    s = make_scanner(fake)
    init_kw = {k: v for k, v in kw.items() if k in ("ir", "dpi")}
    with fast_time():
        s.initialize(**init_kw)
        s.scan(**kw)
    return s.last_diag


def _dual_geom(frame, dpi, slide):
    from of135i import image as image_mod
    from of135i.device import dual_tables
    t = dual_tables(dpi)
    fn = holder.slide_dual_geometry if slide else holder.dual_overscan_geometry
    return fn(frame, dpi=dpi, lines_per_chunk=t.LINES_PER_CHUNK,
              colour_crop_lines=image_mod.align_shift(dpi),
              default_wire_lines=t.DEFAULT_LINES)


def test_slide_scan_diag_carries_travel_margin():
    d = _scan_diag(frame=4, dpi=1200, slide=True)
    geom, wire = _dual_geom(4, 1200, slide=True)
    assert d["feedl"] == geom.feedl
    assert d["travel_margin_steps"] == holder.FEEDL_CEILING - holder.slide_travel_end(
        geom.feedl, wire, 1200)
    assert d["travel_margin_steps"] >= 175
    assert isinstance(d["travel_margin_steps"], int)


def test_plain_scan_diag_carries_travel_margin():
    d = _scan_diag(frame=1)
    from of135i import image as image_mod
    geom = holder.overscan_geometry(
        1, res_units_per_line=2, chunk_lines=device.tables.IMAGE_CHUNK_LINES,
        colour_crop_lines=image_mod.align_shift(3600))
    assert d["travel_margin_steps"] == holder.FEEDL_CEILING - round(geom.end_hwdpi)


def test_strip_dual_scan_diag_carries_travel_margin():
    d = _scan_diag(frame=1, ir=True)
    geom, _wire = _dual_geom(1, 3600, slide=False)
    assert d["travel_margin_steps"] == holder.FEEDL_CEILING - round(geom.end_hwdpi)
    assert d["travel_margin_steps"] > 0


def test_explicit_lines_path_has_no_modelled_margin():
    d = _scan_diag(frame=1, ir=True, lines=200)
    assert d["travel_margin_steps"] is None


@pytest.mark.parametrize("feedl,wire,dpi,end", [
    (100, 3, 2400, 105),     # 104.5 rounds UP: the guard never under-reports
    (100, 1, 2400, 102),
    (100, 7, 1200, 121),
])
def test_slide_travel_end_rounds_up(feedl, wire, dpi, end):
    assert holder.slide_travel_end(feedl, wire, dpi) == end
