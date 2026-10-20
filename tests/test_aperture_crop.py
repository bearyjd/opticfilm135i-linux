#!/usr/bin/env python3
"""Offline tests for of135i.aperture_crop -- no hardware required.

Also covers tools/sane_coverage.py's reader: a frame delivered as a 16-bit
PNM (scanimage) and the SAME frame as a PNG (what digiKam saves) must give
the same coverage verdict, or WP-2's frontend image cannot be judged by the
same criterion as the scanimage one (docs/sane-install.md).

Scope of that check, precisely: it is about the COVERAGE VERDICT surviving
the PNG path, not about bit depth. The PNG it writes is 8-bit, because that
is what the Pillow reader would give us back anyway -- Pillow truncates
16-bit RGB to 8 bits. Whether digiKam actually saved 16 bits per channel is
a different question, answered by tools/image_probe.py and
tests/test_image_probe.py.

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_aperture_crop.py
"""

import importlib.util
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import numpy as np

from of135i import aperture
from of135i.aperture_crop import (ApertureCoverage, along_strip_profile, crop_to_aperture, measure_coverage,
                                  measure_coverage_dual, film_presence, margin_warning,
                                  MAX_PLAUSIBLE_MARGIN_MM)

DPI = 600
LINES_PER_MM = DPI / 25.4
WIDTH = 200


def _line_image(values) -> np.ndarray:
    """Broadcast a 1-D per-line value sequence to a (lines, width, 3)
    uint16 image, the same value on every column and channel -- the
    along-strip profile only needs a per-line signal, not real texture."""
    values = np.asarray(values, dtype=np.float64)
    arr = np.empty((len(values), WIDTH, 3), dtype="<u2")
    arr[:, :, :] = values[:, None, None]
    return arr


def _aperture_lines(margin_mm=25.5):
    """Line count for an aperture comfortably over MIN_APERTURE_MM."""
    return int(round(margin_mm * LINES_PER_MM))


def test_measure_coverage_clean_aperture_is_verified():
    leading, trailing = 100, 100
    n_ap = _aperture_lines()
    plastic, lit = 800.0, 40000.0

    values = [plastic] * leading + [lit] * n_ap + [plastic] * trailing
    image = _line_image(values)

    coverage = measure_coverage(image, dpi=DPI)

    assert coverage.verified, coverage.reason
    assert coverage.leading_line is not None and coverage.trailing_line is not None
    # start of the bright run is at index `leading`; the detector places
    # the crossing on the ramp itself, so within a line or two of it.
    assert abs(coverage.leading_line - leading) <= 2.0, coverage.leading_line
    expected_trailing = leading + n_ap - 1  # last bright line
    assert abs(coverage.trailing_line - expected_trailing) <= 2.0, coverage.trailing_line
    assert coverage.leading_margin_mm > 0.15
    assert coverage.trailing_margin_mm > 0.15
    print("test_measure_coverage_clean_aperture_is_verified OK")


def test_margin_warning_flags_a_shortened_opening_without_failing():
    """A verified frame whose margin is far above the overscan the window
    was sized for (telemetry max 1.10 mm) means the opening found is
    shorter than the holder's -- a film end opaque to the measuring
    channel. It is flagged, never turned into a coverage failure."""
    plastic, lit = 800.0, 40000.0
    n_ap = _aperture_lines()
    wide = int(round(3.0 * LINES_PER_MM))       # 3 mm, above the 2 mm bound
    ok = int(round(0.8 * LINES_PER_MM))         # 0.8 mm, a normal margin

    cov = measure_coverage(_line_image([plastic] * wide + [lit] * n_ap + [plastic] * ok), dpi=DPI)
    assert cov.verified
    w = margin_warning(cov)
    assert w and "leading" in w and "trailing" not in w, w
    assert f"{MAX_PLAUSIBLE_MARGIN_MM:.1f} mm" in w

    cov = measure_coverage(_line_image([plastic] * ok + [lit] * n_ap + [plastic] * ok), dpi=DPI)
    assert cov.verified and margin_warning(cov) is None

    cov = measure_coverage(_line_image([plastic] * 400), dpi=DPI)
    assert not cov.verified and margin_warning(cov) is None
    print("test_margin_warning_flags_a_shortened_opening_without_failing OK")


