"""Calibration read-back "acquisition not observed" guard.

At the calibration read-back sites the capture settled the extended
status word (wValue 0x018e, wIndex 0x0122) with the motor-enabled bit
(0x01) set and the buffer-empty bit (0x40) clear (ad55, bd55, a955).
When a non-strict poll there has already timed out under its unchanged
budget and the last reply is a well-formed word with the motor-enabled
bit CLEAR and the buffer-empty bit SET, the engine stopped with nothing
in its buffer: the measurement that the next bulk read collects was most
likely never acquired (an inference from hardware logs and vendor-app
traffic, not yet confirmed on this driver).

Scanner.cal_readback_guard:
  "off"  -- today's behaviour, no record;
  "log"  -- (default) today's behaviour plus acq_not_observed=True on the
            poll_settle record; the wire sequence is unchanged;
  "fail" -- opt-in: raise safety.CalibrationError at that point, before
            the phase's next transfer (no bulk read, no further write);
            the session is FAILED like every other calibration refusal.

The poll budget and floors are untouched; the guard only ever refuses.
Pure offline logic against fake devices: no USB, no scanner.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_poll import _scanner, _status_poll  # noqa: E402
from test_safety import (FakeUsbDevice, _assert_failed_and_frozen,  # noqa: E402
                         _cal_buffers, _cli, cli_over, fast_time, make_scanner)

from of135i import cli, device, safety  # noqa: E402
from of135i.device import Scanner  # noqa: E402


def _cal_scanner(replies, phase, mode):
    s = _scanner(replies)
    s.session.phase = phase
    s.cal_readback_guard = mode
    return s


def _flags(s):
    return [e.get("acq_not_observed") for e in s._diag_poll_settle]

# ------------------------------------------------------------ table rows


def test_row1_fail_mode_refuses_engine_stopped_buffer_empty():
    s = _cal_scanner(["ec55"], "cal_dark_b", "fail")
    with pytest.raises(safety.CalibrationError) as ei:
        s._poll_one(_status_poll("ad55"))
    msg = str(ei.value)
    assert "acquisition not observed" in msg and "ec55" in msg
    assert safety.POWER_CYCLE_INSTRUCTION in msg
    assert s._diag_poll_settle[-1]["how"] == "timeout"
    assert s._diag_poll_settle[-1]["acq_not_observed"] is True


def test_row2_log_mode_flags_and_continues():
    s = _cal_scanner(["ec55"], "cal_dark_b", "log")
    s._poll_one(_status_poll("ad55"))
    e = s._diag_poll_settle[-1]
    assert e["how"] == "timeout" and e["acq_not_observed"] is True


def test_row3_buffer_not_empty_is_never_flagged():
    s = _cal_scanner(["9c55"], "cal_white", "fail")
    s._poll_one(_status_poll("bd55"))
    assert _flags(s) == [None]


def test_row4_data_seen_before_deadline_settles_exact():
    s = _cal_scanner(["ec55", "ad55"], "cal_dark_a", "fail")
    s._poll_one(_status_poll("ad55"))
    assert s._diag_poll_settle[-1]["how"] == "exact"
    assert _flags(s) == [None]


def test_row5_outside_calibration_phases_never_flagged():
    s = _cal_scanner(["dc55"], "scan", "fail")
    s._poll_one(_status_poll("ad55"))
    assert s._diag_poll_settle[-1]["how"] == "timeout"
    assert _flags(s) == [None]


@pytest.mark.parametrize("reply", ["ec00", "ec", "", "ec5500"])
def test_row6_malformed_reply_is_out_of_scope(reply):
    s = _cal_scanner([reply], "cal_dark_b", "fail")
    s._poll_one(_status_poll("ad55"))
    assert _flags(s) == [None]

# ------------------------------------------------------------ other conditions


def test_off_mode_records_nothing():
    s = _cal_scanner(["ec55"], "cal_dark_b", "off")
    s._poll_one(_status_poll("ad55"))
    assert "acq_not_observed" not in s._diag_poll_settle[-1]


@pytest.mark.parametrize("want", ["9c55", "f955", "ad00"])
def test_captured_value_must_show_motor_on_and_buffer_not_empty(want):
    # 9c55: bit 0x01 clear; f955: bit 0x40 set; ad00: not a status word.
    s = _cal_scanner(["ec55"], "cal_dark_b", "fail")
    s._poll_one(_status_poll(want))
    assert _flags(s) == [None]


def test_other_status_index_never_flagged():
    s = _cal_scanner(["ec55"], "cal_dark_b", "fail")
    s._poll_one(_status_poll("ad55", wi=0x0022))
    assert _flags(s) == [None]


@pytest.mark.parametrize("reply", ["ac55", "ed55", "e855"])
def test_each_bit_of_the_signature_is_required(reply):
    # ac55: buffer-empty clear; ed55: motor-enabled set (accepted by the
    # data-ready rule, never times out); e855: the flagging signature.
    s = _cal_scanner([reply], "cal_dark_b", "fail")
    if reply == "e855":
        with pytest.raises(safety.CalibrationError):
            s._poll_one(_status_poll("ad55"))
    else:
        s._poll_one(_status_poll("ad55"))
        assert _flags(s) == [None]


@pytest.mark.parametrize("phase", sorted(device.CAL_ACQUISITION_PHASES))
def test_every_acquisition_phase_is_covered(phase):
    s = _cal_scanner(["ec55"], phase, "fail")
    with pytest.raises(safety.CalibrationError):
        s._poll_one(_status_poll("ad55"))


def test_fail_mode_still_refuses_when_settle_record_is_full():
    s = _cal_scanner(["ec55"], "cal_dark_b", "fail")
    s._diag_poll_settle = [{}] * device._POLL_SETTLE_CAP
    with pytest.raises(safety.CalibrationError):
        s._poll_one(_status_poll("ad55"))


def test_budget_is_unchanged_by_the_guard():
    import time
    for mode in ("off", "log"):
        s = _cal_scanner(["ec55"], "cal_dark_b", mode)
        t0 = time.monotonic()
        s._poll_one(_status_poll("ad55"))
        elapsed = time.monotonic() - t0
        assert device.NONSTRICT_POLL_FLOOR_S * 0.8 <= elapsed < device.NONSTRICT_POLL_FLOOR_S + 0.3

# ------------------------------------------------------------ attribute


def test_default_mode_is_log():
    assert Scanner.cal_readback_guard == "log"
    s = Scanner.__new__(Scanner)
    assert s.cal_readback_guard == "log"


@pytest.mark.parametrize("bad", ["", "FAIL", "on", None, True])
def test_invalid_mode_is_refused(bad):
    s = Scanner.__new__(Scanner)
    with pytest.raises(ValueError):
        s.cal_readback_guard = bad
    assert s.cal_readback_guard == "log"

# ------------------------------------------------------------ full flows


def _scan(mode):
    # The default fake answers f855 at the read-back sites (captured
    # ad55/bd55/a955): motor-enabled clear, buffer-empty set.
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    scanner = make_scanner(fake)
    if mode is not None:
        scanner.cal_readback_guard = mode
    exc = None
    with fast_time():
        try:
            scanner.initialize()
            scanner.scan(frame=1)
        except safety.SafetyError as e:
            exc = e
    return fake, scanner, exc


def test_log_mode_scan_completes_with_flags_and_identical_wire():
    fake_default, s_default, exc = _scan(None)
    assert exc is None
    flagged = [e for e in s_default.last_diag["poll_settle"] if e.get("acq_not_observed")]
    assert flagged, "the fake's f855 at the read-back sites must be flagged in log mode"
    fake_off, s_off, exc = _scan("off")
    assert exc is None
    assert not any("acq_not_observed" in e for e in s_off.last_diag["poll_settle"])
    assert fake_default.wire_log == fake_off.wire_log


def test_fail_mode_stops_at_the_read_back_poll_and_freezes():
    fake_log, _s, exc = _scan("log")
    assert exc is None
    fake, scanner, exc = _scan("fail")
    assert isinstance(exc, safety.CalibrationError), exc
    n = len(fake.wire_log)
    assert fake.wire_log == fake_log.wire_log[:n] and n < len(fake_log.wire_log)
    last = fake.wire_log[-1]
    assert last["t"] == "cr" and last["wv"] == 0x018E and last["wi"] == 0x0122
    # The log-mode run's very next transfer is the one fail mode withheld.
    assert fake_log.wire_log[n]["t"] in ("bi", "cw", "cr", "bo")
    _assert_failed_and_frozen(fake, scanner, exc, operation="scan", phase_prefix="cal_")

# ------------------------------------------------------------ CLI


@pytest.mark.parametrize("argv_mode,expected", [(None, "log"), ("off", "off"),
                                                ("log", "log"), ("fail", "fail")])
def test_scan_cli_flag_reaches_the_scanner(tmp_path, argv_mode, expected):
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    argv = ["scan", "--frame", "1", "-o", str(tmp_path / "f.tiff"), "--no-diag"]
    if argv_mode is not None:
        argv += ["--cal-readback-guard", argv_mode]
    with cli_over(fake) as opened:
        code, _out, err = _cli(argv)
    scanners = [o for o in opened if isinstance(o, Scanner)]
    assert scanners and scanners[0].cal_readback_guard == expected
    if expected == "fail":
        assert code != 0 and "acquisition not observed" in err
    else:
        assert code == 0, err


def test_cli_rejects_unknown_mode(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["scan", "--frame", "1", "-o", str(tmp_path / "f.tiff"),
                  "--cal-readback-guard", "maybe"])


@pytest.mark.parametrize("cmd", [["digitize", "-o", "x"], ["scan", "--frame", "1", "-o", "f.tiff"]])
def test_parsers_default_to_log_and_accept_fail(cmd):
    parser = cli.build_parser()
    assert parser.parse_args(cmd).cal_readback_guard == "log"
    assert parser.parse_args(cmd + ["--cal-readback-guard", "fail"]).cal_readback_guard == "fail"
