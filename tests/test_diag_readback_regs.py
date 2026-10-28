"""Opt-in read-only diagnostic: Scanner.diag_readback_regs.

When a calibration read-back poll (extended status word, wValue 0x018e /
wIndex 0x0122, captured with the motor-enabled bit set, inside a
calibration acquisition phase) TIMES OUT, and only then, the driver reads
register 0x100 and register 0x101 once each (IN transfers, nothing else)
and adds the decoded values to the poll_settle / poll_timeout_details
records and the log. Default off: the transfer sequence is unchanged.
The extra reads never change control flow: a failing read is logged and
ignored, and the guard decision still comes from the poll's own last
reply.

Pure offline logic against fake devices: no USB, no scanner.
"""
from __future__ import annotations

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_poll import _reg32_poll, _status_poll  # noqa: E402
from test_safety import (FakeUsbDevice, _cal_buffers, _cli, cli_over,  # noqa: E402
                         fast_time, make_scanner)

from of135i import cli, device, safety  # noqa: E402
from of135i.device import Scanner  # noqa: E402

REG101_WI = 0x0122
REG100_WI = 0x0022


class _RegDev:
    """Fake device: the poll's wire (wIndex 0x0122 or 0x3222) answers
    ``poll_reply``; the extra register reads (wValue 0x018e with wIndex
    0x0022 for reg 0x100, 0x0122 shared with the poll for reg 0x101) are
    told apart by call order via ``extra`` once ``arm_extra`` is set."""

    def __init__(self, poll_reply="ec55", reg100="01" + "55", reg101="ec55",
                 fail=()):
        self.poll_reply = poll_reply
        self.reg100 = reg100
        self.reg101 = reg101
        self.fail = set(fail)
        self.calls = []

    def ctrl_transfer(self, bm, br, wv, wi, length):
        self.calls.append((bm, br, wv, wi, length))
        if wv == 0x018E and wi == REG100_WI:
            if 0x100 in self.fail:
                raise OSError("simulated reg 0x100 read failure")
            return bytes.fromhex(self.reg100)
        if wv == 0x018E and wi == REG101_WI and self._is_extra():
            if 0x101 in self.fail:
                raise OSError("simulated reg 0x101 read failure")
            return bytes.fromhex(self.reg101)
        return bytes.fromhex(self.poll_reply)

    def _is_extra(self):
        # The extra reg 0x101 read is the call right after a reg 0x100 read.
        return len(self.calls) >= 2 and self.calls[-2][3] == REG100_WI


def _scanner(dev, phase="cal_dark_b", guard="log", diag=True):
    s = Scanner.__new__(Scanner)
    s.io = types.SimpleNamespace(dev=dev)
    s._diag_poll_timeouts = 0
    s._diag_poll_timeout_details = []
    s._diag_poll_settle = []
    s._diag_poll_settle_max_s = 0.0
    s.session = types.SimpleNamespace(start_reg01="0x22", snapshot=lambda: {},
                                      phase=phase)
    s.cal_readback_guard = guard
    if diag:
        s.diag_readback_regs = True
    return s


def _extra(dev):
    return [c for c in dev.calls if c[3] == REG100_WI]


# ------------------------------------------------------------ default off

def test_default_is_off_and_validated():
    s = Scanner.__new__(Scanner)
    assert s.diag_readback_regs is False
    with pytest.raises(ValueError):
        s.diag_readback_regs = "yes"
    assert s.diag_readback_regs is False


def test_off_makes_no_extra_read_and_records_no_regs():
    dev = _RegDev()
    s = _scanner(dev, diag=False)
    s._poll_one(_status_poll("ad55"))
    assert _extra(dev) == []
    assert set(dev.calls) == {(0xC0, 0x04, 0x018E, REG101_WI, 2)}
    assert "regs" not in s._diag_poll_settle[-1]
    assert "regs" not in s._diag_poll_timeout_details[-1]


# ------------------------------------------------------------ on, timeout

def test_on_timeout_at_readback_site_reads_exactly_reg100_then_reg101():
    dev = _RegDev(reg100="0155", reg101="ec55")
    s = _scanner(dev)
    s._poll_one(_status_poll("ad55"))
    assert s._diag_poll_settle[-1]["how"] == "timeout"
    assert dev.calls[-2:] == [(0xC0, 0x04, 0x018E, REG100_WI, 2),
                              (0xC0, 0x04, 0x018E, REG101_WI, 2)]
    assert len(_extra(dev)) == 1
    # Everything on the wire is an IN transfer.
    assert all(c[0] & 0x80 for c in dev.calls)
    regs = s._diag_poll_settle[-1]["regs"]
    assert regs["reg100"] == {"raw": "01", "data_enable": 1}
    assert regs["reg101"]["raw"] == "ec"
    assert regs["reg101"]["bits"] == device.decode_status_word(bytes.fromhex("ec55"))
    assert s._diag_poll_timeout_details[-1]["regs"] == regs


