"""Opt-in buffer-empty gate before a calibration bulk read
(Scanner.cal_wait_bufempty).

EXPERIMENTAL, unverified on hardware. At the calibration read-back sites
(extended status word, wValue 0x018e, wIndex 0x0122) the capture settled
with the buffer-empty bit (0x40) CLEAR: data was buffered before the bulk
read. Today the poll there also ends on the motor-enable bit (0x01) alone,
or on a state-class match, so the bulk read can start while the buffer is
still empty. Vendor-app traffic instead waits for the buffer-empty bit to
clear before the bulk read.

Scanner.cal_wait_bufempty:
  "off"     -- (default) today's behaviour; the transfer sequence is
               byte-identical;
  "enforce" -- at a calibration acquisition phase's read-back poll whose
               captured word is a well-formed 2-byte word with the
               buffer-empty bit clear, only a 2-byte reply with the 0x55
               acknowledge and the buffer-empty bit clear ends the poll (the
               motor-enable and state-class rules no longer do). The budget
               is the poll's existing one; at its deadline
               safety.CalibrationError is raised before the bulk read, and
               the session is FAILED.

All status values are synthetic. Pure offline logic: no USB, no scanner.
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_poll import _scanner, _status_poll  # noqa: E402
from test_safety import (FakeUsbDevice, _assert_failed_and_frozen,  # noqa: E402
                         _cal_buffers, _cli, cli_over, make_scanner)
from test_wire_baseline import BASELINE, _digest  # noqa: E402

from of135i import cli, device, safety  # noqa: E402
from of135i.device import Scanner  # noqa: E402

FLOOR = device.NONSTRICT_POLL_FLOOR_S


@contextmanager
def sim_time(step: float = 0.01):
    state = {"t": 0.0}

    def mono():
        state["t"] += step
        return state["t"]

    def sleep(seconds):
        state["t"] += max(0.0, float(seconds))
    real_sleep, real_mono = time.sleep, time.monotonic
    time.sleep, time.monotonic = sleep, mono
    try:
        yield state
    finally:
        time.sleep, time.monotonic = real_sleep, real_mono


def _poll(replies, want, phase, mode="enforce", dur=0.016):
    s = _scanner(replies)
    s.session.phase = phase
    if mode is not None:
        s.cal_wait_bufempty = mode
    exc = None
    with sim_time(0.01) as clock:
        try:
            s._poll_one(_status_poll(want, dur=dur))
        except safety.SafetyError as e:
            exc = e
    return s, exc, clock["t"]


def _how(s):
    return [e["how"] for e in s._diag_poll_settle]


# ------------------------------------------------------------ table rows (C1)


def test_row1_waits_through_motor_on_buffer_empty_words():
    s, exc, _ = _poll(["cd55", "cd55", "ed55", "ad55"], "bd55", "cal_dark_b")
    assert exc is None
    assert s.io.dev.calls == 4
    assert _how(s) == ["bufempty_clear"]


def test_row2_off_mode_accepts_the_motor_on_word_as_today():
    s, exc, _ = _poll(["cd55", "cd55", "ed55", "ad55"], "bd55", "cal_dark_b", mode="off")
    assert exc is None
    assert s.io.dev.calls == 1
    assert _how(s) == ["dataenb"]


def test_row3_never_buffered_raises_at_the_existing_budget():
    s, exc, t = _poll(["cc55"], "bd55", "cal_gain_check_a")
    assert isinstance(exc, safety.CalibrationError), exc
    assert "buffer" in str(exc).lower() and "cal_gain_check_a" in str(exc)
    assert _how(s) == ["timeout"]
    assert FLOOR - 0.03 <= t <= FLOOR + 0.05


def test_row4_off_mode_times_out_and_continues_as_today():
    s, exc, t = _poll(["cc55"], "bd55", "cal_gain_check_a", mode="off")
    assert exc is None
    assert _how(s) == ["timeout"]


def test_budget_is_the_poll_existing_budget():
    # dur 0.2 s: today's budget is 3 x dur = 0.6 s, above the floor.
    s_off, _, t_off = _poll(["cc55"], "bd55", "cal_gain_check_a", mode="off", dur=0.2)
    s_on, exc, t_on = _poll(["cc55"], "bd55", "cal_gain_check_a", dur=0.2)
    assert isinstance(exc, safety.CalibrationError)
    assert s_on.io.dev.calls == s_off.io.dev.calls
    assert t_on == pytest.approx(t_off, abs=0.02)
    assert t_on >= 0.6 - 0.03


def test_row5_buffer_ready_on_the_first_read():
    s, exc, _ = _poll(["9d55"], "bd55", "cal_dark_a")
    assert exc is None and s.io.dev.calls == 1
    assert _how(s) == ["bufempty_clear"]


def test_row6_shading_measure_site():
    s, exc, _ = _poll(["c955", "e955", "a955"], "a955", "cal_shading_measure")
    assert exc is None and s.io.dev.calls == 3


@pytest.mark.parametrize("bad", ["ad00", "ad", "ad5500", "ad54"])
def test_row7_malformed_reply_is_not_accepted(bad):
    s, exc, _ = _poll([bad], "bd55", "cal_dark_b")
    assert isinstance(exc, safety.CalibrationError), (bad, exc)


@pytest.mark.parametrize("phase", ["scan", "position", "prep", None])
def test_row8_not_applied_outside_calibration(phase):
    s, exc, _ = _poll(["cd55", "ad55"], "ad55", phase)
    assert exc is None and s.io.dev.calls == 1
    assert _how(s) == ["dataenb"]


@pytest.mark.parametrize("want", ["dc55", "f855", "d855", "bd00", "bd"])
def test_sites_whose_captured_word_shows_buffer_empty_are_not_gated(want):
    # Captured with the buffer-empty bit set (idle checks), or malformed.
    s_off, e_off, _ = _poll(["dd55"], want, "cal_white", mode="off")
    s_on, e_on, _ = _poll(["dd55"], want, "cal_white")
    assert e_off is None and e_on is None
    assert s_on.io.dev.calls == s_off.io.dev.calls
    assert _how(s_on) == _how(s_off)


@pytest.mark.parametrize("mode", ["off", "enforce"])
def test_other_status_index_is_not_gated(mode):
    # Register 0x100 (wIndex 0x0022) captured with bit 0x40 clear: not a
    # buffer-empty site, so a buffer-empty-looking reply is not refused.
    s = _scanner(["f155"])
    s.session.phase = "cal_dark_a"
    s.cal_wait_bufempty = mode
    with sim_time(0.01):
        s._poll_one(_status_poll("bd55", wi=0x0022))
    assert _how(s) == ["timeout"]


def test_strict_completion_polls_are_not_gated():
    s = _scanner(["cd55"])
    s.session.phase = "cal_dark_b"
    s.cal_wait_bufempty = "enforce"
    with sim_time(0.01):
        s._poll_one(_status_poll("bd55"), strict=True, mask=0x01)   # motor-enable only
    assert s.io.dev.calls == 1


def test_buffer_empty_word_with_motor_on_is_never_enough():
    s, exc, _ = _poll(["cd55"], "bd55", "cal_dark_b")
    assert isinstance(exc, safety.CalibrationError)


# ------------------------------------------------------------ full scans


def _scan(mode, script=None, ir=False, step=0.01):
    """Full scan. Calibration phases answer ad55 (buffered) at register
    0x101 unless ``script`` = {phase: callable(n) -> high byte} overrides a
    phase (n = 0-based count of that phase's 0x101 reads)."""
    exc = None
    with sim_time(step):
        fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
        s = make_scanner(fake)
        counts: dict = {}
        cal = device.CAL_ACQUISITION_PHASES

        def high(f):
            p = s.session.phase
            if script and p in script:
                n = counts.get(p, 0)
                counts[p] = n + 1
                return script[p](n)
            return 0xAD if p in cal else None
        fake.status_high = high
        if mode is not None:
            s.cal_wait_bufempty = mode
        try:
            s.initialize(ir=ir)
            s.scan(frame=1, ir=ir)
        except safety.SafetyError as e:
            exc = e
    return fake, s, exc


def test_full_scan_row1_bulk_follows_the_fourth_read():
    seq = [0xCD, 0xCD, 0xED, 0xAD]
    fake, s, exc = _scan("enforce", {"cal_dark_b": lambda n: seq[min(n, 3)]})
    assert exc is None, exc
    # Locate dark_b's execute pulse and its first bulk read in the wire log.
    log = fake.wire_log
    gos = [i for i, e in enumerate(log) if e["t"] == "cw" and e["wv"] == 0x0083
           and any(e["data"][j] == 0x0F and e["data"][j + 1] == 0x01
                   for j in range(0, len(e["data"]) - 1, 2))]
    go = gos[1]                                   # dark_a, dark_b, ...
    bi = next(i for i in range(go, len(log)) if log[i]["t"] == "bi")
    reads = [e for e in log[go:bi] if e["t"] == "cr" and e["wv"] == 0x018E
             and e["wi"] == 0x0122]
    assert len(reads) == 4


def test_full_scan_row3_refuses_with_no_bulk_read():
    fake_ok, _, exc = _scan("enforce")
    assert exc is None
    fake, s, exc = _scan("enforce", {"cal_gain_check_a": lambda n: 0xCC})
    assert isinstance(exc, safety.CalibrationError), exc
    # The last transfer is the read-back poll: the bulk read was withheld.
    assert fake.wire_log[-1]["t"] == "cr" and fake.wire_log[-1]["wi"] == 0x0122
    assert s.session.failure["phase"] == "cal_gain_check_a"
    # Up to the refused phase the traffic matches the healthy run.
    first = next(i for i, e in enumerate(fake_ok.wire_log) if e != fake.wire_log[i])
    assert all(e["t"] == "cr" for e in fake.wire_log[first:])
    _assert_failed_and_frozen(fake, s, exc, operation="scan",
                              phase_prefix="cal_gain_check_a")


@pytest.mark.parametrize("ir", [False, True], ids=["plain", "dual"])
@pytest.mark.parametrize("phase", sorted(device.CAL_ACQUISITION_PHASES))
def test_every_calibration_read_back_site_is_gated(phase, ir):
    fake, s, exc = _scan("enforce", {phase: lambda n: 0xCD}, ir=ir)
    assert isinstance(exc, safety.CalibrationError), (phase, ir, exc)
    assert s.session.failure["phase"] == phase
    assert fake.wire_log[-1]["t"] == "cr" and fake.wire_log[-1]["wi"] == 0x0122


def test_scan_phase_is_not_gated():
    fake, s, exc = _scan("enforce", {"scan": lambda n: 0xCD})
    assert exc is None, exc


def test_enforce_is_identical_when_data_is_already_buffered():
    f_off, _, e1 = _scan("off")
    f_on, _, e2 = _scan("enforce")
    assert e1 is None and e2 is None
    assert f_on.wire_log == f_off.wire_log


def test_default_is_off_and_matches_the_stored_baseline():
    with sim_time(0.01):
        fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
        s = make_scanner(fake)
        assert s.cal_wait_bufempty == "off"
        s.initialize()
        s.scan(frame=1)
    assert (_digest(fake.wire_log), len(fake.wire_log)) == BASELINE["multi_plain_3600_frame1"]


def test_off_mode_is_identical_to_default_on_motor_on_words():
    script = {"cal_dark_b": lambda n: 0xCD}
    f_default, _, e1 = _scan(None, script)
    f_off, _, e2 = _scan("off", script)
    assert e1 is None and e2 is None
    assert f_default.wire_log == f_off.wire_log


# ------------------------------------------------------------ property and CLI


def test_default_and_validation():
    s = make_scanner(FakeUsbDevice(reg01=0x22))
    assert s.cal_wait_bufempty == "off"
    s.cal_wait_bufempty = "enforce"
    assert s.cal_wait_bufempty == "enforce"
    for bad in ("observe", "on", True, None, ""):
        with pytest.raises(ValueError):
            s.cal_wait_bufempty = bad


@pytest.mark.parametrize("cmd", [["digitize", "-o", "x"], ["scan", "--frame", "1", "-o", "f.tiff"]])
def test_parsers_default_off_and_accept_enforce(cmd):
    parser = cli.build_parser()
    assert parser.parse_args(cmd).cal_wait_bufempty == "off"
    assert parser.parse_args(cmd + ["--cal-wait-bufempty", "enforce"]).cal_wait_bufempty == "enforce"
    with pytest.raises(SystemExit):
        parser.parse_args(cmd + ["--cal-wait-bufempty", "observe"])


def test_help_text_says_experimental():
    parser = cli.build_parser()
    for sub in ("scan", "digitize"):
        sp = parser._subparsers._group_actions[0].choices[sub]
        act = next(a for a in sp._actions if "--cal-wait-bufempty" in a.option_strings)
        assert "experimental" in act.help.lower() and "unverified" in act.help.lower()


@pytest.mark.parametrize("argv_mode,expected", [(None, "off"), ("off", "off"),
                                                ("enforce", "enforce")])
def test_scan_cli_flag_reaches_the_scanner(tmp_path, argv_mode, expected):
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    argv = ["scan", "--frame", "1", "-o", str(tmp_path / "f.tiff"), "--no-diag"]
    if argv_mode is not None:
        argv += ["--cal-wait-bufempty", argv_mode]
    with cli_over(fake) as opened:
        code, _out, err = _cli(argv)
    scanners = [o for o in opened if isinstance(o, Scanner)]
    assert scanners and scanners[0].cal_wait_bufempty == expected
    if expected == "enforce":
        # The default fake answers f855 (buffer empty) at the read-back
        # sites: enforce refuses at the first one.
        assert code != 0 and "buffer" in err.lower()
    else:
        assert code == 0, err


def test_digitize_sets_the_mode_on_its_scanner(tmp_path):
    fake = FakeUsbDevice(reg01=0x23)
    with cli_over(fake) as opened:
        _cli(["digitize", "-o", str(tmp_path), "--assume-loaded",
              "--cal-wait-bufempty", "enforce"])
    scanners = [o for o in opened if isinstance(o, Scanner)]
    assert scanners and scanners[0].cal_wait_bufempty == "enforce"
    assert fake.out_count == 0
