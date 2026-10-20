"""Per-scan aperture coverage verification and crop.

docs/holder-position-design.md section 5: overscan sizing (section 4) is
a bet on an assumed ±0.5 mm worst case, not a measured ceiling. The bet
is made safe by checking the aperture's own edges in every delivered
image, so each scan proves its own coverage instead of trusting the
budget. This module is that check, plus the deterministic crop that
follows once coverage is verified.

It shares ``of135i.aperture``'s edge detector (the same half-level,
locally-adaptive crossing finder ``tools/holder_geometry.py`` uses for
the offline holder measurement) so a delivered frame is registered
against the aperture the same way the geometry itself was measured --
one implementation of "where is the plastic edge", used both to build
the geometry and to check a scan against it.

Rule (section 5):
  - exactly one aperture found, both margins >= min_margin_mm of
    overscan beyond its edges -> coverage verified, crop to the
    aperture.
  - an edge missing, or found with less than min_margin_mm slack (or a
    run that runs into the image boundary, so a bounding edge is not
    provable) -> not verified. Never delivered as if it were complete.
  - zero apertures, or more than one, in the profile -> not verified;
    the window's contents cannot be resolved to a single aperture.

The raw scan is never altered by any of this: measure_coverage only
reads it, and crop_to_aperture returns a new array/view, leaving the
caller's original data untouched -- a flagged frame's raw overscan can
always be inspected, never just discarded (section 5).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np

from . import aperture

MM_PER_INCH = 25.4


@dataclass(frozen=True)
class ApertureCoverage:
    """Verdict from :func:`measure_coverage`.

    ``leading_line``/``trailing_line`` are the aperture's edge positions
    in fractional line coordinates (axis 0), or ``None`` if no single
    aperture could be resolved. ``*_margin_mm`` are the overscan present
    beyond each edge; negative would mean the edge is off the near side
    of the image, which the edge detector cannot report (an edge only
    exists where both a lit side and a plastic side were seen), so in
    practice a too-small margin shows up as a very small non-negative
    value or as no edge found at all.
    """

    verified: bool
    leading_line: float | None
    trailing_line: float | None
    leading_margin_mm: float | None
    trailing_margin_mm: float | None
    reason: str
    threshold: float
    # Which array the verdict was measured on: "visible" (default; plain
    # paths) or "ir" (dual paths, see measure_coverage_dual).
    source: str = "visible"


def along_strip_profile(image, *, channel="red", band=0.5) -> np.ndarray:
    """Mean intensity per line over the central `band` of the width, on
    one channel. `channel`: "red" (default), "green", "blue", or "lum"
    (mean of RGB). Red is the default because on a colour (C-41) negative
    the aperture edge falls on the clear inter-frame rebate, whose orange
    base passes strongest in red against the ~dark plastic; on an empty
    holder every channel is saturated so red is equally fine.
    """
    arr = np.asarray(image)
    if arr.ndim == 2:
        # A single-channel (lines, width) array, e.g. the IR channel as
        # image.split_ir returns it: used directly, `channel` is ignored.
        two_d = True
    elif arr.ndim == 3 and arr.shape[2] >= 3:
        two_d = False
    else:
        raise ValueError(f"expected a (lines, width, channels>=3) image, got shape {arr.shape}")
    n_lines, width = arr.shape[0], arr.shape[1]

    band = min(max(float(band), 0.0), 1.0)
    half = width * band / 2.0
    centre = width / 2.0
    lo = int(round(centre - half))
    hi = int(round(centre + half))
    lo = min(max(lo, 0), width)
    hi = min(max(hi, lo + 1 if width > 0 else lo), width)
    if hi <= lo:
        lo, hi = 0, width

    if two_d:
        chan = arr[:, lo:hi].astype(np.float64)
        if chan.size == 0:
            return np.zeros(n_lines, dtype=np.float64)
        return chan.mean(axis=1)

    window = arr[:, lo:hi, :].astype(np.float64)

    if channel == "red":
        chan = window[:, :, 0]
    elif channel == "green":
        chan = window[:, :, 1]
    elif channel == "blue":
        chan = window[:, :, 2]
    elif channel == "lum":
        chan = window[:, :, :3].mean(axis=2)
    else:
        raise ValueError(f"unknown channel {channel!r}")

    if chan.size == 0:
        return np.zeros(n_lines, dtype=np.float64)
    return chan.mean(axis=1)


def measure_coverage(image, *, dpi, min_margin_mm=0.15, channel="red") -> ApertureCoverage:
    """Locate the aperture's two edges in a delivered frame and decide
    whether the whole opening was captured with at least `min_margin_mm`
    of overscan beyond each edge. Rule (section 5):
      - exactly one aperture found, both margins >= min_margin_mm ->
        verified True.
      - one aperture but a margin < min_margin_mm (or an edge at the
        very boundary) -> verified False, reason names the clipped
        side.
      - zero apertures, or more than one -> verified False, reason says
        so.
    Never raises on image content; returns a verdict.
    """
    arr = np.asarray(image)
    n_lines = arr.shape[0] if arr.ndim >= 1 else 0

    profile = along_strip_profile(arr, channel=channel)
    lines_per_mm = dpi / MM_PER_INCH

    # The edge detector (of135i.aperture) is tuned on the 600 dpi holder
    # sweep, where an aperture edge crosses in one or two lines. At higher
    # resolutions the same physical edge is spread over proportionally
    # more lines, so no single line carries the fraction of the lit-to-
    # plastic drop the gradient test looks for and the edge is missed
    # (seen on the first plain-3600 overscan run: both edges present in
    # the image, none found). Bin the profile down to ~600 dpi before
    # detecting, so the edge is one or two binned lines again, then scale
    # the returned positions back. Binning only sharpens the step; the
    # margins below are still measured in full-resolution lines.
    factor = max(1, round(dpi / 600))
    if factor > 1:
        usable = (len(profile) // factor) * factor
        binned = profile[:usable].reshape(-1, factor).mean(axis=1)
        threshold, aps_b = aperture.apertures(binned, lines_per_mm / factor)
        aps = [(a * factor, b * factor) for a, b in aps_b]
    else:
        threshold, aps = aperture.apertures(profile, lines_per_mm)

    if len(aps) == 0:
        return ApertureCoverage(
            verified=False,
            leading_line=None,
            trailing_line=None,
            leading_margin_mm=None,
            trailing_margin_mm=None,
            reason="no aperture found in profile",
            threshold=threshold,
        )
    if len(aps) > 1:
        return ApertureCoverage(
            verified=False,
            leading_line=None,
            trailing_line=None,
            leading_margin_mm=None,
            trailing_margin_mm=None,
            reason=f"{len(aps)} apertures found, expected exactly one",
            threshold=threshold,
        )

    start, end = aps[0]
    leading_margin_mm = start / lines_per_mm
    trailing_margin_mm = (n_lines - 1 - end) / lines_per_mm

    clipped = []
    if leading_margin_mm < min_margin_mm:
        clipped.append("leading")
    if trailing_margin_mm < min_margin_mm:
        clipped.append("trailing")

    if clipped:
        sides = " and ".join(clipped)
        return ApertureCoverage(
            verified=False,
            leading_line=start,
            trailing_line=end,
            leading_margin_mm=leading_margin_mm,
            trailing_margin_mm=trailing_margin_mm,
            reason=f"{sides} margin below min_margin_mm ({min_margin_mm} mm)",
            threshold=threshold,
        )

    return ApertureCoverage(
        verified=True,
        leading_line=start,
        trailing_line=end,
        leading_margin_mm=leading_margin_mm,
        trailing_margin_mm=trailing_margin_mm,
        reason="ok",
        threshold=threshold,
    )


#: Largest overscan margin a verified frame is expected to show. The
#: window is the aperture plus the overscan on each side (0.75 mm default,
#: docs/holder-position-design.md section 4, ±0.5 mm load variation), and
#: production telemetry over 647 verified frames measured 0.43 to 1.10 mm
#: (docs/production-telemetry-2026-10.md section 3). A margin well above
#: that means the opening found is SHORTER than the holder's: the strip's
#: end lying inside the aperture and opaque to the measuring channel
#: (seen once, IR, leading margin 2.54 mm on a film end). The verdict is
#: not changed by this -- the edges found are real -- but the frame is
#: flagged so a workflow can check the crop against its siblings.
MAX_PLAUSIBLE_MARGIN_MM = 2.0


def margin_warning(coverage: ApertureCoverage,
                   max_margin_mm: float = MAX_PLAUSIBLE_MARGIN_MM) -> str | None:
    """A one-line warning when a verified coverage's margin exceeds
    `max_margin_mm` (see MAX_PLAUSIBLE_MARGIN_MM), else None."""
    if not coverage.verified:
        return None
    wide = []
    if coverage.leading_margin_mm is not None and coverage.leading_margin_mm > max_margin_mm:
        wide.append(f"leading {coverage.leading_margin_mm:.2f} mm")
    if coverage.trailing_margin_mm is not None and coverage.trailing_margin_mm > max_margin_mm:
        wide.append(f"trailing {coverage.trailing_margin_mm:.2f} mm")
    if not wide:
        return None
    return (f"margin above {max_margin_mm:.1f} mm ({', '.join(wide)}): the opening "
            f"found is shorter than the holder's aperture, the strip's end may lie "
            f"inside it; check the crop against the strip's other frames")


def measure_coverage_dual(visible, ir, *, dpi, min_margin_mm=0.15) -> ApertureCoverage:
    """Coverage for a dual-light (IR) pass: IR first, then visible.

    The IR channel is blind to the picture, so the aperture edge is
    visible in it even when film content (a black film end, a dark
    picture beside an empty part of the opening) hides the edge in the
    visible red channel. `ir` is the (lines, width) array from
    image.split_ir and must share the visible array's line grid (same
    number of lines, same line index = same position on the strip), so
    the verdict's line positions apply to both.

    Returns the IR verdict if verified, else the visible verdict if
    verified (source "visible"), else a not-verified verdict whose reason
    names both failures.
    """
    v_shape = np.asarray(visible).shape
    i_shape = np.asarray(ir).shape
    if v_shape[0] != i_shape[0]:
        raise ValueError(f"visible and IR do not share a line grid: {v_shape[0]} vs {i_shape[0]} lines")
    cov_ir = measure_coverage(ir, dpi=dpi, min_margin_mm=min_margin_mm)
    if cov_ir.verified:
        return replace(cov_ir, source="ir")
    cov_vis = measure_coverage(visible, dpi=dpi, min_margin_mm=min_margin_mm)
    if cov_vis.verified:
        return replace(cov_vis, source="visible")
    return replace(cov_vis, source="visible",
                   reason=f"IR: {cov_ir.reason}; visible: {cov_vis.reason}")


@dataclass(frozen=True)
class FilmPresence:
    """Verdict from :func:`film_presence`: ``saturated_share`` is the
    share of sampled pixels (inside the aperture) with all three channels
    above the saturation code; ``present`` is False for an empty opening."""

    present: bool
    saturated_share: float


# Share of the sampled aperture that must be open light (saturated in all
# three channels) before the opening counts as EMPTY. A strip end inside
# the opening leaves part of it open and part under film: that frame is
# film and is written (the first production series, 2026-10-20, had a
# leader frame 54 % open light over unexposed base). Only an opening that
# is practically all open light is empty (an empty holder slot measures
# 0.99999).
EMPTY_APERTURE_SHARE = 0.95


def film_presence(image, coverage: ApertureCoverage, *, dpi, band=0.5,
                  saturation_code=60000, empty_share=EMPTY_APERTURE_SHARE,
                  inset_mm=0.3, step=4) -> FilmPresence:
    """Is there film in the verified aperture, or only open light?

    Coverage checks the opening, not what is in it: an empty holder slot
    passes it. In the raw frame an empty opening reads above
    `saturation_code` in all three channels across its whole width, while
    film (even unexposed base) never saturates blue through the orange
    mask. Over the rows strictly inside the aperture (inset by `inset_mm`
    at each edge, so the edge penumbra is excluded), over the central
    `band` of the width (same convention as :func:`along_strip_profile`),
    sampled every `step` rows and columns, this returns the share of
    pixels with all three channels > `saturation_code`; present is
    ``share < empty_share`` (default :data:`EMPTY_APERTURE_SHARE`, 0.95:
    a partly covered opening, as on a strip end, is film).

    A coverage without located edges, or an aperture too short for the
    inset, cannot be judged: reported as present (share 0.0), never as
    empty. Measure it on the array `coverage` was measured on.
    """
    arr = np.asarray(image)
    if arr.ndim != 3 or arr.shape[2] < 3:
        raise ValueError(f"expected a (lines, width, channels>=3) image, got shape {arr.shape}")
    if coverage.leading_line is None or coverage.trailing_line is None:
        return FilmPresence(True, 0.0)
    n_lines, width = arr.shape[0], arr.shape[1]
    inset = inset_mm * dpi / MM_PER_INCH
    lo = max(int(np.ceil(coverage.leading_line + inset)), 0)
    hi = min(int(np.floor(coverage.trailing_line - inset)), n_lines - 1)
    if hi < lo:
        return FilmPresence(True, 0.0)

    band = min(max(float(band), 0.0), 1.0)
    half = width * band / 2.0
    c0 = min(max(int(round(width / 2.0 - half)), 0), width)
    c1 = min(max(int(round(width / 2.0 + half)), c0 + 1), width)
    step = max(1, int(step))
    sample = arr[lo:hi + 1:step, c0:c1:step, :3]
    if sample.size == 0:
        return FilmPresence(True, 0.0)
    saturated = (sample > saturation_code).all(axis=2)
    share = float(saturated.mean())
    return FilmPresence(share < empty_share, share)


def crop_to_aperture(image, coverage: ApertureCoverage, *, dpi, pad_mm=0.0) -> np.ndarray:
    """Return image cropped along axis 0 to [leading, trailing] (+/- pad),
    clamped to the image. Deterministic. Raises ValueError if
    coverage.leading_line/trailing_line is None. Does not require
    verified=True (caller decides); the raw image is never modified.
    """
    if coverage.leading_line is None or coverage.trailing_line is None:
        raise ValueError("coverage has no located aperture edges to crop to")

    arr = np.asarray(image)
    n_lines = arr.shape[0]
    lines_per_mm = dpi / MM_PER_INCH
    pad_lines = pad_mm * lines_per_mm

    lo = int(np.floor(coverage.leading_line - pad_lines))
    hi = int(np.ceil(coverage.trailing_line + pad_lines)) + 1  # +1: end is inclusive

    lo = min(max(lo, 0), n_lines)
    hi = min(max(hi, lo), n_lines)

    return arr[lo:hi]
