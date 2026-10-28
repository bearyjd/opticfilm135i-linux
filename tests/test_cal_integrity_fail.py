"""Fail-closed calibration integrity (--cal-readback-guard fail).

EXPERIMENTAL, unverified on hardware, opt-in (no motor command is added;
the scan only stops earlier). The default "log" mode is unchanged.

In "fail" mode, on top of the existing refusal at a read-back poll whose
acquisition was not observed (before the phase's bulk read), the scan also
stops with safety.CalibrationError when the calibration integrity telemetry
(test_cal_integrity.py) shows, for any calibration acquisition so far:

  stale_suspect            -- re-armed while the previous acquisition still
                              read as running, AND its data came back fast
                              or its read-back saw no acquisition; or
  identical_channel_means  -- the three per-channel sums of the dark pair
                              or a decoded gain check are exactly equal.

The checks run after the dark pair (BEFORE any dark_b substitution or
default-offset fallback is chosen), after the gain-check pair (before the
shading measurement) and after the shading verify (before POSITION).

All numbers are synthetic. Pure offline logic: no USB, no scanner.
"""
from __future__ import annotations

import os
import sys
from collections import deque

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_cal_integrity import (DARK_DISTINCT, DARK_STALE, FRESH_DARK_B,  # noqa: E402
                                STALE_DARK_B, ScriptedFake, _pixels, _residual,
                                _varied, scripted_clock)
from test_safety import _assert_failed_and_frozen, fast_time, make_scanner  # noqa: E402

from of135i import device, safety  # noqa: E402

DUAL_LEN = 6144


def _buffered(script):
    """Every calibration phase's read-back poll answers ad55 (motor on,
    data buffered) unless the script sets it: the fake's default f855 is
    the not-observed signature, which fail mode already refuses."""
    sc = {k: dict(v) for k, v in script.items()}
    for ph in device.CAL_ACQUISITION_PHASES:
        sc.setdefault(ph, {}).setdefault("poll101", 0xAD)
    return sc


def _go(script, dark=None, guard="fail", frames=(1,), dark_fallback="strict", ir=False):
    fake = ScriptedFake(_buffered(script), dark=dark)
    if ir:
        # The dual profile's dark pair and gain checks are 6144-byte reads:
        # distinct, non-residual stand-ins for them.
        n = DUAL_LEN // 6
        fake.cal_buffers[DUAL_LEN] = deque(
            [_varied(2000, n), _varied(4500, n), _varied(9000, n), _varied(9100, n)])
    s = make_scanner(fake)
    fake.scanner = s
    s.cal_readback_guard = guard
    s.dark_fallback = dark_fallback
    exc, diags = None, []
    with fast_time(), scripted_clock(fake):
        try:
            for f in frames:
                s.initialize(ir=ir)
                s.scan(frame=f, ir=ir)
                diags.append(s.last_diag)
                s.recalibrate = True
        except safety.SafetyError as e:
            exc = e
    return s, fake, exc, diags


def _sc(base, **upd):
    sc = {k: dict(v) for k, v in base.items()}
    for ph, d in upd.items():
        sc.setdefault(ph, {}).update(d)
    return sc


def _refused(s, fake, exc, phase, reason):
    assert isinstance(exc, safety.CalibrationError), exc
    assert reason in str(exc), str(exc)
    assert "--cal-readback-guard fail" in str(exc)
    assert s.session.failure["phase"] == phase, s.session.failure["phase"]
    _assert_failed_and_frozen(fake, s, exc, operation="scan", phase_prefix=phase)


# ------------------------------------------------------------ plan rows


def test_row1_not_observed_gain_check_refuses_before_its_bulk_read():
    sc = _sc(FRESH_DARK_B, cal_gain_check_b={"poll101": 0xCC})
    s, fake, exc, _ = _go(sc, dark=DARK_DISTINCT)
    assert isinstance(exc, safety.CalibrationError), exc
    assert s.session.failure["phase"] == "cal_gain_check_b"
    assert fake.wire_log[-1]["t"] == "cr" and fake.wire_log[-1]["wi"] == 0x0122


def test_row2_identical_dark_b_means_refuse_with_no_fallback():
    s, fake, exc, _ = _go(FRESH_DARK_B, dark=DARK_STALE, dark_fallback="default")
    _refused(s, fake, exc, "cal_dark_b", "identical per-channel means")
    assert "cal_dark_b" in str(exc)
    assert s._last_good_dark_b is None          # nothing remembered as healthy
    assert fake.pulses == 2                     # dark_a, dark_b: no white pulse


def test_row2_stale_dark_b_refuses_even_with_distinct_means():
    s, fake, exc, _ = _go(STALE_DARK_B, dark=DARK_DISTINCT, dark_fallback="default")
    _refused(s, fake, exc, "cal_dark_b", "stale")
    assert fake.pulses == 2


def test_row3_healthy_distinct_calibration_continues():
    s, fake, exc, (d,) = _go(FRESH_DARK_B, dark=DARK_DISTINCT)
    assert exc is None, exc
    assert d["cal_integrity"] == "clean"


def test_row4_buffered_white_read_back_is_not_flagged():
    # The white read-back answers with the buffer-empty bit clear: no
    # acquisition-not-observed flag, no refusal.
    sc = _sc(FRESH_DARK_B, cal_white={"poll101": 0x9C})
    s, fake, exc, (d,) = _go(sc, dark=DARK_DISTINCT)
    assert exc is None, exc
    white = [a for a in d["cal_acquisitions"] if a["phase"] == "cal_white"][0]
    assert white["acq_not_observed"] is False


