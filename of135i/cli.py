"""Command-line entry point for the of135i driver.

Layout per driver-design.md:

    of135i scan --frame N [--dpi 600|1200|2400|3600|7200] [--ir] -o out.tiff
    of135i scan --frames 1-4 [--ir] [--eject] -o out.tiff   # batch
    of135i eject
    of135i preview
    of135i status
    of135i watch
    of135i doctor

`scan`, `status`, `eject` and `doctor` are wired to device.py/diag.py.
`preview` has no captured trace to derive a phase list from yet
(driver-design.md open item) and stays a stub.

Hardware safety (safety.py, docs/hardware-safety.md): `status` and
`doctor` open a READ-ONLY session that can never write. `scan`,
`eject` and `watch` open a writing session whose first write is
preceded by the start-state check inside the driver; the CLI adds
nothing to those rules, it only reports the outcome. On any failure
or Ctrl-C the CLI prints the session's failure record and the power-
cycle instruction and exits -- it never sends a recovery command.
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable

import usb.core

from . import diag, holder, image, safety, tables
from .device import SUPPORTED_DPIS, Scanner
from .usbio import InterruptOverflowError, Of135iError, UsbIo

log = logging.getLogger("of135i")

#: Sanity ceiling on --overscan (mm). The real per-frame bound is the
#: transport ceiling, checked by holder.overscan_geometry frame by frame;
#: this only rejects absurd input early.
_MAX_OVERSCAN_MM = 5.0


def _validate_overscan(overscan: float, frames, dpi: int = 3600,
                       dual: bool = False) -> str | None:
    """Check a --overscan value and its whole resulting geometry BEFORE any
    hardware write. Returns an error message, or None if every frame's
    window (FEEDL, scan start/end, line and chunk count) is inside the
    proven transport travel. A bad argument is thus caught before the
    device is even opened, not after calibration."""
    import math
    if not math.isfinite(overscan):
        return f"--overscan must be a finite number, got {overscan}"
    if overscan <= 0:
        return f"--overscan must be greater than 0 mm, got {overscan}"
    if overscan > _MAX_OVERSCAN_MM:
        return (f"--overscan {overscan} mm exceeds the {_MAX_OVERSCAN_MM} mm "
                f"sanity limit")
    for frame in frames:
        try:
            if dual:
                from .device import dual_tables
                t = dual_tables(dpi)
                holder.dual_overscan_geometry(
                    frame,
                    dpi=dpi,
                    lines_per_chunk=t.LINES_PER_CHUNK,
                    colour_crop_lines=image.align_shift(dpi),
                    default_wire_lines=t.DEFAULT_LINES,
                    overscan_mm=overscan,
                )
            else:
                holder.overscan_geometry(
                    frame,
                    res_units_per_line=7200 // 3600,
                    chunk_lines=tables.IMAGE_CHUNK_LINES,
                    colour_crop_lines=image.align_shift(3600),
                    overscan_mm=overscan,
                )
        except (safety.FrameOutOfRangeError, safety.FeedlOutOfRangeError) as e:
            return (f"--overscan {overscan} mm is out of range for frame "
                    f"{frame}: {e}")
    return None

_STATUS_REGS = (0x01, 0x31, 0x32, 0x35)
_BUTTON_NAMES = {0x48: "eject", 0x04: "sensor"}


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        with UsbIo.open(readonly=True) as io:
            for reg in _STATUS_REGS:
                val = io.read_reg(reg)
                print(f"reg 0x{reg:02x} = 0x{val:02x}")
                if reg == 0x01:
                    print(f"  start state: {safety.classify_reg01(val).value}")
            reg101 = io.read_ext_reg(0x101)
            print(f"reg 0x101 = 0x{reg101:02x}")
            if reg101 & 0x08:
                print("magazine: sensor reports present (does not prove it is locked)")
            else:
                print("magazine: not detected")
            try:
                button = io.read_button()
            except InterruptOverflowError as e:
                print(f"button: unreadable -- {e}")
            else:
                if button is None:
                    print("button: idle")
                else:
                    name = _BUTTON_NAMES.get(button, f"0x{button:02x}")
                    print(f"button: {name}")
    except Of135iError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _cmd_doctor(args: argparse.Namespace) -> int:
    try:
        with UsbIo.open(readonly=True) as io:
            report = diag.collect_doctor(io)
            print(diag.format_doctor(report))
            if args.json:
                diag.write_sidecar(args.json, report)
                print(f"wrote {args.json}")
    except Of135iError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _run_writing_session(body: Callable[[Scanner], int]) -> int:
    """Open a writing session, run `body(scanner)`, and report.

    Every failure path ends here with a printed explanation and an
    exit code -- and NOTHING sent to the scanner after the failure:
    the driver marks the session failed the moment an exception (or
    Ctrl-C) escapes a hardware operation, and Scanner.__exit__ only
    closes the USB handle.
    """
    scanner: Scanner | None = None
    try:
        with Scanner.open() as scanner:
            return body(scanner)
    except KeyboardInterrupt:
        print("\ninterrupted.", file=sys.stderr)
        _print_session_failure(scanner)
        return 130
    except safety.SafetyError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 1
    except (Of135iError, usb.core.USBError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        _print_session_failure(scanner)
        return 1
    except Exception as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        _print_session_failure(scanner)
        raise


def _print_session_failure(scanner: Scanner | None) -> None:
    if scanner is None:
        print(safety.POWER_CYCLE_INSTRUCTION, file=sys.stderr)
        return
    s = scanner.session
    if s.failure or s.refusal:
        print(s.describe_failure(), file=sys.stderr)
    elif s.writes:
        print(f"{s.writes} write(s) had been sent; the hardware state is unknown. "
              f"{safety.NO_RECOVERY_ATTEMPTED} {safety.POWER_CYCLE_INSTRUCTION}",
              file=sys.stderr)
    else:
        print(safety.NO_COMMANDS_SENT, file=sys.stderr)


def _axis_dpi(args, positive: bool):
    """The (x_dpi, y_dpi) to stamp on the TIFF, accounting for the profile's
    anisotropy and the orientation transform applied before writing.

    The delivered strip runs along axis 0 (height) with the sensor-across
    direction on axis 1 (width): so unrotated, (x, y) = (across, along) =
    image.sampling_resolution(dpi). The 2400 dpi dual profile is the only
    anisotropic one (3600 across / 2400 along). Every 90 deg turn swaps the
    axes; the positive path adds a rot90(,3) (the horizontal mirror does
    not swap axes), --rotate adds args.rotate // 90. An even number of
    quarter-turns keeps (across, along); an odd number swaps it."""
    across, along = image.sampling_resolution(args.dpi)
    quarter = ((3 if positive else 0) + (getattr(args, "rotate", 0) or 0) // 90) % 2
    return (along, across) if quarter else (across, along)


def _write_image(arr, out: str, positive: bool = False, dpi=None) -> None:
    """Write `arr`; a --positive TIFF gets an sRGB ICC profile embedded
    (the positive rendering targets the vendor app's sRGB output), a
    raw negative none (linear scanner data). `dpi` becomes the TIFF's
    resolution tags, so the file states the scale it was scanned at; pass
    an (x_dpi, y_dpi) tuple for the anisotropic 2400 dpi dual profile (see
    _axis_dpi / image.sampling_resolution). PPM has no such field."""
    if out.lower().endswith((".pnm", ".ppm")):
        image.write_pnm16(arr, out)
    else:
        image.write_tiff16(arr, out, icc=image.srgb_icc() if positive else None,
                           dpi=dpi)


def _parse_frames(spec: str) -> list[int]:
    """Parse a --frames spec: comma-separated frame numbers and/or
    inclusive ranges, e.g. "1-4", "2", "1,3-4". Order is preserved,
    duplicates are not removed (scanning a frame twice is a valid,
    if unusual, request)."""
    frames: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            lo_s, hi_s = part.split("-", 1)
            lo, hi = int(lo_s), int(hi_s)
            if hi < lo:
                raise ValueError(f"descending range {part!r}")
            frames.extend(range(lo, hi + 1))
        else:
            frames.append(int(part))
    if not frames:
        raise ValueError(f"invalid frame spec {spec!r}")
    # The holder's aperture count is the bound (of135i/holder.py). The
    # driver refuses an out-of-range frame again at feedl_for_frame(),
    # before any write; checking here as well turns it into a usage
    # error that never opens the device.
    bad = [f for f in frames if f < 1 or f > holder.DEFAULT.frames]
    if bad:
        raise ValueError(
            f"frame(s) {', '.join(str(f) for f in bad)} outside the "
            f"{holder.DEFAULT.name}: it holds {holder.DEFAULT.frames} "
            f"frames (1-{holder.DEFAULT.frames})")
    return frames


def _frame_output(out: str, frame: int) -> str:
    """Per-frame output path for a batch scan: insert -f<N> before the
    suffix (out.tiff -> out-f2.tiff)."""
    p = Path(out)
    return str(p.with_name(f"{p.stem}-f{frame}{p.suffix}"))


def _validate_film110_args(args: argparse.Namespace) -> "str | None":
    """--placement only applies with --film 110. With --film 110 it is
    optional: images are numbered by their position on the strip
    (of135i.film110.image_number), which needs the placement
    (docs/film-110.md §3), and when it is not given it is INFERRED from the
    first numbered image's position (of135i.film110.infer_placement); an
    image midway between the two phases is refused with a request to give
    it. Checked before any device I/O, like the neighbouring
    --dpi/--overscan checks."""
    film = getattr(args, "film", "135")
    placement = getattr(args, "placement", None)
    if film != "110" and placement is not None:
        return "--placement only applies with --film 110"
    return None


def _cmd_scan(args: argparse.Namespace) -> int:
    if args.dpi not in SUPPORTED_DPIS:
        print(f"error: --dpi must be one of {', '.join(map(str, SUPPORTED_DPIS))}", file=sys.stderr)
        return 2
    err = _validate_film110_args(args)
    if err is not None:
        print(f"error: {err}", file=sys.stderr)
        return 2
    # Every resolution other than 3600 exists only as a dual-light
    # (alternating IR/visible line) capture, so those always run the
    # dual flow; --ir then only decides whether the IR channel is used
    # (dust removal) and written out.
    dual = args.ir or args.dpi != 3600
    if getattr(args, "overscan", None) is None:
        # The production default on BOTH paths: the A+C contract
        # (corrected mapping + overscan + per-scan coverage + registered
        # crop), accepted in Test 58 for plain and carried to the dual
        # profiles in visible-line units. --overscan only tunes the
        # margin; there is no fixed-window path any more.
        args.overscan = holder.OVERSCAN_MM
    if (args.frame is None) == (args.frames is None):
        print("error: give exactly one of --frame or --frames", file=sys.stderr)
        return 2
    if args.frames is not None:
        try:
            frames = _parse_frames(args.frames)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    else:
        try:
            frames = _parse_frames(str(args.frame))
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
    multi = args.frames is not None

    # Validate --overscan and its whole geometry before opening the device,
    # so a bad value is refused with zero hardware activity (Astra point 3).
    if getattr(args, "overscan", None) is not None:
        err = _validate_overscan(args.overscan, frames, dpi=args.dpi, dual=dual)
        if err is not None:
            print(f"error: {err}", file=sys.stderr)
            return 2

    # Validate the output location BEFORE any hardware: a scan whose
    # files cannot be written is a completed pass thrown away (seen
    # 2026-09-10: an empty shell variable made the path '/f.tiff' --
    # the scan and PARK ran to completion and the data was lost in the
    # host-side write). Fail here instead, with zero writes sent.
    import os as _os
    for f in frames:
        o = _frame_output(args.output, f) if multi else args.output
        parent = Path(o).resolve().parent
        if not parent.is_dir():
            print(f"error: output directory {parent} does not exist "
                  f"(for {o}); nothing was sent to the scanner", file=sys.stderr)
            return 2
        if not _os.access(parent, _os.W_OK):
            print(f"error: output directory {parent} is not writable "
                  f"(for {o}); nothing was sent to the scanner", file=sys.stderr)
            return 2

    # One device session for the whole batch, initialize() per frame:
    # the post-scan PARK phase turns the lamp off and tears the scan
    # state down, and the vendor re-runs the PREP/AFE_BASE equivalent
    # before every frame (protocol-notes.md pass 14). initialize()
    # writes the power-on base table only on its first call per
    # session, matching the vendor.
    def body(scanner: Scanner) -> int:
        scanner.park_mode = args.park
        scanner.recalibrate = bool(getattr(args, 'recalibrate', False))
        if getattr(args, 'warmup_budget', None) is not None:
            scanner.warmup_budget_s = float(args.warmup_budget)
        # Read-only start-state check up front, so an unsafe scanner
        # is reported before the magazine message (the driver would
        # refuse at the first write anyway).
        scanner.check_start_state()
        # Check the loader sensor BEFORE initialize() — the base
        # register table written by initialize() changes ext reg
        # 0x101 state, making the sensor bit unreliable after it.
        if not scanner.is_magazine_present():
            print("error: no magazine detected — insert the cassette "
                  "and run load_magazine.py first", file=sys.stderr)
            return 1
        incomplete: list[int] = []
        # --film 110: one running counter/state object for the WHOLE
        # command (shared across every aperture scanned), not per frame.
        film110_state = (Film110State(getattr(args, "placement", None))
                         if getattr(args, "film", "135") == "110" else None)
        for frame in frames:
            scanner.initialize(ir=dual, dpi=args.dpi)
            out = _frame_output(args.output, frame) if multi else args.output
            log.info("scanning frame %d @ %d dpi%s", frame, args.dpi,
                      " (dual-light pass)" if dual else "")
            cov = None
            if dual:
                raw, width, _meta = scanner.scan(
                    frame=frame, ir=True, dpi=args.dpi,
                    overscan_mm=getattr(args, "overscan", None))
                cov = _finish_dual_scan(args, raw, width, out, write_ir=args.ir,
                                        frame=frame, film110_state=film110_state)
            else:
                raw, width = scanner.scan(
                    frame=frame, overscan_mm=getattr(args, "overscan", None))
                cov = _finish_plain_scan(args, raw, width, out, frame=frame,
                                         film110_state=film110_state)
            del raw
            if cov is not None and not cov.verified:
                incomplete.append(frame)
            _write_diag_sidecar(args, scanner, out, frame, coverage=cov)
        if args.eject:
            scanner.eject()
            print("ejected")
        if incomplete:
            # Fail closed on image integrity: a coverage failure is not a
            # normal complete scan. The full overscan raw is preserved, but
            # no aperture-registered product was written and the exit status
            # says so (Astra point 2).
            print(f"error: aperture coverage NOT verified for frame(s) "
                  f"{', '.join(map(str, incomplete))} — see the .diag.json "
                  f"and the .overscan raw file(s); no registered product was "
                  f"written for them", file=sys.stderr)
            return 4
        if film110_state is not None:
            if film110_state.placement is None:
                print("110: placement undecided (no numbered image found)")
            else:
                print(f"110: placement {film110_state.placement} "
                      f"({'given' if film110_state.placement_given else 'inferred'})")
        if film110_state is not None and film110_state.problems:
            # A refused overwrite or an image off the chosen placement's
            # phase grid: the aperture/overscan products above are fine,
            # but at least one 110 product was NOT written -- fail closed,
            # the same way an unverified aperture does.
            print("error: 110 products refused for: " +
                  "; ".join(film110_state.problems), file=sys.stderr)
            return 5
        return 0

    return _run_writing_session(body)


def _orient_plain(args: argparse.Namespace, arr):
    """The plain-scan orientation transform: the vendor mirror + rotate for
    --positive (then the density inversion), and --rotate. Applied to a
    delivered image whose strip runs along axis 0."""
    import numpy as _np
    if args.positive:
        # Match the vendor apps' orientation: the sensor image is
        # mirrored (vendor ini HorizontalMirror=1) and rotated.
        arr = _np.ascontiguousarray(_np.rot90(arr, 3)[:, ::-1])
        arr = image.to_positive(arr)
    if args.rotate:
        arr = _np.ascontiguousarray(_np.rot90(arr, k=args.rotate // 90))
    return arr


def _orient_pair(args: argparse.Namespace, vis, irr):
    """Like `_orient_plain`, applied identically to a visible/IR pair so
    they stay pixel-aligned through the transform (`irr` may be None, in
    which case it passes through unchanged). Used by the dual-scan path
    and by the --film 110 hook, which needs the SAME transform the
    aperture product gets, on each individual 110 image crop."""
    import numpy as _np
    if args.positive:
        vis = _np.ascontiguousarray(_np.rot90(vis, 3)[:, ::-1])
        if irr is not None:
            irr = _np.ascontiguousarray(_np.rot90(irr, 3)[:, ::-1])
        vis = image.to_positive(vis)
    if args.rotate:
        vis = _np.ascontiguousarray(_np.rot90(vis, k=args.rotate // 90))
        if irr is not None:
            irr = _np.ascontiguousarray(_np.rot90(irr, k=args.rotate // 90))
    return vis, irr


class Film110State:
    """Per-command state for --film 110 (scan and digitize). Images are
    numbered by their position on the strip (of135i.film110.image_number,
    docs/film-110.md §3), not by a running counter, so the same photograph
    gets the same number whether it was scanned in placement A or B.

    `placement` is the command's decided placement: given with
    --placement, otherwise None until the first numbered image decides it
    (of135i.film110.infer_placement); `placement_given` says which.
    `problems` collects every 110 product this command declined to write
    (an off-phase or ambiguous image, or a file that already exists), and
    `_cmd_scan` turns a non-empty list into a failing exit code once the
    whole command has finished. `images` has one entry per numbered image
    (digitize writes it to the manifest). `forbidden` is a set of
    placements ("A"/"B") that must not be decided because the roll
    already holds output for them (digitize); deciding one sets `aborted`
    and writes nothing."""

    def __init__(self, placement: "str | None" = None):
        self.placement = placement
        self.placement_given = placement is not None
        self.announced = False
        self.problems: list = []
        self.images: list = []
        self.forbidden: set = set()
        self.aborted: "str | None" = None


def _film110_image_path(out: str, n: int, ir: bool = False) -> str:
    """<stem>-image<N>.tiff / <stem>-image<N>-ir.tiff beside `out`, always
    .tiff regardless of -o's own extension -- the same convention the
    dual path's -ir.tiff sidecar already uses."""
    p = Path(out)
    suffix = f"-image{n}-ir.tiff" if ir else f"-image{n}.tiff"
    return str(p.with_name(f"{p.stem}{suffix}"))


def _film110_process_aperture(args: argparse.Namespace, state: Film110State,
                              aperture: int, vis_crop, ir_crop, emit) -> None:
    """The 110 detection + numbering + cropping shared by `scan` and
    `digitize` (one detector call per aperture, one implementation of
    placement inference, numbering and the phase check). `emit(n, fr,
    crops)` is the caller's writer, called for every WHOLE, correctly
    placed image: `crops()` returns the padded (visible, IR-or-None)
    crops in the aperture's own, un-oriented axes; `emit` returns a dict
    {"main", "ir", "replaced_free_end_copy", "refused", "message"} --
    `refused` set means nothing was written for the image."""
    from . import film110, holder as _holder

    if state.placement is None and getattr(args, "placement", None):
        state.placement = args.placement
        state.placement_given = True

    find = film110.detect(vis_crop, dpi=args.dpi, film=_holder.FILM_110)
    if find.empty:
        print(f"110: aperture {aperture} — empty")
        return
    if not find.frames:
        print(f"110: aperture {aperture} — {find.reason or 'no images predicted'}")
        return

    px_per_mm = args.dpi / film110.MM_PER_INCH
    parts = []
    for fr in find.frames:
        if fr.split == "leading":
            # The tail of an image that started in the previous aperture:
            # it was numbered there (as a trailing split), so it is not
            # counted again here.
            parts.append("leading lines continue the previous aperture's "
                         "split image (already counted)")
            continue

        pos_mm = fr.line0 / px_per_mm
        note = ""
        if state.placement is None:
            inferred, _res = film110.infer_placement(aperture, fr.line0, args.dpi)
            if inferred is None:
                _na, res_a = film110.image_number(aperture, fr.line0, args.dpi, "A")
                _nb, res_b = film110.image_number(aperture, fr.line0, args.dpi, "B")
                msg = (f"image at {pos_mm:.1f} mm lies between the two "
                       f"placements' phases (A off by {res_a:+.1f} mm, B off "
                       f"by {res_b:+.1f} mm) -> give --placement A or B; no "
                       f"110 product written")
                parts.append(msg)
                state.problems.append(msg)
                continue
            if inferred in state.forbidden:
                msg = (f"image at {pos_mm:.1f} mm reads as placement "
                       f"{inferred}, which this roll already holds output "
                       f"for -> stopped; no 110 product written")
                parts.append(msg)
                state.problems.append(msg)
                state.aborted = msg
                break
            state.placement = inferred
        if not state.announced:
            how = "given" if state.placement_given else "inferred"
            note = f"placement {state.placement} ({how}); "
            state.announced = True

        n, residual = film110.image_number(aperture, fr.line0, args.dpi,
                                           state.placement)
        if (abs(residual) > film110.PLACEMENT_PHASE_TOLERANCE_MM
                or n < 1):
            msg = (f"image at {pos_mm:.1f} mm is not at placement "
                   f"{state.placement}'s phase (off by {residual:+.1f} mm) "
                   f"-> check the placement; no 110 product written")
            parts.append(note + msg)
            state.problems.append(msg)
            continue

        entry = {"number": n, "aperture": aperture, "whole": bool(fr.whole),
                 "split": fr.split, "free_end_near": bool(fr.free_end_near),
                 "main": None, "ir": None,
                 "size_mm": ([round(float(fr.size_mm[0]), 2),
                              round(float(fr.size_mm[1]), 2)]
                             if fr.whole else None),
                 "replaced_free_end_copy": False}
        state.images.append(entry)

        if fr.whole:
            def crops(fr=fr):
                # Grown by PRODUCT_PAD_MM: a safety margin of dark printed
                # border around an edge that lies within 0.06 mm of the
                # independently measured foot (docs/film-110.md §9.1) --
                # not a correction for a biased edge.
                pad = film110.PRODUCT_PAD_MM
                v = film110.crop(vis_crop, fr, dpi=args.dpi, pad_mm=pad)
                i = (film110.crop(ir_crop, fr, dpi=args.dpi, pad_mm=pad)
                     if ir_crop is not None else None)
                return v, i

            res = emit(n, fr, crops)
            entry["main"] = res.get("main")
            entry["ir"] = res.get("ir")
            entry["replaced_free_end_copy"] = bool(res.get("replaced_free_end_copy"))
            if res.get("refused"):
                entry["refused"] = res["refused"]
                parts.append(note + res["refused"])
                state.problems.append(res["refused"])
                continue
            parts.append(note + res.get("message",
                         f"image {n} whole ({fr.size_mm[0]:.1f} x "
                         f"{fr.size_mm[1]:.1f} mm)"))
        else:
            # The only other split value reaching here is "trailing" (a
            # leading split was handled above): the tail of this image
            # runs under the next aperture's bar.
            other = "B" if state.placement == "A" else "A"
            parts.append(note + f"image {n} split by the trailing bar -> "
                         f"take it in placement {other}")
        if fr.free_end_near:
            parts[-1] += " [free strip end nearby: sharpness untrusted]"

    print(f"110: aperture {aperture} — " + "; ".join(parts))
    if find.leading_continuation:
        print(f"110: aperture {aperture} — leading lines continue a split "
              f"image from the previous aperture")


def _film110_hook(args: argparse.Namespace, state: "Film110State | None",
                  aperture: int, out: str, vis_crop, ir_crop) -> None:
    """`scan --film 110`: detect 110 images inside one aperture-registered
    crop and write `<stem>-image<N>.tiff` per WHOLE one, numbered by strip
    position so the same photograph gets the same number whether it was
    scanned in placement A or B. Called from both `_finish_plain_scan` and
    `_finish_dual_scan`, on the SAME aperture-registered arrays those
    write their own product from (before orientation -- the strip runs
    along axis 0 here), so the plain and dual paths cannot drift apart.
    A no-op when --film is not "110" (state is None in that case) or when
    coverage did not verify (the caller only invokes this once it has).

    The placement is `args.placement` when given, otherwise inferred from
    the first numbered image (see `_film110_process_aperture`). An image
    that does not land on the placement's phase grid, is ambiguous, or
    whose product file already exists, writes nothing and is recorded in
    `state.problems`; `_cmd_scan` turns a non-empty list into a failing
    exit code once the whole command has finished.

    `ir_crop` is None on the plain path and when --ir was not given on
    the dual path -- the 110 IR sidecar is then simply not written,
    matching the aperture product's own --ir behaviour.
    """
    if state is None:
        return

    def emit(n, fr, crops):
        vis_path = _film110_image_path(out, n)
        if Path(vis_path).exists():
            return {"refused": f"{vis_path} exists -> not overwritten (use "
                               f"one -o stem per placement, docs/film-110.md §7)"}
        vis_img, ir_img = crops()
        vis_img, ir_img = _orient_pair(args, vis_img, ir_img)
        image.write_tiff16(vis_img, vis_path,
                           icc=image.srgb_icc() if args.positive else None,
                           dpi=_axis_dpi(args, args.positive))
        print(f"wrote {vis_path} ({vis_img.shape[1]}x{vis_img.shape[0]}, "
              f"16-bit RGB, 110 image {n})")
        ir_path = None
        if ir_img is not None:
            import numpy as _np
            ir_path = _film110_image_path(out, n, ir=True)
            image.write_tiff16(_np.stack([ir_img, ir_img, ir_img], axis=-1),
                               ir_path, dpi=_axis_dpi(args, args.positive))
            print(f"wrote {ir_path} ({ir_img.shape[1]}x{ir_img.shape[0]}, "
                  f"16-bit, 110 image {n} IR channel)")
        return {"main": vis_path, "ir": ir_path}

    _film110_process_aperture(args, state, aperture, vis_crop, ir_crop, emit)


def _overscan_raw_path(out: str) -> str:
    """The filename for the preserved full overscan frame beside `out`."""
    p = Path(out)
    return str(p.with_name(f"{p.stem}.overscan{p.suffix}"))


def _finish_plain_scan(args: argparse.Namespace, raw: bytes, width: int,
                       out: str, frame: int = 1, film110_state=None):
    """Write a plain scan's output.

    Without --overscan: `out` is the channel-aligned delivered image, as
    before. Returns None.

    With --overscan there are three distinct artefacts, never conflated:
      1. the raw scanner buffer (`raw`, not written here);
      2. the channel-aligned full overscan frame -> `<out>.overscan.<ext>`,
         always written (the raw-data principle);
      3. the aperture-registered production image -> `out`, cropped to the
         plastic edges detected in THIS scan -- written only when coverage
         verifies. On a coverage failure `out` is deliberately NOT written,
         so nothing that looks like a finished scan exists for an
         unverified frame (fail closed on image integrity).
    Returns the ApertureCoverage in the overscan case.

    `film110_state`, when not None (--film 110), runs the 110 hook on
    the SAME aperture-registered crop right after it is computed, before
    orientation -- see `_film110_hook`.
    """
    arr = image.assemble(raw, width)
    arr = image.align_channels(arr, dpi=args.dpi)
    if getattr(args, "overscan", None) is None:
        arr = _orient_plain(args, arr)
        _write_image(arr, out, positive=args.positive, dpi=_axis_dpi(args, args.positive))
        print(f"wrote {out} ({arr.shape[1]}x{arr.shape[0]}, 16-bit RGB)")
        return None

    # Coverage on the delivered (colour-cropped) image, before orientation
    # (the strip runs along axis 0 here). docs/holder-position-design.md
    # section 5.
    from . import aperture_crop
    cov = aperture_crop.measure_coverage(arr, dpi=args.dpi)

    # (2) preserve the full overscan frame regardless of the verdict.
    raw_out = _overscan_raw_path(out)
    full = _orient_plain(args, arr)
    _write_image(full, raw_out, positive=args.positive, dpi=_axis_dpi(args, args.positive))
    print(f"wrote {raw_out} ({full.shape[1]}x{full.shape[0]}, full overscan frame)")
    del full

    if cov.verified:
        # (3) the aperture-registered product, cropped to the detected edges.
        crop = aperture_crop.crop_to_aperture(arr, cov, dpi=args.dpi)
        _film110_hook(args, film110_state, frame, out, crop, None)
        crop = _orient_plain(args, crop)
        _write_image(crop, out, positive=args.positive, dpi=_axis_dpi(args, args.positive))
        print(f"aperture coverage: VERIFIED — margins lead "
              f"{cov.leading_margin_mm:.3f} mm, trail {cov.trailing_margin_mm:.3f} mm")
        print(f"wrote {out} ({crop.shape[1]}x{crop.shape[0]}, aperture-registered)")
    else:
        print(f"aperture coverage: NOT verified — {cov.reason}. Kept the full "
              f"overscan at {raw_out}; no aperture-registered product written "
              f"for this frame.", file=sys.stderr)
    return cov


def _finish_dual_scan(args: argparse.Namespace, raw: bytes, width: int,
                      out: str, write_ir: bool = True, frame: int = 1,
                      film110_state=None):
    """Split a dual-light scan's raw buffer into visible/IR images and
    write <out> (visible, color) and, with write_ir, <out stem>-ir.tiff
    (the IR channel, replicated into R=G=B so it opens in any RGB
    viewer). Returns the frame's ApertureCoverage.

    The dual path carries the same A+C production contract as plain
    (Test 58): coverage is measured on the aligned visible frame, the
    full overscan frame(s) are always preserved (`<out>.overscan.<ext>`,
    and with write_ir `<stem>-ir.overscan.tiff`), and the products are
    written only when coverage verifies -- cropped to the SAME line
    indices in both channels, so visible and IR stay exactly registered
    through the crop (they are on one pixel grid from the alignment
    step; dust removal runs before the crop on that same grid).

    Any orientation transform (the --positive mirror+rotate, and
    --rotate) is applied identically to both images so they stay
    pixel-aligned -- also what keeps them aligned for the dust-removal
    pass below, which runs before either transform (it only needs the
    two images on the same pixel grid, not any particular orientation).

    With write_ir (i.e. --ir), `visible` is cleaned of dust/scratches
    using the IR channel's dust map (image.remove_dust) before any
    further processing (including --positive) unless --no-clean is
    given. Without --ir (a non-3600 dpi scan, which is always a dual-
    light pass on the wire) the IR channel is discarded and the
    visible image written as-is.

    `film110_state`, when not None (--film 110), runs the 110 hook on
    the SAME aperture-registered visible/IR crop pair right after they
    are computed, before orientation -- see `_film110_hook`.
    """
    import numpy as _np

    visible, ir = image.split_ir(raw, width=width, dpi=args.dpi)

    # Channel alignment BEFORE dust removal: the staggered R/G/B lines
    # give every dust speck a colored halo wider than its dark core;
    # cleaning on unaligned data leaves rainbow ghosts around the
    # inpainted area (observed 2026-08-30). The stagger in the de-
    # interleaved visible array is the full 24*dpi/7200 lines, the same
    # as in a plain scan: each physical line position yields one IR and
    # one visible raw line, so the visible array has the nominal line
    # density (measured on the 600/1200/2400 dpi captures: 2/4/8 lines,
    # docs/protocol-notes.md pass 18; the earlier code halved it). Crop
    # `ir` identically to keep the two images on the same pixel grid
    # (split_ir has already rolled the IR channels into register; its
    # wrapped edge rows are exactly the rows cropped here).
    _shift = round(24 * args.dpi / 7200)
    visible = image.align_channels(visible, dpi=args.dpi)
    if _shift:
        ir = ir[_shift:-_shift]

    if write_ir and not args.no_clean:
        visible = image.remove_dust(visible, ir)

    from . import aperture_crop
    # `visible` (aligned, cropped by the colour shift at both ends) and `ir`
    # (split_ir rolled it into register, then cropped by the same shift
    # above) share one line grid; measure_coverage_dual checks that. The IR
    # array is used for coverage even when no IR file is written.
    cov = aperture_crop.measure_coverage_dual(visible, ir, dpi=args.dpi)

    # _orient_pair is the module-level helper above (shared with the
    # --film 110 hook): same orientation fix as the plain path, applied
    # identically to both channels so they stay pixel-aligned.

    def _write_ir_file(arr, path):
        # The IR channel is oriented IDENTICALLY to the visible image by
        # _orient_pair (the --positive rot90(,3) mirror and --rotate are
        # applied to both), so its delivered axes match the visible ones
        # -- stamp the SAME per-axis dpi (args.positive), not _axis_dpi(
        # ..., False). Anything else made a --positive 2400 dpi IR frame
        # claim the un-rotated (across, along) while its pixels were turned
        # like the visible frame (the dual-path metadata bug). This differs
        # from digitize, where the IR is only --rotate'd (positive=False is
        # correct there).
        image.write_tiff16(_np.stack([arr, arr, arr], axis=-1), path,
                           dpi=_axis_dpi(args, args.positive))

    # Preserve the full overscan frame(s) regardless of the verdict
    # (raw-data principle), oriented like the products.
    raw_out = _overscan_raw_path(out)
    ov_vis, ov_ir = _orient_pair(args, visible, ir if write_ir else None)
    _write_image(ov_vis, raw_out, positive=args.positive, dpi=_axis_dpi(args, args.positive))
    print(f"wrote {raw_out} ({ov_vis.shape[1]}x{ov_vis.shape[0]}, full "
          f"overscan frame, visible)")
    if write_ir:
        out_path = Path(out)
        ir_over = str(out_path.with_name(out_path.stem + "-ir.overscan.tiff"))
        _write_ir_file(ov_ir, ir_over)
        print(f"wrote {ir_over} ({ov_ir.shape[1]}x{ov_ir.shape[0]}, full "
              f"overscan frame, IR channel)")
    del ov_vis, ov_ir

    if not cov.verified:
        print(f"aperture coverage: NOT verified — {cov.reason}. Kept the full "
              f"overscan frame(s); no registered product written for this "
              f"frame.", file=sys.stderr)
        return cov

    print(f"aperture coverage: VERIFIED (source: {cov.source}) — margins lead "
          f"{cov.leading_margin_mm:.3f} mm, trail {cov.trailing_margin_mm:.3f} mm")
    vis_c = aperture_crop.crop_to_aperture(visible, cov, dpi=args.dpi)
    ir_c = aperture_crop.crop_to_aperture(ir, cov, dpi=args.dpi) if write_ir else None
    _film110_hook(args, film110_state, frame, out, vis_c, ir_c)
    vis_c, ir_c = _orient_pair(args, vis_c, ir_c)

    _write_image(vis_c, out, positive=args.positive, dpi=_axis_dpi(args, args.positive))
    print(f"wrote {out} ({vis_c.shape[1]}x{vis_c.shape[0]}, 16-bit RGB, "
          f"visible, aperture-registered)")

    if write_ir:
        out_path = Path(out)
        ir_out = str(out_path.with_name(out_path.stem + "-ir.tiff"))
        _write_ir_file(ir_c, ir_out)
        print(f"wrote {ir_out} ({ir_c.shape[1]}x{ir_c.shape[0]}, 16-bit, "
              f"IR channel, aperture-registered)")
    return cov


def _write_diag_sidecar(args: argparse.Namespace, scanner: Scanner, out: str,
                        frame: int, coverage=None, film=None) -> None:
    """Write <out>'s .diag.json sidecar from scanner.last_diag (see
    diag.py/device.py) unless --no-diag was given, and log a one-line
    INFO summary of the per-frame calibration/health counters.

    When `coverage` is given (the overscan path), the sidecar records
    whether the aperture was fully captured and, if not, why -- so an
    automated workflow can tell a verified frame from an incomplete one
    without re-reading the image (Astra point 2)."""
    if args.no_diag or scanner.last_diag is None:
        return
    d = scanner.last_diag
    sidecar = dict(d)
    sidecar["output"] = out
    if coverage is not None:
        sidecar["coverage"] = {
            "verified": coverage.verified,
            "reason": coverage.reason,
            "leading_line": coverage.leading_line,
            "trailing_line": coverage.trailing_line,
            "leading_margin_mm": coverage.leading_margin_mm,
            "trailing_margin_mm": coverage.trailing_margin_mm,
            "source": getattr(coverage, "source", "visible"),
        }
        if film is not None:
            # digitize only: film presence inside the verified aperture.
            sidecar["film"] = {"present": bool(film["present"]),
                               "saturated_share": film["saturated_share"]}
            if not film["present"]:
                sidecar["coverage"]["reason"] = "empty aperture"
    sidecar["cli"] = {
        "frame": frame,
        "frames": args.frames,
        "dpi": args.dpi,
        "ir": args.ir,
        "positive": args.positive,
        "rotate": args.rotate,
        "no_clean": args.no_clean,
        "overscan": getattr(args, "overscan", None),
    }
    # Per-pixel calibration data (device.Scanner.last_cal_data): only a
    # fresh calibration has any, and --no-cal-data skips it. Decided and
    # written BEFORE the sidecar so the sidecar records the outcome.
    cal_path = None
    cal_data = getattr(scanner, "last_cal_data", None)
    if cal_data is not None and not getattr(args, "no_cal_data", False):
        import numpy as _np
        try:
            cal_path = diag.cal_data_path(out)
            _np.savez_compressed(cal_path, **cal_data)
            print(f"wrote {cal_path}")
        except OSError as e:
            log.warning("could not write the calibration data file: %s", e)
            cal_path = None
    sidecar["cal_data"] = cal_path
    path = diag.sidecar_path(out)
    diag.write_sidecar(path, sidecar)
    print(f"wrote {path}")
    gain_r, gain_g, gain_b = d.get("gain_codes", [0, 0, 0])
    off_r, off_g, off_b = d.get("offset_codes", [0, 0, 0])
    log.info(
        "frame %d diag: gain R=%#04x G=%#04x B=%#04x offset R=%#06x G=%#06x B=%#06x "
        "warmup_attempts=%s poll_timeouts=%s cr_mismatches=%s",
        frame, gain_r, gain_g, gain_b, off_r, off_g, off_b,
        d.get("warmup_attempts"), d.get("poll_timeouts"), d.get("cr_mismatches"),
    )


def _cmd_load(args: argparse.Namespace) -> int:
    """The magazine load flow (of135i.loadflow). Interactive: it asks the
    operator to take the magazine out and reinsert it to the stop, so it
    needs a real terminal (a piped stdin ends it at the prompt, exit 130).
    ``--release`` stops after the jog and never asks. ``--next-strip``
    loads the next strip after an eject in the same power-on: no jog, no
    reinsert prompt, and it never asks either."""
    from . import loadflow
    if args.release and args.double_jog:
        print("error: --release and --double-jog exclude each other", file=sys.stderr)
        return 2
    if args.next_strip and (args.release or args.double_jog):
        print("error: --next-strip excludes --release and --double-jog", file=sys.stderr)
        return 2
    return loadflow.run(ask=input, release_only=args.release, double_jog=args.double_jog,
                        next_strip=args.next_strip)


def _finish_digitize_frame(args: argparse.Namespace, raw: bytes, width: int,
                           out: str, dual: bool, progress: dict | None = None,
                           film110=None):
    """digitize's per-frame writer. The MAIN image (`out`, fN.tiff) is
    always the raw NEGATIVE -- never inverted -- so it is the archival
    product. With --positive a SEPARATE preview (fN-preview.tiff, positive,
    sRGB) is written from the SAME scan (no extra hardware pass). This
    differs from `scan`, where --positive replaces the main image; here the
    negative is preserved and the preview is a side file.

    Note: with IR (and unless --no-clean) the main negative is
    dust-cleaned; it is calibrated, channel-aligned linear data, not an
    untouched sensor dump. Returns (main, ir_file, preview_file,
    dust_cleaned, coverage) -- coverage is the frame's ApertureCoverage.

    Both paths carry the same A+C production contract as `scan`
    (Test 58): the device delivers the overscan window; the full frame
    is always preserved as `<out stem>.overscan.<ext>` (and, with IR,
    `<stem>-ir.overscan.tiff`), and the MAIN negative is written only
    when aperture coverage verifies, cropped to the edges detected in
    THIS scan -- on the dual path the IR channel is cropped by the SAME
    line indices, keeping the channels exactly registered. On a
    coverage failure main is None -- nothing that looks like a finished
    frame exists for an unverified frame.

    `progress`, if given, is filled in as each file lands ("main", "ir",
    "preview"), so a failure part-way through a frame leaves a record of
    what was actually written.

    `film110`, if given (digitize --film 110), is called as
    film110(visible, ir_or_None) with the aperture-registered crops -- the
    same arrays the main negative is written from, before rotation or the
    preview -- and only for a frame whose coverage verified and whose
    aperture holds film. It runs the shared 110 detector/numbering code
    (`_film110_process_aperture`) with digitize's own writer."""
    import numpy as _np

    def _note(key: str, value) -> None:
        if progress is not None:
            progress[key] = value

    def _note_film(arr, cov) -> bool:
        """Film presence on the array coverage was measured on; records
        it in `progress` and returns True when the aperture is empty
        (no product is then written; the overscan frame already is)."""
        fp = aperture_crop.film_presence(arr, cov, dpi=args.dpi)
        _note("film_present", fp.present)
        _note("saturated_share", fp.saturated_share)
        if not fp.present:
            print(f"aperture EMPTY — no film in this opening "
                  f"({fp.saturated_share:.1%} of sampled pixels saturated in "
                  f"all channels); no main negative written, the overscan "
                  f"frame is kept")
        return not fp.present

    def _note_margin(cov) -> None:
        """Flag (never fail) a verified frame whose margin is implausibly
        wide -- the opening found is shorter than the holder's, e.g. a
        film end opaque to the measuring channel (aperture_crop
        MAX_PLAUSIBLE_MARGIN_MM). Recorded as `coverage_warning`."""
        w = aperture_crop.margin_warning(cov)
        if w:
            _note("coverage_warning", w)
            print(f"aperture coverage: WARNING — {w}", file=sys.stderr)

    from . import aperture_crop
    ir_file = None
    preview_file = None
    coverage = None
    if dual:
        visible, ir = image.split_ir(raw, width=width, dpi=args.dpi)
        _shift = round(24 * args.dpi / 7200)
        visible = image.align_channels(visible, dpi=args.dpi)
        if _shift:
            ir = ir[_shift:-_shift]
        if args.ir and not args.no_clean:
            visible = image.remove_dust(visible, ir)
        # visible and ir share one line grid (both trimmed by the same
        # colour shift above); the IR array is used for coverage even when
        # no IR file is written (dpi != 3600 without --ir).
        coverage = aperture_crop.measure_coverage_dual(visible, ir, dpi=args.dpi)
        # Preserve the full overscan frame(s) regardless of the verdict
        # (raw-data principle) -- rotated like the main negative, never
        # mirrored.
        over_v, over_i = visible, (ir if args.ir else None)
        if args.rotate:
            k = args.rotate // 90
            over_v = _np.ascontiguousarray(_np.rot90(visible, k=k))
            if over_i is not None:
                over_i = _np.ascontiguousarray(_np.rot90(over_i, k=k))
        raw_out = _overscan_raw_path(out)
        _write_image(over_v, raw_out, positive=False, dpi=_axis_dpi(args, False))
        _note("overscan", raw_out)
        print(f"wrote {raw_out} ({over_v.shape[1]}x{over_v.shape[0]}, full "
              f"overscan frame, visible)")
        if over_i is not None:
            ir_over = str(Path(out).with_name(Path(out).stem + "-ir.overscan.tiff"))
            image.write_tiff16(_np.stack([over_i, over_i, over_i], axis=-1),
                               ir_over, dpi=_axis_dpi(args, False))
            _note("ir_overscan", ir_over)
            print(f"wrote {ir_over} ({over_i.shape[1]}x{over_i.shape[0]}, "
                  f"full overscan frame, IR channel)")
        del over_v, over_i
        if not coverage.verified:
            print(f"aperture coverage: NOT verified — {coverage.reason}. "
                  f"Kept the full overscan frame(s); no main negative "
                  f"written for this frame.", file=sys.stderr)
            return None, None, None, bool(args.ir and not args.no_clean), coverage
        print(f"aperture coverage: VERIFIED (source: {coverage.source}) — "
              f"margins lead {coverage.leading_margin_mm:.3f} mm, trail "
              f"{coverage.trailing_margin_mm:.3f} mm")
        _note_margin(coverage)
        if _note_film(visible, coverage):
            return None, None, None, bool(args.ir and not args.no_clean), coverage
        visible = aperture_crop.crop_to_aperture(visible, coverage, dpi=args.dpi)
        ir = aperture_crop.crop_to_aperture(ir, coverage, dpi=args.dpi)
        if film110 is not None:
            film110(visible, ir)
    else:
        full = image.align_channels(image.assemble(raw, width), dpi=args.dpi)
        coverage = aperture_crop.measure_coverage(full, dpi=args.dpi)
        # Preserve the full overscan frame regardless of the verdict
        # (raw-data principle) -- rotated like the main negative, never
        # mirrored.
        over = full
        if args.rotate:
            over = _np.ascontiguousarray(_np.rot90(full, k=args.rotate // 90))
        raw_out = _overscan_raw_path(out)
        _write_image(over, raw_out, positive=False, dpi=_axis_dpi(args, False))
        _note("overscan", raw_out)
        print(f"wrote {raw_out} ({over.shape[1]}x{over.shape[0]}, full "
              f"overscan frame)")
        del over
        if not coverage.verified:
            print(f"aperture coverage: NOT verified — {coverage.reason}. "
                  f"Kept the full overscan at {raw_out}; no main negative "
                  f"written for this frame.", file=sys.stderr)
            return None, None, None, False, coverage
        print(f"aperture coverage: VERIFIED — margins lead "
              f"{coverage.leading_margin_mm:.3f} mm, trail "
              f"{coverage.trailing_margin_mm:.3f} mm")
        _note_margin(coverage)
        if _note_film(full, coverage):
            return None, None, None, False, coverage
        visible = aperture_crop.crop_to_aperture(full, coverage, dpi=args.dpi)
        del full
        ir = None
        if film110 is not None:
            film110(visible, None)

    # Dust removal already ran in the dual branch above (before the
    # coverage measurement, on the uncropped grid).
    dust_cleaned = bool(dual and args.ir and not args.no_clean)

    # Preview (positive): built with the SAME vendor orientation the scan
    # command applies before to_positive -- mirror + rot90(·,3) (vendor ini
    # HorizontalMirror=1). Without it the preview is mirrored and text reads
    # backwards. Built from the un-rotated visible; --rotate is applied to
    # it below too, so it matches `scan --positive` on the same raw data.
    prev = None
    if args.positive:
        prev = _np.ascontiguousarray(_np.rot90(visible, 3)[:, ::-1])
        prev = image.to_positive(prev)

    # Rotate the archival negative (and IR, and the preview) if requested.
    # The negative is only rotated, never mirrored -- the mirror belongs to
    # the positive path -- matching scan's raw output.
    if args.rotate:
        k = args.rotate // 90
        visible = _np.ascontiguousarray(_np.rot90(visible, k=k))
        if ir is not None:
            ir = _np.ascontiguousarray(_np.rot90(ir, k=k))
        if prev is not None:
            prev = _np.ascontiguousarray(_np.rot90(prev, k=k))

    _write_image(visible, out, positive=False, dpi=_axis_dpi(args, False))  # raw negative, never inverted
    _note("main", out)
    print(f"wrote {out} ({visible.shape[1]}x{visible.shape[0]}, 16-bit RGB "
          f"negative{', dust-cleaned' if dust_cleaned else ''})")

    if dual and args.ir:
        ir_rgb = _np.stack([ir, ir, ir], axis=-1)
        ir_file = str(Path(out).with_name(Path(out).stem + "-ir.tiff"))
        image.write_tiff16(ir_rgb, ir_file, dpi=_axis_dpi(args, False))
        _note("ir", ir_file)
        print(f"wrote {ir_file} ({ir.shape[1]}x{ir.shape[0]}, 16-bit, IR channel)")

    if prev is not None:
        preview_file = str(Path(out).with_name(Path(out).stem + "-preview.tiff"))
        _write_image(prev, preview_file, positive=True, dpi=_axis_dpi(args, True))
        _note("preview", preview_file)
        print(f"wrote {preview_file} ({prev.shape[1]}x{prev.shape[0]}, "
              f"16-bit RGB, positive preview)")

    return out, ir_file, preview_file, dust_cleaned, coverage


def _film110_digitize_emit(args: argparse.Namespace, state: Film110State,
                           roll: int):
    """The photograph writer `digitize --film 110` hands to
    `_film110_process_aperture`: <roll_dir>/image<N>.tiff (+ image<N>-ir.tiff
    with --ir), N = strip position, oriented like the frame's main negative
    (rotated if asked, never inverted or mirrored). An existing image<N>.tiff
    is not overwritten, except by the sag rule (docs/film-110.md §4): when
    the manifest (or this run) shows the existing copy was delivered with
    `free_end_near` and the new one is not, the supported copy replaces it
    (`replaced_free_end_copy`)."""
    from . import digitize
    import numpy as _np

    def emit(n, fr, crops):
        vis_path = digitize.film110_image_path(args.out, args.prefix, roll, n)
        ir_path = digitize.film110_image_path(args.out, args.prefix, roll, n, ir=True)
        replaced = False
        if vis_path.exists():
            # state.images[-1] is the entry being written right now.
            records = digitize.roll_records(args.out, roll, args.prefix)
            records.append({"images": state.images[:-1]})
            prev = digitize.delivered_image(records, roll, args.prefix, n)
            if (prev is not None and prev.get("free_end_near")
                    and not fr.free_end_near):
                replaced = True
            else:
                return {"refused": f"{vis_path.name} exists -> kept, not "
                                   f"overwritten"}
        vis_img, ir_img = crops()
        if args.rotate:
            k = args.rotate // 90
            vis_img = _np.ascontiguousarray(_np.rot90(vis_img, k=k))
            if ir_img is not None:
                ir_img = _np.ascontiguousarray(_np.rot90(ir_img, k=k))
        _write_image(vis_img, str(vis_path), positive=False,
                     dpi=_axis_dpi(args, False))
        print(f"wrote {vis_path} ({vis_img.shape[1]}x{vis_img.shape[0]}, "
              f"16-bit RGB negative, 110 image {n}"
              f"{', replaced the free-end copy' if replaced else ''})")
        ir_out = None
        if ir_img is not None:
            image.write_tiff16(_np.stack([ir_img, ir_img, ir_img], axis=-1),
                               str(ir_path), dpi=_axis_dpi(args, False))
            ir_out = str(ir_path)
            print(f"wrote {ir_path} ({ir_img.shape[1]}x{ir_img.shape[0]}, "
                  f"16-bit, 110 image {n} IR channel)")
        return {"main": str(vis_path), "ir": ir_out,
                "replaced_free_end_copy": replaced,
                "message": f"image {n} whole ({fr.size_mm[0]:.1f} x "
                           f"{fr.size_mm[1]:.1f} mm)"
                           f"{' [replaced the free-end copy]' if replaced else ''}"}

    return emit


def _film110_fill_record(record: dict, state: Film110State, rdir: Path) -> None:
    """Write the 110 fields of a digitize roll record: the decided
    placement (null when no image decided it), whether it was given, one
    `images` entry per numbered image, the refusals, and `advice` -- the
    numbers the other placement is still needed for: images split in
    this placement that no run has delivered yet, and images this run
    delivered with the strip end free (the sag rule, docs/film-110.md
    §4: the other placement's supported copy replaces them)."""
    record["film"] = "110"
    record["placement"] = state.placement
    record["placement_given"] = bool(state.placement_given)
    record["images"] = state.images
    record["problems"] = list(state.problems)
    missing: set = set()
    for e in state.images:
        n = e["number"]
        if (e.get("split") == "trailing" and not e.get("whole")
                and not (Path(rdir) / f"image{n}.tiff").exists()):
            missing.add(n)          # split here: whole in the other placement
        elif e.get("whole") and e.get("free_end_near") and e.get("main"):
            missing.add(n)          # delivered, but with the strip end free
    other = None if state.placement is None else (
        "B" if state.placement == "A" else "A")
    record["advice"] = {"other_placement": other, "images": sorted(missing)}


def _film110_roll_guard(args: argparse.Namespace, roll: int, rdir: Path,
                        state: Film110State) -> "str | None":
    """Before any hardware: may this 110 run write into `rdir`? None = yes
    (and `state.forbidden` is filled with the placements the roll already
    holds); otherwise the refusal message. A roll takes two runs, one per
    placement (docs/film-110.md §11): the second is accepted without
    --force when the directory holds only the OTHER placement's files (or
    provisional x- files) and the roll's last manifest record is a 110 run.
    The same placement twice is an operator error and is refused."""
    from . import digitize
    if digitize.roll_is_done(args.out, roll, args.prefix):
        return (f"roll {roll} is recorded done (nothing is left for the other "
                f"placement); use --roll for a new strip, or --force to redo it")
    if not digitize.roll_dir_has_output(args.out, args.prefix, roll):
        return None
    recs = digitize.roll_records(args.out, roll, args.prefix)
    if not recs or recs[-1].get("film") != "110":
        return (f"roll {roll} already has output in {rdir} that is not a "
                f"recorded 110 run; use --roll for a different one, or "
                f"--force to redo it")
    tags = digitize.film110_placements_on_disk(rdir)
    have = sorted(t.upper() for t in tags if t in ("a", "b"))
    given = getattr(args, "placement", None)
    if given and given.lower() in tags:
        return (f"roll {roll} already has placement {given} output in {rdir}; "
                f"this run is placement {given} again. Re-seat the strip for "
                f"the other placement, or use --force to redo the roll")
    if len(have) == 2:
        return (f"roll {roll} already has output for both placements in "
                f"{rdir}; use --roll for a new strip, or --force to redo it")
    state.forbidden = set(have)
    return None


def _cmd_digitize(args: argparse.Namespace) -> int:
    """Bulk-digitise one film strip into a resumable staging tree
    (of135i.digitize): pick the roll number, load the magazine, scan
    frames 1-4 to <out>/<prefix>roll-NNN/, eject, and record the roll in
    an append-only manifest. Run once per strip; resume is BETWEEN strips
    -- the roll number advances past the highest recorded or on-disk roll,
    and an incomplete strip is recorded failed and re-run whole (there is
    no mid-strip resume). Interactive (the load flow prompts), so it needs
    a real terminal. The main image is the raw negative (the archival
    starting point); colour interpretation is the application's job."""
    from datetime import datetime, timezone
    from . import digitize, loadflow

    # --frames decides the strip length. The default stays 1-4: most
    # strips in hand are four frames, and a longer default would scan
    # empty apertures for everyone with a shorter strip. Validated here,
    # before the load prompt and before any hardware.
    try:
        dig_frames = _parse_frames(getattr(args, "frames", None) or "1-4")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    err = _validate_film110_args(args)
    if err is not None:
        print(f"error: {err}", file=sys.stderr)
        return 2
    film110 = getattr(args, "film", "135") == "110"
    state = Film110State(getattr(args, "placement", None)) if film110 else None

    roll = args.roll if args.roll is not None else digitize.next_roll(args.out, args.prefix)
    rdir = digitize.roll_dir(args.out, args.prefix, roll)

    # Overwrite guard, BEFORE any hardware: refuse a roll that already has a
    # scan on disk (checked directly, not just via the manifest, so a crash
    # between scanning and recording is caught) or is recorded done, unless
    # --force.
    if film110:
        # A 110 roll takes two runs (one per placement); the guard knows it.
        if not args.force:
            msg = _film110_roll_guard(args, roll, rdir, state)
            if msg is not None:
                print(f"error: {msg}.", file=sys.stderr)
                return 2
    elif not args.force and (digitize.roll_dir_has_output(args.out, args.prefix, roll)
                             or digitize.roll_is_done(args.out, roll, args.prefix)):
        print(f"error: roll {roll} already has output in {rdir} (or is recorded "
              f"done); use --roll for a different one, or --force to redo it.",
              file=sys.stderr)
        return 2
    print(f"=== digitize roll {roll} -> {rdir} ===")

    # --force redoes a roll: clear the previous run's frame outputs first, so
    # the dir can't end up a mix of two runs (e.g. an old f1-preview.tiff left
    # beside a new negative scanned without --positive). Done before hardware.
    if args.force:
        removed = digitize.clear_roll_outputs(args.out, args.prefix, roll)
        if film110:
            removed += digitize.clear_film110_outputs(args.out, args.prefix, roll)
        if removed:
            print(f"--force: removed {len(removed)} existing file(s) in {rdir} "
                  f"before re-scanning")

    args.eject = True

    started = datetime.now(timezone.utc).isoformat()
    record: dict = {"roll": roll, "prefix": args.prefix, "dir": str(rdir),
                    "started_utc": started, "dpi": args.dpi,
                    "positive": args.positive, "ir": args.ir,
                    "frames": dig_frames,
                    "no_clean": args.no_clean, "rotate": args.rotate}
    if film110:
        _film110_fill_record(record, state, rdir)

    def record_failed(stage: str, **extra) -> None:
        """Record this roll as failed, tolerating a manifest write that
        itself fails -- never let that mask the original error."""
        try:
            if film110:
                _film110_fill_record(record, state, rdir)
            record.update(status="failed", stage=stage,
                          finished_utc=datetime.now(timezone.utc).isoformat(),
                          **extra)
            digitize.append_manifest(args.out, record)
        except Exception as me:
            log.warning("manifest write failed while recording a failed roll "
                        "(original error preserved): %s", me)

    # 1) Load (its own session; interactive). A failure here leaves the
    #    scanner in an unknown state -- power cycle -- so we record and stop.
    if not args.assume_loaded:
        rc = loadflow.run(ask=input)
        if rc != 0:
            record_failed("load", load_rc=rc)
            print(f"load failed (rc {rc}); roll {roll} recorded as failed. "
                  f"Power-cycle before retrying.", file=sys.stderr)
            return rc

    # 2) Scan the requested frames to the roll dir (a fresh writing
    #    session). Same
    #    plain/dual dispatch as `scan`: non-3600 dpi is always a dual-light
    #    pass, and --ir on 3600 selects the dual flow; --no-ir on 3600 uses
    #    the plain flow.
    rdir.mkdir(parents=True, exist_ok=True)
    dual = args.ir or args.dpi != 3600
    per_frame: list[dict] = []

    def body(scanner: Scanner) -> int:
        scanner.park_mode = args.park
        scanner.recalibrate = bool(getattr(args, "recalibrate", False))
        if getattr(args, "warmup_budget", None) is not None:
            scanner.warmup_budget_s = float(args.warmup_budget)
        scanner.check_start_state()
        if not scanner.is_magazine_present():
            print("error: no magazine detected after load", file=sys.stderr)
            return 1
        incomplete: list[int] = []
        seen_film = False
        pending: list = []          # (frame, entry) written under the x- stem
        emit110 = _film110_digitize_emit(args, state, roll) if film110 else None

        def stem_tag() -> str:
            return state.placement.lower() if state.placement else "x"

        for idx, frame in enumerate(dig_frames):
            scanner.initialize(ir=dual, dpi=args.dpi)
            if film110:
                out = str(digitize.frame_path_110(args.out, args.prefix, roll,
                                                  stem_tag(), frame))
            else:
                out = str(digitize.frame_path(args.out, args.prefix, roll, frame))
            log.info("scanning frame %d @ %d dpi%s", frame, args.dpi,
                     " (dual-light pass)" if dual else "")
            # The frame is recorded BEFORE its writes and filled in as each
            # file lands, so a failure part-way through still says which
            # frame fell, what had been saved, and which step was running
            # (`stage`); an entry that completed has stage None.
            entry: dict = {"frame": frame, "main": None, "ir": None,
                           "preview": None, "dust_cleaned": None,
                           "stage": "scan"}
            per_frame.append(entry)
            if dual:
                raw, width, _meta = scanner.scan(frame=frame, ir=True, dpi=args.dpi)
            else:
                raw, width = scanner.scan(frame=frame)
            entry["stage"] = "write"
            if film110:
                def hook(vis, irr, frame=frame):
                    _film110_process_aperture(
                        args, state, frame, vis, irr if args.ir else None,
                        emit110)
                main, irf, prev, cleaned, cov = _finish_digitize_frame(
                    args, raw, width, out, dual, progress=entry, film110=hook)
            else:
                main, irf, prev, cleaned, cov = _finish_digitize_frame(
                    args, raw, width, out, dual, progress=entry)
            entry.update(main=main, ir=irf, preview=prev, dust_cleaned=cleaned)
            if film110:
                # Provisional stem: frames written before the placement was
                # known are renamed, in this one place, as soon as it is
                # (this frame's own detection may have just decided it).
                if out.rsplit("/", 1)[-1].startswith("x-"):
                    pending.append((frame, entry))
                if state.placement and not state.aborted and pending:
                    new_tag = state.placement.lower()
                    for pframe, pentry in pending:
                        moved = dict(digitize.rename_film110_frame(
                            digitize.roll_dir(args.out, args.prefix, roll),
                            pframe, "x", new_tag))
                        for k, v in list(pentry.items()):
                            if isinstance(v, str) and v in moved:
                                pentry[k] = moved[v]
                    pending.clear()
                    out = str(digitize.frame_path_110(args.out, args.prefix,
                                                      roll, new_tag, frame))
            film = None
            if cov is not None:
                entry["coverage_verified"] = bool(cov.verified)
                entry["coverage_reason"] = cov.reason
                entry["coverage_source"] = cov.source
                if not cov.verified:
                    incomplete.append(frame)
                elif "film_present" in entry:
                    film = {"present": entry["film_present"],
                            "saturated_share": entry["saturated_share"]}
                    if not film["present"]:
                        # Not a coverage failure: the opening was captured
                        # whole, there is just nothing in it.
                        entry["coverage_reason"] = "empty aperture"
            del raw
            entry["stage"] = "diag"
            if film is not None:
                _write_diag_sidecar(args, scanner, out, frame, coverage=cov, film=film)
            else:
                _write_diag_sidecar(args, scanner, out, frame, coverage=cov)
            d = scanner.last_diag or {}
            entry.update(stage=None,
                         calibration=d.get("calibration"),
                         gain_codes=d.get("gain_codes"),
                         offset_codes=d.get("offset_codes"),
                         dark_b_substituted=d.get("dark_b_substituted"))
            if film110 and state.aborted:
                print(f"stopping: {state.aborted}", file=sys.stderr)
                break
            if film is not None:
                if film["present"]:
                    seen_film = True
                elif seen_film and not getattr(args, "no_stop_at_empty", False):
                    # Empty aperture after film in this strip: the strip has
                    # ended; skip the rest (no hardware work), still eject.
                    skipped = list(dig_frames[idx + 1:])
                    if skipped:
                        record["skipped_frames"] = skipped
                        record["stop_reason"] = f"empty aperture at frame {frame}"
                        print(f"stopping: empty aperture at frame {frame} after film; "
                              f"skipping frame(s) {', '.join(map(str, skipped))} "
                              f"(--no-stop-at-empty scans them anyway)")
                    break
        scanner.eject()
        print("ejected")
        if film110 and state.aborted:
            return 5
        if incomplete:
            # Fail closed on image integrity, exactly as `scan` does: a
            # coverage failure is not a complete frame. The roll is
            # recorded failed (per_frame says which frames and why) and
            # re-run whole, digitize's existing resume unit.
            print(f"error: aperture coverage NOT verified for frame(s) "
                  f"{', '.join(map(str, incomplete))} — see the .diag.json "
                  f"and .overscan file(s); no main negative was written for "
                  f"them", file=sys.stderr)
            return 4
        return 0

    # Even if the scan/write flow raises (e.g. an OSError writing an image),
    # record the roll as failed -- with what was saved -- before the error
    # propagates, so its number is not silently reused. No further scan,
    # eject or recovery runs after a failure.
    try:
        rc = _run_writing_session(body)
    except BaseException as e:
        record_failed("scan", per_frame=per_frame, error=repr(e))
        raise
    record.update(finished_utc=datetime.now(timezone.utc).isoformat(),
                  per_frame=per_frame,
                  status="ok" if rc == 0 else "failed",
                  stage=None if rc == 0 else "scan")
    if film110:
        _film110_fill_record(record, state, rdir)
    # The images are already on disk; a manifest write that fails here must
    # not turn a successful scan into a crash. Warn loudly instead -- the
    # disk-based overwrite guard still protects the roll on the next run.
    try:
        digitize.append_manifest(args.out, record)
    except Exception as me:
        print(f"warning: roll {roll} scanned OK but the manifest write failed "
              f"({me!r}); the images are saved in {rdir}. The manifest is out "
              f"of date -- record it by hand or re-run with --force.",
              file=sys.stderr)

    if rc == 0:
        subs = [pf["frame"] for pf in per_frame if pf.get("dark_b_substituted")]
        note = f" (dark_b substituted on frame(s) {subs})" if subs else ""
        if film110:
            adv = record["advice"]
            pl = state.placement or "undecided"
            if adv["images"]:
                nxt = (f"Images {adv['images']} need the other placement "
                       f"(split by a bar, or scanned with the strip end free): "
                       f"re-seat the strip for placement {adv['other_placement']} "
                       f"and run 'of135i digitize --film 110 --roll {roll}' again.")
            else:
                nxt = (f"Nothing is left for the other placement. Next strip: "
                       f"roll {roll + 1}.")
            print(f"roll {roll} done (110, placement {pl}): {len(per_frame)} "
                  f"frames, {sum(1 for e in state.images if e['main'])} "
                  f"photograph(s) -> {rdir}{note}. {nxt}")
        else:
            print(f"roll {roll} done: {len(per_frame)} frames -> {rdir}{note}. "
                  f"Next: insert the next strip and run 'of135i digitize' again "
                  f"(roll {roll + 1}).")
    elif rc == 5 and film110:
        print(f"roll {roll} STOPPED: {state.aborted}. The strip was ejected; "
              f"nothing was overwritten (this run's frame files keep the "
              f"x- stem). Re-seat for the other placement, or --force.",
              file=sys.stderr)
    elif rc == 4:
        # Coverage failure: the pass and eject completed normally -- the
        # transport needs no recovery, the roll just is not complete.
        print(f"roll {roll} recorded FAILED: aperture coverage did not "
              f"verify on every frame (see per_frame in the manifest). "
              f"Re-run the roll with --force.", file=sys.stderr)
    else:
        print(f"roll {roll} FAILED at scan (rc {rc}); recorded. "
              f"Power-cycle before the next strip.", file=sys.stderr)
    return rc


def _cmd_version(args: argparse.Namespace) -> int:
    from . import __version__, diag
    host = diag._collect_host()
    rev = host.get("driver_revision") or "unknown"
    print(f"of135i {__version__} (driver revision {rev}, python {host.get('python')}, pyusb {host.get('pyusb')})")
    return 0


def _cmd_eject(args: argparse.Namespace) -> int:
    def body(scanner: Scanner) -> int:
        scanner.eject()
        print("ejected")
        return 0

    return _run_writing_session(body)


def _cmd_watch(args: argparse.Namespace) -> int:
    """Poll the buttons (reads only) and eject on the eject button.
    Presses are seen via the EP 0x83 interrupt and via reg 0x31 (Test
    97). The start state is checked read-only up front so an unsafe
    scanner is refused immediately instead of at the first button press;
    a failed eject ends the watch (the session is failed; a further
    press must not send anything). Only "eject" ever acts, and only
    with a magazine present; other buttons are printed."""
    def body(scanner: Scanner) -> int:
        scanner.check_start_state()
        print("watching for button events (Ctrl+C to stop)", flush=True)
        previous: set[str] = set()
        while True:
            try:
                pressed = scanner.poll_buttons(strict_interrupts=True)
                time.sleep(0.05)
            except KeyboardInterrupt:
                # Idle wait interrupted: nothing was in progress.
                print("\nstopped")
                return 0
            except InterruptOverflowError as e:
                print(f"cannot watch: {e}", file=sys.stderr)
                return 1
            for name in pressed:
                if name in previous:
                    continue  # same held press, already handled
                if name.startswith("unknown-"):
                    print(f"other event 0x{name[len('unknown-0x'):]}", flush=True)
                elif name == "eject":
                    print("eject button pressed", flush=True)
                    if scanner.is_magazine_present():
                        scanner.eject()
                        print("ejected", flush=True)
                    else:
                        print("magazine not detected, ignoring", flush=True)
                else:
                    print(f"{name} button pressed (no action)", flush=True)
            previous = set(pressed)

    return _run_writing_session(body)


def _cmd_buttons(args: argparse.Namespace) -> int:
    """Read-only: print each front-panel button press with a timestamp
    until Ctrl-C or --duration seconds. Works on a cold scanner too."""
    try:
        with Scanner(UsbIo.open(readonly=True)) as scanner:
            print("reading buttons (Ctrl+C to stop)", flush=True)
            end = None if args.duration is None else time.monotonic() + args.duration
            previous: set[str] = set()
            try:
                while end is None or time.monotonic() < end:
                    pressed = scanner.poll_buttons()
                    for name in pressed:
                        if name not in previous:
                            print(f"{time.strftime('%H:%M:%S')} {name}", flush=True)
                    previous = set(pressed)
                    time.sleep(0.05)
            except KeyboardInterrupt:
                print("\nstopped")
    except Of135iError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    return 0


def _not_wired_yet(args: argparse.Namespace) -> int:
    print(f"'{args.command}' is not wired yet (needs device.py).", file=sys.stderr)
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="of135i", description="Userspace driver for the Plustek OpticFilm 135i."
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="enable debug logging"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="scan a frame to a raw 16-bit RGB image")
    p_scan.add_argument("--positive", action="store_true",
        help="convert the raw negative to a display-ready positive")
    p_scan.add_argument("--rotate", type=int, default=0,
        choices=(0, 90, 180, 270),
        help="rotate output counter-clockwise (degrees)")
    p_scan.add_argument("--frame", type=int,
                        help=f"frame number, 1-{holder.DEFAULT.frames} "
                             f"({holder.DEFAULT.name})")
    p_scan.add_argument("--frames",
        help="batch scan: comma/range spec of frames, e.g. '1-4' or '1,3'; "
             "output files get a -f<N> suffix per frame")
    p_scan.add_argument("--eject", action="store_true",
        help="eject the film magazine after the last frame")
    p_scan.add_argument("--dpi", type=int, default=3600, choices=SUPPORTED_DPIS,
        help="scan resolution (default 3600; resolutions other than 3600 "
             "always run a dual-light pass)")
    p_scan.add_argument("--ir", action="store_true",
        help="capture an IR (dust/scratch) pass: write the IR channel and "
             "clean the visible image with it")
    p_scan.add_argument("--no-clean", action="store_true",
        help="skip IR-based dust/scratch removal on the visible image (--ir only)")
    p_scan.add_argument("--overscan", type=float, default=None, metavar="MM",
        help="overscan margin per side around the whole aperture (default "
             "0.75, the production geometry accepted in Test 58; "
             "docs/holder-position-design.md). Applies to plain and "
             "dual-light scans alike: the window uses the corrected mean "
             "mapping, the aperture-registered image goes to -o (with --ir "
             "also <stem>-ir.tiff, cropped to the same lines) and the full "
             "overscan frame(s) to <o>.overscan.<ext> (and "
             "<stem>-ir.overscan.tiff); on a coverage failure only the "
             "overscan raw is kept and the exit status is non-zero")
    p_scan.add_argument("--no-diag", action="store_true",
        help="skip writing the <output>.diag.json calibration/timing sidecar")
    p_scan.add_argument("--no-cal-data", action="store_true",
        help="do not write the <output>.cal.npz per-pixel calibration data "
             "(white line, dark/shading measurements, uploaded tables; about 13 "
             "MB per frame; written only when the scan measured a fresh "
             "calibration, and only with the .diag.json sidecar)")
    p_scan.add_argument("--warmup-budget", type=float, default=None, metavar="SECONDS",
                        help="total time to wait for the lamp after a cold start before "
                             "failing the scan (default: the driver's bounded default, 60 s)")
    p_scan.add_argument("--park", choices=("verbatim", "semantic"), default="semantic",
        help="PARK phase implementation: semantic issues the same motor "
             "sequence as verbatim but replaces the captured trace's fixed "
             "pacing with real read-modify-write and condition waits "
             "(default since 2026-09-17: 3.7-3.8 s vs verbatim's captured "
             "13.6-16.9 s; hardware-confirmed via the SANE backend's PARK "
             "program, docs/park-completion-analysis.md); verbatim replays "
             "the captured op stream byte for byte, for A/B comparison")
    p_scan.add_argument("--recalibrate", action="store_true",
        help="force a full dark/white/gain/shading calibration on every "
             "frame (today's behaviour). Default since 2026-09-17: reuse "
             "this session's first successful calibration on later frames "
             "of the same (dpi, dual) kind, re-applying only the AFE gain "
             "codes -- the vendor does the same on a skip-calibration "
             "frame (docs/protocol-notes.md pass 14) and the shading table "
             "survives in scanner RAM across PARK. Plain 3600 dpi only; "
             "dual/IR always recalibrates (docs/driver-design.md)")
    p_scan.add_argument("--film", choices=("135", "110"), default="135",
        help="film format inside the strip holder (default 135, today's "
             "behaviour, byte-identical). 110 (Pocket Instamatic) runs the "
             "110 frame detector on each aperture-registered crop and "
             "writes <out>-image<N>.tiff per whole image found (plus "
             "-ir.tiff with --ir); the aperture product and overscan are "
             "still written exactly as today. docs/film-110.md")
    p_scan.add_argument("--placement", choices=("A", "B"), default=None,
        help="which of the two 110 placements the strip is in "
             "(docs/film-110.md §3). Optional with --film 110: when "
             "omitted it is inferred from the first image's position "
             "(an image midway between the two phases is refused with a "
             "request to give it); given, it overrides the inference. "
             "Images are numbered by their position on the strip, so the "
             "same photograph gets the same number in both placements")
    p_scan.add_argument("-o", "--output", required=True, help="output file path (.tiff or .pnm)")
    p_scan.set_defaults(func=_cmd_scan)

    p_preview = sub.add_parser("preview", help="run a quick preview sweep")
    p_preview.add_argument("-o", "--output", help="output file path")
    p_preview.set_defaults(func=_not_wired_yet)

    p_load = sub.add_parser("load", help="load the film magazine (the vendor's insert flow; interactive)")
    p_load.add_argument("--release", action="store_true",
        help="only release a latched magazine (cold init + the app-start jog), then "
             "stop: the first cycle of the two-cycle exit after a power cycle with "
             "the magazine latched (docs/test-log.md Test 49)")
    p_load.set_defaults(func=_cmd_load)
    p_load.add_argument("--double-jog", action="store_true",
        help="EXPERIMENT (docs/test-log.md Test 49 exit, unverified): after the first "
             "jog and reinsert, run the app-start jog a second time from the loose "
             "position and reinsert again before loading -- the A/B for loading in one "
             "power cycle from a latched magazine")
    p_load.add_argument("--next-strip", action="store_true",
        help="load the next strip after an eject in the same power-on: no jog, no "
             "reinsert prompt; the magazine must already be pushed in to the stop with "
             "the new strip. Refused on a cold (power-cycled) scanner. Hardware-"
             "verified once (docs/test-log.md Test 89)")

    p_version = sub.add_parser("version", help="print the driver version and git revision")
    p_version.set_defaults(func=_cmd_version)

    p_eject = sub.add_parser("eject", help="eject the film magazine")
    p_eject.set_defaults(func=_cmd_eject)

    p_status = sub.add_parser("status", help="read and print status registers")
    p_status.set_defaults(func=_cmd_status)

    p_watch = sub.add_parser("watch", help="poll buttons; eject on the eject button")
    p_watch.set_defaults(func=_cmd_watch)

    p_buttons = sub.add_parser(
        "buttons", help="read-only: print front-panel button presses")
    p_buttons.add_argument("--duration", type=float, metavar="S",
        help="stop after S seconds (default: until Ctrl-C)")
    p_buttons.set_defaults(func=_cmd_buttons)

    p_doctor = sub.add_parser(
        "doctor", help="read-only hardware health report (no motor/register writes)")
    p_doctor.add_argument("--json", metavar="PATH", help="also write the report as JSON to PATH")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_dig = sub.add_parser(
        "digitize",
        help="bulk-digitise one strip into a resumable staging tree "
             "(load, scan frames 1-4, eject, record in a manifest); interactive")
    p_dig.add_argument("-o", "--out", required=True, metavar="DIR",
        help="staging directory; rolls go in <out>/<prefix>roll-NNN/, logged in "
             "<out>/manifest.jsonl")
    p_dig.add_argument("--prefix", default="",
        help="roll-directory name prefix, e.g. 'boxA-' -> boxA-roll-001/ (default none)")
    p_dig.add_argument("--roll", type=int, default=None,
        help="roll number (default: one past the highest for this prefix, "
             "counting BOTH the manifest and existing roll dirs on disk)")
    p_dig.add_argument("--force", action="store_true",
        help="scan even if this roll already has files on disk or is "
             "recorded done; clears the roll's previous f*.tiff/.diag.json "
             "first so the dir is not a mix of two runs (--film 110: "
             "both placements' files and the image<N> photographs)")
    p_dig.add_argument("--frames", default="1-4", metavar="SPEC",
        help=f"which frames of the strip to scan, e.g. 1-4 (default), "
             f"1-6 for a full-length strip, or 1,3-4. The holder holds "
             f"{holder.DEFAULT.frames}; the default stays 1-4 so a "
             f"shorter strip does not scan empty apertures.")
    p_dig.add_argument("--assume-loaded", action="store_true",
        help="skip the load flow (the magazine is already latched)")
    p_dig.add_argument("--dpi", type=int, default=3600, choices=SUPPORTED_DPIS,
        help="scan resolution (default 3600)")
    p_dig.add_argument("--positive", action="store_true",
        help="also write a positive preview as a SEPARATE file "
             "(fN-preview.tiff); the main fN.tiff always stays the raw "
             "negative (default off)")
    p_dig.add_argument("--rotate", type=int, default=0, choices=(0, 90, 180, 270),
        help="rotate output counter-clockwise (degrees; default 0)")
    p_dig.add_argument("--film", choices=("135", "110"), default="135",
        help="film format in the strip holder (default 135). 110 (Pocket "
             "Instamatic): a strip takes TWO runs, one per placement "
             "(docs/film-110.md §3), both into the same roll directory "
             "(--roll N for the second). Frame files get the placement as a "
             "prefix (a-f1.tiff, b-f1.tiff) and every whole photograph is "
             "written once as image<N>.tiff (N = position on the strip), "
             "never overwritten except a free-end copy by the supported one")
    p_dig.add_argument("--placement", choices=("A", "B"), default=None,
        help="which 110 placement this run is (docs/film-110.md §3). "
             "Optional with --film 110: inferred from the first image's "
             "position; given, it overrides the inference")
    p_dig.add_argument("--no-ir", dest="ir", action="store_false",
        help="skip the IR pass and dust removal (non-3600 profiles still "
             "run a dual-light capture; only the IR output is dropped)")
    p_dig.add_argument("--no-stop-at-empty", action="store_true",
                       help="scan every requested frame even after an empty "
                            "aperture that follows film (default: the strip "
                            "has ended, the remaining frames are skipped)")
    p_dig.add_argument("--no-clean", action="store_true",
        help="skip IR-based dust/scratch removal on the visible image")
    p_dig.add_argument("--no-diag", action="store_true",
        help="skip writing per-frame .diag.json sidecars")
    p_dig.add_argument("--no-cal-data", action="store_true",
        help="do not write the per-frame .cal.npz per-pixel calibration data "
             "(white line, dark/shading measurements, uploaded tables; about 13 "
             "MB per frame; written only for frames that measured a fresh "
             "calibration, and only with the .diag.json sidecars)")
    p_dig.add_argument("--park", choices=("verbatim", "semantic"), default="semantic",
        help="PARK implementation: semantic (default since 2026-09-17, "
             "3.7-3.8 s, hardware-confirmed via the SANE backend) or "
             "verbatim (replays the captured stream byte for byte)")
    p_dig.add_argument("--recalibrate", action="store_true",
        help="force a full calibration on every frame (today's behaviour) "
             "instead of reusing this roll's first successful plain-3600 "
             "calibration on later frames (default since 2026-09-17; the "
             "vendor does the same, docs/protocol-notes.md pass 14). "
             "Dual/IR always recalibrates")
    p_dig.add_argument("--warmup-budget", type=float, default=None, metavar="SECONDS",
        help="lamp warmup budget after a cold start (default 60 s)")
    p_dig.set_defaults(func=_cmd_digitize, ir=True)

    return parser


class _ResilientStream:
    """A console stream that can never kill a scan.

    A print to a stdout/stderr pipe whose reader has gone away raises
    BrokenPipeError; unhandled, that aborted a scanning session
    mid-strip (production telemetry, 2026-09/10, 711 frames). This
    wrapper catches BrokenPipeError and OSError(EPIPE) in write/flush,
    marks the stream broken, points the underlying file descriptor at
    os.devnull (the standard recipe, so the interpreter's flush at exit
    cannot fail either) and drops all further output silently. Everything
    else (fileno, isatty, other attributes) delegates to the wrapped
    stream. KeyboardInterrupt and every other error pass through.
    """

    def __init__(self, stream, *, redirect_fd: bool = True):
        self._stream = stream
        self._redirect_fd_on_break = redirect_fd
        self.broken = False

    @staticmethod
    def _is_pipe_error(exc: BaseException) -> bool:
        return isinstance(exc, BrokenPipeError) or (
            isinstance(exc, OSError) and exc.errno == errno.EPIPE)

    def _redirect_fd(self) -> None:
        """Point the wrapped stream's fd at os.devnull. Overridable (the
        unit test does not touch real descriptors)."""
        try:
            fd = self._stream.fileno()
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, fd)
            finally:
                os.close(devnull)
        except Exception:
            pass

    def _mark_broken(self) -> None:
        self.broken = True
        if self._redirect_fd_on_break:
            self._redirect_fd()

    def write(self, data):
        if self.broken:
            return len(data)
        try:
            return self._stream.write(data)
        except OSError as e:
            if not self._is_pipe_error(e):
                raise
            self._mark_broken()
            return len(data)

    def flush(self):
        if self.broken:
            return None
        try:
            return self._stream.flush()
        except OSError as e:
            if not self._is_pipe_error(e):
                raise
            self._mark_broken()
            return None

    def __getattr__(self, name):
        # Only reached for attributes not defined above.
        return getattr(self._stream, name)


def _install_resilient_console() -> None:
    """Wrap sys.stdout/sys.stderr (once) so console output cannot abort
    a command. Must run before logging is configured, so the logging
    StreamHandler binds to the wrapped stderr."""
    if not isinstance(sys.stdout, _ResilientStream):
        sys.stdout = _ResilientStream(sys.stdout)
    if not isinstance(sys.stderr, _ResilientStream):
        sys.stderr = _ResilientStream(sys.stderr)


def main(argv: list[str] | None = None) -> int:
    _install_resilient_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