def test_measure_coverage_clipped_leading_is_not_verified():
    # Almost no plastic before the aperture -- leading margin well under
    # min_margin_mm even though the edge is still (barely) resolvable.
    leading, trailing = 3, 100
    n_ap = _aperture_lines()
    plastic, lit = 800.0, 40000.0

    values = [plastic] * leading + [lit] * n_ap + [plastic] * trailing
    image = _line_image(values)

    coverage = measure_coverage(image, dpi=DPI, min_margin_mm=0.5)

    assert not coverage.verified
    assert "leading" in coverage.reason, coverage.reason
    print("test_measure_coverage_clipped_leading_is_not_verified OK")


def test_measure_coverage_clipped_trailing_is_not_verified():
    leading, trailing = 100, 3
    n_ap = _aperture_lines()
    plastic, lit = 800.0, 40000.0

    values = [plastic] * leading + [lit] * n_ap + [plastic] * trailing
    image = _line_image(values)

    coverage = measure_coverage(image, dpi=DPI, min_margin_mm=0.5)

    assert not coverage.verified
    assert "trailing" in coverage.reason, coverage.reason
    print("test_measure_coverage_clipped_trailing_is_not_verified OK")


def test_measure_coverage_no_aperture_found():
    # Uniformly dark -- no lit run at all.
    image = _line_image([800.0] * 400)
    coverage = measure_coverage(image, dpi=DPI)
    assert not coverage.verified
    assert coverage.leading_line is None and coverage.trailing_line is None
    assert "no aperture" in coverage.reason, coverage.reason
    print("test_measure_coverage_no_aperture_found OK")


def test_measure_coverage_colour_negative_mid_dip_stays_one_aperture():
    # A colour negative: the aperture itself reads at the inter-frame
    # rebate level (~9000), not full white, and a block of real "image
    # content" sits in the middle of it, well short of black. Chosen
    # levels: plastic 800, rebate 9000, content 4500. The PLASTIC_LEVEL
    # rule in of135i.aperture only treats a transition as a holder edge
    # if one side is within 15% of the floor-to-lit range of the floor;
    # here that cutoff is floor(800) + 0.15*(9000-800) = 2030, so a dip
    # to 4500 sits well above it and is correctly *not* mistaken for
    # plastic. (A dip closer to black, e.g. ~1500 as a first guess,
    # falls under that cutoff and gets treated as a second aperture
    # edge -- this level was raised specifically to stay clear of it,
    # per the task's "tune and document" allowance.)
    leading, trailing = 100, 100
    n_ap = _aperture_lines()
    plastic, rebate, content = 800.0, 9000.0, 4500.0

    dip_start, dip_len = n_ap // 2 - 25, 50
    ap_values = [rebate] * n_ap
    for i in range(dip_start, dip_start + dip_len):
        ap_values[i] = content

    values = [plastic] * leading + ap_values + [plastic] * trailing
    image = _line_image(values)

    coverage = measure_coverage(image, dpi=DPI)

    assert coverage.verified, coverage.reason
    assert abs(coverage.leading_line - leading) <= 2.0, coverage.leading_line
    expected_trailing = leading + n_ap - 1
    assert abs(coverage.trailing_line - expected_trailing) <= 2.0, coverage.trailing_line
    print("test_measure_coverage_colour_negative_mid_dip_stays_one_aperture OK")