@pytest.mark.parametrize("guard", ["log", "off"])
def test_row5_other_modes_keep_todays_behaviour(guard):
    # Stale + identical dark_b, fallback allowed: log/off modes continue
    # with the default offsets exactly as before.
    s, fake, exc, (d,) = _go(STALE_DARK_B, dark=DARK_STALE, guard=guard,
                             dark_fallback="default")
    assert exc is None, exc
    assert d["dark_b_fallback"] is None or d["dark_b_fallback"] == "default_offset_codes"
    if guard == "log":
        assert d["cal_integrity"] == "degraded"


@pytest.mark.parametrize("guard", ["log", "off"])
def test_row5_wire_unchanged_in_other_modes(guard):
    s1, f1, e1, _ = _go(STALE_DARK_B, dark=DARK_STALE, guard=guard, dark_fallback="default")
    assert e1 is None
    # Compare against the same run with the integrity telemetry switched
    # off (which can never refuse): identical transfers.
    fake = ScriptedFake(_buffered(STALE_DARK_B), dark=DARK_STALE)
    s = make_scanner(fake)
    fake.scanner = s
    s.cal_readback_guard = guard
    s.dark_fallback = "default"
    s.cal_integrity_telemetry = False
    with fast_time(), scripted_clock(fake):
        s.initialize()
        s.scan(frame=1)
    assert f1.wire_log == fake.wire_log


# ------------------------------------------------------------ the other checkpoints


def test_residual_dark_b_on_a_later_frame_is_refused_not_substituted():
    dark = DARK_DISTINCT + [_varied(2000), _residual(), _varied(9000), _varied(9100)]
    s, fake, exc, diags = _go({}, dark=dark, frames=(1, 2))
    assert len(diags) == 1                      # frame 1 completed
    _refused(s, fake, exc, "cal_dark_b", "identical per-channel means")
    # log mode substitutes as today
    s2, _f, exc2, (d1, d2) = _go({}, dark=dark, frames=(1, 2), guard="log")
    assert exc2 is None and d2["dark_b_substituted"] is True


def test_identical_gain_check_means_refuse_before_the_shading_measurement():
    dark = [_varied(2000), _varied(4500), _pixels(9000, 9000, 9000), _varied(9100)]
    s, fake, exc, _ = _go(FRESH_DARK_B, dark=dark)
    _refused(s, fake, exc, "cal_gain_check_b", "cal_gain_check_a: identical")
    assert fake.pulses == 5                     # no shading-measure pulse


def test_stale_shading_verify_refuses_before_position():
    sc = _sc(FRESH_DARK_B, cal_shading_measure={"r100": 0xF1})
    s, fake, exc, _ = _go(sc, dark=DARK_DISTINCT)
    _refused(s, fake, exc, "cal_shading_verify", "cal_shading_verify: stale")
    assert fake.pulses == 7                     # calibration only, no POSITION


def test_dual_path_refuses_too():
    s, fake, exc, _ = _go(STALE_DARK_B, dark=DARK_DISTINCT, ir=True)
    _refused(s, fake, exc, "cal_dark_b", "stale")


def test_fail_mode_needs_the_telemetry_records():
    # With the telemetry switched off (API only) there is nothing to judge:
    # the new checks are silent, the existing read-back refusal still works.
    fake = ScriptedFake(_buffered(STALE_DARK_B), dark=DARK_STALE)
    s = make_scanner(fake)
    fake.scanner = s
    s.cal_readback_guard = "fail"
    s.cal_integrity_telemetry = False
    s.dark_fallback = "default"
    with fast_time(), scripted_clock(fake):
        s.initialize()
        s.scan(frame=1)                         # completes: nothing to judge
    assert s.last_diag["cal_integrity"] is None


@pytest.mark.parametrize("fallback", ["strict", "default"])
def test_the_dark_pair_check_comes_before_the_residual_decision(fallback):
    # A residual first-frame dark_b: today strict mode refuses with the
    # residual message and "default" falls back to default offsets. In fail
    # mode the integrity check speaks first, so neither path is taken.
    dark = [_varied(2000), _residual(), _varied(9000), _varied(9100)]
    s, fake, exc, _ = _go({}, dark=dark, dark_fallback=fallback)
    _refused(s, fake, exc, "cal_dark_b", "after the dark pair")
    assert "residual" not in str(exc)


def test_dual_path_gain_check_checkpoint():
    sc = _sc(FRESH_DARK_B, cal_gain_check_b={"go_delay": 0.009, "bulk_delay": 0.0005})
    sc["cal_gain_check_a"].update({"r100": 0xF1, "r101": 0xDD})
    s, fake, exc, _ = _go(sc, dark=DARK_DISTINCT, ir=True)
    _refused(s, fake, exc, "cal_gain_check_b", "after the gain checks")


def test_dual_path_shading_verify_checkpoint():
    sc = _sc(FRESH_DARK_B, cal_shading_measure={"r100": 0xF1})
    s, fake, exc, _ = _go(sc, dark=DARK_DISTINCT, ir=True)
    _refused(s, fake, exc, "cal_shading_verify", "after the shading verify")
