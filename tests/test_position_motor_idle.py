"""Opt-in POSITION completion that also requires the motor-enable bit clear.

The strict POSITION wait (the per-frame carriage move before SCAN)
accepts a status word whose state class matches the captured completion
(f455) under POSITION_STATUS_MASK (0xF0). That also accepts a class-F
word with the motor-enable bit (0x01) still set, such as f955 -- a value
seen as a busy step of moves in vendor-app traffic, where the vendor app
waits for the motor-enable bit to clear after every positioning move.

Scanner.position_require_motor_idle (default False; CLI
--position-require-motor-idle on scan and digitize):
  False -- today's rule exactly (mask 0xF0); the wire sequence is
           unchanged;
  True  -- the strict POSITION poll uses POSITION_STATUS_MASK_IDLE
           (0xF1): class F AND the motor-enable bit clear. Strictly
           tighter; the budget, floor and FEEDL scale are unchanged; a
           timeout is the existing StrictPollTimeoutError (session
           FAILED, nothing more sent).

Pure offline logic against fake devices: no USB, no scanner.
"""
from __future__ import annotations

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_poll import _scanner, _status_poll  # noqa: E402
from test_safety import (FakeUsbDevice, SessionState, _assert_failed_and_frozen,  # noqa: E402
                         _cal_buffers, _cli, cli_over, fast_time, make_scanner)
from test_wire_baseline import tick_time  # noqa: E402

from of135i import cli, device, safety  # noqa: E402
from of135i.device import Scanner  # noqa: E402

#: plain 3600 plus every dual-light profile (ir, dpi).
PROFILES = [(False, 3600)] + [(True, d) for d in (600, 1200, 2400, 3600, 7200)]
PROFILE_IDS = ["plain3600"] + [f"dual{d}" for d in (600, 1200, 2400, 3600, 7200)]


def _script_position(fake, scanner, script):
    """Answer the status word (reg 0x101) with ``script`` (hex words, the
    last repeated) for every read in the POSITION phase after its execute
    pulse; every other transfer is the fake's own. Returns a dict whose
    "reads" counts the scripted reads."""
    real = fake.ctrl_transfer
    state = {"start": None, "reads": 0}

    def ctrl_transfer(bm, br, wv=0, wi=0, data_or_wLength=None, timeout=None):
        if scanner.session.phase == "position" and state["start"] is None:
            state["start"] = fake.pulses        # before the phase's first transfer
        got = real(bm, br, wv, wi, data_or_wLength, timeout)
        if scanner.session.phase != "position":
            return got
        if (bm & 0x80) and br == 0x04 and wv == 0x018E and (wi >> 8) == 0x01 \
                and fake.pulses > state["start"]:
            i = min(state["reads"], len(script) - 1)
            state["reads"] += 1
            return bytes.fromhex(script[i])
        return got
    fake.ctrl_transfer = ctrl_transfer
    return state


def _run(script, opt_in, ir=False, dpi=3600, frame=1, clock=None):
    """Full initialize + scan with the POSITION wait answered by
    ``script``. ``opt_in`` None leaves the attribute untouched."""
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    scanner = make_scanner(fake)
    if opt_in is not None:
        scanner.position_require_motor_idle = opt_in
    state = _script_position(fake, scanner, script) if script else {"reads": 0}
    exc = None
    with (clock() if clock else fast_time()):
        try:
            if ir:
                scanner.initialize(ir=True, dpi=dpi)
                scanner.scan(frame=frame, ir=True, dpi=dpi)
            else:
                scanner.initialize()
                scanner.scan(frame=frame)
        except safety.SafetyError as e:
            exc = e
    return fake, scanner, exc, state


def _scan_go_sent_after_position(fake, scanner):
    return scanner.session.phase not in (None, "position")

# ------------------------------------------------------------ constants / attribute


def test_masks():
    assert device.POSITION_STATUS_MASK == 0xF0
    assert device.POSITION_STATUS_MASK_IDLE == 0xF1


def test_default_is_off_even_without_init():
    assert Scanner.__new__(Scanner).position_require_motor_idle is False


