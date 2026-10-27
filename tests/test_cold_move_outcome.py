"""Cold-start motor move: classified timeout outcome, opt-in early failure.

Each of cold_init()'s motor moves waits STRICTLY for the exact idle word
f855 for COLD_MOVE_TIMEOUT (30 s); a timeout ends the cold start, the
session is FAILED and nothing more is sent. f855 stays the only success
value -- f055 (motor stopped, home-sensor bit clear) is never accepted.

Classification (default ON, no timing or acceptance change): the timeout
error (``outcome``) and the session failure record (``"outcome"``) say
what the read history looked like:

  ended-off-sensor  -- busy (motor-enable bit set) was seen, and the last
                       reply has the motor-enable and home-sensor bits clear;
  still-busy        -- the last reply has the motor-enable bit set;
  no-busy-observed  -- busy was never seen;
  malformed         -- the last reply was not 2 bytes with ack 0x55;
  other             -- none of the above (e.g. idle on the sensor but not
                       the exact f855).

Early exit (opt-in, Scanner.cold_move_early_fail / --cold-move-early-fail):
after busy was seen after this move's execute pulse, if every read for
>= 1.0 s is well-formed with the motor-enable and home-sensor bits clear,
raise safety.ColdMoveEndedOffSensorError (a StrictPollTimeoutError) at
once instead of waiting out the 30 s. It is a failure, never a success.

Pure offline logic against fake devices: no USB, no scanner.
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_safety import (FakeUsbDevice, SessionState, _cal_buffers, _cli,  # noqa: E402
                         cli_over, fast_time, make_scanner)

from of135i import cli, device, safety  # noqa: E402
from of135i.device import Scanner  # noqa: E402

STEP = 0.02


@contextmanager
def sim_clock(step: float = STEP):
    """time.monotonic() advancing ``step`` per call, time.sleep(s)
    advancing it by ``s`` without waiting. Yields the state dict."""
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


def _script_first_move(fake, clock, script):
    """Answer the status word for every read after the cold start's FIRST
    execute pulse (and before a second) from ``script``: a list of
    (hex, seconds) -- seconds 0 serves the reply once, None forever,
    otherwise for that much simulated time. Records (t, hex) per read."""
    real = fake.ctrl_transfer
    st = {"i": 0, "since": None, "reads": []}

    def ctrl_transfer(bm, br, wv=0, wi=0, data_or_wLength=None, timeout=None):
        got = real(bm, br, wv, wi, data_or_wLength, timeout)
        if not ((bm & 0x80) and br == 0x04 and wv == 0x018E and (wi >> 8) == 0x01
                and fake.pulses == 1):
            return got
        t = clock["t"]
        while True:
            reply, dur = script[st["i"]]
            if st["since"] is None:
                st["since"] = t
            done = (dur == 0 and t > st["since"]) or (dur not in (0, None)
                                                       and t - st["since"] >= dur)
            if done and st["i"] < len(script) - 1:
                st["i"] += 1
                st["since"] = None
                continue
            break
        st["reads"].append((t, reply))
        return bytes.fromhex(reply)
    fake.ctrl_transfer = ctrl_transfer
    return st


def _cold(script, early=None):
    fake = FakeUsbDevice(reg01=0x00)
    scanner = make_scanner(fake)
    if early is not None:
        scanner.cold_move_early_fail = early
    exc = None
    with sim_clock() as clock:
        st = _script_first_move(fake, clock, script)
        try:
            scanner.cold_init()
        except safety.SafetyError as e:
            exc = e
        end = clock["t"]
    return fake, scanner, exc, st, end


def _first_post_busy_idle(st):
    busy = False
    for t, r in st["reads"]:
        b = bytes.fromhex(r)
        if len(b) == 2 and b[1] == 0x55 and b[0] & 0x01:
            busy = True
        elif busy and r == "f055":
            return t
    raise AssertionError(st["reads"])


def _assert_stopped_after_first_move(fake, scanner, exc, outcome):
    assert isinstance(exc, safety.StrictPollTimeoutError), exc
    assert exc.outcome == outcome
    assert f"outcome={outcome}" in str(exc)
    assert exc.want.hex() == "f855"
    assert scanner.session.state is SessionState.FAILED
    assert scanner.session.failure["outcome"] == outcome
    assert not scanner.session.cold_init_done
    assert fake.pulses == 1                         # no further move started
    last = fake.wire_log[-1]
    assert last["t"] == "cr" and last["wv"] == 0x018E
    n = fake.out_count
    with fast_time():
        with pytest.raises(safety.SessionFailedError):
            scanner.cold_init()
    assert fake.out_count == n
    assert safety.POWER_CYCLE_INSTRUCTION in str(exc)

# ------------------------------------------------------------ table rows


@pytest.mark.parametrize("early", [None, True])
def test_row1_success(early):
    fake, scanner, exc, st, _ = _cold([("d955", 0), ("f855", None)], early)
    assert exc is None, exc
    assert scanner.session.cold_init_done and fake.pulses == 9
    assert [r for _t, r in st["reads"]][:2] == ["d955", "f855"]


def test_row2_classified_ended_off_sensor_after_full_budget():
    fake, scanner, exc, st, end = _cold([("f055", 0), ("d955", 0), ("f055", None)])
    _assert_stopped_after_first_move(fake, scanner, exc, "ended-off-sensor")
    assert type(exc) is safety.StrictPollTimeoutError
    assert exc.last.hex() == "f055"
    assert st["reads"][-1][0] >= device.COLD_MOVE_TIMEOUT


def test_row2_early_exit_about_one_second_after_motor_stopped():
    fake, scanner, exc, st, end = _cold([("f055", 0), ("d955", 0), ("f055", None)], True)
    assert isinstance(exc, safety.ColdMoveEndedOffSensorError), exc
    _assert_stopped_after_first_move(fake, scanner, exc, "ended-off-sensor")
    assert exc.last.hex() == "f055"
    elapsed = st["reads"][-1][0] - _first_post_busy_idle(st)
    assert 1.0 <= elapsed < 1.0 + 3 * STEP, elapsed
    assert st["reads"][-1][0] < 2.0


@pytest.mark.parametrize("early", [None, True])
def test_row3_stale_idle_without_busy_never_exits_early(early):
    fake, scanner, exc, st, _ = _cold([("f055", None)], early)
    _assert_stopped_after_first_move(fake, scanner, exc, "no-busy-observed")
    assert type(exc) is safety.StrictPollTimeoutError
    assert st["reads"][-1][0] >= device.COLD_MOVE_TIMEOUT


@pytest.mark.parametrize("early", [None, True])
def test_row4_still_busy(early):
    fake, scanner, exc, st, _ = _cold([("d155", None)], early)
    _assert_stopped_after_first_move(fake, scanner, exc, "still-busy")
    assert type(exc) is safety.StrictPollTimeoutError
    assert st["reads"][-1][0] >= device.COLD_MOVE_TIMEOUT


@pytest.mark.parametrize("early", [None, True])
def test_row5_short_idle_then_done_succeeds(early):
    fake, scanner, exc, st, _ = _cold([("d955", 0), ("f055", 0.4), ("f855", None)], early)
    assert exc is None, exc
    assert scanner.session.cold_init_done and fake.pulses == 9


@pytest.mark.parametrize("bad", ["f0", "f05a", ""])
@pytest.mark.parametrize("early", [None, True])
def test_row6_malformed_never_counts(bad, early):
    fake, scanner, exc, st, _ = _cold([("d955", 0), (bad, None)], early)
    _assert_stopped_after_first_move(fake, scanner, exc, "malformed")
    assert type(exc) is safety.StrictPollTimeoutError
    assert st["reads"][-1][0] >= device.COLD_MOVE_TIMEOUT

# ------------------------------------------------------------ window details


def test_busy_again_restarts_the_window():
    script = [("d955", 0), ("f055", 0.6), ("d955", 0), ("f055", None)]
    fake, scanner, exc, st, _ = _cold(script, True)
    assert isinstance(exc, safety.ColdMoveEndedOffSensorError), exc
    second = [t for t, r in st["reads"] if r == "d955"][-1]
    assert st["reads"][-1][0] - second >= 1.0


@pytest.mark.parametrize("breaker", ["f0", "f05a", "fc55"])
def test_malformed_or_sensor_set_restarts_the_window(breaker):
    script = [("d955", 0), ("f055", 0.6), (breaker, 0), ("f055", None)]
    fake, scanner, exc, st, _ = _cold(script, True)
    assert isinstance(exc, safety.ColdMoveEndedOffSensorError), exc
    brk = [t for t, r in st["reads"] if r == breaker][-1]
    assert st["reads"][-1][0] - brk >= 1.0


@pytest.mark.parametrize("bad_busy", ["d9", "d95a"])
def test_malformed_busy_does_not_count_as_busy(bad_busy):
    fake, scanner, exc, st, _ = _cold([(bad_busy, 0), ("f055", None)], True)
    _assert_stopped_after_first_move(fake, scanner, exc, "no-busy-observed")
    assert type(exc) is safety.StrictPollTimeoutError
    assert st["reads"][-1][0] >= device.COLD_MOVE_TIMEOUT


def test_idle_on_sensor_but_not_f855_is_other_and_never_early():
    fake, scanner, exc, st, _ = _cold([("d955", 0), ("fc55", None)], True)
    _assert_stopped_after_first_move(fake, scanner, exc, "other")
    assert type(exc) is safety.StrictPollTimeoutError


@pytest.mark.parametrize("early", [None, True])
def test_f055_is_never_success(early):
    _f, scanner, exc, _st, _ = _cold([("d955", 0), ("f055", None)], early)
    assert isinstance(exc, safety.StrictPollTimeoutError)
    assert not scanner.session.cold_init_done


def test_classification_does_not_change_the_wire_or_read_count():
    # Same busy-forever script with the early exit on: busy never ends,
    # so the read count and every transfer must match the default run.
    a, *_ = _cold([("d955", None)])
    b, *_ = _cold([("d955", None)], True)
    c, *_ = _cold([("d955", None)], False)
    assert a.wire_log == b.wire_log == c.wire_log


def test_early_exit_with_a_normal_device_changes_nothing_on_the_wire():
    a, sa, ea, *_ = _cold([("d955", 0), ("f855", None)])
    b, sb, eb, *_ = _cold([("d955", 0), ("f855", None)], True)
    assert ea is None and eb is None and a.wire_log == b.wire_log


def test_non_strict_status_polls_are_not_classified():
    # The early exit watches only the motor-completion waits: a cold
    # start that completes leaves no outcome anywhere.
    _f, scanner, exc, *_ = _cold([("d955", 0), ("f855", None)], True)
    assert exc is None and scanner.session.failure is None

# ------------------------------------------------------------ attribute / exception


def test_exception_hierarchy():
    assert issubclass(safety.ColdMoveEndedOffSensorError, safety.StrictPollTimeoutError)


def test_default_is_off_even_without_init():
    assert Scanner.__new__(Scanner).cold_move_early_fail is False


@pytest.mark.parametrize("bad", ["True", "", 1, 0, None])
def test_non_bool_is_refused(bad):
    s = Scanner.__new__(Scanner)
    with pytest.raises(ValueError):
        s.cold_move_early_fail = bad
    assert s.cold_move_early_fail is False

# ------------------------------------------------------------ CLI


@pytest.mark.parametrize("cmd", [["digitize", "-o", "x"], ["scan", "--frame", "1", "-o", "f.tiff"],
                                 ["eject"]])
def test_parsers_default_off_and_accept_the_flag(cmd):
    parser = cli.build_parser()
    assert parser.parse_args(cmd).cold_move_early_fail is False
    assert parser.parse_args(cmd + ["--cold-move-early-fail"]).cold_move_early_fail is True


@pytest.mark.parametrize("flag", [False, True])
def test_scan_cli_flag_reaches_the_scanner(tmp_path, flag):
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    argv = ["scan", "--frame", "1", "-o", str(tmp_path / "f.tiff"), "--no-diag"]
    if flag:
        argv.append("--cold-move-early-fail")
    with cli_over(fake) as opened:
        code, _out, err = _cli(argv)
    scanners = [o for o in opened if isinstance(o, Scanner)]
    assert scanners and scanners[0].cold_move_early_fail is flag
    assert code == 0, err


@pytest.mark.parametrize("flag", [False, True])
def test_eject_cli_flag_reaches_the_scanner(flag):
    fake = FakeUsbDevice(reg01=0x22)
    argv = ["eject"] + (["--cold-move-early-fail"] if flag else [])
    with cli_over(fake) as opened, fast_time():
        _cli(argv)
    scanners = [o for o in opened if isinstance(o, Scanner)]
    assert scanners and scanners[0].cold_move_early_fail is flag


def test_digitize_sets_the_flag_on_its_scanner(tmp_path):
    fake = FakeUsbDevice(reg01=0x23)
    with cli_over(fake) as opened:
        _cli(["digitize", "-o", str(tmp_path), "--assume-loaded", "--cold-move-early-fail"])
    scanners = [o for o in opened if isinstance(o, Scanner)]
    assert scanners and scanners[0].cold_move_early_fail is True
    assert fake.out_count == 0


def test_cold_eject_via_cli_fails_early_when_opted_in():
    fake = FakeUsbDevice(reg01=0x00)
    # cli_over installs fast_time(); the simulated clock goes inside it.
    with cli_over(fake), sim_clock() as clock:
        _script_first_move(fake, clock, [("d955", 0), ("f055", None)])
        code, _out, err = _cli(["eject", "--cold-move-early-fail"])
    assert clock["t"] < 3.0
    assert code != 0
    assert "ended-off-sensor" in err
    assert fake.pulses == 1
