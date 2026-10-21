#!/usr/bin/env python3
"""Offline tests for `digitize --film 110` (docs/film-110.md §11): the
two-run roll, the provisional x- stem, the photograph products and the
manifest fields, driven through the real `_cmd_digitize` with a fake
scanner that returns synthetic 110 apertures (the fixture builder of
tests/test_film110.py). No hardware, no private data.

Plain asserts, no pytest dependency. Run with:
    .venv/bin/python tests/test_film110_digitize.py
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))

import numpy as np

from of135i import cli, digitize, film110
from of135i.holder import FILM_110, STRIP
from test_film110 import AIR, PLASTIC, _build_fixture

DPI = 600
PPM = DPI / 25.4
PAD_MM = 3.0                      # plastic either side of the opening
APERTURE_MM = 36.0


def _strip_apertures(placement, free_end_in_a=True):
    """Apertures 1-4 of the four-image strip of docs/film-110.md §5 in the
    given placement; aperture 4 is empty (past the strip). In placement A
    the strip's end lies free in aperture 3 right after image 4 (the sag
    case); in B image 4 sits under the next bar (no end in view)."""
    phase = film110.placement_phase_mm(placement)
    lead, ilen = FILM_110.perforation_lead_mm, FILM_110.image_mm[0]
    col0 = 2.0 + FILM_110.image_lateral_offset_mm
    col1 = col0 + FILM_110.image_mm[1]
    out = {}
    for j in (1, 2, 3):
        base = (j - 1) * STRIP.pitch_mm
        holes, imgs, dense = [], [], None
        for k in range(4):
            local = phase + k * FILM_110.pitch_mm - base
            if local >= APERTURE_MM or local + ilen <= 0:
                continue
            if local < 0:
                dense = local + ilen            # the tail of a split image
            else:
                imgs.append((local, local + ilen, col0, col1))
            if local - lead > 0:
                holes.append((local - lead - 1.5, local - lead))
        free_end = None
        if placement == "A" and free_end_in_a and j == 3:
            free_end = imgs[-1][1] + 4.1        # 25.4 mm: within a pitch
        arr, _p, _lo, _hi = _build_fixture(
            DPI, holes_mm=holes, images_mm=imgs, leading_dense_to_mm=dense,
            free_end_mm=free_end, seed=10 * j)
        out[j] = arr
    out[4] = _empty_aperture()
    return out


def _empty_aperture():
    n = int(round(APERTURE_MM * PPM))
    w = int(round(24.0 * PPM))
    return np.full((n, w, 3), AIR, dtype=np.uint16)


def _raw_frame(vis):
    """A digitize frame buffer (alternating IR/visible lines) around one
    aperture-registered array: PAD_MM of dark plastic either side, the IR
    opening exactly the aperture."""
    pad = int(round(PAD_MM * PPM))
    n, w = vis.shape[0], vis.shape[1]
    total = n + 2 * pad
    full = np.full((total, w, 3), PLASTIC, dtype=np.uint16)
    full[pad:pad + n] = vis
    ir = np.full((total, w), 800, dtype=np.uint16)
    ir[pad:pad + n] = 40000
    inter = np.empty((total * 2, w, 3), dtype=np.uint16)
    inter[0::2] = ir[:, :, None]
    inter[1::2] = full
    return inter.astype("<u2").tobytes(), w


def _run(d, strip, *, roll=1, force=False, placement=None, frames="1-4",
         **flags):
    """One `_cmd_digitize --film 110` run against `d`. Returns (rc, record,
    scan-call list, stdout, stderr)."""
    scans: list = []

    class _Fake:
        park_mode = "verbatim"
        warmup_budget_s = 60.0
        last_diag = {"gain_codes": [1, 2, 3], "offset_codes": [4, 5, 6],
                     "dark_b_substituted": False, "calibration": "fresh"}

        def check_start_state(self): pass
        def is_magazine_present(self): return True
        def initialize(self, ir, dpi): pass
        def scan(self, frame, ir=None, dpi=None):
            scans.append(frame)
            raw, w = _raw_frame(strip[frame])
            return raw, w, None
        def eject(self): scans.append("eject")

    args = argparse.Namespace(
        out=d, prefix="", roll=roll, force=force, assume_loaded=True,
        dpi=DPI, positive=False, rotate=0, ir=True, no_clean=True,
        no_diag=False, park="verbatim", warmup_budget=None, frames=frames,
        film="110", placement=placement, **flags)
    orig = cli._run_writing_session
    cli._run_writing_session = lambda body: body(_Fake())
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli._cmd_digitize(args)
    finally:
        cli._run_writing_session = orig
    recs = digitize.read_manifest(d)
    rec = recs[-1] if recs and rc != 2 else None
    return rc, rec, scans, out.getvalue(), err.getvalue()


def _names(d, roll=1):
    return sorted(p.name for p in digitize.roll_dir(d, "", roll).iterdir())


def _by_number(rec):
    return {e["number"]: e for e in rec["images"]}


def _run_a(d, **kw):
    return _run(d, _strip_apertures("A"), **kw)


def _run_b(d, **kw):
    return _run(d, _strip_apertures("B"), **kw)


# ------------------------------------------------------------------ tests


def test_run_a_infers_placement_numbers_and_advises():
    with tempfile.TemporaryDirectory() as d:
        rc, rec, scans, out, err = _run_a(d)
        assert rc == 0 and rec["status"] == "ok", (rc, rec, err)
        assert scans == [1, 2, 3, 4, "eject"], scans
        assert rec["film"] == "110" and rec["placement"] == "A", rec
        assert rec["placement_given"] is False
        assert "placement A (inferred)" in out, out
        imgs = _by_number(rec)
        assert sorted(imgs) == [1, 2, 3, 4], imgs
        assert [imgs[n]["whole"] for n in (1, 2, 3, 4)] == [True, False, True, True]
        assert imgs[2]["split"] == "trailing" and imgs[2]["main"] is None
        assert imgs[4]["free_end_near"] is True and imgs[1]["free_end_near"] is False
        assert imgs[1]["size_mm"] and len(imgs[1]["size_mm"]) == 2
        assert imgs[2]["size_mm"] is None
        # 2 is split by a bar; 4 was delivered with the strip end free (§4),
        # so placement B is still needed for both.
        assert rec["advice"] == {"other_placement": "B", "images": [2, 4]}, rec["advice"]
        names = _names(d)
        for n in (1, 3, 4):
            assert f"image{n}.tiff" in names and f"image{n}-ir.tiff" in names, names
        assert "image2.tiff" not in names
        # Frame files carry the placement; no provisional stem is left.
        assert "a-f1.tiff" in names and "a-f1-ir.tiff" in names, names
        assert "a-f1.overscan.tiff" in names and "a-f1-ir.overscan.tiff" in names
        assert "a-f1.diag.json" in names
        assert not any(n.startswith("x-") for n in names), names
        assert not any(n.startswith("f1") for n in names), names
        # The per-frame entry points at the renamed files.
        pf = rec["per_frame"][0]
        assert pf["main"].endswith("a-f1.tiff"), pf
        assert pf["overscan"].endswith("a-f1.overscan.tiff"), pf
        assert digitize.roll_is_done(d, 1) is False
    print("test_run_a_infers_placement_numbers_and_advises OK")


def test_two_run_roll_end_to_end():
    with tempfile.TemporaryDirectory() as d:
        rc, rec_a, *_ = _run_a(d)
        assert rc == 0
        first = {n: (Path(d) / "roll-001" / f"image{n}.tiff").read_bytes()
                 for n in (1, 3, 4)}
        # Run B: same roll, no --force, no --placement.
        rc, rec_b, scans, out, err = _run_b(d)
        assert rc == 0 and rec_b["status"] == "ok", (rc, rec_b, err)
        assert rec_b["placement"] == "B" and rec_b["placement_given"] is False
        imgs = _by_number(rec_b)
        assert sorted(imgs) == [1, 2, 3, 4], imgs
        # image 1: a good copy exists -> kept, recorded as refused/kept.
        assert imgs[1]["main"] is None and "kept" in imgs[1]["refused"], imgs[1]
        assert (Path(d) / "roll-001" / "image1.tiff").read_bytes() == first[1]
        # image 2: new, whole in B.
        assert imgs[2]["whole"] and imgs[2]["main"].endswith("image2.tiff")
        assert (Path(d) / "roll-001" / "image2-ir.tiff").exists()
        # image 3: split in B, whole in A -> untouched, nothing to advise.
        assert imgs[3]["split"] == "trailing" and imgs[3]["main"] is None
        assert (Path(d) / "roll-001" / "image3.tiff").read_bytes() == first[3]
        # image 4: A's copy had its strip end free; B's is the supported one.
        assert imgs[4]["replaced_free_end_copy"] is True, imgs[4]
        assert imgs[4]["free_end_near"] is False
        assert (Path(d) / "roll-001" / "image4.tiff").read_bytes() != first[4]
        assert rec_b["advice"] == {"other_placement": "A", "images": []}, rec_b["advice"]
        assert "image1.tiff" in " ".join(rec_b["problems"]), rec_b["problems"]
        names = _names(d)
        assert {"a-f1.tiff", "b-f1.tiff", "a-f2.tiff", "b-f2.tiff"} <= set(names), names
        assert [n for n in names if n.startswith("image") and not n.endswith("-ir.tiff")] \
            == ["image1.tiff", "image2.tiff", "image3.tiff", "image4.tiff"], names
        # One record per run, same roll; the roll is now done.
        recs = digitize.roll_records(d, 1)
        assert [r["placement"] for r in recs] == ["A", "B"], recs
        assert digitize.roll_is_done(d, 1) is True
        # A third run on a finished roll is refused before hardware.
        rc, rec, scans, out, err = _run_b(d)
        assert rc == 2 and scans == [] and "done" in err, (rc, scans, err)
    print("test_two_run_roll_end_to_end OK")


def test_same_placement_twice_is_refused_before_hardware():
    with tempfile.TemporaryDirectory() as d:
        assert _run_a(d)[0] == 0
        before = _names(d)
        rc, rec, scans, out, err = _run_a(d, placement="A")
        assert rc == 2 and scans == [], (rc, scans)
        assert "placement A" in err and "again" in err, err
        assert _names(d) == before
        # Given B on the roll that holds A is accepted (the second run).
        rc, rec, scans, out, err = _run_b(d, placement="B")
        assert rc == 0 and rec["placement"] == "B" and rec["placement_given"] is True
        assert "placement B (given)" in out, out
    print("test_same_placement_twice_is_refused_before_hardware OK")


def test_inferred_same_placement_stops_without_overwriting():
    """Placement not given, but the strip reads as the placement the roll
    already holds: the run stops at the deciding frame, ejects, rc 5,
    and every existing file is byte-identical."""
    with tempfile.TemporaryDirectory() as d:
        assert _run_a(d)[0] == 0
        snap = {p.name: p.read_bytes() for p in digitize.roll_dir(d, "", 1).iterdir()}
        rc, rec, scans, out, err = _run_a(d)
        assert rc == 5, (rc, err)
        assert scans == [1, "eject"], scans
        assert rec["status"] == "failed" and rec["placement"] is None, rec
        assert "already holds output" in err, err
        names = _names(d)
        for name, data in snap.items():
            assert (digitize.roll_dir(d, "", 1) / name).read_bytes() == data, name
        extra = sorted(set(names) - set(snap))
        assert extra and all(n.startswith("x-f1") for n in extra), extra
    print("test_inferred_same_placement_stops_without_overwriting OK")


def test_force_clears_both_placements_and_photographs():
    with tempfile.TemporaryDirectory() as d:
        assert _run_a(d)[0] == 0 and _run_b(d)[0] == 0
        assert "b-f1.tiff" in _names(d) and "image2.tiff" in _names(d)
        (digitize.roll_dir(d, "", 1) / "notes.txt").write_text("keep")
        rc, rec, scans, out, err = _run_a(d, force=True)
        assert rc == 0, (rc, err)
        names = _names(d)
        assert not any(n.startswith("b-") for n in names), names
        assert "image2.tiff" not in names, names           # B-only photograph gone
        assert "notes.txt" in names
        assert {"image1.tiff", "image3.tiff", "image4.tiff"} <= set(names)
        assert "removed" in out
    print("test_force_clears_both_placements_and_photographs OK")


def test_provisional_stem_is_renamed_when_a_later_frame_decides():
    """Aperture 1 empty, aperture 2 decides: frame 1's files were written
    as x-f1.* and are renamed, manifest paths included."""
    strip = _strip_apertures("A")
    strip[1] = _empty_aperture()
    with tempfile.TemporaryDirectory() as d:
        rc, rec, scans, out, err = _run(d, strip, frames="1-3")
        assert rc == 0, (rc, err)
        assert rec["placement"] == "A", rec
        names = _names(d)
        assert not any(n.startswith("x-") for n in names), names
        assert "a-f1.overscan.tiff" in names and "a-f1.diag.json" in names, names
        assert "a-f1.tiff" not in names                 # empty aperture: no main
        assert rec["per_frame"][0]["overscan"].endswith("a-f1.overscan.tiff")
        side = json.loads((digitize.roll_dir(d, "", 1) / "a-f1.diag.json").read_text())
        # Documented: the sidecar written under the provisional stem keeps
        # that stem in its own `output` field (the rename does not rewrite it).
        assert side["output"].endswith("x-f1.tiff"), side["output"]
        # Number comes from the strip position, not the aperture index.
        assert sorted(_by_number(rec)) == [3, 4], _by_number(rec)
    print("test_provisional_stem_is_renamed_when_a_later_frame_decides OK")


def test_nothing_found_keeps_x_stem_and_null_placement():
    strip = {n: _empty_aperture() for n in (1, 2, 3, 4)}
    with tempfile.TemporaryDirectory() as d:
        rc, rec, scans, out, err = _run(d, strip)
        assert rc == 0 and rec["status"] == "ok", (rc, err)
        assert rec["placement"] is None and rec["images"] == [], rec
        assert rec["advice"]["images"] == []
        names = _names(d)
        assert "x-f1.overscan.tiff" in names and not any(
            n.startswith(("a-", "b-", "image")) for n in names), names
        # Not "done": the strip is to be tried again, without --force.
        assert digitize.roll_is_done(d, 1) is False
        rc, rec2, scans, out, err = _run_a(d)
        assert rc == 0 and rec2["placement"] == "A", (rc, err)
    print("test_nothing_found_keeps_x_stem_and_null_placement OK")


def test_roll_is_done_semantics():
    with tempfile.TemporaryDirectory() as d:
        m = digitize.append_manifest
        img = [{"number": 1, "whole": True}]
        m(d, {"roll": 1, "status": "ok", "film": "110", "placement": "A",
              "images": img, "advice": {"other_placement": "B", "images": [2]}})
        assert digitize.roll_is_done(d, 1) is False
        m(d, {"roll": 1, "status": "ok", "film": "110", "placement": "B",
              "images": img, "advice": {"other_placement": "A", "images": []}})
        assert digitize.roll_is_done(d, 1) is True          # last ok, nothing left
        # Nothing split in the first run: done after one run.
        m(d, {"roll": 2, "status": "ok", "film": "110", "placement": "A",
              "images": img, "advice": {"other_placement": "B", "images": []}})
        assert digitize.roll_is_done(d, 2) is True
        # A failed run, a run that found nothing: not done.
        m(d, {"roll": 3, "status": "failed", "film": "110", "placement": "A",
              "advice": {"other_placement": "B", "images": []}})
        m(d, {"roll": 4, "status": "ok", "film": "110", "placement": None,
              "images": [], "advice": {"other_placement": None, "images": []}})
        assert digitize.roll_is_done(d, 3) is False
        assert digitize.roll_is_done(d, 4) is False
        # Both placements ok, even if the last advice lists something.
        m(d, {"roll": 5, "status": "ok", "film": "110", "placement": "A",
              "images": img, "advice": {"other_placement": "B", "images": [2]}})
        m(d, {"roll": 5, "status": "ok", "film": "110", "placement": "B",
              "images": img, "advice": {"other_placement": "A", "images": [3]}})
        assert digitize.roll_is_done(d, 5) is True
        # A roll without a single image is never done, whatever its
        # placement/advice say (Test 103: an unread strip in slot 2), in
        # one run or in both.
        m(d, {"roll": 7, "status": "ok", "film": "110", "placement": "A",
              "images": [], "advice": {"other_placement": "B", "images": []}})
        assert digitize.roll_is_done(d, 7) is False
        m(d, {"roll": 7, "status": "ok", "film": "110", "placement": "B",
              "images": [], "advice": {"other_placement": "A", "images": []}})
        assert digitize.roll_is_done(d, 7) is False
        # 135 rolls keep the old rule.
        m(d, {"roll": 6, "status": "ok"})
        assert digitize.roll_is_done(d, 6) is True
    print("test_roll_is_done_semantics OK")


def test_non_110_output_is_not_a_second_run():
    """A roll directory with 135 output (or no 110 record) is not a
    first-placement roll: the guard refuses without --force."""
    with tempfile.TemporaryDirectory() as d:
        rd = digitize.roll_dir(d, "", 1)
        rd.mkdir(parents=True)
        (rd / "f1.tiff").write_bytes(b"x")
        rc, rec, scans, out, err = _run_a(d)
        assert rc == 2 and scans == [], (rc, scans)
        assert "not a recorded 110 run" in err, err
    print("test_non_110_output_is_not_a_second_run OK")


def test_digitize_parser_and_validation():
    parser = cli.build_parser()
    a = parser.parse_args(["digitize", "-o", "x"])
    assert a.film == "135" and a.placement is None
    a = parser.parse_args(["digitize", "-o", "x", "--film", "110"])
    assert cli._validate_film110_args(a) is None
    a = parser.parse_args(["digitize", "-o", "x", "--placement", "A"])
    assert cli._validate_film110_args(a) is not None
    # Refused before any hardware or directory is touched.
    with tempfile.TemporaryDirectory() as d:
        rc = cli._cmd_digitize(parser.parse_args(
            ["digitize", "-o", d, "--placement", "A", "--assume-loaded"]))
        assert rc == 2 and not (Path(d) / "roll-001").exists()
    print("test_digitize_parser_and_validation OK")


def test_clear_and_rename_helpers():
    with tempfile.TemporaryDirectory() as d:
        rd = digitize.roll_dir(d, "", 1)
        rd.mkdir(parents=True)
        for n in ("x-f2.tiff", "x-f2.diag.json", "x-f2.overscan.tiff", "b-f1.tiff",
                  "image1.tiff", "image9-ir.tiff", "family.tiff"):
            (rd / n).write_bytes(b"x")
        assert digitize.film110_placements_on_disk(rd) == {"x", "b"}
        moved = digitize.rename_film110_frame(rd, 2, "x", "a")
        assert len(moved) == 3 and (rd / "a-f2.tiff").exists()
        assert digitize.film110_placements_on_disk(rd) == {"a", "b"}
        (rd / "x-f2.tiff").write_bytes(b"x")
        (rd / "a-f2.diag.json").write_bytes(b"clash")
        (rd / "x-f2.diag.json").write_bytes(b"x")
        try:
            digitize.rename_film110_frame(rd, 2, "x", "a")
            assert False, "must refuse to overwrite"
        except FileExistsError:
            pass
        assert (rd / "x-f2.tiff").exists()             # all-or-nothing
        removed = digitize.clear_film110_outputs(d, "", 1)
        assert "family.tiff" not in removed and (rd / "family.tiff").exists()
        assert not any(p.name != "family.tiff" for p in rd.iterdir())
    print("test_clear_and_rename_helpers OK")


# ------------------------------------------------- two strips (docs §12)
#
# Six apertures, two strips of up to four images: strip 1 against the
# holder's left end (slot 1 = apertures 1-3), strip 2 against its right end
# (slot 2 = apertures 4-6). Positions below are GLOBAL mm from aperture 1's
# start; aperture j starts at (j-1)*pitch and is APERTURE_MM long. Each
# strip carries its own lateral side ("lo"/"hi" = which film edge is
# perforated), so the fixtures cover either rail.

PITCH = STRIP.pitch_mm
LEAD = FILM_110.perforation_lead_mm
ILEN = FILM_110.image_mm[0]
FPITCH = FILM_110.pitch_mm
# The hole's length along transport, consistent with the film model: one
# pitch holds an image, the lead, the hole and the trail. (The real holes
# of Test 103 measured ~2.3 mm; the earlier fixed 1.5 mm put the
# detector's prediction of the image BEFORE a hole 1.1 mm off, so a real
# leading split's end edge was never confirmed in these fixtures.)
HOLE_MM = FPITCH - ILEN - LEAD - FILM_110.perforation_trail_mm
S2_END = 5 * PITCH + APERTURE_MM - 0.6      # strip 2's last image ends here (A)
B_SHIFT = FPITCH / 2
BOTH_RUNS = (1, 2)


def _starts(first, n):
    return [first + FPITCH * k for k in range(n)]


def _strip_spec(starts, side, *, lead_in=-50.0, tail=4.1):
    """A strip: image starts (global mm), perforated side, where its film
    begins (default: before the holder's left end) and ends."""
    return {"starts": starts, "side": side,
            "start": lead_in, "end": starts[-1] + ILEN + tail}


def _strip1(placement, n=4, side="lo", tail=4.1):
    return _strip_spec(_starts(film110.placement_phase_mm(placement), n),
                       side, tail=tail)


def _strip2(placement, n=4, side="hi", first=None):
    """Strip 2 against the holder's right end in A; moved half a film pitch
    towards the middle in B. `first` overrides the first image's start."""
    a_first = S2_END - (n - 1) * FPITCH - ILEN
    if first is None:
        first = a_first - (B_SHIFT if placement == "B" else 0.0)
    return {"starts": _starts(first, n), "side": side,
            "start": first - LEAD - 1.5 - 2.0,
            "end": first + (n - 1) * FPITCH + ILEN}


def _piece(j, spec, seed):
    """Aperture j's view of one strip: (array, rows-with-film mask,
    band_lo, band_hi) from the single-strip fixture builder."""
    base = (j - 1) * PITCH
    col0 = 2.0 + FILM_110.image_lateral_offset_mm
    col1 = col0 + FILM_110.image_mm[1]
    if spec["side"] == "hi":
        col1 = 22.0 - FILM_110.image_lateral_offset_mm
        col0 = col1 - FILM_110.image_mm[1]
    holes, imgs = [], []
    # The film is perforated at every pitch, so there is a hole after the
    # last image too, when the tail is long enough to hold it whole (a
    # part of one would be a hole stub at the free end, not modelled).
    hole_starts = list(spec["starts"])
    after_last = spec["starts"][-1] + FPITCH
    if after_last - LEAD <= spec["end"]:
        hole_starts.append(after_last)
    for s in hole_starts:
        local = s - base
        if 0 < local - LEAD < APERTURE_MM:
            holes.append((local - LEAD - HOLE_MM, local - LEAD))
    for s in spec["starts"]:
        local = s - base
        if local >= APERTURE_MM or local + ILEN <= 0:
            continue
        # An image that started under the leading edge is painted like
        # any other (the builder clips it at line 0): it has picture
        # content and a visible END edge, as every real leading split
        # had (Test 103). The earlier `leading_dense_to_mm` fill was
        # painted over by the printed border, leaving a leading image
        # indistinguishable from the strip's leader.
        imgs.append((local, local + ILEN, col0, col1))
    arr, _p, blo, bhi = _build_fixture(
        DPI, holes_mm=holes, images_mm=imgs,
        seed=seed, perforated_side=spec["side"], printed_border=True)
    r0 = max(0, int(round((spec["start"] - base) * PPM)))
    r1 = min(arr.shape[0], int(round((spec["end"] - base) * PPM)))
    mask = np.zeros(arr.shape[0], bool)
    if r1 > r0:
        mask[r0:r1] = True
    return arr, mask, blo, bhi


def _holder_apertures(*specs):
    """Apertures 1-6 for any number of strips (None = nothing there). Rows
    where a strip has no film revert to air in its band; where two strips
    share an aperture the later one overlays its own rows."""
    out = {}
    for j in range(1, 7):
        arr = None
        for k, spec in enumerate(specs):
            if spec is None:
                continue
            a, mask, blo, bhi = _piece(j, spec, seed=10 * j + k)
            if not mask.any():
                continue
            if arr is None:
                arr = a.copy()
                arr[~mask, blo:bhi, :] = AIR
            else:
                arr[mask] = a[mask]
        out[j] = arr if arr is not None else _empty_aperture()
    return out


def _run2(d, apertures, *, roll=None, force=False, placement=None, frames=None,
          **flags):
    """One `digitize --film 110 --strips 2` run. Returns (rc, [rec1, rec2],
    scans, stdout, stderr); the records are the last ones of rolls N, N+1
    (None when the run exited 2 before hardware)."""
    r = roll if roll is not None else digitize.next_roll(d, "")
    rc, _rec, scans, out, err = _run(d, apertures, roll=roll, force=force,
                                     placement=placement, frames=frames,
                                     strips=2, **flags)
    recs = []
    for n in (r, r + 1):
        rr = digitize.roll_records(d, n)
        recs.append(rr[-1] if rr and rc != 2 else None)
    return rc, recs, scans, out, err


def _images(rec):
    return {e["number"]: e for e in rec["images"]}


def _shape(rec):
    """number -> 'W' (whole), 'L' (leading split), 'T' (trailing split)."""
    return {n: ("W" if e["whole"] else "L" if e["split"] == "leading" else "T")
            for n, e in _images(rec).items()}


def test_slot_constants_and_anchored_numbering():
    assert film110.SLOT_APERTURES == 3
    assert [film110.slot_of(a, 2) for a in range(1, 7)] == [1, 1, 1, 2, 2, 2]
    assert [film110.slot_of(a, 1) for a in range(1, 7)] == [1] * 6
    assert list(film110.slot_apertures(2)) == [4, 5, 6]
    assert film110.image_number_anchored(100.0, 100.0) == (1, 0.0)
    n, r = film110.image_number_anchored(100.0 + FPITCH * 2 + 1.5, 100.0)
    assert n == 3 and abs(r - 1.5) < 1e-9, (n, r)
    n, r = film110.image_number_anchored(100.0 - FPITCH, 100.0)
    assert n == 0, n                       # before the anchor: the caller refuses
    # Agrees with image_number's arithmetic when anchored on its phase.
    g = film110.global_position_mm(2, 12.0 * PPM, DPI)
    assert abs(g - (PITCH + 12.0)) < 1e-9
    n1, _ = film110.image_number(2, 12.0 * PPM, DPI, "A")
    n2, _ = film110.image_number_anchored(
        g, film110.placement_phase_mm("A"))
    assert n1 == n2
    print("test_slot_constants_and_anchored_numbering OK")


def test_two_strips_run_a_makes_two_rolls():
    with tempfile.TemporaryDirectory() as d:
        rc, (r1, r2), scans, out, err = _run2(
            d, _holder_apertures(_strip1("A"), _strip2("A")))
        assert rc == 0, (rc, err)
        assert scans == [1, 2, 3, 4, 5, 6, "eject"], scans
        # Slot 1 = roll 1, slot 2 = roll 2; each with its own record.
        assert (r1["roll"], r1["slot"], r1["companion_roll"]) == (1, 1, 2)
        assert (r2["roll"], r2["slot"], r2["companion_roll"]) == (2, 2, 1)
        assert r1["strips"] == r2["strips"] == 2
        assert r1["frames"] == [1, 2, 3] and r2["frames"] == [4, 5, 6]
        assert r1["status"] == r2["status"] == "ok"
        assert r1["film_found"] is True and r2["film_found"] is True
        assert r1["placement"] == r2["placement"] == "A"
        assert r1["placement_source"] == "inferred", r1["placement_source"]
        assert r2["placement_source"] == "slot 1 inferred", r2["placement_source"]
        # Slot 1 numbers as a lone strip: 2 is split by a bar.
        assert _shape(r1) == {1: "W", 2: "T", 3: "W", 4: "W"}, _shape(r1)
        # Slot 2 is anchored on its first image, wherever it lies.
        assert _shape(r2) == {1: "W", 2: "W", 3: "T", 4: "W"}, _shape(r2)
        assert r1["advice"] == {"other_placement": "B", "images": [2, 4]}, r1["advice"]
        assert r2["advice"] == {"other_placement": "B", "images": [1, 3, 4]}, r2["advice"]
        assert r1["problems"] == [] and r2["problems"] == [], (r1["problems"], r2["problems"])
        n1, n2 = _names(d, 1), _names(d, 2)
        assert {"a-f1.tiff", "a-f2.tiff", "a-f3.tiff"} <= set(n1), n1
        assert not any(n.startswith(("a-f4", "a-f5", "a-f6")) for n in n1), n1
        # The HOLDER aperture number stays in slot 2's file names.
        assert {"a-f4.tiff", "a-f5.tiff", "a-f6.tiff"} <= set(n2), n2
        assert not any(n.startswith("x-") for n in n1 + n2), (n1, n2)
        for n in (1, 3, 4):
            assert f"image{n}.tiff" in n1 and f"image{n}-ir.tiff" in n1, n1
        assert "image2.tiff" not in n1
        for n in (1, 2, 4):
            assert f"image{n}.tiff" in n2, n2
        assert "image3.tiff" not in n2
        assert digitize.roll_is_done(d, 1) is False
        assert digitize.roll_is_done(d, 2) is False
        # The closing message names both rolls and both advices.
        assert "rolls 1 and 2 done" in out and "roll 1 (slot 1)" in out \
            and "roll 2 (slot 2)" in out, out
        assert "images [2, 4]" in out and "images [1, 3, 4]" in out, out
        assert "--strips 2 --roll 1" in out, out
    print("test_two_strips_run_a_makes_two_rolls OK")


def test_second_run_fills_both_rolls():
    with tempfile.TemporaryDirectory() as d:
        assert _run2(d, _holder_apertures(_strip1("A"), _strip2("A")))[0] == 0
        first = {(r, n): (digitize.roll_dir(d, "", r) / f"image{n}.tiff").read_bytes()
                 for r, ns in ((1, (1, 3)), (2, (2,))) for n in ns}
        rc, (r1, r2), scans, out, err = _run2(
            d, _holder_apertures(_strip1("B"), _strip2("B")), roll=1)
        assert rc == 0, (rc, err)
        assert (r1["placement"], r2["placement"]) == ("B", "B")
        assert r1["placement_source"] == "inferred"
        assert _shape(r1) == {1: "W", 2: "W", 3: "T", 4: "W"}, _shape(r1)
        assert _shape(r2) == {1: "W", 2: "T", 3: "W", 4: "W"}, _shape(r2)
        # Every photograph of both strips now exists; the sag rule replaced
        # the free-end copies (1 of roll 2, 4 of roll 1) with supported ones.
        for r in (1, 2):
            names = _names(d, r)
            for n in (1, 2, 3, 4):
                assert f"image{n}.tiff" in names, (r, n, names)
        assert _images(r1)[4]["replaced_free_end_copy"] is True
        assert _images(r2)[1]["replaced_free_end_copy"] is True
        # Delivered images are kept, not overwritten.
        for (r, n), data in first.items():
            assert (digitize.roll_dir(d, "", r) / f"image{n}.tiff").read_bytes() == data \
                or (r, n) == (2, 1), (r, n)
        assert r1["advice"]["images"] == [] and r2["advice"]["images"] == [], (r1["advice"], r2["advice"])
        assert "nothing left" in out.lower() or "Nothing is left" in out, out
        assert digitize.roll_is_done(d, 1) and digitize.roll_is_done(d, 2)
        # b- frame files per slot, aperture numbers kept.
        assert {"b-f1.tiff", "b-f3.tiff"} <= set(_names(d, 1))
        assert {"b-f4.tiff", "b-f6.tiff"} <= set(_names(d, 2))
        # The pair is finished: another run on it is refused before hardware.
        rc, recs, scans, out, err = _run2(
            d, _holder_apertures(_strip1("B"), _strip2("B")), roll=1)
        assert rc == 2 and scans == [] and "done" in err, (rc, scans, err)
        # A new pair numbers on at 3, 4.
        rc, (r3, r4), *_ = _run2(d, _holder_apertures(_strip1("A"), _strip2("A")))
        assert rc == 0 and (r3["roll"], r4["roll"]) == (3, 4)
    print("test_second_run_fills_both_rolls OK")


def test_either_rail_gives_the_same_numbering():
    """Nothing in the slot logic assumes which rail a strip lies against:
    the four combinations (and lone-side swaps) number identically."""
    results = {}
    for sides in (("lo", "hi"), ("hi", "lo"), ("lo", "lo"), ("hi", "hi")):
        for placement in ("A", "B"):
            with tempfile.TemporaryDirectory() as d:
                rc, (r1, r2), *_ = _run2(d, _holder_apertures(
                    _strip1(placement, side=sides[0]),
                    _strip2(placement, side=sides[1])))
                assert rc == 0, (sides, placement, rc)
                assert r1["problems"] == [] and r2["problems"] == [], (sides, placement)
                results.setdefault(placement, set()).add(
                    (tuple(sorted(_shape(r1).items())), tuple(sorted(_shape(r2).items()))))
    for placement, seen in results.items():
        assert len(seen) == 1, (placement, seen)
    print("test_either_rail_gives_the_same_numbering OK")


def test_slot_two_leading_split_is_image_one_and_slot_one_split_is_reported():
    """Placement B with strip 2's first image straddling the bar between
    apertures 3 and 4: slot 2 counts the leading split in aperture 4 as its
    image 1 (no product), slot 1 reports the matching trailing split in
    aperture 3 as the next strip's -- not a problem, not a number of its own
    -- and drops the image the detector predicts BEFORE that strip's first
    hole (no strip lies there). Both strips on the same side so the detector
    can read aperture 3 (see the mixed-side note in docs §12)."""
    first_b = 110.0
    a_first = first_b + B_SHIFT
    with tempfile.TemporaryDirectory() as d:
        # Run A first.
        rc, (r1, r2), scans, out, err = _run2(d, _holder_apertures(
            _strip1("A", n=3, side="hi"),
            _strip2("A", side="hi", first=a_first)))
        assert rc == 0, (rc, err)
        assert _shape(r2) == {1: "W", 2: "T", 3: "T", 4: "W"}, _shape(r2)
        assert sorted(_shape(r1)) == [1, 2, 3], _shape(r1)
        rc, (b1, b2), scans, out, err = _run2(d, _holder_apertures(
            _strip1("B", n=3, side="hi"),
            _strip2("B", side="hi", first=first_b)), roll=1)
        assert rc == 0, (rc, err)
        assert all("exists -> kept" in p for p in b1["problems"] + b2["problems"]), (b1["problems"], b2["problems"])
        assert "next strip's leading image, counted there" in out, out
        assert "no strip lies there" in out, out
        # Slot 1 (B): its own three images, nothing from the next strip.
        assert _shape(b1) == {1: "W", 2: "W", 3: "T"}, _shape(b1)
        assert sorted(_images(b1)) == [1, 2, 3]
        # Slot 2 (B): the leading split in aperture 4 IS image 1.
        e1 = _images(b2)[1]
        assert e1["aperture"] == 4 and e1["split"] == "leading" and e1["whole"] is False \
            and e1["main"] is None, e1
        assert _shape(b2) == {1: "L", 2: "T", 3: "W", 4: "T"}, _shape(b2)
        # Image 1 (whole in A) is not wanted again; 2 is split in A AND B
        # (unlucky phase) and stays on the advice, 4 likewise delivered in A.
        assert b2["advice"] == {"other_placement": "A", "images": [2]}, b2["advice"]
        assert digitize.roll_is_done(d, 1) and digitize.roll_is_done(d, 2)
    print("test_slot_two_leading_split_is_image_one_and_slot_one_split_is_reported OK")


def test_image_seated_on_the_aperture_edge_is_a_leading_split_of_its_own():
    """Test 103, strip 1: image 1 flush with aperture 1's edge (-0.1 mm)
    and image 4 starting at +0.03 mm in aperture 3 with no trailing split
    before it (its predecessor ended under aperture 2's bar). Both are
    first sightings: numbered as leading splits (no product, the other
    placement delivers them), not dropped as "already counted"."""
    starts = [-0.1, 25.4, 50.9, 2 * PITCH + 0.03]
    spec = _strip_spec(starts, "lo", tail=8.0)     # room for the hole after image 4
    with tempfile.TemporaryDirectory() as d:
        rc, (r1, r2), scans, out, err = _run2(d, _holder_apertures(spec, None))
        assert rc == 0, (rc, err)
        assert r1["placement"] == "A" and r1["problems"] == [], r1
        assert _shape(r1) == {1: "L", 2: "T", 3: "W", 4: "L"}, _shape(r1)
        assert _images(r1)[1]["aperture"] == 1 and _images(r1)[4]["aperture"] == 3
        assert r1["advice"] == {"other_placement": "B", "images": [1, 2, 4]}, r1["advice"]
        assert "split image 2 (already counted)" in out, out
        # (The leader ghost needs the first hole inside the aperture: in A
        # it lies before aperture 1; placement B exercises it in
        # test_either_rail_gives_the_same_numbering.)
    print("test_image_seated_on_the_aperture_edge_is_a_leading_split_of_its_own OK")


def test_empty_slot_two_is_a_found_nothing_roll():
    with tempfile.TemporaryDirectory() as d:
        aps = _holder_apertures(_strip1("A"), None)
        rc, (r1, r2), scans, out, err = _run2(d, aps)
        assert rc == 0, (rc, err)
        assert scans == [1, 2, 3, 4, 5, 6, "eject"], scans   # no film seen in slot 2: no stop
        assert r2["status"] == "ok" and r2["images"] == [], r2
        assert r2["film_found"] is False and r1["film_found"] is True
        assert r2["placement"] == "A" and r2["placement_source"] == "slot 1 inferred"
        assert r2["advice"]["images"] == []
        # Not done (no image), yet the number is not reused ...
        assert digitize.roll_is_done(d, 2) is False
        assert digitize.next_roll(d, "") == 3
        names = _names(d, 2)
        assert {"a-f4.overscan.tiff", "a-f5.overscan.tiff", "a-f6.overscan.tiff"} <= set(names), names
        assert not any(n.startswith("x-") for n in names), names
        assert "(no film found)" in out, out
        # ... yet the other placement's run may still use it.
        rc, (b1, b2), *_ = _run2(d, _holder_apertures(_strip1("B"), None), roll=1)
        assert rc == 0 and b2["placement"] == "B" and b2["film_found"] is False
    print("test_empty_slot_two_is_a_found_nothing_roll OK")


def test_empty_slot_one_labels_the_run_by_run_order():
    with tempfile.TemporaryDirectory() as d:
        aps = _holder_apertures(None, _strip2("A"))
        rc, (r1, r2), scans, out, err = _run2(d, aps)
        assert rc == 0, (rc, err)
        assert r1["film_found"] is False and r1["images"] == [], r1
        assert r1["placement"] == "A" and r1["placement_source"] == "run order", r1
        assert r2["placement"] == "A" and r2["placement_source"] == "run order", r2
        assert _shape(r2) == {1: "W", 2: "W", 3: "T", 4: "W"}, _shape(r2)
        n1 = _names(d, 1)
        assert "a-f1.overscan.tiff" in n1 and not any(n.startswith("x-") for n in n1), n1
        # The second run: roll 1 already has a 110 record -> "B".
        aps = _holder_apertures(None, _strip2("B"))
        rc, (b1, b2), *_ = _run2(d, aps, roll=1)
        assert rc == 0, rc
        assert b1["placement"] == "B" and b1["placement_source"] == "run order", b1
        assert b2["placement"] == "B" and b2["placement_source"] == "run order", b2
        assert not digitize.roll_is_done(d, 1) and digitize.roll_is_done(d, 2)
    print("test_empty_slot_one_labels_the_run_by_run_order OK")


def test_given_placement_covers_both_slots():
    with tempfile.TemporaryDirectory() as d:
        rc, (r1, r2), *_ = _run2(d, _holder_apertures(_strip1("A"), _strip2("A")),
                                 placement="A")
        assert rc == 0
        assert (r1["placement_source"], r2["placement_source"]) == ("given", "given")
        assert r1["placement_given"] is True and r2["placement_given"] is True
    print("test_given_placement_covers_both_slots OK")


def test_stop_rule_is_per_slot():
    with tempfile.TemporaryDirectory() as d:
        # Slot 1 holds one image; aperture 2 is empty after film -> aperture
        # 3 is skipped, but slot 2 is still scanned.
        aps = _holder_apertures(_strip_spec([3.5], "lo"), _strip2("A", n=2))
        rc, (r1, r2), scans, out, err = _run2(d, aps)
        assert rc == 0, (rc, err)
        assert scans == [1, 2, 4, 5, 6, "eject"], scans
        assert r1["skipped_frames"] == [3] and "empty aperture at frame 2" in r1["stop_reason"]
        assert "skipped_frames" not in r2
        assert r2["film_found"] is True
    with tempfile.TemporaryDirectory() as d:
        # Slot 2 holds one image in aperture 4; aperture 5 is empty after
        # film -> aperture 6 skipped. An empty slot-1 aperture never stops
        # slot 2 and empty slot-2 apertures BEFORE film do not stop it.
        aps = _holder_apertures(_strip1("A"), _strip_spec(
            [PITCH * 3 + 8.0], "hi", lead_in=PITCH * 3 + 8.0 - LEAD - 3.5))
        rc, (r1, r2), scans, out, err = _run2(d, aps)
        assert rc == 0, (rc, err)
        assert scans == [1, 2, 3, 4, 5, "eject"], scans
        assert r2["skipped_frames"] == [6] and "skipped_frames" not in r1
        assert _shape(r2).get(1) == "W", _shape(r2)
    with tempfile.TemporaryDirectory() as d:
        aps = _holder_apertures(_strip_spec([3.5], "lo"), _strip2("A", n=2))
        rc, (r1, r2), scans, out, err = _run2(d, aps, no_stop_at_empty=True)
        assert scans == [1, 2, 3, 4, 5, 6, "eject"], scans
    print("test_stop_rule_is_per_slot OK")


def test_frames_validation_and_flags():
    parser = cli.build_parser()
    a = parser.parse_args(["digitize", "-o", "x", "--film", "110"])
    assert a.strips == 1 and a.frames is None
    a = parser.parse_args(["digitize", "-o", "x", "--film", "110", "--strips", "2"])
    assert a.strips == 2 and cli._validate_film110_args(a) is None
    a = parser.parse_args(["digitize", "-o", "x", "--strips", "2"])
    assert cli._validate_film110_args(a) == "--strips 2 only applies with --film 110"
    aps = _holder_apertures(_strip1("A"), _strip2("A"))
    for spec, which in (("1-3", "slot 2"), ("4-6", "slot 1"), ("2", "slot 2")):
        with tempfile.TemporaryDirectory() as d:
            rc, recs, scans, out, err = _run2(d, aps, frames=spec)
            assert rc == 2 and scans == [] and which in err, (spec, rc, err)
            assert not (Path(d) / "roll-001").exists()
    with tempfile.TemporaryDirectory() as d:
        rc, recs, scans, out, err = _run2(d, aps, frames="1,4")
        assert rc == 0 and scans == [1, 4, "eject"], (rc, scans, err)
        assert recs[0]["frames"] == [1] and recs[1]["frames"] == [4]
    with tempfile.TemporaryDirectory() as d:
        rc, _r, scans, out, err = _run(d, aps, strips=2, frames="1-4")
        assert rc == 0 and scans == [1, 2, 3, 4, "eject"], (rc, scans, err)
    print("test_frames_validation_and_flags OK")


def test_pair_guard_and_force():
    with tempfile.TemporaryDirectory() as d:
        # A lone-strip roll 1 is not half of a pair.
        assert _run_a(d)[0] == 0
        rc, recs, scans, out, err = _run2(
            d, _holder_apertures(_strip1("A"), _strip2("A")), roll=1)
        assert rc == 2 and scans == [] and "not recorded as slot 1" in err, (rc, err)
    with tempfile.TemporaryDirectory() as d:
        aps = _holder_apertures(_strip1("A"), _strip2("A"))
        assert _run2(d, aps)[0] == 0
        # Same placement twice on the pair: refused (given), before hardware.
        rc, recs, scans, out, err = _run2(d, aps, roll=1, placement="A")
        assert rc == 2 and scans == [] and "placement A" in err, (rc, err)
        # An inferred repeat stops at the deciding frame of slot 1.
        rc, recs, scans, out, err = _run2(d, aps, roll=1)
        assert rc == 5 and scans == [1, "eject"], (rc, scans, err)
        assert [r["status"] for r in recs] == ["failed", "failed"]
        # --force on a finished pair clears BOTH rolls.
        assert _run2(d, _holder_apertures(_strip1("B"), _strip2("B")), roll=1,
                     force=True)[0] == 0
    with tempfile.TemporaryDirectory() as d:
        aps = _holder_apertures(_strip1("A"), _strip2("A"))
        assert _run2(d, aps)[0] == 0
        assert _run2(d, _holder_apertures(_strip1("B"), _strip2("B")), roll=1)[0] == 0
        rc, (r1, r2), scans, out, err = _run2(d, aps, roll=1, force=True)
        assert rc == 0, (rc, err)
        for r in (1, 2):
            names = _names(d, r)
            assert not any(n.startswith("b-") for n in names), (r, names)
        assert "removed" in out
    print("test_pair_guard_and_force OK")


def test_strips_one_is_unchanged():
    """--strips 1 (and the default) write no two-strip fields."""
    with tempfile.TemporaryDirectory() as d:
        rc, rec, scans, out, err = _run_a(d, strips=1)
        assert rc == 0 and scans == [1, 2, 3, 4, "eject"]
        for k in ("strips", "slot", "companion_roll", "film_found", "placement_source"):
            assert k not in rec, (k, rec)
        assert "roll 1 done (110, placement A)" in out, out
    print("test_strips_one_is_unchanged OK")


def main() -> int:
    tests = [
        test_slot_constants_and_anchored_numbering,
        test_two_strips_run_a_makes_two_rolls,
        test_second_run_fills_both_rolls,
        test_either_rail_gives_the_same_numbering,
        test_slot_two_leading_split_is_image_one_and_slot_one_split_is_reported,
        test_image_seated_on_the_aperture_edge_is_a_leading_split_of_its_own,
        test_empty_slot_two_is_a_found_nothing_roll,
        test_empty_slot_one_labels_the_run_by_run_order,
        test_given_placement_covers_both_slots,
        test_stop_rule_is_per_slot,
        test_frames_validation_and_flags,
        test_pair_guard_and_force,
        test_strips_one_is_unchanged,
        test_run_a_infers_placement_numbers_and_advises,
        test_two_run_roll_end_to_end,
        test_same_placement_twice_is_refused_before_hardware,
        test_inferred_same_placement_stops_without_overwriting,
        test_force_clears_both_placements_and_photographs,
        test_provisional_stem_is_renamed_when_a_later_frame_decides,
        test_nothing_found_keeps_x_stem_and_null_placement,
        test_roll_is_done_semantics,
        test_non_110_output_is_not_a_second_run,
        test_digitize_parser_and_validation,
        test_clear_and_rename_helpers,
    ]
    for t in tests:
        t()
    print(f"\n{len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
