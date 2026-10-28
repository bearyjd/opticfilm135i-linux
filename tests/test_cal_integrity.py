"""Calibration stale-read telemetry and the per-frame integrity verdict.

Default-on DIAGNOSTICS ONLY. For every calibration acquisition (one
execute pulse inside a calibration acquisition phase) the driver records,
from transfers it already makes plus a host clock (time.perf_counter,
which no poll loop uses):

  rearm_gap_s     -- the previous acquisition's SCAN-clear write to this
                     acquisition's execute pulse;
  prev_post       -- the previous acquisition's existing post-clear reads
                     of register 0x100 and register 0x101, decoded;
  prev_running    -- data-enable (register 0x100 bit 0) or motor-enable
                     (register 0x101 bit 0) set in either of them;
  go_to_data_s    -- execute pulse to the end of the last bulk read;
  served_fast     -- go_to_data_s below CAL_SERVED_FAST_S (inferred);
  acq_not_observed-- the read-back poll timed out with the motor stopped
                     and the buffer empty;
  identical_channel_means -- dark pair and decoded gain checks only;
  stale_suspect   -- prev_running AND (served_fast OR acq_not_observed).

Each frame gets cal_integrity = "clean" | "degraded" (any stale_suspect,
the dark_b fallback or the dark_b substitution) and, when degraded, ONE
log WARNING. Exit code, control flow and wire traffic are unchanged.

All numbers below are synthetic. Pure offline logic: no USB, no scanner.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import sys
import time
from collections import deque
from contextlib import contextmanager

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_safety import FakeUsbDevice, _cal_buffers, fast_time, make_scanner  # noqa: E402
from test_wire_baseline import tick_time  # noqa: E402

from of135i import device  # noqa: E402

DARK_LEN = 3072          # bulk length of every dark / gain-check acquisition


def _pixels(r, g, b, n=DARK_LEN // 6):
    return np.tile(np.array([r, g, b], dtype="<u2"), n).tobytes()


def _varied(base, n=DARK_LEN // 6):
    """A dark-like buffer with many distinct values and DIFFERENT per-channel
    means (not residual, not identical)."""
    i = np.arange(n, dtype=np.int64)
    px = np.stack([base + (i * 7) % 97, base + 900 + (i * 5) % 89,
                   base + 400 + (i * 3) % 83], axis=1).astype("<u2")
    return px.tobytes()


def _residual(n=DARK_LEN // 6):
    """A short repeating block of a few values (calibrate.dark_is_residual)."""
    vals = np.array([100, 101, 102, 103], dtype="<u2")
    return np.resize(vals, n * 3).astype("<u2").tobytes()


class ScriptedFake(FakeUsbDevice):
    """FakeUsbDevice plus a per-phase script and a simulated host clock.

    The clock only moves on transfers (``go_delay`` before a phase's
    execute-pulse write, ``bulk_delay`` before each of its bulk reads), so
    every interval the driver measures is exact whatever number of clock
    reads it takes. Per phase: ``r100``/``r101`` answer the post-clear
    reads of register 0x100/0x101, ``poll101`` answers register 0x101 reads
    between the execute pulse and the SCAN-clear."""

    def __init__(self, script, dark=None, **kw):
        bufs = _cal_buffers()
        if dark is not None:
            bufs[DARK_LEN] = deque(dark)
        super().__init__(reg01=0x22, cal_buffers=bufs, **kw)
        self.script = script
        self.clock = 1000.0
        self.scanner = None
        self._stage: dict[str, str] = {}

    def _phase(self):
        return getattr(getattr(self.scanner, "session", None), "phase", None)

    def ctrl_transfer(self, bm, br, wv=0, wi=0, data_or_wLength=None, timeout=None):
        ph = self._phase()
        sc = self.script.get(ph, {})
        if not (bm & 0x80) and wv == 0x0083:
            data = bytes(data_or_wLength or b"")
            pairs = [(data[i], data[i + 1]) for i in range(0, len(data) - 1, 2)]
            if (0x0F, 0x01) in pairs:
                self.clock += sc.get("go_delay", 0.0)
                self._stage[ph] = "running"
            elif (0x01, 0x02) in pairs and self._stage.get(ph) == "running":
                self._stage[ph] = "cleared"
        if (bm & 0x80) and br == 0x04 and wv == 0x018E and wi in (0x0022, 0x0122):
            super().ctrl_transfer(bm, br, wv, wi, data_or_wLength, timeout)
            stage = self._stage.get(ph)
            if wi == 0x0022:
                return bytes([sc.get("r100", 0xF8) if stage == "cleared" else 0xF8, 0x55])
            if stage == "cleared" and "r101" in sc:
                return bytes([sc["r101"], 0x55])
            if stage == "running" and "poll101" in sc:
                return bytes([sc["poll101"], 0x55])
            return bytes([0xF8, 0x55])
        return super().ctrl_transfer(bm, br, wv, wi, data_or_wLength, timeout)

    def read(self, endpoint, size_or_buffer, timeout=None):
        if endpoint != 0x83:
            self.clock += self.script.get(self._phase(), {}).get("bulk_delay", 0.0)
        return super().read(endpoint, size_or_buffer, timeout)


@contextmanager
def scripted_clock(fake):
    real = time.perf_counter
    time.perf_counter = lambda: fake.clock
    try:
        yield
    finally:
        time.perf_counter = real


def _run(script, dark=None, frames=(1,), dark_fallback="strict", telemetry=True,
         guard="log"):
    fake = ScriptedFake(script, dark=dark)
    s = make_scanner(fake)
    fake.scanner = s
    s.dark_fallback = dark_fallback
    s.cal_readback_guard = guard
    s.cal_integrity_telemetry = telemetry
    diags = []
    with fast_time(), scripted_clock(fake):
        for f in frames:
            s.initialize()
            s.scan(frame=f)
            diags.append(s.last_diag)
            s.recalibrate = True
    return s, fake, diags


def _acq(diag, phase, n=0):
    return [a for a in diag["cal_acquisitions"] if a["phase"] == phase][n]


def _warnings(caplog):
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and "calibration integrity" in r.getMessage()]


# Synthetic stand-ins for the timing table: a stale re-arm 7 ms after the
# clear with data 3.5 ms after GO; a fresh one 84.6 ms / 37.7 ms.
STALE_DARK_B = {
    "cal_dark_a": {"r100": 0xF1, "r101": 0xDD, "go_delay": 0.010, "bulk_delay": 0.036},
    "cal_dark_b": {"go_delay": 0.007, "bulk_delay": 0.0035, "poll101": 0xCD,
                   "r100": 0xF6, "r101": 0xCD},
    "cal_white": {"go_delay": 0.060, "bulk_delay": 0.040, "poll101": 0xDD},
    "cal_gain_check_a": {"go_delay": 0.070, "bulk_delay": 0.030, "poll101": 0xDD},
    "cal_gain_check_b": {"go_delay": 0.070, "bulk_delay": 0.030, "poll101": 0xDD},
}
FRESH_DARK_B = {
    "cal_dark_a": {"r100": 0xF1, "r101": 0xDC, "go_delay": 0.010, "bulk_delay": 0.036},
    "cal_dark_b": {"go_delay": 0.0846, "bulk_delay": 0.0377, "poll101": 0xDD,
                   "r100": 0xF1, "r101": 0xDC},
    "cal_white": {"go_delay": 0.060, "bulk_delay": 0.040, "poll101": 0xDD},
    "cal_gain_check_a": {"go_delay": 0.070, "bulk_delay": 0.030, "poll101": 0xDD},
    "cal_gain_check_b": {"go_delay": 0.070, "bulk_delay": 0.030, "poll101": 0xDD},
}
DARK_DISTINCT = [_varied(2000), _varied(4500), _varied(9000), _varied(9100)]
DARK_STALE = [_varied(2000), _pixels(26000, 26000, 26000), _varied(9000), _varied(9100)]


# ------------------------------------------------------- the table rows


def test_stale_dark_b_is_suspect_and_degrades_the_frame(caplog):
    caplog.set_level(logging.WARNING, logger="of135i")
    _, _, (d,) = _run(STALE_DARK_B, dark=DARK_STALE)
    a = _acq(d, "cal_dark_b")
    assert a["rearm_gap_s"] == pytest.approx(0.007, abs=1e-9)
    assert a["prev_phase"] == "cal_dark_a"
    assert a["prev_post"]["reg_0x100"] == "f1"
    assert a["prev_post"]["data_enabled"] == 1
    assert a["prev_post"]["reg_0x101"] == "dd"
    assert a["prev_post"]["motor_enabled"] == 1
    assert a["prev_running"] is True
    assert a["go_to_data_s"] == pytest.approx(0.0035, abs=1e-9)
    assert a["served_fast"] is True
    assert a["acq_not_observed"] is False
    assert a["identical_channel_means"] is True
    assert a["stale_suspect"] is True
    assert d["cal_integrity"] == "degraded"
    assert any("cal_dark_b" in r and "stale" in r for r in d["cal_integrity_reasons"])
    w = _warnings(caplog)
    assert len(w) == 1, [r.getMessage() for r in w]
    assert "cal_dark_b" in w[0].getMessage()


def test_fresh_dark_b_is_clean(caplog):
    caplog.set_level(logging.WARNING, logger="of135i")
    _, _, (d,) = _run(FRESH_DARK_B, dark=DARK_DISTINCT)
    a = _acq(d, "cal_dark_b")
    assert a["rearm_gap_s"] == pytest.approx(0.0846, abs=1e-9)
    assert a["go_to_data_s"] == pytest.approx(0.0377, abs=1e-9)
    # Data-enable still reads set after the clear in healthy runs too: the
    # verdict comes from the slow (fresh) data, not from prev_running.
    assert a["prev_running"] is True
    assert a["served_fast"] is False
    assert a["acq_not_observed"] is False
    assert a["identical_channel_means"] is False
    assert a["stale_suspect"] is False
    assert all(x["stale_suspect"] is False for x in d["cal_acquisitions"])
    assert d["cal_integrity"] == "clean"
    assert d["cal_integrity_reasons"] == []
    assert _warnings(caplog) == []


def _gain_timeout_script(bulk_delay):
    sc = {k: dict(v) for k, v in FRESH_DARK_B.items()}
    sc["cal_white"].update({"r100": 0xF1, "r101": 0xDD})
    sc["cal_gain_check_a"] = {"go_delay": 0.013, "bulk_delay": bulk_delay, "poll101": 0xCC}
    return sc


@pytest.mark.parametrize("guard", ["log", "off"])
def test_gain_check_not_observed_after_running_white_is_suspect(caplog, guard):
    caplog.set_level(logging.WARNING, logger="of135i")
    # Slow data, so only acq_not_observed can make it suspect.
    _, _, (d,) = _run(_gain_timeout_script(0.030), dark=DARK_DISTINCT, guard=guard)
    a = _acq(d, "cal_gain_check_a")
    assert a["prev_phase"] == "cal_white"
    assert a["prev_running"] is True
    assert a["served_fast"] is False
    assert a["acq_not_observed"] is True
    assert a["stale_suspect"] is True
    assert d["cal_integrity"] == "degraded"
    assert len(_warnings(caplog)) == 1


def test_instant_gain_check_after_running_white_is_suspect():
    sc = {k: dict(v) for k, v in FRESH_DARK_B.items()}
    sc["cal_white"].update({"r100": 0xF0, "r101": 0xDD})       # motor-enable only
    sc["cal_gain_check_a"] = {"go_delay": 0.0098, "bulk_delay": 0.0008, "poll101": 0xCD}
    _, _, (d,) = _run(sc, dark=DARK_DISTINCT)
    a = _acq(d, "cal_gain_check_a")
    assert a["prev_post"]["data_enabled"] == 0
    assert a["prev_post"]["motor_enabled"] == 1
    assert a["prev_running"] is True
    assert a["served_fast"] is True
    assert a["acq_not_observed"] is False
    assert a["stale_suspect"] is True
    assert d["cal_integrity"] == "degraded"


def test_not_observed_without_running_previous_is_not_suspect(caplog):
    # The fake's default answers: every read-back poll times out with the
    # motor stopped and the buffer empty, data comes back instantly, but
    # nothing was running after any clear -> benign, clean, no warning.
    caplog.set_level(logging.WARNING, logger="of135i")
    _, _, (d,) = _run({})
    accs = d["cal_acquisitions"]
    assert [a["phase"] for a in accs][:5] == [
        "cal_dark_a", "cal_dark_b", "cal_white", "cal_gain_check_a", "cal_gain_check_b"]
    assert accs[0]["rearm_gap_s"] is None and accs[0]["prev_post"] is None
    assert accs[0]["prev_running"] is None
    assert accs[1]["acq_not_observed"] is True
    assert accs[1]["served_fast"] is True
    assert accs[1]["prev_running"] is False
    assert all(a["stale_suspect"] is False for a in accs)
    assert d["cal_integrity"] == "clean"
    assert _warnings(caplog) == []


def test_identical_means_alone_does_not_degrade():
    sc = {k: dict(v) for k, v in FRESH_DARK_B.items()}
    _, _, (d,) = _run(sc, dark=DARK_STALE)
    a = _acq(d, "cal_dark_b")
    assert a["identical_channel_means"] is True
    assert a["stale_suspect"] is False
    assert d["cal_integrity"] == "clean"


def test_channel_means_recorded_for_dark_pair_and_gain_checks_only():
    _, _, (d,) = _run(FRESH_DARK_B, dark=DARK_DISTINCT)
    by = {a["phase"]: a["identical_channel_means"] for a in d["cal_acquisitions"]}
    for ph in ("cal_dark_a", "cal_dark_b", "cal_gain_check_a", "cal_gain_check_b"):
        assert by[ph] is False, ph
    for ph in ("cal_white", "cal_shading_measure", "cal_shading_verify"):
        assert by[ph] is None, ph


def test_served_fast_threshold_is_the_named_constant():
    assert device.CAL_SERVED_FAST_S == pytest.approx(0.010)
    for delay, fast in ((0.0099, True), (0.0101, False)):
        sc = {k: dict(v) for k, v in STALE_DARK_B.items()}
        sc["cal_dark_b"]["bulk_delay"] = delay
        _, _, (d,) = _run(sc, dark=DARK_STALE)
        a = _acq(d, "cal_dark_b")
        assert a["served_fast"] is fast, delay
        assert a["stale_suspect"] is fast, delay


@pytest.mark.parametrize("r100,r101,running", [
    (0xF0, 0xDC, False), (0xF1, 0xDC, True), (0xF0, 0xDD, True), (0xF1, 0xDD, True),
    (0xFE, 0xFE, False),
])
def test_prev_running_is_either_enable_bit(r100, r101, running):
    sc = {k: dict(v) for k, v in STALE_DARK_B.items()}
    sc["cal_dark_a"].update({"r100": r100, "r101": r101})
    _, _, (d,) = _run(sc, dark=DARK_STALE)
    a = _acq(d, "cal_dark_b")
    assert a["prev_running"] is running
    assert a["stale_suspect"] is running
    assert d["cal_integrity"] == ("degraded" if running else "clean")


def test_prev_post_is_decoded_in_words():
    _, _, (d,) = _run(STALE_DARK_B, dark=DARK_STALE)
    p = _acq(d, "cal_dark_b")["prev_post"]
    assert p["status_bits"]["motor_enabled"] == 1
    assert p["status_bits"]["buffer_empty"] == 1
    assert "data-enable on" in p["words"] and "motor-enable on" in p["words"]


# ------------------------------------------- residual fallback/substitution


def test_dark_b_fallback_degrades_the_frame(caplog):
    caplog.set_level(logging.WARNING, logger="of135i")
    dark = [_varied(2000), _residual(), _varied(9000), _varied(9100)]
    _, _, (d,) = _run({}, dark=dark, dark_fallback="default")
    assert d["dark_b_fallback"] == "default_offset_codes"
    assert all(a["stale_suspect"] is False for a in d["cal_acquisitions"])
    assert d["cal_integrity"] == "degraded"
    assert any("fallback" in r for r in d["cal_integrity_reasons"])
    assert len(_warnings(caplog)) == 1


def test_dark_b_substitution_degrades_only_that_frame(caplog):
    caplog.set_level(logging.WARNING, logger="of135i")
    dark = DARK_DISTINCT + [_varied(2000), _residual(), _varied(9000), _varied(9100)]
    _, _, (d1, d2) = _run({}, dark=dark, frames=(1, 2))
    assert d1["cal_integrity"] == "clean"
    assert d2["dark_b_substituted"] is True
    assert d2["cal_integrity"] == "degraded"
    assert any("substitut" in r for r in d2["cal_integrity_reasons"])
    assert len(_warnings(caplog)) == 1


def test_cached_calibration_carries_its_verdict(caplog):
    caplog.set_level(logging.WARNING, logger="of135i")
    fake = ScriptedFake(STALE_DARK_B, dark=DARK_STALE)
    s = make_scanner(fake)
    fake.scanner = s
    with fast_time(), scripted_clock(fake):
        s.initialize()
        s.scan(frame=1)
        d1 = s.last_diag
        s.initialize()
        s.scan(frame=2)
        d2 = s.last_diag
    assert d2["calibration"] == "cached"
    assert d2["cal_acquisitions"] == []
    assert d1["cal_integrity"] == d2["cal_integrity"] == "degraded"
    assert any("cached" in r for r in d2["cal_integrity_reasons"])
    assert len(_warnings(caplog)) == 2          # one per frame


def test_dual_path_records_acquisitions_and_verdict():
    fake = ScriptedFake({})
    s = make_scanner(fake)
    fake.scanner = s
    with fast_time(), scripted_clock(fake):
        s.initialize(ir=True)
        s.scan(frame=1, ir=True)
    d = s.last_diag
    assert d["dual"] is True
    assert [a["phase"] for a in d["cal_acquisitions"]][:2] == ["cal_dark_a", "cal_dark_b"]
    assert d["cal_integrity"] == "clean"


def test_diag_is_json_serialisable():
    _, _, (d,) = _run(STALE_DARK_B, dark=DARK_STALE)
    json.dumps({k: d[k] for k in ("cal_integrity", "cal_integrity_reasons",
                                  "cal_acquisitions")})
    assert not any(k.startswith("_") for a in d["cal_acquisitions"] for k in a)


# --------------------------------------------------- telemetry on / off


def test_default_is_on_and_setter_validates():
    s = make_scanner(FakeUsbDevice())
    assert s.cal_integrity_telemetry is True
    with pytest.raises(ValueError):
        s.cal_integrity_telemetry = "yes"


def test_off_records_nothing_and_never_warns(caplog):
    caplog.set_level(logging.WARNING, logger="of135i")
    _, _, (d,) = _run(STALE_DARK_B, dark=DARK_STALE, telemetry=False)
    assert d["cal_integrity"] is None
    assert d["cal_acquisitions"] == []
    assert _warnings(caplog) == []


def _digest(wire_log):
    canon = [{k: (v.hex() if isinstance(v, (bytes, bytearray)) else v)
              for k, v in sorted(e.items())} for e in wire_log]
    return hashlib.sha256(json.dumps(canon, sort_keys=True).encode()).hexdigest()


@contextmanager
def counted_clock(step=0.01):
    """tick_time-style clock that also logs every monotonic/sleep call."""
    calls = []
    state = {"t": 0.0}

    def mono():
        state["t"] += step
        calls.append("m")
        return state["t"]

    def sleep(s):
        state["t"] += max(0.0, float(s))
        calls.append(("s", round(float(s), 9)))
    real_sleep, real_mono = time.sleep, time.monotonic
    time.sleep, time.monotonic = sleep, mono
    try:
        yield calls
    finally:
        time.sleep, time.monotonic = real_sleep, real_mono


def _flow_plain(s):
    s.initialize()
    s.scan(frame=1)


def _flow_dual(s):
    s.initialize(ir=True)
    s.scan(frame=1, ir=True)


@pytest.mark.parametrize("flow", [_flow_plain, _flow_dual])
@pytest.mark.parametrize("script", [{}, STALE_DARK_B])
def test_telemetry_cannot_change_traffic_or_clock_calls(flow, script):
    results = []
    for on in (True, False):
        fake = ScriptedFake(script, dark=list(DARK_STALE))
        s = make_scanner(fake)
        fake.scanner = s
        s.cal_integrity_telemetry = on
        with counted_clock() as calls, scripted_clock(fake):
            flow(s)
        results.append((_digest(fake.wire_log), len(fake.wire_log), calls))
    assert results[0][:2] == results[1][:2]
    assert results[0][2] == results[1][2]


def test_telemetry_on_off_identical_under_fast_time():
    digests = []
    for on in (True, False):
        fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
        s = make_scanner(fake)
        s.cal_integrity_telemetry = on
        with fast_time():
            s.initialize()
            s.scan(frame=1)
        digests.append((_digest(fake.wire_log), len(fake.wire_log)))
    assert digests[0] == digests[1]


def test_telemetry_on_off_identical_under_tick_time():
    digests = []
    for on in (True, False):
        fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
        s = make_scanner(fake)
        s.cal_integrity_telemetry = on
        with tick_time(0.01):
            s.initialize()
            s.scan(frame=1)
        digests.append((_digest(fake.wire_log), len(fake.wire_log)))
    assert digests[0] == digests[1]


# --------------------------------------------------------- pure verdict


def test_stale_suspect_truth_table():
    f = device.cal_stale_suspect
    for running in (None, False, True):
        for fast in (False, True):
            for nobs in (False, True):
                rec = {"prev_running": running, "served_fast": fast,
                       "acq_not_observed": nobs}
                assert f(rec) is bool(running is True and (fast or nobs)), rec


def test_verdict_function():
    v = device.cal_integrity_verdict
    clean = [{"phase": "cal_dark_a", "stale_suspect": False}]
    assert v(clean, False, None) == ("clean", [])
    st, rs = v([{"phase": "cal_dark_b", "stale_suspect": True,
                 "rearm_gap_s": 0.007, "go_to_data_s": 0.0035}], False, None)
    assert st == "degraded" and len(rs) == 1
    assert v(clean, True, None)[0] == "degraded"
    assert v(clean, False, "default_offset_codes")[0] == "degraded"
