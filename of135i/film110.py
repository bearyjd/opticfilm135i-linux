"""110 (Pocket Instamatic) frame detector.

docs/film-110-proposal.md: 110 support is a film model
(``holder.FILM_110``) plus an image-side detector on top of the existing
aperture pipeline. The motor side is untouched -- this module only reads
an already aperture-registered image (the output of
``aperture_crop.crop_to_aperture``) and finds where, inside that single
physical aperture, the 110 strip's own images and perforations fall.

Why perforations are the primary fiducial: thresholding the image content
itself against the clear film base fails on thin (shadow) frames and on
skies alike -- both look too much like the base at a naive threshold.
The perforation is the one feature that is reliably at the air level
(nothing between the sensor and the lamp there), so every image position
is *predicted* from a nearby hole and only *refined* (by a bounded
amount) against the image content -- never re-derived from it.

Axis convention (matching ``aperture_crop``/``aperture``): axis 0 of the
input image is along the transport (the line axis), axis 1 is lateral
(across the film, the column axis). All internal positions are in those
same units (fractional lines / fractional columns), the same convention
``ApertureCoverage.leading_line`` uses -- millimetres are only used for
constants and for the caller-facing ``size_mm`` field.

Every threshold below is relative to levels measured on the image itself
(the "air" level, mainly), not a fixed absolute count, so the detector is
insensitive to per-frame gain/exposure the same way ``of135i.aperture``
is. Never raises on image content: a frame that cannot be resolved comes
back with ``reason`` set and ``frames`` empty; the aperture product this
sits on top of is unaffected either way.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import warnings

import numpy as np

from .holder import Film, FILM_110, STRIP

MM_PER_INCH = 25.4

# --------------------------------------------------------------- thresholds
#
# All are the proposal's own numbers (docs/film-110-proposal.md, and the
# task spec that refined it) except where noted "(tuned)" -- those are
# genuinely mine, made against the fixtures, and documented as such.

AIR_PERCENTILE = 99.5          # column-median percentile defining "air"
AIR_FRAC = 0.95                 # >= this fraction of air -> "air" column
PLASTIC_FRAC = 0.05              # <= this fraction of air -> "plastic" column (real rail: 0.033)
PLASTIC_MIN_RUN_MM = 2.0         # shorter dark runs are film (the 110 printed border reads 0.066)
FILM_WIDTH_TOLERANCE_MM = 1.0
# A 16 mm strip lies loose across the 24 mm opening; its far edge can slide
# under the rail on that side (Test 103, 2026-10-21: 0.6 mm for one strip,
# 2.1 mm for the other), so the visible band is narrower than the film by
# that much. Accepted up to this, when the narrow side abuts plastic (the
# perforated edge must still face air); the hidden width is reported as
# `rail_hidden_mm` and the picture is clipped by whatever of its own margin
# lies under the rail.
RAIL_HIDDEN_MAX_MM = 3.0
FILM_PRESENT_FRAC = 0.85        # < this fraction of air -> film present (a line)
HOLE_EDGE_MARGIN_MM = 2.5        # lateral search band next to the perforated edge
HOLE_FRAC = 0.95                # >= this fraction of air -> hole (air-level) line
HOLE_MIN_MM = 0.8
HOLE_MAX_MM = 4.0
# (tuned) A run of "film present" shorter than this is noise, not a real
# free end or continuation: the aperture crop's very first (and last)
# line can be a single-line partial-pixel blend of plastic and air right
# at the crop boundary, which reads dark enough to look like "film
# present" for exactly one line (observed on real hardware data,
# 2026-09-19: svep600-f1.tiff's line 0 alone, followed by ~300 genuinely
# clear-film lines). Below this, a "touches line 0 / touches the last
# line" run is not trusted.
FILM_PRESENT_MIN_RUN_MM = 0.15
REFINE_WINDOW_MM = 0.5
REFINE_SMOOTH_MM = 0.1
REFINE_NEIGHBOURHOOD_MM = 3.0
REFINE_GRADIENT_RATIO = 3.0
FOOT_WALK_MM = 0.6              # how far a refined edge may walk out to the transition's foot
FOOT_STOP_FRAC = 0.10           # ... while the step per pixel stays above this fraction of the peak
LEVEL_NEAR_MM = 0.3             # the inside/outside levels that decide an edge's step direction
LEVEL_FAR_MM = 1.0              # ... are medians over this band either side of the predicted edge
LEVEL_MIN_CONTRAST = 0.08       # below this relative level difference nothing is refined (real 110 edges: > 0.6)
WHOLE_MARGIN_MM = 0.3
LEADING_CONTINUATION_MM = 1.0
LEADING_CONTINUATION_FRAC = 0.8
CLEAR_BASE_PERCENTILE = 99.0

# Design choices not dictated by the spec, made here and documented:
#  - all "levels" (air, film presence, holes, gradients) are read from the
#    GREEN channel, the same choice step (a) makes explicit for the "air"
#    level -- kept for every other level so one aperture is judged on one
#    consistent scale.
#  - the ambiguous case (film band touching air on BOTH lateral edges):
#    each edge's hole search is tried and the one that finds valid runs
#    wins; if both do (not expected on a real 16 mm-in-24 mm-aperture
#    strip), the low-column edge wins -- documented, not exercised by any
#    known fixture.
#  - candidate sub-pixel position at a refined edge is the midpoint of
#    the strongest-gradient sample pair (index + 0.5), which is accurate
#    to about one line/column -- comfortably inside the +/-0.3 mm
#    acceptance band at both 600 and 3600 dpi.


@dataclass
class FrameFind:
    """One predicted 110 image inside a single aperture.

    ``index`` is 1-based, in perforation order, *within this aperture
    only* (a running image number across a whole scan is the CLI's job,
    not this module's -- see cli.py). ``line0``/``line1``/``col0``/
    ``col1`` are fractional positions in the input image's own axes
    (line0/line1 may lie outside ``[0, n_lines)`` for a split image).
    ``refined`` maps each edge name ("line0", "line1", "col0", "col1") to
    whether local refinement moved it (False = the perforation-based
    prediction stands, unrefined).
    """

    index: int
    perforation_line: float
    line0: float
    line1: float
    col0: float
    col1: float
    whole: bool
    split: "str | None"
    refined: dict
    free_end_near: bool
    size_mm: tuple
    orientation: str
    col_clamp: "tuple | None" = None   # (lo, hi): the film band minus RAIL_EDGE_INSET_MM; `crop` never pads past it


@dataclass
class ApertureFind:
    """The detector's verdict for one aperture."""

    empty: bool
    film_cols: "tuple | None"
    film_line_start: "float | None"
    film_line_end: "float | None"
    leading_continuation: bool
    frames: list = field(default_factory=list)
    reason: str = ""
    rail_hidden_mm: float = 0.0        # film width hidden under a rail (0 = fully visible)


# ------------------------------------------------------------- small helpers


def _longest_true_run(mask: np.ndarray):
    """(start, end_exclusive) of the longest run of True in `mask`, or
    None if there is none."""
    best = None
    n = len(mask)
    i = 0
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            if best is None or (j - i) > (best[1] - best[0]):
                best = (i, j)
            i = j
        else:
            i += 1
    return best


def _true_runs(mask: np.ndarray):
    """[(start, end_inclusive), ...] of every run of True in `mask`."""
    runs = []
    n = len(mask)
    i = 0
    while i < n:
        if mask[i]:
            j = i
            while j < n and mask[j]:
                j += 1
            runs.append((i, j - 1))
            i = j
        else:
            i += 1
    return runs


def _box_smooth(profile: np.ndarray, box: int) -> np.ndarray:
    prof = np.asarray(profile, dtype=np.float64)
    if box <= 1 or prof.size == 0:
        return prof
    kernel = np.ones(box, dtype=np.float64) / box
    return np.convolve(prof, kernel, mode="same")


def _refine_edge(profile: np.ndarray, predicted: float, px_per_mm: float,
                 outward: int, limit_lo: float | None = None,
                 limit_hi: float | None = None) -> tuple:
    """Refine one edge within +/-REFINE_WINDOW_MM of `predicted` against
    `profile` (a 1-D line- or column-level profile). `outward` says on
    which side of the edge the OUTSIDE of the image lies: < 0 at lower
    indices (the image starts here), > 0 at higher indices (the image
    ends here). `limit_lo`/`limit_hi` clip the search window. Returns
    (position, refined_bool).

    The direction of the step is NOT assumed -- it is read from the
    profile itself: the median level LEVEL_NEAR_MM..LEVEL_FAR_MM inside
    the predicted edge against the same band outside it. On 110 film the
    picture sits inside a pre-exposed dark printed border on all four
    sides, so leaving the picture gets DARKER; on a 35 mm-like clear
    surround it gets brighter. The first version of this function assumed
    the clear case and, on the real 110 strip, locked onto the fall-off
    of the bright gate-edge halo just INSIDE the picture on every edge
    (0.1-0.4 mm of picture cut on the trailing edges; found by Astra's
    review, measured 2026-09-20). If the two levels do not differ by at
    least LEVEL_MIN_CONTRAST of the larger one there is no edge to refine
    and the prediction stands.

    The position returned is the OUTER FOOT of the transition, not its
    steepest point: from the strongest step of the expected direction the
    search walks outward while the profile keeps changing that way, for
    at most FOOT_WALK_MM, so the soft camera-gate ramp stays inside the
    crop.
    """
    n = len(profile)
    if n == 0 or predicted < 0 or predicted > n - 1:
        return predicted, False
    box = max(1, round(REFINE_SMOOTH_MM * px_per_mm))
    smoothed = _box_smooth(profile, box)

    # Which way does the profile step at this edge? Compare the level a
    # little way inside the predicted edge with the level the same
    # distance outside it.
    near = LEVEL_NEAR_MM * px_per_mm
    far = LEVEL_FAR_MM * px_per_mm
    if outward > 0:
        in_lo, in_hi = predicted - far, predicted - near
        out_lo, out_hi = predicted + near, predicted + far
    else:
        in_lo, in_hi = predicted + near, predicted + far
        out_lo, out_hi = predicted - far, predicted - near
    in_lo, in_hi = max(0, int(round(in_lo))), min(n, int(round(in_hi)))
    out_lo, out_hi = max(0, int(round(out_lo))), min(n, int(round(out_hi)))
    if in_hi - in_lo < 2 or out_hi - out_lo < 2:
        return predicted, False
    inside = float(np.median(smoothed[in_lo:in_hi]))
    outside = float(np.median(smoothed[out_lo:out_hi]))
    contrast = abs(outside - inside)
    if contrast < LEVEL_MIN_CONTRAST * max(inside, outside, 1.0):
        return predicted, False
    # Expected sign of the step along increasing index.
    step_sign = (1 if outside > inside else -1) * (1 if outward > 0 else -1)

    window_px = max(1, round(REFINE_WINDOW_MM * px_per_mm))
    lo = max(0, int(np.floor(predicted)) - window_px)
    hi = min(n - 1, int(np.ceil(predicted)) + window_px)
    if limit_lo is not None:
        lo = max(lo, int(np.ceil(limit_lo)))
    if limit_hi is not None:
        hi = min(hi, int(np.floor(limit_hi)))
    if hi <= lo:
        return predicted, False
    grad = np.diff(smoothed[lo:hi + 1]) * step_sign
    if grad.size == 0:
        return predicted, False
    i = int(np.argmax(grad))
    g = float(grad[i])
    if g <= 0:
        return predicted, False
    candidate = lo + i  # index of the steepest step (between i and i+1)

    nb_px = max(2, round(REFINE_NEIGHBOURHOOD_MM * px_per_mm))
    nlo = max(0, int(round(predicted - nb_px / 2)))
    nhi = min(n - 1, int(round(predicted + nb_px / 2)))
    if nhi - nlo < 2:
        return predicted, False
    nb_grad = np.abs(np.diff(smoothed[nlo:nhi + 1]))
    if nb_grad.size == 0:
        return predicted, False
    scale = float(np.median(nb_grad))
    if scale <= 0:
        return predicted, False
    if g < REFINE_GRADIENT_RATIO * scale:
        return predicted, False

    # Walk to the outer foot: keep going outward while the profile keeps
    # stepping in the expected direction by at least FOOT_STOP_FRAC of
    # the peak step.
    walk_px = max(1, round(FOOT_WALK_MM * px_per_mm))
    floor = FOOT_STOP_FRAC * g
    if outward < 0:
        k = candidate
        while k - 1 >= max(0, candidate - walk_px) and \
                (smoothed[k] - smoothed[k - 1]) * step_sign >= floor:
            k -= 1
        pos = k
    else:
        k = candidate + 1
        while k + 1 <= min(n - 1, candidate + 1 + walk_px) and \
                (smoothed[k + 1] - smoothed[k]) * step_sign >= floor:
            k += 1
        pos = k
    return float(pos) + 0.5, True


LATERAL_CONTRAST_NEAR_MM = 0.1   # the bands beside a lateral step candidate ...
LATERAL_CONTRAST_FAR_MM = 0.4    # ... run from NEAR to FAR on each side of it
LATERAL_MIN_CONTRAST = 0.04      # relative level difference across the step (second strip: 0.05-0.12)
LATERAL_CANDIDATE_FRAC = 0.5     # a lateral step candidate is at least this fraction of the window's strongest


def _refine_lateral(profile: np.ndarray, predicted: float, px_per_mm: float,
                    outward: int, limit_lo: float | None = None,
                    limit_hi: float | None = None) -> tuple:
    """Direction-free variant of `_refine_edge` for the lateral edges
    (see the caller): the strongest step of either sign within
    +/-REFINE_WINDOW_MM of `predicted` (clipped to the limits), accepted
    when the median levels LATERAL_CONTRAST_NEAR_MM..FAR_MM on its two
    sides differ by at least LATERAL_MIN_CONTRAST of the larger and the
    step is >= REFINE_GRADIENT_RATIO x the neighbourhood's median step;
    then walked outward to the foot exactly as `_refine_edge` does.
    Returns (position, refined_bool)."""
    n = len(profile)
    if n == 0 or predicted < 0 or predicted > n - 1:
        return predicted, False
    box = max(1, round(REFINE_SMOOTH_MM * px_per_mm))
    smoothed = _box_smooth(profile, box)
    window_px = max(1, round(REFINE_WINDOW_MM * px_per_mm))
    lo = max(0, int(np.floor(predicted)) - window_px)
    hi = min(n - 1, int(np.ceil(predicted)) + window_px)
    if limit_lo is not None:
        lo = max(lo, int(np.ceil(limit_lo)))
    if limit_hi is not None:
        hi = min(hi, int(np.floor(limit_hi)))
    if hi <= lo:
        return predicted, False
    grad = np.diff(smoothed[lo:hi + 1])
    if grad.size == 0:
        return predicted, False
    peak = float(np.max(np.abs(grad)))
    if peak <= 0:
        return predicted, False

    nb_px = max(2, round(REFINE_NEIGHBOURHOOD_MM * px_per_mm))
    nlo = max(0, int(round(predicted - nb_px / 2)))
    nhi = min(n - 1, int(round(predicted + nb_px / 2)))
    nb_grad = np.abs(np.diff(smoothed[nlo:nhi + 1]))
    scale = float(np.median(nb_grad)) if nb_grad.size else 0.0

    # Candidates: local maxima of |step| at least LATERAL_CANDIDATE_FRAC of
    # the strongest step in the window and REFINE_GRADIENT_RATIO x the
    # neighbourhood's median step. Taken INNERMOST first (nearest the
    # picture): a border's outer edge (border -> clear film) can be the
    # stronger step, and the picture's edge is the first step outward
    # from the picture that has real contrast on both sides. Picture
    # content steps inside the frame fail the contrast test (their
    # neighbourhoods are picture on both sides) and are passed over.
    near = LATERAL_CONTRAST_NEAR_MM * px_per_mm
    far = LATERAL_CONTRAST_FAR_MM * px_per_mm
    order = range(grad.size) if outward > 0 else range(grad.size - 1, -1, -1)
    candidate = None
    for i in order:
        g = float(grad[i])
        if abs(g) < LATERAL_CANDIDATE_FRAC * peak:
            continue
        if scale > 0 and abs(g) < REFINE_GRADIENT_RATIO * scale:
            continue
        if (i > 0 and abs(grad[i - 1]) > abs(g)) or \
                (i + 1 < grad.size and abs(grad[i + 1]) > abs(g)):
            continue  # not a local maximum
        c = lo + i
        a_lo, a_hi = int(round(c - far)), int(round(c - near))
        b_lo, b_hi = int(round(c + 1 + near)), int(round(c + 1 + far))
        a_lo, b_hi = max(0, a_lo), min(n, b_hi)
        if a_hi - a_lo < 2 or b_hi - b_lo < 2:
            continue
        level_a = float(np.median(smoothed[a_lo:a_hi]))
        level_b = float(np.median(smoothed[b_lo:b_hi]))
        if abs(level_b - level_a) < LATERAL_MIN_CONTRAST * max(level_a, level_b, 1.0):
            continue
        candidate = c
        break
    if candidate is None:
        return predicted, False
    g = float(grad[candidate - lo])
    step_sign = 1 if g > 0 else -1

    walk_px = max(1, round(FOOT_WALK_MM * px_per_mm))
    floor = FOOT_STOP_FRAC * abs(g)
    if outward < 0:
        k = candidate
        while k - 1 >= max(0, candidate - walk_px) and \
                (smoothed[k] - smoothed[k - 1]) * step_sign >= floor:
            k -= 1
        pos = k
    else:
        k = candidate + 1
        while k + 1 <= min(n - 1, candidate + 1 + walk_px) and \
                (smoothed[k + 1] - smoothed[k]) * step_sign >= floor:
            k += 1
        pos = k
    return float(pos) + 0.5, True


# Per-line tracking of the film's air-facing edge. The band edge from the
# column medians is the MEDIAN edge over the whole aperture; on real data
# (2026-09-19, 3600 dpi) the free lateral edge of a 16 mm strip wandered
# by 1-2 mm along one aperture, so a search zone fixed next to the median
# edge saw plain air as "holes" wherever the film had moved inward. The
# hole zone is therefore placed relative to the edge found on EACH line.
# The perforation itself does not break the film edge: on this film the
# hole sits ~0.8 mm inside the edge, in a dark printed border, with an
# intact ~0.6 mm rim outside it (measured 2026-09-19), so a hole is an
# air-level region INSIDE the band next to the edge, not a jump of the
# edge.
EDGE_TRANSITION_FRAC = 0.6      # green < this x air = film, scanning in from the air side
EDGE_SEARCH_MM = 4.0            # per-line search zone either side of the median band edge
EDGE_TRACK_WINDOW_MM = 10.0     # rolling-median window for the local edge (> 2 x HOLE_MAX_MM)
RAIL_TRANSITION_MM = 0.5        # the rail's blurred edge, excluded from the edge tracker's zone
HOLE_ZONE_FROM_MM = 0.3         # hole zone starts this far inside the per-line film edge ...
HOLE_ZONE_TO_MM = 3.0           # ... and ends this far inside it
HOLE_STUB_MIN_MM = 0.3          # a hole cut by the aperture boundary may be this short


def _track_edge(green: np.ndarray, band_edge: int, air: float,
                px_per_mm: float, edge_is_low: bool,
                outer_limit: "int | None" = None) -> np.ndarray:
    """Per-line column of the film's air-facing edge (float, NaN where no
    film is seen in the search zone -- e.g. beyond a free strip end).
    `outer_limit` clips the search zone on the air side, short of the
    rail (a strip lying free in the channel, air on both sides, has the
    rail within EDGE_SEARCH_MM of its edge, and the rail and its blurred
    edge are as dark as film -- Test 104's first pass tracked the rail as
    the film edge and found no holes)."""
    n_lines, width = green.shape
    search = max(1, round(EDGE_SEARCH_MM * px_per_mm))
    z_lo, z_hi = max(0, band_edge - search), min(width, band_edge + search)
    if outer_limit is not None:
        if edge_is_low:
            z_lo = max(z_lo, outer_limit)
        else:
            z_hi = min(z_hi, outer_limit)
    if z_hi <= z_lo:
        return np.full(n_lines, np.nan)
    zone = green[:, z_lo:z_hi]
    if not edge_is_low:
        zone = zone[:, ::-1]
    film_mask = zone < EDGE_TRANSITION_FRAC * air
    has = film_mask.any(axis=1)
    first = np.argmax(film_mask, axis=1).astype(np.float64)
    if edge_is_low:
        edge = z_lo + first
    else:
        edge = (z_hi - 1) - first
    return np.where(has, edge, np.nan)


def _rolling_nanmedian(x: np.ndarray, window: int) -> np.ndarray:
    """Rolling median ignoring NaN, same length as x (edges padded by
    repetition). Window is forced odd. All-NaN windows give NaN."""
    n = len(x)
    if n == 0:
        return x
    window = max(1, min(window | 1, n | 1))
    half = window // 2
    padded = np.concatenate([np.full(half, x[0]), x, np.full(half, x[-1])])
    view = np.lib.stride_tricks.sliding_window_view(padded, window)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        med = np.nanmedian(view, axis=1)
    return med[:n]


def _hole_runs(green: np.ndarray, lo: int, hi: int, width: int, air: float,
               film_present: np.ndarray, px_per_mm: float, edge_is_low: bool,
               outer_limit: "int | None" = None):
    """Candidate perforation runs on one lateral edge of the film band,
    as (start, end_inclusive) line runs, plus the per-line LOCAL film
    edge (rolling median of the tracked edge) used for lateral
    prediction. A hole line is one whose zone [edge + HOLE_ZONE_FROM_MM,
    edge + HOLE_ZONE_TO_MM] (measured inward from THAT line's film edge)
    reaches air level."""
    n_lines = green.shape[0]
    band_edge = lo if edge_is_low else hi
    edge = _track_edge(green, band_edge, air, px_per_mm, edge_is_low, outer_limit)
    local = _rolling_nanmedian(edge, max(3, round(EDGE_TRACK_WINDOW_MM * px_per_mm)))
    ref = np.where(np.isnan(edge), local, edge)
    valid = ~np.isnan(ref)
    ref_i = np.where(valid, ref, band_edge).astype(np.int64)
    offs = np.arange(round(HOLE_ZONE_FROM_MM * px_per_mm),
                     max(round(HOLE_ZONE_FROM_MM * px_per_mm) + 1,
                         round(HOLE_ZONE_TO_MM * px_per_mm)))
    if edge_is_low:
        cols = ref_i[:, None] + offs[None, :]
    else:
        cols = ref_i[:, None] - offs[None, :]
    cols = np.clip(cols, 0, width - 1)
    zone_max = np.max(green[np.arange(n_lines)[:, None], cols], axis=1)
    hole_bool = (zone_max >= HOLE_FRAC * air) & film_present & valid
    runs = _true_runs(hole_bool)
    min_len = HOLE_MIN_MM * px_per_mm
    max_len = HOLE_MAX_MM * px_per_mm
    stub_len = HOLE_STUB_MIN_MM * px_per_mm
    # A hole may be cut short by the aperture's boundary OR by the strip's
    # own cut end (Test 104: a strip cut through a hole, starting inside
    # the aperture): a stub touching either counts.
    present_runs = _true_runs(film_present)
    cut = {0, n_lines - 1}
    for (ps, pe) in present_runs:
        cut.update((ps, pe))
    slack = max(1, round(HOLE_STUB_MIN_MM * px_per_mm))

    def at_cut(s, e):
        return any(abs(s - c) <= slack or abs(e - c) <= slack for c in cut)

    runs = [(s, e) for (s, e) in runs
            if (min_len <= (e - s + 1) <= max_len)
            or (at_cut(s, e) and stub_len <= (e - s + 1) <= max_len)]
    return runs, local


# Plausibility of a predicted image: the mean of the interior profile
# over the part of the window inside the aperture must be denser than
# this fraction of the clear-base level, else the window is clear film
# (a hole at the strip's cut end predicts an image that is not there).
IMAGE_DENSITY_FRAC = 0.85
IMAGE_VARIATION_FRAC = 0.10       # std/mean of the profile inside a window that is image, not base
PLAUSIBILITY_TRIM_MM = 1.0        # ... measured on the window's interior: its ends may hold the
                                  # aperture edge or the next frame's border (Test 104: a clear
                                  # leader passed as "varying" on its first dark lines alone)
RAIL_GUARD_MM = 0.4             # lateral refinement stays this far from the rail-side band edge
RIM_GUARD_MM = 1.0              # ... and this far inside the perforated film edge (rim 0.6 mm, hole 0.6-2.1 mm)
PRODUCT_PAD_MM = 0.25           # the CLI grows a whole image's crop by this on every side
MIN_OVERLAP_MM = 1.0            # a window overlapping the aperture by less carries nothing
MERGE_FRAC_OF_PITCH = 0.5       # two predictions closer than this are the same image


RAIL_EDGE_INSET_MM = 0.05         # `crop` never pads past the film band minus this (rail rim, light gap, air)


def _candidates(runs, film: Film, px_per_mm: float, reversed_: bool):
    """Every hole predicts the image AFTER it (hole end + lead) and the
    image BEFORE it (hole start - trail); consecutive holes predict the
    same image twice and those are merged. `reversed_` swaps lead and
    trail (the strip inserted the other way round). Returns
    [(line0, line1, perforation_line), ...] sorted by line0."""
    lead = film.perforation_lead_mm * px_per_mm
    trail = film.perforation_trail_mm * px_per_mm
    if reversed_:
        lead, trail = trail, lead
    img = film.image_mm[0] * px_per_mm
    cands = []
    for (s, e) in runs:
        cands.append((e + lead, e + lead + img, float(e)))
        cands.append((s - trail - img, s - trail, float(s)))
    cands.sort()
    merged: list = []
    tol = MERGE_FRAC_OF_PITCH * film.pitch_mm * px_per_mm
    for c in cands:
        if merged and abs(c[0] - merged[-1][0]) < tol:
            m = merged[-1]
            merged[-1] = ((m[0] + c[0]) / 2, (m[1] + c[1]) / 2, m[2])
        else:
            merged.append(c)
    return merged


def detect(image, *, dpi: float, film: Film = FILM_110) -> ApertureFind:
    """Find every 110 image predicted inside one aperture-registered
    image. Never raises on image content -- an aperture the detector
    cannot resolve comes back with `reason` set and `frames` empty; the
    caller's aperture product is unaffected either way.
    """
    arr = np.asarray(image)
    if arr.ndim != 3 or arr.shape[2] < 3:
        return ApertureFind(False, None, None, None, False, [],
                             f"expected a (lines, width, channels>=3) image, got shape {arr.shape}")
    n_lines, width = arr.shape[0], arr.shape[1]
    if n_lines == 0 or width == 0:
        return ApertureFind(True, None, None, None, False, [], "empty aperture")

    px_per_mm = dpi / MM_PER_INCH
    green = arr[:, :, 1].astype(np.float64)

    # a. Levels and column classes.
    col_medians = np.median(green, axis=0)
    air = float(np.percentile(col_medians, AIR_PERCENTILE))
    if air <= 0:
        return ApertureFind(True, None, None, None, False, [], "empty aperture")
    is_air = col_medians >= AIR_FRAC * air
    # Plastic only where a dark run is at least PLASTIC_MIN_RUN_MM wide:
    # the film's own dark printed border (~1.2 mm, 0.066 x air on the
    # 2026-09-19 strip) must stay inside the band.
    plastic_raw = col_medians <= PLASTIC_FRAC * air
    is_plastic = np.zeros_like(plastic_raw)
    min_plastic = PLASTIC_MIN_RUN_MM * px_per_mm
    for (ps, pe) in _true_runs(plastic_raw):
        if (pe - ps + 1) >= min_plastic:
            is_plastic[ps:pe + 1] = True
    is_film = ~is_air & ~is_plastic
    if not is_film.any():
        return ApertureFind(True, None, None, None, False, [], "empty aperture")

    # b. Film band: the longest run of film columns.
    lo, hi = _longest_true_run(is_film)
    band_width_mm = (hi - lo) / px_per_mm

    touches_air_lo = lo > 0 and bool(is_air[lo - 1])
    touches_air_hi = hi < width and bool(is_air[hi])
    touches_plastic_lo = lo > 0 and bool(is_plastic[lo - 1])
    touches_plastic_hi = hi < width and bool(is_plastic[hi])
    rail_hidden_mm = 0.0
    if abs(band_width_mm - film.width_mm) > FILM_WIDTH_TOLERANCE_MM:
        hidden = film.width_mm - band_width_mm
        under_rail = (touches_plastic_lo != touches_plastic_hi) and (touches_air_lo or touches_air_hi)
        if not (0.0 < hidden <= RAIL_HIDDEN_MAX_MM and under_rail):
            return ApertureFind(False, (lo, hi), None, None, False, [],
                                 f"no 110 film band ({band_width_mm:.1f} mm)")
        rail_hidden_mm = float(hidden)

    # Interior columns for the along-transport profile: the band with the
    # perforation search margin trimmed off BOTH sides, so a hole (which
    # can only ever be within HOLE_EDGE_MARGIN_MM of ONE edge) never
    # biases the per-line median regardless of which edge that turns out
    # to be.
    margin_px = max(1, round(HOLE_EDGE_MARGIN_MM * px_per_mm))
    int_lo, int_hi = lo + margin_px, hi - margin_px
    if int_hi <= int_lo:
        int_lo, int_hi = lo, hi
    interior_profile = np.median(green[:, int_lo:int_hi], axis=1)

    # c. Film presence along transport, and the free-end positions. Runs
    # shorter than FILM_PRESENT_MIN_RUN_MM are discarded as crop-boundary
    # noise before deciding whether film reaches line 0 / the last line.
    film_present = interior_profile < FILM_PRESENT_FRAC * air
    min_run_px = max(1, round(FILM_PRESENT_MIN_RUN_MM * px_per_mm))
    present_runs = [(s, e) for (s, e) in _true_runs(film_present)
                     if (e - s + 1) >= min_run_px]
    if not present_runs:
        film_line_start = film_line_end = None
    else:
        first_s, _first_e = present_runs[0]
        _last_s, last_e = present_runs[-1]
        film_line_start = None if first_s == 0 else float(first_s)
        film_line_end = None if last_e == n_lines - 1 else float(last_e)

    # leading_continuation: the aperture already opens on image content
    # (no free end at line 0), and that content is dense (dark) rather
    # than clear film -- i.e. a split image's tail is continuing in.
    if film_line_start is not None:
        leading_continuation = False
    else:
        first_n = min(n_lines, max(1, round(LEADING_CONTINUATION_MM * px_per_mm)))
        first_profile = interior_profile[:first_n]
        if film_present.any():
            clear_base = float(np.percentile(interior_profile[film_present],
                                              CLEAR_BASE_PERCENTILE))
        else:
            clear_base = air
        leading_continuation = bool(
            first_profile.size and np.median(first_profile) < LEADING_CONTINUATION_FRAC * clear_base
        )

    if not (touches_air_lo or touches_air_hi):
        return ApertureFind(False, (lo, hi), film_line_start, film_line_end,
                             leading_continuation, [],
                             "film band not adjacent to air; perforations hidden",
                             rail_hidden_mm)

    # d. Perforations: resolve the perforated edge and its hole runs. The
    # edge tracker's zone stops at the first plastic column beyond the
    # band on either side (a strip lying free in the channel has air AND
    # the rail within reach of its edge).
    # The edge tracker's zone must stop short of the rail: the rail is as
    # dark as film, and so is its blurred edge, which begins about
    # RAIL_TRANSITION_MM before the first column classed plastic. (It must
    # NOT stop at the first non-air column: where a strip covers only
    # part of the aperture's lines, the columns of its rim and holes are
    # air in the median and the band ends short of the real film edge.)
    plastic_cols = np.flatnonzero(is_plastic)
    transition = int(round(RAIL_TRANSITION_MM * px_per_mm))
    before = plastic_cols[plastic_cols < lo]
    after = plastic_cols[plastic_cols >= hi]
    limit_lo = int(before[-1]) + 1 + transition if before.size else None
    limit_hi = int(after[0]) - transition if after.size else None
    edge_is_low = None
    runs: list = []
    local_edge = None
    if touches_air_lo and not touches_air_hi:
        edge_is_low = True
        runs, local_edge = _hole_runs(green, lo, hi, width, air, film_present, px_per_mm, True, limit_lo)
    elif touches_air_hi and not touches_air_lo:
        edge_is_low = False
        runs, local_edge = _hole_runs(green, lo, hi, width, air, film_present, px_per_mm, False, limit_hi)
    else:
        runs_lo, local_lo = _hole_runs(green, lo, hi, width, air, film_present, px_per_mm, True, limit_lo)
        runs_hi, local_hi = _hole_runs(green, lo, hi, width, air, film_present, px_per_mm, False, limit_hi)
        if runs_lo and not runs_hi:
            edge_is_low, runs, local_edge = True, runs_lo, local_lo
        elif runs_hi and not runs_lo:
            edge_is_low, runs, local_edge = False, runs_hi, local_hi
        elif runs_lo and runs_hi:
            edge_is_low, runs, local_edge = True, runs_lo, local_lo  # tie-break: documented above
        else:
            edge_is_low, runs, local_edge = None, [], None

    if not runs:
        reason = ("no perforation found near either film edge" if edge_is_low is None
                   else "no perforation found in the film band")
        return ApertureFind(False, (lo, hi), film_line_start, film_line_end,
                             leading_continuation, [], reason, rail_hidden_mm)

    perforated_edge_col = lo if edge_is_low else hi
    lat_len_px = film.image_mm[1] * px_per_mm
    offset_px = film.image_lateral_offset_mm * px_per_mm
    whole_margin_px = WHOLE_MARGIN_MM * px_per_mm
    pitch_px = film.pitch_mm * px_per_mm
    min_overlap_px = MIN_OVERLAP_MM * px_per_mm
    if film_present.any():
        clear_base = float(np.percentile(interior_profile[film_present],
                                          CLEAR_BASE_PERCENTILE))
    else:
        clear_base = air

    def _evaluate(reversed_: bool):
        """Refine and classify every plausible candidate under one
        lead/trail assignment; returns (frames, score) where score is
        the number of along-transport edges the data confirmed."""
        out = []
        score = 0
        for (line0, line1, perforation_line) in _candidates(runs, film, px_per_mm, reversed_):
            clo = max(0, int(np.floor(line0)))
            chi = min(n_lines, int(np.ceil(line1)))
            if chi - clo < min_overlap_px:
                continue
            # Plausibility: the window must look like an image -- denser
            # than clear film, OR varying along its length (image content
            # varies, clear film does not). The second test matters where
            # the aperture holds little clear film to estimate the base
            # from (a fogged leader plus one image, 2026-09-19 aperture 1).
            trim = int(round(PLAUSIBILITY_TRIM_MM * px_per_mm))
            if chi - clo > 4 * trim:
                seg = interior_profile[clo + trim:chi - trim]
            else:
                seg = interior_profile[clo:chi]
            seg_mean = float(np.mean(seg))
            seg_std = float(np.std(seg))
            if (seg_mean >= IMAGE_DENSITY_FRAC * clear_base
                    and seg_std < IMAGE_VARIATION_FRAC * max(seg_mean, 1.0)):
                continue

            r_line0, ok_l0 = _refine_edge(interior_profile, line0, px_per_mm, outward=-1)
            r_line1, ok_l1 = _refine_edge(interior_profile, line1, px_per_mm, outward=+1)
            score += int(ok_l0) + int(ok_l1)

            # Lateral prediction from the LOCAL film edge over this image's
            # own lines (the tracked edge's rolling median), not the
            # aperture-wide median edge: the free edge of a 16 mm strip can
            # wander 1-2 mm along one aperture (2026-09-19), and the image
            # follows the film.
            edge_here = perforated_edge_col
            if local_edge is not None:
                seg = local_edge[clo:chi]
                seg = seg[~np.isnan(seg)]
                if seg.size:
                    edge_here = float(np.median(seg))
            if edge_is_low:
                col0 = edge_here + offset_px
                col1 = col0 + lat_len_px
            else:
                col1 = edge_here - offset_px
                col0 = col1 - lat_len_px

            # Lateral refinement, both sides, direction-free: the border
            # beside the picture is DARKER than it on the first real strip
            # (a pre-exposed printed border, 2026-09-19) and LIGHTER on the
            # second (a ~0.5 mm fogged margin of density ~0.8 next to a
            # denser picture, 2026-09-20), and a level comparison around
            # the prediction straddles border and picture when the
            # prediction is 0.4 mm off. So the strongest step of EITHER
            # sign in the window is taken, its contrast checked in the
            # bands right beside it, and the edge walked outward to the
            # foot. The search stays RIM_GUARD_MM inside the perforated
            # film edge (rim and hole) and RAIL_GUARD_MM short of the rail
            # side of the band (the rail's bright rim).
            col_profile = np.median(green[clo:chi, :], axis=0)
            guard = RAIL_GUARD_MM * px_per_mm
            rim_guard = RIM_GUARD_MM * px_per_mm
            if edge_is_low:
                r_col0, ok_c0 = _refine_lateral(col_profile, col0, px_per_mm, outward=-1,
                                                limit_lo=edge_here + rim_guard)
                r_col1, ok_c1 = _refine_lateral(col_profile, col1, px_per_mm, outward=+1,
                                                limit_hi=hi - guard)
            else:
                r_col0, ok_c0 = _refine_lateral(col_profile, col0, px_per_mm, outward=-1,
                                                limit_lo=lo + guard)
                r_col1, ok_c1 = _refine_lateral(col_profile, col1, px_per_mm, outward=+1,
                                                limit_hi=edge_here - rim_guard)

            inset = RAIL_EDGE_INSET_MM * px_per_mm
            col_clamp = (lo + inset, hi - inset)

            # Not whole = split, by the same margin: an image whose start
            # (end) lies within WHOLE_MARGIN_MM of the aperture's edge has
            # that edge under the bar as far as the product is concerned
            # (Test 103: image 4 seated at +0.03 mm was "not whole" with
            # no split side, and the hook reported it split by no bar).
            whole = (r_line0 >= whole_margin_px) and (r_line1 <= n_lines - whole_margin_px)
            if r_line0 < whole_margin_px:
                split = "leading"
            elif r_line1 > n_lines - whole_margin_px:
                split = "trailing"
            else:
                split = None

            free_end_near = False
            for fend in (film_line_start, film_line_end):
                if fend is None:
                    continue
                if abs(r_line0 - fend) <= pitch_px or abs(r_line1 - fend) <= pitch_px:
                    free_end_near = True
                    break

            size_mm = ((r_line1 - r_line0) / px_per_mm, (r_col1 - r_col0) / px_per_mm)
            out.append(FrameFind(
                index=len(out) + 1,
                perforation_line=perforation_line,
                line0=r_line0, line1=r_line1,
                col0=r_col0, col1=r_col1,
                whole=whole, split=split,
                refined={"line0": ok_l0, "line1": ok_l1, "col0": ok_c0, "col1": ok_c1},
                free_end_near=free_end_near,
                size_mm=size_mm,
                orientation="hole-before-image" if not reversed_ else "hole-after-image",
                col_clamp=col_clamp,
            ))
        return out, score

    # The strip may be inserted either way round; the two assignments of
    # lead/trail differ by ~1 mm, more than the refinement window, so the
    # data decides: the assignment whose predicted along-transport edges
    # the profile confirms more often wins; a tie keeps the normal one.
    frames_normal, score_normal = _evaluate(False)
    frames_rev, score_rev = _evaluate(True)
    frames_out = frames_rev if score_rev > score_normal else frames_normal

    return ApertureFind(False, (lo, hi), film_line_start, film_line_end,
                         leading_continuation, frames_out, "", rail_hidden_mm)


def product_bounds(find: FrameFind, *, dpi: float | None = None, pad_mm: float = 0.0,
                   shape=None) -> tuple:
    """The integer slice bounds (line_lo, line_hi, col_lo, col_hi) `crop`
    uses: [find.line0:find.line1, find.col0:find.col1] grown outward by
    `pad_mm` on every side (needs `dpi`; both axes use dpi/25.4 px per
    mm, as `detect` does), the columns never padded past
    `find.col_clamp` (the film band minus RAIL_EDGE_INSET_MM: beyond it
    lie the rail's bright rim, the light gap at the film's cut edge, or
    air -- nothing a picture can contain), and everything clamped to
    `shape` (lines, cols) when given. Shared by `crop` and by
    tools/film110_check.py --edge-check so the check measures the
    product that is actually written.
    """
    if pad_mm and dpi is None:
        raise ValueError("pad_mm needs dpi")
    pad = pad_mm * dpi / MM_PER_INCH if pad_mm else 0.0
    lo_l = int(np.floor(find.line0 - pad))
    hi_l = int(np.ceil(find.line1 + pad))
    lo_c = int(np.floor(find.col0 - pad))
    hi_c = int(np.ceil(find.col1 + pad))
    if find.col_clamp is not None:
        c_lo, c_hi = find.col_clamp
        lo_c = max(lo_c, int(np.ceil(c_lo)))
        hi_c = min(hi_c, int(np.floor(c_hi)))
    if shape is not None:
        n_lines, n_cols = shape[0], shape[1]
        lo_l = min(max(lo_l, 0), n_lines)
        hi_l = min(max(hi_l, lo_l), n_lines)
        lo_c = min(max(lo_c, 0), n_cols)
        hi_c = min(max(hi_c, lo_c), n_cols)
    return lo_l, hi_l, lo_c, hi_c


def crop(image, find: FrameFind, *, dpi: float | None = None, pad_mm: float = 0.0):
    """Slice `image` to `product_bounds(find, dpi, pad_mm)`. Works on a
    3-channel visible array and a 2-D IR array alike -- the SAME indices
    apply to both, which is what keeps a visible/IR pair registered
    through the crop (the dual scan path already relies on this: same
    crop indices, same grid).
    """
    arr = np.asarray(image)
    lo_l, hi_l, lo_c, hi_c = product_bounds(find, dpi=dpi, pad_mm=pad_mm, shape=arr.shape)
    return arr[lo_l:hi_l, lo_c:hi_c]


# ---------------------------------------------------- numbering (protocol)
# Under the two-placement protocol (docs/film-110.md §3) image 1's start
# sits PLACEMENT_A_PHASE_MM inside aperture 1 in placement A, and half a
# film pitch further on in placement B. An image's number is then its
# position along the strip, whichever placement and apertures it was
# scanned in -- so the same photograph gets the same number in A and B.
# Measured on the 2026-09-19 strip (n = 1 strip, one operator): A image 1
# at 3.53 / 3.58 mm (3600 / 600 dpi); B image 1 at 15.73 mm against the
# derived 16.25. Rounding tolerates +/- PLACEMENT_PHASE_TOLERANCE_MM.
PLACEMENT_A_PHASE_MM = 3.5
PLACEMENT_PHASE_TOLERANCE_MM = 6.0


def placement_phase_mm(placement: str, film: Film = FILM_110) -> float:
    """Image 1's start inside aperture 1 for placement "A" or "B"
    (docs/film-110.md §3): A is the strip loaded flush (a hair of clear
    film before image 1, exactly like a 35 mm strip); B is shifted half a
    film pitch further on, so the bar that split image 2 in A falls
    between images 1 and 2 instead."""
    if placement == "A":
        return PLACEMENT_A_PHASE_MM
    if placement == "B":
        return PLACEMENT_A_PHASE_MM + film.pitch_mm / 2
    raise ValueError(f'placement must be "A" or "B", got {placement!r}')


def image_number(aperture: int, line0: float, dpi: float, placement: str,
                 film: Film = FILM_110, holder=STRIP) -> tuple:
    """Strip-position number of the image whose start is `line0` (px,
    aperture-registered) in `aperture` (1-based), and the residual in mm
    between that position and the nearest phase-grid position (signed,
    |residual| <= pitch/2). The caller refuses when |residual| >
    PLACEMENT_PHASE_TOLERANCE_MM or the number is < 1.

    `global_mm` is `line0`'s position measured from aperture 1's own
    start, along the holder's own aperture grid (`holder.pitch_mm`) --
    NOT the film's pitch, since apertures and film images do not share
    one grid. `k` is then how many film pitches past this placement's
    phase that position is; the image number is `k` rounded to the
    nearest integer, offset by one (image 1 is k == 0)."""
    px_per_mm = dpi / MM_PER_INCH
    global_mm = (aperture - 1) * holder.pitch_mm + line0 / px_per_mm
    k = (global_mm - placement_phase_mm(placement, film)) / film.pitch_mm
    n = round(k) + 1
    residual = (k - round(k)) * film.pitch_mm
    return n, residual


# The two placements' phases are half a film pitch (12.75 mm) apart, so a
# strip seated by the visual rule of docs/film-110.md §3 lies within a few
# mm of exactly one of them. An image whose position is farther than this
# from BOTH phases is midway between them (the farthest possible is
# pitch/4 = 6.4 mm from each) and the data cannot say which placement the
# operator meant. 4.0 mm sits inside the +/-6 mm rounding tolerance used
# for numbering, so an inferred placement never trips the phase check.
PLACEMENT_INFER_TOLERANCE_MM = 4.0


def infer_placement(aperture: int, line0: float, dpi: float,
                    film: Film = FILM_110, holder=STRIP) -> tuple:
    """Which placement ("A" or "B") a strip is in, from one image's
    position alone: the placement whose phase grid the image lies closer
    to, and that signed residual in mm. (None, residual) when even the
    closer residual exceeds PLACEMENT_INFER_TOLERANCE_MM -- the image is
    midway between the two phases and the operator must say. Pure."""
    best = None
    for placement in ("A", "B"):
        _n, residual = image_number(aperture, line0, dpi, placement,
                                    film=film, holder=holder)
        if best is None or abs(residual) < abs(best[1]):
            best = (placement, residual)
    placement, residual = best
    if abs(residual) > PLACEMENT_INFER_TOLERANCE_MM:
        return None, residual
    return placement, residual


# ------------------------------------------------------------ two strips
#
# `digitize --film 110 --strips 2` (docs/film-110.md §12) loads two 110
# strips side by side in the six-aperture holder: slot 1 = apertures 1-3,
# slot 2 = apertures 4-6. Slot 1 is numbered exactly like a lone strip
# (`image_number`, phase from the holder's left end). Slot 2's strip lies
# against the holder's RIGHT end, so its start depends on the strip's
# length and leader: its numbering is anchored by the data instead -- its
# first numbered image is image 1, every later one is numbered by its
# distance from that one in film pitches (`image_number_anchored`).
# Nothing here assumes which lateral rail a strip lies against.
SLOT_APERTURES = 3
# An image cannot start before the film does; an image predicted this far
# before a free film start (px_per_mm * mm) is a hole's ghost, not an image.
FILM_START_TOLERANCE_MM = 1.0


def slot_of(aperture: int, strips: int = 1) -> int:
    """1-based slot of a holder aperture: always 1 when `strips == 1`;
    with two strips, apertures 1-3 are slot 1 and 4-6 slot 2."""
    if strips <= 1:
        return 1
    return (aperture - 1) // SLOT_APERTURES + 1


def slot_apertures(slot: int) -> range:
    """The holder apertures of a slot (two-strip loads)."""
    first = (slot - 1) * SLOT_APERTURES + 1
    return range(first, first + SLOT_APERTURES)


def global_position_mm(aperture: int, line0: float, dpi: float,
                       holder=STRIP) -> float:
    """`line0` (px, aperture-registered) as a position in mm from
    aperture 1's start along the holder's own aperture grid -- the same
    coordinate `image_number` works in."""
    return (aperture - 1) * holder.pitch_mm + line0 / (dpi / MM_PER_INCH)


def image_number_anchored(line0_mm_global: float, anchor_mm: float,
                          film: Film = FILM_110) -> tuple:
    """Number (1-based) and phase residual in mm (signed, |r| <= pitch/2)
    of the image starting at `line0_mm_global`, counted from the image
    that starts at `anchor_mm` (image 1). The anchor itself is (1, 0.0).
    A number < 1 means the image lies before the anchor; the caller
    refuses it. Pure."""
    k = (line0_mm_global - anchor_mm) / film.pitch_mm
    n = round(k) + 1
    return n, (k - round(k)) * film.pitch_mm


# ------------------------------------------------------- free passes (shift)
#
# `digitize --film 110` runs on an existing roll are not tied to the two
# fixed placements: the strip may be seated anywhere, slid or turned. From
# the second run on, numbering therefore works from the roll's OWN earlier
# data. The strip moved as one piece between two runs, so every image's
# position differs from the recorded one by the same amount (Test 103:
# within 0.3 mm); that amount is the SHIFT. Because the film is periodic a
# shift is only known modulo the film pitch from positions alone, so the
# shift is never taken from positions: it is the one at which the images
# LOOK like the recorded ones (`decide_orientation`, below), and a run in
# which no image can be recognised is refused rather than guessed. The
# window below only bounds the moves the driver SUGGESTS (`pick_shift`).
SHIFT_INWARD_SLACK_MM = 1.0     # extra room beyond +/- pitch/2 towards the middle
SHIFT_AGREE_MM = 2.0            # pairings within this of each other are one shift
SHIFT_RESIDUAL_MM = 2.0         # an image this far off the shifted grid is refused


def shift_window(slot: int = 1, film: Film = FILM_110) -> tuple:
    """(lo, hi) in mm: the shifts (global positions, signed) a run may have
    moved the strip by since an earlier run. "Further in" is towards the
    holder's middle: positive for slot 1 (its strip lies against the left
    end), negative for slot 2. The window is one film pitch wide, half a
    pitch either side of no move, widened by SHIFT_INWARD_SLACK_MM on the
    inward side."""
    half = film.pitch_mm / 2
    if slot == 2:
        return (-half - SHIFT_INWARD_SLACK_MM, half - SHIFT_INWARD_SLACK_MM)
    return (-half + SHIFT_INWARD_SLACK_MM, half + SHIFT_INWARD_SLACK_MM)


def number_by_shift(position_mm: float, shift_mm: float, recorded,
                    film: Film = FILM_110) -> tuple:
    """Number (1-based; < 1 means before the roll's first image, the
    caller refuses it) and residual in mm (signed, |r| <= pitch/2) of the
    image starting at `position_mm`, counted from the recorded image
    (number, position_mm) that lies nearest once the strip's `shift_mm`
    is taken off. `recorded` must not be empty. Pure."""
    num, pos = min(recorded,
                   key=lambda e: abs(position_mm - e[1] - shift_mm))
    k = (position_mm - pos - shift_mm) / film.pitch_mm
    return num + round(k), (k - round(k)) * film.pitch_mm


def bar_spans_mm(slot: int = 1, strips: int = 2, holder=STRIP) -> list:
    """The crossbars after the apertures of a slot, as (start, end) mm
    along the holder. A lone strip (`strips` 1) owns all six apertures."""
    aps = slot_apertures(slot) if strips == 2 else range(1, 7)
    return [((j - 1) * holder.pitch_mm + holder.aperture_mm,
             j * holder.pitch_mm) for j in aps]


def bar_centering_shifts(position_mm: float, bars, film: Film = FILM_110) -> list:
    """Shifts (mm, signed along the holder) that put a bar which splits the
    image starting at `position_mm` into the gap between films -- the gap
    after the image or the gap before it -- at the gap's middle. The gap is
    a pitch minus an image long (8.3 mm), the bar about 2 mm, so the
    centring leaves about 3 mm either way. Pure."""
    length = film.image_mm[0]
    gap = film.pitch_mm - length
    out = []
    for b0, b1 in bars:
        if b1 > position_mm and b0 < position_mm + length:
            c = (b0 + b1) / 2
            out.append(c - (position_mm + length + gap / 2))
            out.append(c - (position_mm - gap / 2))
    return out


def pick_shift(candidates, slot: int = 1, film: Film = FILM_110):
    """The candidate shift of least magnitude that lies inside the slot's
    shift window, or None."""
    lo, hi = shift_window(slot, film)
    ok = [s for s in candidates if lo <= s < hi]
    return min(ok, key=abs) if ok else None


# ------------------------------------------- slid or turned? (image content)
#
# Between two runs the operator may also TURN the strip 180 degrees in its
# plane (docs/film-110.md): the images then lie in reverse order along the
# holder and each picture is rotated 180 degrees in the scan. With a
# regular image pitch the positions alone cannot tell a turned strip from a
# slid one (a three-image strip turned is geometrically a slide), so the
# decision is made on what the images look like: small, normalised
# thumbnails of the new images are correlated with thumbnails of the
# recorded ones, once as they are (SLIDE) and once with the new thumbnail
# rotated 180 degrees (TURNED). The numbers stay with the photographs.
THUMB_SIZE = (96, 72)           # (lines, columns): a 17.2 x 13.0 mm image, ~96 px long
THUMB_INSET = 0.12              # fraction of each side cut off before binning: the
                                # product pad, the gate halo and the printed border
                                # are the same on every image and would make any
                                # two images correlate; only the picture decides
ORIENT_MIN_CORR = 0.5           # the winning hypothesis' mean correlation at least ...
ORIENT_MARGIN = 0.15            # ... and this far above the other one
END_AGREE_MM = 3.0              # a slide must agree with a free film end seen in both runs
PATCH_PX_PER_MM = 8.0           # the working copy of a sighting kept for the content check
OVERLAP_MIN_MM = 6.0            # two sightings of an image are compared on at least this much


def thumbnail(arr, size=THUMB_SIZE, inset=THUMB_INSET):
    """`arr` ((lines, cols) or (lines, cols, channels)), its outer `inset`
    fraction on every side cut off, averaged into `size` bins and
    normalised to zero mean and unit variance (all zeros for a flat
    patch). Pure; the area average makes the thumbnail independent of the
    scan resolution."""
    a = np.asarray(arr, dtype=np.float64)
    if a.ndim == 3:
        a = a.mean(axis=2)
    if inset:
        dy, dx = int(a.shape[0] * inset), int(a.shape[1] * inset)
        a = a[dy:a.shape[0] - dy, dx:a.shape[1] - dx]
    h, w = size
    if a.shape[0] < h or a.shape[1] < w:
        raise ValueError(f"crop {a.shape} is smaller than the thumbnail {size}")
    rows = np.linspace(0, a.shape[0], h + 1).astype(int)
    cols = np.linspace(0, a.shape[1], w + 1).astype(int)
    a = np.add.reduceat(a, rows[:-1], axis=0) / np.diff(rows)[:, None]
    a = np.add.reduceat(a, cols[:-1], axis=1) / np.diff(cols)[None, :]
    a = a - a.mean()
    sd = a.std()
    return (a / sd).astype(np.float32) if sd > 1e-9 else np.zeros_like(a, dtype=np.float32)


def patch(arr, px_per_mm: float):
    """A sighting's working copy for the content check: `arr` ((lines,
    cols[, channels]), the VISIBLE part of an image as scanned) averaged
    over channels and area-binned down to about PATCH_PX_PER_MM, as
    float32. Small enough to hold for every sighting of a strip, fine
    enough for a THUMB_SIZE thumbnail of a 6 mm overlap. Pure."""
    a = np.asarray(arr, dtype=np.float64)
    if a.ndim == 3:
        a = a.mean(axis=2)
    f = max(1, int(round(px_per_mm / PATCH_PX_PER_MM)))
    if f > 1:
        h, w = (a.shape[0] // f) * f, (a.shape[1] // f) * f
        if h == 0 or w == 0:
            return a.astype(np.float32)
        a = a[:h, :w].reshape(h // f, f, w // f, f).mean(axis=(1, 3))
    return a.astype(np.float32)


def pair_correlation(new_item, old_item, kind: str, film: Film = FILM_110):
    """How much two sightings look like the same photograph. `new_item` =
    (position_mm, u0, u1, patch), `old_item` = (number, position_mm, u0,
    u1, patch): u0..u1 is the part of the image the sighting shows, in mm
    from the image's own start (a whole image is 0..length, a split shows
    less), `patch` from `patch()`. Under `kind` "same" the two show the
    same part where their u-ranges overlap; under "turned" the new
    sighting's u runs the other way (u' = length - u) and its picture is
    rotated 180 degrees. The overlapping parts are thumbnailed alike and
    correlated. None when they overlap by less than OVERLAP_MIN_MM. Pure."""
    _p, nu0, nu1, npatch = new_item
    _n, _r, ou0, ou1, opatch = old_item
    length = film.image_mm[0]
    if kind == "turned":
        nu0, nu1 = length - nu1, length - nu0
    a, b = max(nu0, ou0), min(nu1, ou1)
    if b - a < OVERLAP_MIN_MM:
        return None

    def rows(pch, u0, u1, lo, hi):
        n = pch.shape[0]
        r0 = int(round((lo - u0) / (u1 - u0) * n))
        r1 = int(round((hi - u0) / (u1 - u0) * n))
        return pch[max(0, r0):max(r0 + 1, min(n, r1))]

    size = (max(16, int(round(THUMB_SIZE[0] * (b - a) / length))), THUMB_SIZE[1])
    try:
        if kind == "turned":
            sub = rows(npatch, nu0, nu1, a, b)[::-1, ::-1]
        else:
            sub = rows(npatch, nu0, nu1, a, b)
        tn = thumbnail(sub, size)
        to = thumbnail(rows(opatch, ou0, ou1, a, b), size)
    except ValueError:
        return None
    return thumb_correlation(tn, to)


def thumb_correlation(a, b) -> float:
    """Pearson correlation of two thumbnails of the same shape (both
    produced by `thumbnail`). Pure."""
    return float(np.mean(np.asarray(a, np.float64) * np.asarray(b, np.float64)))


def _best_cluster(pairs):
    """pairs: [(key_mm, correlation)]. The group of pairs whose keys lie
    within SHIFT_AGREE_MM/2 of an anchor pair's, best by (members with a
    usable correlation, mean correlation). Returns (median key, mean
    correlation, members) or None."""
    best = None
    tol = SHIFT_AGREE_MM / 2
    for key0, _c in pairs:
        members = [(k, c) for k, c in pairs if abs(k - key0) <= tol]
        mean = sum(c for _k, c in members) / len(members)
        good = sum(1 for _k, c in members if c >= ORIENT_MIN_CORR)
        score = (good, mean)
        if best is None or score > best[0]:
            keys = sorted(k for k, _c in members)
            best = (score, (keys[len(keys) // 2], mean, len(members)))
    return None if best is None else best[1]


def decide_orientation(new, old, film: Film = FILM_110, *,
                       pos_range=None, end_shift=None):
    """Did the strip SLIDE or was it TURNED since an earlier record, and by
    how much? `new`: [(position_mm, u0, u1, patch)] -- every sighting of
    this run, whole or split; `old`: [(number, position_mm, u0, u1,
    patch)] of the record's sightings (see `pair_correlation` for the
    items).

    Every (new, old) pair that overlaps by OVERLAP_MIN_MM is a hypothesis:
    SLIDE by d = p - r, compared as they are; TURNED about c = p + r, the
    new picture rotated 180 degrees. Pairs that agree on d (or c) within
    SHIFT_AGREE_MM/2 form a cluster. Clusters are first pruned by what is
    physically possible -- `pos_range` (lo, hi): every recorded image
    must still lie inside it after the move -- and by the strip's own
    free end when one was seen in both runs (`end_shift` = its move, mm:
    a slide must agree with it within END_AGREE_MM; a turn is not
    checked against it). Among the clusters left, the best -- most pairs
    with a usable correlation, then mean correlation -- wins when its
    mean is at least ORIENT_MIN_CORR and ORIENT_MARGIN above every other
    remaining cluster of either kind. No window on the shift: a move of
    more than half a pitch, or of whole apertures, is told from a smaller
    one by the pictures (and the free end), which positions alone cannot
    do -- the film is periodic. Returns {"orientation": "same"|"turned",
    "key": mm, "mean": r, "other": r, "pairs": n} or None when no two
    sightings overlap or the pictures do not decide it. Pure."""
    slide, turned = [], []
    for item in new:
        p = item[0]
        for oitem in old:
            r = oitem[1]
            c = pair_correlation(item, oitem, "same", film)
            if c is not None:
                slide.append((p - r, c))
            c = pair_correlation(item, oitem, "turned", film)
            if c is not None:
                turned.append((p + r, c))
    if not slide and not turned:
        return None
    length = film.image_mm[0]
    recorded = [o[1] for o in old]

    def possible(kind, key):
        if pos_range is not None:
            lo, hi = pos_range
            moved = ([r + key for r in recorded] if kind == "same"
                     else [key - r - length for r in recorded])
            if any(not (lo <= m <= hi) for m in moved):
                return False
        if (end_shift is not None and kind == "same"
                and abs(key - end_shift) > END_AGREE_MM):
            return False
        return True

    tol = SHIFT_AGREE_MM / 2
    clusters = []
    for kind, pairs in (("same", slide), ("turned", turned)):
        for key0, _c in pairs:
            members = [(k, c) for k, c in pairs if abs(k - key0) <= tol]
            keys = sorted(k for k, _c in members)
            key = keys[len(keys) // 2]
            if not possible(kind, key):
                continue
            mean = sum(c for _k, c in members) / len(members)
            good = sum(1 for _k, c in members if c >= ORIENT_MIN_CORR)
            clusters.append(((good, mean), kind, key, mean, len(members)))
    if not clusters:
        return None
    clusters.sort(key=lambda x: x[0], reverse=True)
    _score, kind, key, mean, n = clusters[0]
    others = [c[3] for c in clusters[1:]
              if c[1] != kind or abs(c[2] - key) > tol]
    other = max(others) if others else -1.0
    if mean < ORIENT_MIN_CORR or mean - other < ORIENT_MARGIN:
        return None
    return {"orientation": kind, "key": key, "mean": mean, "other": other,
            "pairs": n}


def slot_position_range(slot: int = 1, strips: int = 1, film: Film = FILM_110,
                        holder=STRIP) -> tuple:
    """(lo, hi) mm along the holder where an image START of this slot's
    strip can lie: from one image length before the slot's first aperture
    (a leading split) to the end of its last one. A lone strip owns the
    whole holder. Pure."""
    aps = slot_apertures(slot) if strips == 2 else range(1, 7)
    lo = (aps[0] - 1) * holder.pitch_mm - film.image_mm[0]
    hi = aps[-1] * holder.pitch_mm
    return lo, hi


def number_turned(position_mm: float, c_mm: float, recorded,
                  film: Film = FILM_110) -> tuple:
    """`number_by_shift` for a TURNED strip: the new image starting at
    `position_mm` is the photograph that started at c - position_mm in the
    recorded record's frame (c from `decide_orientation`). Pure."""
    return number_by_shift(c_mm - position_mm, 0.0, recorded, film)
