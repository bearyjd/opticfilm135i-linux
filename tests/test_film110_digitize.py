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
        assert rec["advice"] == {"other_placement": "B", "images": [2]}, rec["advice"]
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
        m(d, {"roll": 1, "status": "ok", "film": "110", "placement": "A",
              "advice": {"other_placement": "B", "images": [2]}})
        assert digitize.roll_is_done(d, 1) is False
        m(d, {"roll": 1, "status": "ok", "film": "110", "placement": "B",
              "advice": {"other_placement": "A", "images": []}})
        assert digitize.roll_is_done(d, 1) is True          # last ok, nothing left
        # Nothing split in the first run: done after one run.
        m(d, {"roll": 2, "status": "ok", "film": "110", "placement": "A",
              "advice": {"other_placement": "B", "images": []}})
        assert digitize.roll_is_done(d, 2) is True
        # A failed run, a run that found nothing: not done.
        m(d, {"roll": 3, "status": "failed", "film": "110", "placement": "A",
              "advice": {"other_placement": "B", "images": []}})
        m(d, {"roll": 4, "status": "ok", "film": "110", "placement": None,
              "advice": {"other_placement": None, "images": []}})
        assert digitize.roll_is_done(d, 3) is False
        assert digitize.roll_is_done(d, 4) is False
        # Both placements ok, even if the last advice lists something.
        m(d, {"roll": 5, "status": "ok", "film": "110", "placement": "A",
              "advice": {"other_placement": "B", "images": [2]}})
        m(d, {"roll": 5, "status": "ok", "film": "110", "placement": "B",
              "advice": {"other_placement": "A", "images": [3]}})
        assert digitize.roll_is_done(d, 5) is True
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


def main() -> int:
    tests = [
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
