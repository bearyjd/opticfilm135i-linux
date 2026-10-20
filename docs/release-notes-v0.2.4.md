# v0.2.4 — shaped by production use: IR-first coverage, empty apertures, calibration export, a dust guard

Release notes for the `v0.2.4` tag (2026-10-20) of the
Plustek OpticFilm 135i Linux driver. The software is **unofficial**, not
affiliated with, endorsed by or supported by Plustek, and was developed and
tested by one person on a single OpticFilm 135i unit. Previous release
notes:
[v0.2.3](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.4/docs/release-notes-v0.2.3.md).

## What this release is

The first release shaped by production use. The owner's digitising
project had run the driver on 193 strips (711 frames) since September;
their manifests and diagnostic sidecars were read back
([docs/production-telemetry-2026-10.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.4/docs/production-telemetry-2026-10.md),
de-identified), four things were changed, and a ten-strip series was then
scanned on the release candidate
([Test 101](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.4/docs/test-log.md)),
which found two more. Everything here is the Python driver; the SANE
backend is unchanged.

**Coverage is measured on the infrared channel first** on the dual
(IR) paths. Nine production frames had failed coverage with "no aperture
found", eight of them frame 1, where dark film content hid the aperture
edge in the visible red channel. The IR channel is blind to the picture,
so the edge is always there; the visible frame is used only if IR does
not verify. The manifest records `coverage_source`. A verified frame
whose margin exceeds 2.0 mm (the production maximum is 1.10 mm) gets a
`coverage_warning` — the opening found is shorter than the holder's, as
on a film end that is opaque to the measuring channel — and is still
written.

**An empty aperture is detected.** Coverage checks the opening, not what
is in it, so an empty holder slot had passed and been written as a
picture of open light. `digitize` now measures how much of the verified
opening is saturated in all three channels: at 95 % or more
(`aperture_crop.EMPTY_APERTURE_SHARE`) the frame is empty, gets only its
overscan files and `film_present: false`, and the strip stops after an
empty frame that follows film (`--no-stop-at-empty` scans all). A strip
end inside the opening is film and is written: the production series had
a leader frame 54 % open light over unexposed base, which an earlier 50 %
threshold had dropped.

**The calibration is exported.** Every freshly calibrated frame gets
`<stem>.cal.npz` (about 13 MB) next to its diag sidecar: the white line,
both dark measurements, the dark and white shading measurements and the
uploaded tables (`--no-cal-data` skips it). It is what made the next
finding possible.

**A guard against dust in the calibration light path.** The ten-strip
series carried a bright line of +2–4 % through 36 of its 40 frames,
visible and IR, at a column that wandered between strips. The exports
show why: a dust speck about 0.1 mm wide sits where the white reference
is measured but not where the film is imaged; it dims 16–30 columns of
the white measurement by 2–12 %, the per-pixel gain computed from it
over-corrects those columns, and the frame carries the inverse. The
vendor flow measures the same way and has the same flaw.
`calibrate.guard_white_dips` replaces such a dip by the running median
of its neighbourhood before the gain table is computed — but only a dip
that shows in all three colour channels (a shadow is achromatic); the
sensor's own dim pixels are single-channel, are corrected correctly by
the gain table, and are never touched. Validated on the 40 exports and
on one strip re-scanned with the guard: residual under 1 % predicted, no
dark line in the image. The speck itself was then found to sit on the
holder or magazine (two empty-holder runs before and after cleaning them:
5–7 % dip, then none), so a user who sees such a line should clean those
first — and the guard covers the next speck. The sidecar lists the guarded dips in
`white_dips`; `tools/cal_dips.py` lists them for exported data together
with the per-column factor that corrects a frame scanned before the
guard existed.

**Console output can no longer abort a scan.** Three production strips
had died of `BrokenPipeError` when the app's stdout closed mid-scan.

## What did not change

- The SANE backend and everything it sends to the scanner; the merge
  request (!1032, revision v8) is unchanged and still under review. The
  backend computes its shading tables in C++ and does not have the dust
  guard; that is a follow-up.
- The Python driver's motor sequences, waits and profiles: every change
  here is host-side computation on measurements the scanner already made,
  except the content of the uploaded gain table at guarded columns.
- The supported hardware and its limits: one unit, the strip holder, as
  described in the v0.2.0 notes.

## Verification

- Hardware: ten production strips (40 frames, one roll) scanned on the
  release candidate without the guard and the threshold change, all
  verified, loads 10/10; one strip re-scanned with the guard active
  (Test 101 and 101b in docs/test-log.md).
- Read-only checks on real production data: IR and visible edges agree
  within 0.13 mm on 16 random verified frames; the guard on 44
  calibration exports touches every speck dip and no sensor pixel.
- The offline suite: 414 passed, 0 skipped (`tools/release_check.py`,
  FULL VERIFICATION; v0.2.3 had 393).
- Test 100 (the shorter poll floor, on its own branch) is planned and
  not run; it is not part of this release.

## Changes since v0.2.3

The full list is the `v0.2.4` section of
[CHANGELOG.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.4/CHANGELOG.md).