def test_data_enable_is_bit0_of_reg100():
    dev = _RegDev(reg100="fe55")
    s = _scanner(dev)
    s._poll_one(_status_poll("ad55"))
    assert s._diag_poll_settle[-1]["regs"]["reg100"] == {"raw": "fe", "data_enable": 0}


def test_extra_reads_come_after_the_last_poll_read_only_once():
    dev = _RegDev()
    s = _scanner(dev)
    s._poll_one(_status_poll("ad55"))
    idx = next(i for i, c in enumerate(dev.calls) if c[3] == REG100_WI)
    assert idx == len(dev.calls) - 2
    assert all(c[3] == REG101_WI for c in dev.calls[:idx])


def test_log_line_carries_the_values(caplog):
    dev = _RegDev(reg100="0155", reg101="ec55")
    s = _scanner(dev)
    with caplog.at_level("WARNING", logger=device.log.name):
        s._poll_one(_status_poll("ad55"))
    text = " ".join(r.getMessage() for r in caplog.records)
    assert "reg100=01" in text and "data_enable=1" in text and "reg101=ec" in text


def test_runs_whatever_the_guard_mode_and_signature():
    # Guard "off", and a last reply that does not match the acq signature.
    for guard in ("off", "log"):
        dev = _RegDev(poll_reply="9c55")
        s = _scanner(dev, guard=guard)
        s._poll_one(_status_poll("bd55"))
        assert len(_extra(dev)) == 1 and "regs" in s._diag_poll_settle[-1]


@pytest.mark.parametrize("phase", sorted(device.CAL_ACQUISITION_PHASES))
def test_every_calibration_acquisition_phase(phase):
    dev = _RegDev()
    s = _scanner(dev, phase=phase)
    s._poll_one(_status_poll("ad55"))
    assert len(_extra(dev)) == 1


def test_fail_mode_still_raises_after_the_extra_reads_are_recorded():
    dev = _RegDev()
    s = _scanner(dev, guard="fail")
    with pytest.raises(safety.CalibrationError):
        s._poll_one(_status_poll("ad55"))
    assert len(_extra(dev)) == 1
    assert "regs" in s._diag_poll_settle[-1]
    assert s._diag_poll_settle[-1]["acq_not_observed"] is True


# ------------------------------------------------------------ on, no extra reads

def test_clean_polls_read_nothing_extra():
    # exact, data-ready (dataenb) and class settles are not timeouts.
    for replies_first, want in (("ad55", "ad55"), ("cd55", "bd55"), ("9c55", "9d55")):
        dev = _RegDev(poll_reply=replies_first)
        s = _scanner(dev)
        s._poll_one(_status_poll(want))
        assert s._diag_poll_settle[-1]["how"] != "timeout"
        assert _extra(dev) == []
        assert "regs" not in s._diag_poll_settle[-1]


def test_timeout_outside_calibration_phase_is_not_affected():
    dev = _RegDev()
    s = _scanner(dev, phase="scan")
    s._poll_one(_status_poll("ad55"))
    assert s._diag_poll_settle[-1]["how"] == "timeout"
    assert _extra(dev) == []
    assert "regs" not in s._diag_poll_settle[-1]


def test_timeout_without_phase_is_not_affected():
    dev = _RegDev()
    s = _scanner(dev)
    del s.session.phase
    s._poll_one(_status_poll("ad55"))
    assert _extra(dev) == []


def test_non_readback_timeouts_are_not_affected():
    # Captured word without the motor-enabled bit: not a read-back site.
    dev = _RegDev(poll_reply="ec55")
    s = _scanner(dev)
    s._poll_one(_status_poll("9c55"))
    assert s._diag_poll_settle[-1]["how"] == "timeout"
    assert _extra(dev) == []
    # Other status index.
    dev = _RegDev()
    s = _scanner(dev)
    s._poll_one(_status_poll("ad55", wi=0x0022))
    assert s._diag_poll_settle[-1]["how"] == "timeout"
    assert set(dev.calls) == {(0xC0, 0x04, 0x018E, REG100_WI, 2)}  # the poll only
    assert "regs" not in s._diag_poll_settle[-1]
    # Reg 0x32 poll.
    dev = _RegDev(poll_reply="0155")
    s = _scanner(dev)
    s._poll_one(_reg32_poll("1855"))
    assert s._diag_poll_settle[-1]["how"] == "timeout"
    assert all(c[3] == 0x3222 for c in dev.calls)


