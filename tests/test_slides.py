#!/usr/bin/env python3
"""Offline tests for the mounted-slide scan path and the opt-in dark_b
fallback (docs/slide-scanning.md).

Plain asserts, no pytest dependency. Run with:
    PYTHONPATH=. python3 tests/test_slides.py

Slides: the geometry (holder.SLIDE_FIDUCIAL / slide_dual_geometry) is the
strip path's own dual geometry with a different fiducial model, so these
pin (a) the documented arithmetic, (b) the per-profile FEEDL grid, (c) the
refusals before any hardware activity, (d) the device wiring (same phases,
slide FEEDL/line count), and (e) the CLI contract: -f<N> naming, the full
overscan pair as the product, no coverage verdict, exit 0.

dark_b fallback: strict (default) is unchanged and fails closed; the
opt-in "default" mode substitutes calibrate._OFFSET_DEFAULT only in the
residual-and-nothing-healthy case, and records it -- also on a later frame
that reuses the cached calibration.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from of135i import calibrate, holder, image, safety, tables
from of135i.device import Scanner, dual_tables

DPIS = (600, 1200, 2400, 3600, 7200)
MM = holder.MM_PER_UNIT


def _slide_geom(slide, dpi, overscan=holder.SLIDE_OVERSCAN_MM):
    t = dual_tables(dpi)
    return holder.slide_dual_geometry(
        slide, dpi=dpi, lines_per_chunk=t.LINES_PER_CHUNK,
        colour_crop_lines=image.align_shift(dpi),
        default_wire_lines=t.DEFAULT_LINES, overscan_mm=overscan)


def _strip_geom(frame, dpi, overscan=holder.OVERSCAN_MM):
    t = dual_tables(dpi)
    return holder.dual_overscan_geometry(
        frame, dpi=dpi, lines_per_chunk=t.LINES_PER_CHUNK,
        colour_crop_lines=image.align_shift(dpi),
        default_wire_lines=t.DEFAULT_LINES, overscan_mm=overscan)


def _raises(exc, fn, *a, **k):
    try:
        fn(*a, **k)
    except exc as e:
        return e
    raise AssertionError(f"expected {exc.__name__}")


# --------------------------------------------------------------- geometry


def test_slide_model_matches_documented_numbers():
    """The 600-dual anchor K is the table module's; strip frame 1 maps to
    Test 84's FEEDL 6519; the slide openings land on 23.5 + (n-1) x 62.6
    mm in Test 84's coordinate; the shift from strip frame 1 is -0.248."""
    t600 = dual_tables(600)
    k =t600.DEFAULT_LINES * ((7200 // 600) / 2) / 2
    assert holder.DUAL600_START_ANCHOR_HWDPI == k == 5292, k
    g, _ = _strip_geom(1, 600)
    assert g.feedl == 6519, g.feedl          # Test 84's frame-1 ledger value
    assert abs(holder.SLIDE_SHIFT_MM - (-0.248)) < 0.001, holder.SLIDE_SHIFT_MM
    for n, start in enumerate((23.5, 86.1, 148.7, 211.3), start=1):
        lead = holder.SLIDE_FIDUCIAL.leading_hwdpi(n)
        got = (lead + holder.DUAL600_START_ANCHOR_HWDPI) * MM
        assert abs(got - start) < 0.01, (n, got, start)
        trail = holder.SLIDE_FIDUCIAL.trailing_hwdpi(n)
        assert abs((trail - lead) * MM - holder.SLIDE_OPENING_MM) < 1e-9
    # Test 84's measured openings 2-4 (86.12 / 148.68 / 211.35): within
    # 0.06 mm of the model.
    for n, meas in ((2, 86.12), (3, 148.68), (4, 211.35)):
        got = (holder.SLIDE_FIDUCIAL.leading_hwdpi(n)
               + holder.DUAL600_START_ANCHOR_HWDPI) * MM
        assert abs(got - meas) < 0.06, (n, got, meas)
    assert holder.SLIDE.frames == 4 and holder.SLIDE_OVERSCAN_MM == 1.5
    print("test_slide_model_matches_documented_numbers OK")


def test_slide_feedl_grid_every_profile():
    """FEEDL(N) = FEEDL(1) + (N-1) x 17744.88 (+-1 rounding) on every
    profile; the window covers the 38.05 mm opening plus the overscan on
    both sides; registers and parity as for strips."""
    for dpi in DPIS:
        t = dual_tables(dpi)
        over = holder.SLIDE_OVERSCAN_MM
        g1, _ = _slide_geom(1, dpi, over)
        for n in range(1, 5):
            g, wire = _slide_geom(n, dpi, over)
            want = g1.feedl + (n - 1) * holder.SLIDE_PITCH_HWDPI
            assert abs(g.feedl - want) <= 1, (dpi, n, g.feedl, want)
            assert g.leading_margin_mm == over, (dpi, n)
            assert g.trailing_margin_mm >= over - 1e-9, (dpi, n, g.trailing_margin_mm)
            span_mm = g.delivered_lines * (7200 // dpi) * MM
            assert span_mm >= holder.SLIDE_OPENING_MM + 2 * over - 1e-9, (dpi, n, span_mm)
            assert wire == g.chunks * t.LINES_PER_CHUNK and wire % 2 == 0
            assert g.feedl + wire * (7200 // dpi) / 2 <= holder.FEEDL_CEILING, (dpi, n)
    print("test_slide_feedl_grid_every_profile OK")


#: docs/slide-scanning.md's table at the default overscan: per profile,
#: slide -> (commanded FEEDL, FEEDL + wire travel).
_DEFAULT_LEDGER = {
    600: {1: (6236, 17996), 2: (23981, 35741), 3: (41726, 53486), 4: (59471, 71231)},
    1200: {1: (6272, 18080), 2: (24017, 35825), 3: (41762, 53570), 4: (59507, 71315)},
    2400: {1: (6260, 17948), 2: (24005, 35693), 3: (41750, 53438), 4: (59495, 71183)},
    3600: {1: (6255, 17951), 2: (24000, 35696), 3: (41745, 53441), 4: (59490, 71186)},
    7200: {1: (6256, 17944), 2: (24001, 35689), 3: (41746, 53434), 4: (59491, 71179)},
}


def test_slide_ledger_default_every_profile():
    """With the default overscan ALL four slides pass validation on every
    profile (600, 3600 and 7200 included), and the numbers are the ones
    quoted in docs/slide-scanning.md."""
    from of135i import cli
    for dpi, want in _DEFAULT_LEDGER.items():
        got = {}
        for n in range(1, 5):
            g, wire = _slide_geom(n, dpi)
            got[n] = (g.feedl, round(g.feedl + wire * (7200 // dpi) / 2))
            assert got[n][1] <= holder.FEEDL_CEILING
        assert got == want, (dpi, got)
        assert cli._validate_overscan(holder.SLIDE_OVERSCAN_MM, [1, 2, 3, 4],
                                      dpi=dpi, dual=True, slide=True) is None, dpi
    print("test_slide_ledger_default_every_profile OK")


def test_slide_geometry_refusals():
    """Slide numbers outside 1-4, and any window whose FEEDL + travel
    passes FEEDL_CEILING, are refused by the geometry itself."""
    for bad in (0, 5, 6):
        _raises(safety.FrameOutOfRangeError, _slide_geom, bad, 3600)
    # Explicit overscans whose FEEDL + travel passes 71490 at slide 4: at
    # 600 dual the 588-step chunk rounding makes 2.0 / 2.5 too much.
    for dpi, ov in ((600, 2.0), (600, 2.5), (1200, 2.5), (3600, 3.0), (7200, 3.0)):
        e = _raises(safety.FeedlOutOfRangeError, _slide_geom, 4, dpi, ov)
        assert e.ceiling == holder.FEEDL_CEILING
        _slide_geom(3, dpi, ov)                   # slide 3 is far inside
    # The strip path is untouched by the stricter slide-only check.
    for dpi in DPIS:
        for frame in range(1, 7):
            _strip_geom(frame, dpi)
    print("test_slide_geometry_refusals OK")


# ----------------------------------------------------------- device wiring


def _feedl_lines(runs):
    pos = next(inj for ph, inj in runs if ph.name == "position")
    scan_inj = next(inj for ph, inj in runs if ph.name == "scan")
    feedl = (pos["feedl_hi"][0] << 16) | (pos["feedl_mid"][0] << 8) | pos["feedl_lo"][0]
    lines = ((scan_inj["lines_top"][0] << 16)
             | (scan_inj["lines_hi"][0] << 8) | scan_inj["lines_lo"][0])
    return feedl, lines


def test_slide_wiring_same_phases_slide_geometry():
    """_scan_dual(slide=True) runs the SAME phase sequence as a strip
    frame and only the injected FEEDL / line count differ."""
    from tests.test_dual_overscan import _RecordingDualScanner
    for dpi in (600, 3600):
        t = dual_tables(dpi)
        over = 1.25 if dpi == 600 else 2.0     # explicit, non-default
        strip = _RecordingDualScanner(t)
        strip.s._scan_dual(t, frame=1)
        for n in (1, 2, 3):
            rec = _RecordingDualScanner(t)
            rec.s._scan_dual(t, frame=n, overscan_mm=over, slide=True)
            assert [p.name for p, _ in rec.runs] == [p.name for p, _ in strip.runs], dpi
            g, wire = _slide_geom(n, dpi, over)
            assert _feedl_lines(rec.runs) == (g.feedl, wire), (dpi, n)
            d = rec.s.last_diag
            assert d["slide"] == n and d["holder"] == holder.SLIDE.name
            assert d["overscan_mm"] == over and d["feedl"] == g.feedl
        # The strip record carries no slide keys (unchanged schema).
        assert "slide" not in strip.s.last_diag and "holder" not in strip.s.last_diag
        g, wire = _strip_geom(1, dpi)
        assert _feedl_lines(strip.runs) == (g.feedl, wire)
    print("test_slide_wiring_same_phases_slide_geometry OK")


def test_device_scan_slide_refusals_send_nothing():
    """scan(slide=True) on the plain profile, or with lines=, is refused
    before the operation: zero writes, session not failed."""
    from tests.test_calibrate import MockUsbIo
    s = Scanner(MockUsbIo({}))
    s._prepared_for_scan = True
    for kw in ({"slide": True},                                    # plain 3600
               {"slide": True, "ir": True, "lines": 196}):
        _raises(ValueError, s.scan, frame=1, **kw)
        assert s.session.writes == 0, s.session.writes
        assert s.session.state is not safety.SessionState.FAILED
    s.dark_fallback = "lenient"
    _raises(ValueError, s.scan, frame=1, ir=True)
    assert s.session.writes == 0
    print("test_device_scan_slide_refusals_send_nothing OK")


def test_line_count_encode_guard():
    """check_line_count, applied right before every line-count encode:
    rejects < 1 and > 24 bits, returns every valid value unchanged -- in
    particular every strip and slide geometry the driver can produce."""
    from of135i import device
    from of135i.usbio import Of135iError
    for bad in (0, -1, -490, 0x1000000):
        _raises(Of135iError, device.check_line_count, bad)
    for ok in (1, 98, 0xFFFFFF):
        assert device.check_line_count(ok) == ok
    for dpi in DPIS:
        for frame in range(1, 7):
            assert device.check_line_count(_strip_geom(frame, dpi)[1]) > 0
        for n in range(1, 5):
            assert device.check_line_count(_slide_geom(n, dpi)[1]) > 0
    for frame in range(1, 7):
        g = holder.overscan_geometry(
            frame, res_units_per_line=2, chunk_lines=tables.IMAGE_CHUNK_LINES,
            colour_crop_lines=image.align_shift(3600))
        n = tables.scan_lines_for_chunks(g.chunks)
        assert device.check_line_count(n) == n
    # The slide geometry itself refuses before a count could go negative.
    for ov in (-25, -1, 0, float("inf")):
        _raises(ValueError, _slide_geom, 1, 600, ov)
    print("test_line_count_encode_guard OK")


def test_device_scan_slide_geometry_refused_before_any_phase():
    """Public scan(): an invalid slide number or a window past the travel
    ceiling is refused BEFORE the operation -- no phase runs (no
    calibration command), zero writes, the session is not failed and the
    scanner stays prepared."""
    from tests.test_calibrate import MockUsbIo
    cases = [
        (safety.FrameOutOfRangeError, dict(frame=5, ir=True, slide=True)),
        (safety.FrameOutOfRangeError, dict(frame=0, dpi=600, slide=True)),
        (safety.FeedlOutOfRangeError, dict(frame=4, dpi=600, slide=True, overscan_mm=2.0)),
        (safety.FeedlOutOfRangeError, dict(frame=4, ir=True, slide=True, overscan_mm=3.0)),
        # non-positive / non-finite overscan: would clip the opening, or
        # (-25) give -490 wire lines that encode as 0xFFFE16
        (ValueError, dict(frame=1, dpi=600, slide=True, overscan_mm=-25)),
        (ValueError, dict(frame=1, dpi=600, slide=True, overscan_mm=-1)),
        (ValueError, dict(frame=1, dpi=600, slide=True, overscan_mm=0)),
        (ValueError, dict(frame=1, ir=True, slide=True, overscan_mm=-1)),
        (ValueError, dict(frame=1, ir=True, slide=True, overscan_mm=float("nan"))),
    ]
    for exc, kw in cases:
        s = Scanner(MockUsbIo({}))
        s._prepared_for_scan = True
        phases = []
        s._run_phase = lambda ph, **k: phases.append(ph.name) or [b""]  # type: ignore[method-assign]
        s._exec_ops = lambda ops: phases.append("exec") or [b""]       # type: ignore[method-assign]
        _raises(exc, s.scan, **kw)
        assert phases == [], (kw, phases)
        assert s.session.writes == 0, (kw, s.session.writes)
        assert s.session.state is not safety.SessionState.FAILED, (kw, s.session.state)
        assert s.session.failure is None, kw
        assert s._prepared_for_scan is True, kw
    print("test_device_scan_slide_geometry_refused_before_any_phase OK")


# ------------------------------------------------------------ CLI contract


def _cli_args(**kw):
    base = dict(dpi=3600, ir=True, frame=None, frames=None, slide=None, slides=None,
                overscan=None, positive=False, rotate=0, no_clean=True,
                no_diag=False, no_cal_data=True, eject=False, park="semantic",
                recalibrate=False, warmup_budget=None, film="135",
                placement=None, dark_fallback="strict", output=None)
    base.update(kw)
    return argparse.Namespace(**base)


def _forbid_session(body):
    raise AssertionError("the device was opened for a refused command")


def _run_cli(args, session=_forbid_session):
    from of135i import cli
    orig = cli._run_writing_session
    cli._run_writing_session = session
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli._cmd_scan(args)
    finally:
        cli._run_writing_session = orig
    return rc, out.getvalue(), err.getvalue()


def test_cli_slide_refusals_open_nothing():
    """Every bad slide request is a usage error (exit 2) with zero
    hardware activity: the device is never opened."""
    with tempfile.TemporaryDirectory() as d:
        o = os.path.join(d, "s.tiff")
        cases = [
            _cli_args(output=o, slides="1-4", frames="1-4"),       # exclusive
            _cli_args(output=o, slide=1, frame=1),
            _cli_args(output=o, slides="1-4", slide=2),
            _cli_args(output=o, slides="1-5"),                     # 4 openings
            _cli_args(output=o, slide=0),
            _cli_args(output=o, slides="1-4", ir=False),           # plain 3600
            _cli_args(output=o, slides="1-4", film="110"),
            _cli_args(output=o, slides="4", dpi=600, overscan=2.0),  # FEEDL+travel
            _cli_args(output=o, slides="1-4", dpi=7200, overscan=3.0),
            _cli_args(output=o, slides="1-4", overscan=3.0),       # slide 4 @ 3600
            _cli_args(output=o, slides="1", overscan=-1.0),
        ]
        for a in cases:
            rc, _, err = _run_cli(a)
            assert rc == 2, (vars(a), rc, err)
            assert err.startswith("error:"), err
        # ...and a valid 600 dpi slide 4 with a smaller overscan is accepted
        # up to the device (the forbidding session is what stops it here).
        a = _cli_args(output=o, slides="1-4", dpi=600)      # the default
        try:
            _run_cli(a)
        except AssertionError as e:
            assert "device was opened" in str(e)
        else:
            raise AssertionError("valid slide request did not reach the session")
    print("test_cli_slide_refusals_open_nothing OK")


def test_cli_derived_output_is_directory_refused():
    """A directory sitting at ANY derived output name (overscan, IR pair,
    diag, cal data, product) is refused before the device opens -- slide
    and strip scans alike."""
    names = ["s-f1.overscan.tiff", "s-f1-ir.overscan.tiff", "s-f1.diag.json",
             "s-f1.cal.npz", "s-f1.tiff", "s-f1-ir.tiff"]
    for name in names:
        for kw in ({"slides": "1-2"}, {"slide": 1}, {"frames": "1-2"}):
            with tempfile.TemporaryDirectory() as d:
                os.mkdir(os.path.join(d, name))
                a = _cli_args(output=os.path.join(d, "s.tiff"), **kw)
                rc, _, err = _run_cli(a)
                assert rc == 2 and "existing directory" in err, (name, kw, rc, err)
    with tempfile.TemporaryDirectory() as d:        # single --frame: bare names
        os.mkdir(os.path.join(d, "s.overscan.tiff"))
        rc, _, err = _run_cli(_cli_args(output=os.path.join(d, "s.tiff"), frame=1))
        assert rc == 2 and "existing directory" in err, err
    print("test_cli_derived_output_is_directory_refused OK")


class _FakeSlideScanner:
    """Stands in for Scanner in the CLI body: records scan() calls and
    returns a small synthetic dual raw with a bright opening."""

    def __init__(self, scans):
        self.scans = scans
        self.park_mode = "semantic"
        self.recalibrate = False
        self.dark_fallback = "strict"
        self.warmup_budget_s = 60.0
        self.last_diag = None
        self.last_cal_data = None

    def check_start_state(self): pass
    def is_magazine_present(self): return True
    def initialize(self, ir, dpi): pass
    def eject(self): self.scans.append("eject")

    def scan(self, frame, ir=None, dpi=None, overscan_mm=None, slide=False):
        from tests.test_dual_overscan import _small_dual_raw
        self.scans.append((frame, ir, dpi, overscan_mm, slide, self.dark_fallback))
        self.last_diag = {"gain_codes": [1, 2, 3], "offset_codes": [4, 5, 6],
                          "dark_b_substituted": False, "dark_b_fallback": None,
                          "calibration": "fresh", "slide": frame}
        raw, w = _small_dual_raw(dpi, 1200, (60, 1140))
        return raw, w, {"width": w, "alternating": True, "dpi": dpi}


def test_cli_slides_write_overscan_pair_named_fN_exit_0():
    """--slides 1-4 --ir @ 3600: one scan(slide=True) per slide with the
    default slide overscan, <stem>-fN.overscan.tiff + <stem>-fN-ir.overscan.tiff
    per slide, no registered product, sidecar coverage not applicable,
    exit 0. --slide N names its files -fN as well."""
    for spec_kw, want in (({"slides": "1-4"}, [1, 2, 3, 4]), ({"slide": 3}, [3])):
        with tempfile.TemporaryDirectory() as d:
            scans: list = []
            a = _cli_args(output=os.path.join(d, "roll.tiff"),
                          dark_fallback="default", **spec_kw)
            rc, out, err = _run_cli(a, session=lambda body: body(_FakeSlideScanner(scans)))
            assert rc == 0, (rc, err)
            assert scans == [(n, True, 3600, holder.SLIDE_OVERSCAN_MM, True, "default")
                             for n in want], scans
            files = set(os.listdir(d))
            for n in want:
                for name in (f"roll-f{n}.overscan.tiff", f"roll-f{n}-ir.overscan.tiff",
                             f"roll-f{n}.diag.json"):
                    assert name in files, (name, sorted(files))
                assert f"roll-f{n}.tiff" not in files and f"roll-f{n}-ir.tiff" not in files
                sc = json.loads(Path(d, f"roll-f{n}.diag.json").read_text())
                assert sc["coverage"]["applicable"] is False, sc["coverage"]
                assert sc["coverage"]["verified"] is None
                assert sc["cli"]["slides"] == spec_kw.get("slides", str(spec_kw.get("slide")))
                assert sc["cli"]["dark_fallback"] == "default"
                assert sc["cli"]["overscan"] == holder.SLIDE_OVERSCAN_MM
            assert "not applicable" in out and "roll.tiff" not in files
    print("test_cli_slides_write_overscan_pair_named_fN_exit_0 OK")


def test_cli_slides_without_ir_still_write_ir_overscan():
    """600 and 7200 always run the dual engine: a slide scan WITHOUT --ir
    still writes <stem>-fN-ir.overscan.tiff (the pair is the product), and
    IR cleaning still follows --ir / --no-clean only."""
    from of135i import cli
    cleaned: list = []
    orig = cli.image.remove_dust

    def _spy(vis, ir, *, dpi):
        cleaned.append((vis.shape, dpi))
        return vis
    cli.image.remove_dust = _spy
    try:
        for dpi in (600, 7200):
            for ir, no_clean, want_clean in ((False, False, False), (False, True, False),
                                             (True, False, True), (True, True, False)):
                cleaned.clear()
                with tempfile.TemporaryDirectory() as d:
                    scans: list = []
                    a = _cli_args(output=os.path.join(d, "slide.tiff"), slide=1,
                                  dpi=dpi, ir=ir, no_clean=no_clean)
                    rc, _, err = _run_cli(a, session=lambda body: body(_FakeSlideScanner(scans)))
                    assert rc == 0, (dpi, ir, rc, err)
                    files = set(os.listdir(d))
                    for name in ("slide-f1.overscan.tiff", "slide-f1-ir.overscan.tiff"):
                        assert name in files, (dpi, ir, name, sorted(files))
                    assert scans[0][1] is True and scans[0][4] is True, scans
                    assert bool(cleaned) is want_clean, (dpi, ir, no_clean, cleaned)
                    if want_clean:
                        assert cleaned[0][1] == dpi, cleaned
    finally:
        cli.image.remove_dust = orig
    print("test_cli_slides_without_ir_still_write_ir_overscan OK")


def test_cli_strip_frames_unchanged():
    """--frames still scans strip frames: no slide flag reaches scan(),
    the strip default overscan, and dark_fallback defaults to strict."""
    scans: list = []

    class _Strip(_FakeSlideScanner):
        def scan(self, frame, ir=None, dpi=None, overscan_mm=None, **kw):
            assert "slide" not in kw, kw
            return super().scan(frame, ir=ir, dpi=dpi, overscan_mm=overscan_mm)

    with tempfile.TemporaryDirectory() as d:
        a = _cli_args(output=os.path.join(d, "s.tiff"), frames="1-2")
        _run_cli(a, session=lambda body: body(_Strip(scans)))
        assert [s[:5] for s in scans] == [(1, True, 3600, holder.OVERSCAN_MM, False),
                                          (2, True, 3600, holder.OVERSCAN_MM, False)], scans
        assert all(s[5] == "strict" for s in scans)
    print("test_cli_strip_frames_unchanged OK")


# ------------------------------------------------------ dark_b fallback


_RESIDUAL = np.tile(np.array([[10, 20, 30], [40, 50, 60]], dtype=np.uint16), (512, 1))
_HEALTHY = np.random.default_rng(1).integers(20000, 26000, (1024, 3), dtype=np.uint16)
# A residual dark_b whose bracket against _HEALTHY would give a plausible
# slope: offset_codes() on it would NOT return _OFFSET_DEFAULT, so seeing
# _OFFSET_DEFAULT on the wire proves the fallback, not the slope<1 branch.
_RESIDUAL_HIGH = np.tile(np.array([[26000, 26010, 26020], [26030, 26040, 26050]],
                                  dtype=np.uint16), (512, 1))


def test_dark_b_for_offsets_modes():
    from tests.test_calibrate import MockUsbIo
    # strict (the default): identical to _healthy_dark_b -> fails closed
    s = Scanner(MockUsbIo({}))
    assert s.dark_fallback == "strict"
    _raises(safety.CalibrationError, s._dark_b_for_offsets, _RESIDUAL)
    # default: residual + nothing healthy -> fallback, never remembered
    s = Scanner(MockUsbIo({}))
    s.dark_fallback = "default"
    out, sub, fb = s._dark_b_for_offsets(_RESIDUAL)
    assert fb == "default_offset_codes" and sub is False and out is _RESIDUAL
    assert s._last_good_dark_b is None
    # healthy passes through and is remembered (no fallback)
    out, sub, fb = s._dark_b_for_offsets(_HEALTHY)
    assert (out is _HEALTHY, sub, fb) == (True, False, None)
    # a later residual is the A10 substitution, unchanged -- not the fallback
    out, sub, fb = s._dark_b_for_offsets(_RESIDUAL)
    assert sub is True and fb is None and np.array_equal(out, _HEALTHY)
    print("test_dark_b_for_offsets_modes OK")


def _offsets_injected(runs):
    inj = next(inj for ph, inj in runs if ph.name == "cal_shading_measure")
    return tuple((inj[f"offset_{c}_hi"][0] << 8) | inj[f"offset_{c}_lo"][0] for c in "rgb")


def _residual_dark_b_recorder(base_cls, *a):
    rec = base_cls(*a)
    orig = rec._buffers_for

    def _buffers_for(phase):
        if phase.name == "cal_dark_b":
            return [_RESIDUAL_HIGH.astype("<u2").tobytes()]
        if phase.name == "cal_dark_a":
            return [_HEALTHY.astype("<u2").tobytes()]
        return orig(phase)
    rec._buffers_for = _buffers_for
    return rec


def test_dark_b_fallback_plain_path_and_cache():
    """Plain 3600: strict fails closed before the white measurement;
    default uses _OFFSET_DEFAULT on the wire and in the record, and the
    NEXT frame (cached calibration) reports the fallback too."""
    from tests.test_overscan import _RecordingScanner
    assert calibrate.dark_is_residual(_RESIDUAL_HIGH)
    assert calibrate.offset_codes(_HEALTHY, _RESIDUAL_HIGH) != calibrate._OFFSET_DEFAULT
    rec = _residual_dark_b_recorder(_RecordingScanner)
    _raises(safety.CalibrationError, rec.s._scan_plain, frame=1)
    assert [p.name for p, _ in rec.runs] == ["cal_dark_a", "cal_dark_b"]

    rec = _residual_dark_b_recorder(_RecordingScanner)
    rec.s.dark_fallback = "default"
    rec.s._scan_plain(frame=1)
    assert _offsets_injected(rec.runs) == calibrate._OFFSET_DEFAULT
    d = rec.s.last_diag
    assert d["dark_b_fallback"] == "default_offset_codes" and d["calibration"] == "fresh"
    assert tuple(d["offset_codes"]) == calibrate._OFFSET_DEFAULT
    assert d["dark_b_substituted"] is False
    rec.s._scan_plain(frame=2)
    d2 = rec.s.last_diag
    assert d2["calibration"] == "cached" and d2["dark_b_fallback"] == "default_offset_codes"
    assert tuple(d2["offset_codes"]) == calibrate._OFFSET_DEFAULT
    print("test_dark_b_fallback_plain_path_and_cache OK")


def test_dark_b_fallback_dual_path_and_strict_unchanged():
    """Dual: the fallback reaches the shading-measure offsets and the
    record; with a healthy dark_b the default mode is indistinguishable
    from strict (same phases, same injections)."""
    from tests.test_dual_overscan import _RecordingDualScanner
    t = dual_tables(600)
    rec = _residual_dark_b_recorder(_RecordingDualScanner, t)
    _raises(safety.CalibrationError, rec.s._scan_dual, t, frame=1)
    rec = _residual_dark_b_recorder(_RecordingDualScanner, t)
    rec.s.dark_fallback = "default"
    rec.s._scan_dual(t, frame=1)
    assert _offsets_injected(rec.runs) == calibrate._OFFSET_DEFAULT
    assert rec.s.last_diag["dark_b_fallback"] == "default_offset_codes"
    assert int(rec.s.last_cal_data["offset_codes"][0]) == 0x010B

    strict, lenient = _RecordingDualScanner(t), _RecordingDualScanner(t)
    lenient.s.dark_fallback = "default"
    strict.s._scan_dual(t, frame=1)
    lenient.s._scan_dual(t, frame=1)
    assert ([(p.name, sorted(i.items())) for p, i in strict.runs]
            == [(p.name, sorted(i.items())) for p, i in lenient.runs])
    assert lenient.s.last_diag["dark_b_fallback"] is None
    assert strict.s.last_diag["dark_b_fallback"] is None
    print("test_dark_b_fallback_dual_path_and_strict_unchanged OK")


def test_cli_dark_b_fallback_provenance_in_sidecar():
    """End to end through the CLI with a REAL residual dark_b: the slide
    scan's calibration takes the fallback, and the written .diag.json and
    .cal.npz carry dark_b_fallback = "default_offset_codes" and the offset
    codes actually used (_OFFSET_DEFAULT) -- under strict the same command
    fails instead."""
    from tests.test_dual_overscan import _RecordingDualScanner
    dpi = 600
    t = dual_tables(dpi)

    class _CalibratingFake(_FakeSlideScanner):
        def scan(self, frame, ir=None, dpi=None, overscan_mm=None, slide=False):
            rec = _residual_dark_b_recorder(_RecordingDualScanner, t)
            rec.s.dark_fallback = self.dark_fallback
            raw, w, meta = rec.s._scan_dual(t, frame=frame, overscan_mm=overscan_mm,
                                            slide=slide)
            self.last_diag, self.last_cal_data = rec.s.last_diag, rec.s.last_cal_data
            return raw, w, meta

    with tempfile.TemporaryDirectory() as d:
        a = _cli_args(output=os.path.join(d, "r.tiff"), slide=2, dpi=dpi,
                      no_cal_data=False, dark_fallback="default")
        rc, _, err = _run_cli(a, session=lambda body: body(_CalibratingFake([])))
        assert rc == 0, (rc, err)
        sc = json.loads(Path(d, "r-f2.diag.json").read_text())
        assert sc["dark_b_fallback"] == "default_offset_codes", sc.get("dark_b_fallback")
        assert sc["offset_codes"] == list(calibrate._OFFSET_DEFAULT), sc["offset_codes"]
        assert sc["dark_b_substituted"] is False and sc["slide"] == 2
        assert sc["cli"]["dark_fallback"] == "default"
        cal = np.load(sc["cal_data"])
        assert cal["offset_codes"].tolist() == list(calibrate._OFFSET_DEFAULT)
        assert calibrate.dark_is_residual(cal["dark_b"])   # the residual is kept on record

    with tempfile.TemporaryDirectory() as d:
        a = _cli_args(output=os.path.join(d, "r.tiff"), slide=2, dpi=dpi)
        _raises(safety.CalibrationError, _run_cli, a,
                session=lambda body: body(_CalibratingFake([])))
        assert not any(n.endswith(".diag.json") for n in os.listdir(d))
    print("test_cli_dark_b_fallback_provenance_in_sidecar OK")


def main() -> int:
    tests = [
        test_slide_model_matches_documented_numbers,
        test_slide_feedl_grid_every_profile,
        test_slide_ledger_default_every_profile,
        test_slide_geometry_refusals,
        test_slide_wiring_same_phases_slide_geometry,
        test_device_scan_slide_refusals_send_nothing,
        test_line_count_encode_guard,
        test_device_scan_slide_geometry_refused_before_any_phase,
        test_cli_slide_refusals_open_nothing,
        test_cli_derived_output_is_directory_refused,
        test_cli_slides_write_overscan_pair_named_fN_exit_0,
        test_cli_slides_without_ir_still_write_ir_overscan,
        test_cli_strip_frames_unchanged,
        test_dark_b_for_offsets_modes,
        test_dark_b_fallback_plain_path_and_cache,
        test_dark_b_fallback_dual_path_and_strict_unchanged,
        test_cli_dark_b_fallback_provenance_in_sidecar,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