@pytest.mark.parametrize("bad", ["False", "", 1, 0, None, "yes"])
def test_non_bool_is_refused(bad):
    s = Scanner.__new__(Scanner)
    with pytest.raises(ValueError):
        s.position_require_motor_idle = bad
    assert s.position_require_motor_idle is False


@pytest.mark.parametrize("ir,dpi", PROFILES, ids=PROFILE_IDS)
def test_captured_completion_has_motor_enable_clear(ir, dpi):
    # Under 0xF1 a captured value with bit 0x01 set would mean "wait for
    # the motor to run"; every profile's captured POSITION completion
    # must have it clear for the opt-in rule to mean "motor idle".
    from of135i import tables
    t = device.dual_tables(dpi) if ir else tables
    polls = [op for op in t.POSITION.ops if op.kind == "poll" and op.wv == 0x018E]
    assert polls and all(op.resp[1] == 0x55 and (op.resp[0] & 0xF1) == 0xF0 for op in polls)

# ------------------------------------------------------------ status rule (table rows 3-5)


@pytest.mark.parametrize("got,default,opt_in", [
    ("f455", True, True), ("f055", True, True), ("f855", True, True),
    ("f955", True, False), ("f155", True, False), ("fd55", True, False),
    ("e455", False, False), ("d555", False, False), ("f45a", False, False),
    ("f4", False, False), ("", False, False),
])
def test_status_rule(got, default, opt_in):
    want = bytes.fromhex("f455")
    g = bytes.fromhex(got)
    assert device.status_matches(g, want, device.POSITION_STATUS_MASK) is default
    assert device.status_matches(g, want, device.POSITION_STATUS_MASK_IDLE) is opt_in


@pytest.mark.parametrize("reply", ["f45a", "e455"])
def test_rows_4_5_unit_strict_poll_times_out_under_either_mask(reply):
    for mask in (device.POSITION_STATUS_MASK, device.POSITION_STATUS_MASK_IDLE):
        s = _scanner([reply])
        with pytest.raises(safety.StrictPollTimeoutError) as ei:
            s._poll_one(_status_poll("f455", dur=0.1), strict=True, mask=mask)
        assert ei.value.last.hex() == reply

# ------------------------------------------------------------ full scans (table rows 1-5)


def test_row1_default_accepts_class_f_with_motor_enable_set():
    _f, scanner, exc, st = _run(["d555", "f955", "f455"], None, clock=tick_time)
    assert exc is None, exc
    assert st["reads"] == 2
    assert scanner.session.state is SessionState.ARMED


def test_row1_opt_in_waits_for_motor_enable_clear():
    _f, scanner, exc, st = _run(["d555", "f955", "f455"], True, clock=tick_time)
    assert exc is None, exc
    assert st["reads"] == 3
    assert scanner.session.state is SessionState.ARMED


@pytest.mark.parametrize("ir,dpi", PROFILES, ids=PROFILE_IDS)
def test_row2_default_completes_on_every_profile(ir, dpi):
    _f, scanner, exc, st = _run(["f155"], None, ir=ir, dpi=dpi)
    assert exc is None, exc
    assert st["reads"] == 1
    assert scanner.session.state is SessionState.ARMED


@pytest.mark.parametrize("ir,dpi", PROFILES, ids=PROFILE_IDS)
def test_row2_opt_in_fails_closed_on_every_profile(ir, dpi):
    fake, scanner, exc, _st = _run(["f155"], True, ir=ir, dpi=dpi)
    assert isinstance(exc, safety.StrictPollTimeoutError), exc
    assert exc.last.hex() == "f155" and exc.want.hex() == "f455"
    assert "mask 0xf155" in str(exc)
    assert safety.POWER_CYCLE_INSTRUCTION in str(exc)
    assert scanner.session.failure["phase"] == "position"
    # Nothing after POSITION: the last transfer is the timed-out status read.
    last = fake.wire_log[-1]
    assert last["t"] == "cr" and last["wv"] == 0x018E
    _assert_failed_and_frozen(fake, scanner, exc, operation="scan", phase_prefix="position")


@pytest.mark.parametrize("opt_in", [None, True])
def test_row3_idle_off_sensor_accepted_either_way(opt_in):
    _f, scanner, exc, st = _run(["d555", "f055"], opt_in, clock=tick_time)
    assert exc is None, exc
    assert st["reads"] == 2