def test_strict_timeout_is_not_affected():
    dev = _RegDev()
    s = _scanner(dev)
    with pytest.raises(safety.StrictPollTimeoutError):
        s._poll_one(_status_poll("ad55"), strict=True)
    assert _extra(dev) == []


# ------------------------------------------------------------ failures ignored

@pytest.mark.parametrize("fail", [{0x100}, {0x101}, {0x100, 0x101}])
def test_read_failure_is_logged_and_ignored(fail, caplog):
    dev = _RegDev(fail=fail)
    s = _scanner(dev, guard="fail")
    with caplog.at_level("WARNING", logger=device.log.name):
        # Same control flow as without the diagnostic: fail mode raises.
        with pytest.raises(safety.CalibrationError):
            s._poll_one(_status_poll("ad55"))
    failures = [r for r in caplog.records if "diagnostic register read failed" in r.getMessage()]
    assert len(failures) == 1
    assert s._diag_poll_settle[-1]["acq_not_observed"] is True
    assert s._diag_poll_timeouts == 1


def test_read_failure_in_log_mode_continues():
    dev = _RegDev(fail={0x100, 0x101})
    s = _scanner(dev)
    s._poll_one(_status_poll("ad55"))
    assert s._diag_poll_settle[-1]["how"] == "timeout"


def test_malformed_extra_reply_is_recorded_as_undecodable():
    dev = _RegDev(reg100="", reg101="ec")
    s = _scanner(dev)
    s._poll_one(_status_poll("ad55"))
    regs = s._diag_poll_settle[-1]["regs"]
    assert regs["reg100"] is None and regs["reg101"] is None


def test_cap_reached_still_reads_and_logs(caplog):
    dev = _RegDev()
    s = _scanner(dev)
    s._diag_poll_settle = [{}] * device._POLL_SETTLE_CAP
    s._poll_one(_status_poll("ad55"))
    assert len(_extra(dev)) == 1
    assert len(s._diag_poll_settle) == device._POLL_SETTLE_CAP


# ------------------------------------------------------------ full flows

def _scan(on):
    # The default fake answers f855 at the read-back sites: timeouts there.
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    scanner = make_scanner(fake)
    if on is not None:
        scanner.diag_readback_regs = on
    with fast_time():
        scanner.initialize()
        scanner.scan(frame=1)
    return fake, scanner


def test_full_scan_default_and_explicit_off_are_wire_identical():
    f_default, s_default = _scan(None)
    f_off, s_off = _scan(False)
    assert f_default.wire_log == f_off.wire_log
    assert not any("regs" in e for e in s_default.last_diag["poll_settle"])


def test_full_scan_on_adds_only_reads_and_only_at_timeouts():
    f_off, s_off = _scan(False)
    f_on, s_on = _scan(True)
    timeouts = [e for e in s_on.last_diag["poll_settle"] if "regs" in e]
    assert timeouts and all(e["how"] == "timeout" for e in timeouts)
    def reg100_reads(log):
        return sum(1 for w in log
                   if w["t"] == "cr" and w["wv"] == 0x018E and w["wi"] == REG100_WI)
    assert reg100_reads(f_on.wire_log) - reg100_reads(f_off.wire_log) == len(timeouts)
    assert len(f_on.wire_log) == len(f_off.wire_log) + 2 * len(timeouts)
    # The "off" log is a subsequence of the "on" log: only reads were added.
    it = iter(f_on.wire_log)
    assert all(any(w == x for x in it) for w in f_off.wire_log)
    added = [w for w in f_on.wire_log if w not in f_off.wire_log]
    assert all(w["t"] == "cr" for w in added)


# ------------------------------------------------------------ CLI

@pytest.mark.parametrize("cmd", [["digitize", "-o", "x"], ["scan", "--frame", "1", "-o", "f.tiff"]])
def test_parsers_default_off_and_accept_flag(cmd):
    parser = cli.build_parser()
    assert parser.parse_args(cmd).diag_readback_regs is False
    assert parser.parse_args(cmd + ["--diag-readback-regs"]).diag_readback_regs is True


@pytest.mark.parametrize("flag,expected", [(False, False), (True, True)])
def test_scan_cli_flag_reaches_the_scanner(tmp_path, flag, expected):
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    argv = ["scan", "--frame", "1", "-o", str(tmp_path / "f.tiff"), "--no-diag"]
    if flag:
        argv.append("--diag-readback-regs")
    with cli_over(fake) as opened:
        code, _out, err = _cli(argv)
    scanners = [o for o in opened if isinstance(o, Scanner)]
    assert scanners and scanners[0].diag_readback_regs is expected
    assert code == 0, err