def test_measure_coverage_multiple_apertures_not_verified():
    n_ap = _aperture_lines()
    plastic, lit = 800.0, 40000.0
    crossbar = 40  # < MIN_APERTURE_MM in line-length, separates two apertures

    values = (
        [plastic] * 100
        + [lit] * n_ap
        + [plastic] * crossbar
        + [lit] * n_ap
        + [plastic] * 100
    )
    image = _line_image(values)

    coverage = measure_coverage(image, dpi=DPI)

    assert not coverage.verified
    assert coverage.leading_line is None and coverage.trailing_line is None
    assert "2" in coverage.reason or "apertures" in coverage.reason, coverage.reason
    print("test_measure_coverage_multiple_apertures_not_verified OK")


def test_along_strip_profile_uses_requested_channel():
    lines = 10
    arr = np.zeros((lines, WIDTH, 3), dtype="<u2")
    arr[:, :, 0] = 100   # red
    arr[:, :, 1] = 200   # green
    arr[:, :, 2] = 300   # blue

    red = along_strip_profile(arr, channel="red")
    green = along_strip_profile(arr, channel="green")
    blue = along_strip_profile(arr, channel="blue")
    lum = along_strip_profile(arr, channel="lum")

    assert np.allclose(red, 100)
    assert np.allclose(green, 200)
    assert np.allclose(blue, 300)
    assert np.allclose(lum, 200)  # mean(100, 200, 300)
    print("test_along_strip_profile_uses_requested_channel OK")


def test_crop_to_aperture_line_count_and_no_mutation():
    lines = 80
    image = _line_image(list(range(lines)))
    original = image.copy()

    coverage = ApertureCoverage(
        verified=True,
        leading_line=10.4,
        trailing_line=50.6,
        leading_margin_mm=10.4 / LINES_PER_MM,
        trailing_margin_mm=(lines - 1 - 50.6) / LINES_PER_MM,
        reason="ok",
        threshold=0.0,
    )

    cropped = crop_to_aperture(image, coverage, dpi=DPI)

    # floor(10.4)=10 .. ceil(50.6)+1=52 (exclusive) -> 42 lines
    assert cropped.shape[0] == 42, cropped.shape[0]
    assert cropped.shape[1:] == image.shape[1:]
    assert np.array_equal(image, original), "crop_to_aperture mutated the input image"
    print("test_crop_to_aperture_line_count_and_no_mutation OK")


def test_crop_to_aperture_clamps_to_image_bounds_with_pad():
    lines = 60
    image = _line_image(list(range(lines)))

    coverage = ApertureCoverage(
        verified=True,
        leading_line=2.0,
        trailing_line=57.0,
        leading_margin_mm=2.0 / LINES_PER_MM,
        trailing_margin_mm=(lines - 1 - 57.0) / LINES_PER_MM,
        reason="ok",
        threshold=0.0,
    )

    # A large pad pushes both bounds past the image edges; the crop must
    # clamp rather than raise or return a negative-length slice.
    cropped = crop_to_aperture(image, coverage, dpi=DPI, pad_mm=5.0)
    assert cropped.shape[0] == lines
    print("test_crop_to_aperture_clamps_to_image_bounds_with_pad OK")


def test_crop_to_aperture_raises_without_located_edges():
    image = _line_image([100.0] * 20)
    coverage = ApertureCoverage(
        verified=False,
        leading_line=None,
        trailing_line=None,
        leading_margin_mm=None,
        trailing_margin_mm=None,
        reason="no aperture found in profile",
        threshold=0.0,
    )
    try:
        crop_to_aperture(image, coverage, dpi=DPI)
        raised = False
    except ValueError:
        raised = True
    assert raised, "expected ValueError when edges are not located"
    print("test_crop_to_aperture_raises_without_located_edges OK")