def _timeout_in_both_modes(reply, ir=False, dpi=3600, frame=1):
    """Same budget: the same number of reads, the same stated seconds and
    the same wire sequence with and without the opt-in."""
    results = {}
    for opt_in in (None, True):
        fake, scanner, exc, st = _run([reply], opt_in, ir=ir, dpi=dpi, frame=frame,
                                      clock=tick_time)
        assert isinstance(exc, safety.StrictPollTimeoutError), exc
        assert exc.last.hex() == reply
        secs = float(re.search(r"after ([0-9.]+)s", str(exc)).group(1))
        results[opt_in] = (st["reads"], secs, fake.wire_log)
        _assert_failed_and_frozen(fake, scanner, exc, operation="scan", phase_prefix="position")
    assert results[None] == results[True]
    assert results[None][0] > 10
    return results[True][1]


@pytest.mark.parametrize("reply", ["f45a", "e455"])
def test_rows_4_5_time_out_identically_in_both_modes(reply):
    _timeout_in_both_modes(reply)


@pytest.mark.parametrize("ir,dpi", PROFILES, ids=PROFILE_IDS)
def test_opt_in_keeps_the_feedl_scaled_budget_on_every_profile(ir, dpi):
    # Frame 4 is a move more than five times frame 1's: the stated budget
    # must scale the same way in opt-in mode as in default mode.
    s1 = _timeout_in_both_modes("e455", ir=ir, dpi=dpi, frame=1)
    s4 = _timeout_in_both_modes("e455", ir=ir, dpi=dpi, frame=4)
    assert s4 > 4.5 * s1, (s1, s4)

# ------------------------------------------------------------ row 6: wire sequence


def test_row6_explicit_false_is_the_untouched_default_on_the_wire():
    a, *_ = _run(["d555", "f955", "f455"], None, clock=tick_time)
    b, *_ = _run(["d555", "f955", "f455"], False, clock=tick_time)
    assert a.wire_log == b.wire_log


@pytest.mark.parametrize("ir,dpi", PROFILES, ids=PROFILE_IDS)
def test_opt_in_with_idle_device_changes_nothing_on_the_wire(ir, dpi):
    # The fake's own completion (f855) has the motor-enable bit clear.
    a, sa, ea, _ = _run(None, None, ir=ir, dpi=dpi)
    b, sb, eb, _ = _run(None, True, ir=ir, dpi=dpi)
    assert ea is None and eb is None
    assert a.wire_log == b.wire_log

# ------------------------------------------------------------ CLI


@pytest.mark.parametrize("flag,expected", [(False, False), (True, True)])
def test_scan_cli_flag_reaches_the_scanner(tmp_path, flag, expected):
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    argv = ["scan", "--frame", "1", "-o", str(tmp_path / "f.tiff"), "--no-diag"]
    if flag:
        argv.append("--position-require-motor-idle")
    with cli_over(fake) as opened:
        code, _out, err = _cli(argv)
    scanners = [o for o in opened if isinstance(o, Scanner)]
    assert scanners and scanners[0].position_require_motor_idle is expected
    assert code == 0, err


@pytest.mark.parametrize("cmd", [["digitize", "-o", "x"], ["scan", "--frame", "1", "-o", "f.tiff"]])
def test_parsers_default_off_and_accept_the_flag(cmd):
    parser = cli.build_parser()
    assert parser.parse_args(cmd).position_require_motor_idle is False
    assert parser.parse_args(cmd + ["--position-require-motor-idle"]).position_require_motor_idle is True


def test_digitize_sets_the_flag_on_its_scanner(tmp_path):
    # Unsafe start state: digitize stops at its start-state check, after
    # its body has configured the scanner. Nothing is written.
    fake = FakeUsbDevice(reg01=0x23)
    with cli_over(fake) as opened:
        _cli(["digitize", "-o", str(tmp_path), "--assume-loaded",
              "--position-require-motor-idle"])
    scanners = [o for o in opened if isinstance(o, Scanner)]
    assert scanners and scanners[0].position_require_motor_idle is True
    assert fake.out_count == 0