def _sane_coverage_module():
    """tools/ is not a package; load the tool by path, as the shell does."""
    path = Path(__file__).resolve().parents[1] / "tools" / "sane_coverage.py"
    spec = importlib.util.spec_from_file_location("sane_coverage", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_sane_coverage_reads_png_and_pnm_alike():
    """The coverage verdict must survive the PNG path: a 16-bit PNM and the
    same frame as an 8-bit RGB PNG put the aperture edges in the same place.

    This says nothing about bit depth -- it deliberately compares a 16-bit
    PNM against the 8-bit RGB Pillow hands back for any PNG. Bit-depth
    preservation is tools/image_probe.py's job (tests/test_image_probe.py)."""
    try:
        from PIL import Image
    except ImportError:
        print("SKIP: Pillow not available")
        return "skipped"
    sc = _sane_coverage_module()
    leading = trailing = 100
    n_ap = _aperture_lines()
    image = _line_image([800.0] * leading + [40000.0] * n_ap + [800.0] * trailing)

    with tempfile.TemporaryDirectory() as d:
        pnm = Path(d) / "frame.pnm"
        h, w, _ = image.shape
        # P6 is big-endian by definition; the test image is little-endian u2
        pnm.write_bytes(b"P6\n%d %d\n65535\n" % (w, h) +
                        image.astype(">u2").tobytes())
        # What digiKam actually writes: an RGB PNG. Pillow gives 8-bit RGB
        # back for those, which is the point of the check -- the verdict must
        # not depend on the bit depth.
        png = Path(d) / "frame.png"
        Image.fromarray((image >> 8).astype("uint8"), mode="RGB").save(png)

        from_pnm = sc.read_image(str(pnm))
        from_png = sc.read_image(str(png))
        assert from_pnm.shape == image.shape, f"PNM shape {from_pnm.shape}"
        assert from_png.shape == image.shape, f"PNG shape {from_png.shape}"
        assert np.array_equal(from_pnm[:, :, 0] >> 8, from_png[:, :, 0]), \
            "PNM and PNG readers disagree on the pixel data"

        cov_pnm = measure_coverage(from_pnm, dpi=DPI)
        cov_png = measure_coverage(from_png, dpi=DPI)
        assert cov_pnm.verified and cov_png.verified, \
            f"coverage not verified: pnm={cov_pnm.reason!r} png={cov_png.reason!r}"
        assert (cov_pnm.leading_line, cov_pnm.trailing_line) == \
               (cov_png.leading_line, cov_png.trailing_line), \
            "the two readers put the aperture edges in different places"
    print("test_sane_coverage_reads_png_and_pnm_alike OK")


def _aperture_profile(leading=100, trailing=100, plastic=800.0, lit=40000.0):
    return [plastic] * leading + [lit] * _aperture_lines() + [plastic] * trailing


def _ir_2d(values) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    arr = np.empty((len(values), WIDTH), dtype="<u2")
    arr[:, :] = values[:, None]
    return arr


def test_along_strip_profile_accepts_2d_ir_channel():
    values = np.linspace(100, 60000, 50)
    ir = _ir_2d(values)
    prof = along_strip_profile(ir)
    assert prof.shape == (50,)
    assert np.allclose(prof, values, atol=1.0)
    # channel is ignored for a 2-D array
    assert np.array_equal(prof, along_strip_profile(ir, channel="blue"))
    # 3-channel behaviour unchanged
    rgb = _line_image(values)
    assert np.allclose(along_strip_profile(rgb), values, atol=1.0)
    print("test_along_strip_profile_accepts_2d_ir_channel OK")


def test_measure_coverage_dual_ir_first_when_visible_hides_edge():
    n = len(_aperture_profile())
    visible = _line_image([800.0] * n)          # film content hides the edge
    ir = _ir_2d(_aperture_profile())
    assert not measure_coverage(visible, dpi=DPI).verified
    cov = measure_coverage_dual(visible, ir, dpi=DPI)
    assert cov.verified and cov.source == "ir", (cov.reason, cov.source)
    print("test_measure_coverage_dual_ir_first_when_visible_hides_edge OK")


def test_measure_coverage_dual_falls_back_to_visible():
    n = len(_aperture_profile())
    visible = _line_image(_aperture_profile())
    ir = _ir_2d([800.0] * n)
    cov = measure_coverage_dual(visible, ir, dpi=DPI)
    assert cov.verified and cov.source == "visible", (cov.reason, cov.source)
    print("test_measure_coverage_dual_falls_back_to_visible OK")


def test_measure_coverage_dual_both_fail_combines_reasons():
    n = len(_aperture_profile())
    visible = _line_image([800.0] * n)
    ir = _ir_2d([800.0] * n)
    cov = measure_coverage_dual(visible, ir, dpi=DPI)
    ir_reason = measure_coverage(ir, dpi=DPI).reason
    vis_reason = measure_coverage(visible, dpi=DPI).reason
    assert not cov.verified
    assert cov.reason == f"IR: {ir_reason}; visible: {vis_reason}", cov.reason
    assert ApertureCoverage(True, 0.0, 1.0, 1.0, 1.0, "ok", 0.0).source == "visible"
    print("test_measure_coverage_dual_both_fail_combines_reasons OK")


def test_film_presence_empty_vs_film():
    n_ap = _aperture_lines()
    n = 100 + n_ap + 100
    empty = np.full((n, WIDTH, 3), 800, dtype="<u2")
    empty[100:100 + n_ap] = 65000
    cov = measure_coverage(empty, dpi=DPI)
    assert cov.verified
    fp = film_presence(empty, cov, dpi=DPI)
    assert not fp.present and fp.saturated_share > 0.9, fp

    film = empty.copy()
    film[100:100 + n_ap] = 40000
    film[150:154, 90:96, :] = 65000            # a few saturated specks
    cov = measure_coverage(film, dpi=DPI)
    assert cov.verified
    fp = film_presence(film, cov, dpi=DPI)
    assert fp.present and fp.saturated_share < 0.05, fp
    # Saturated in only some channels is still film (blue never saturates).
    mixed = empty.copy()
    mixed[100:100 + n_ap, :, 2] = 20000
    fp = film_presence(mixed, measure_coverage(mixed, dpi=DPI), dpi=DPI)
    assert fp.present and fp.saturated_share == 0.0, fp
    # Edges outside the inset are excluded: plastic rows never count.
    unlocated = ApertureCoverage(False, None, None, None, None, "x", 0.0)
    assert film_presence(film, unlocated, dpi=DPI).present
    print("test_film_presence_empty_vs_film OK")


def main() -> int:
    tests = [
        test_measure_coverage_clean_aperture_is_verified,
        test_measure_coverage_clipped_leading_is_not_verified,
        test_measure_coverage_clipped_trailing_is_not_verified,
        test_measure_coverage_no_aperture_found,
        test_measure_coverage_colour_negative_mid_dip_stays_one_aperture,
        test_measure_coverage_multiple_apertures_not_verified,
        test_along_strip_profile_uses_requested_channel,
        test_crop_to_aperture_line_count_and_no_mutation,
        test_crop_to_aperture_clamps_to_image_bounds_with_pad,
        test_crop_to_aperture_raises_without_located_edges,
        test_sane_coverage_reads_png_and_pnm_alike,
        test_along_strip_profile_accepts_2d_ir_channel,
        test_measure_coverage_dual_ir_first_when_visible_hides_edge,
        test_measure_coverage_dual_falls_back_to_visible,
        test_measure_coverage_dual_both_fail_combines_reasons,
        test_film_presence_empty_vs_film,
        test_margin_warning_flags_a_shortened_opening_without_failing,
    ]
    passed = skipped = 0
    for t in tests:
        if t() == "skipped":
            skipped += 1
        else:
            passed += 1
    if skipped:
        print(f"\n{passed} tests passed, {skipped} skipped.")
    else:
        print(f"\n{passed} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
