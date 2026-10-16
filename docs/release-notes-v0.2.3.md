# v0.2.3 — submission revision v8: the process lock is gone

Release notes for the `v0.2.3` tag (2026-10-16) of the
Plustek OpticFilm 135i Linux driver. The software is **unofficial**, not
affiliated with, endorsed by or supported by Plustek, and was developed and
tested by one person on a single OpticFilm 135i unit. Previous release
notes:
[v0.2.2](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.3/docs/release-notes-v0.2.2.md).

## What this release is

A maintenance release of the SANE backend, made in answer to the first
review question on merge request !1032. Nothing sent to the scanner
changes.

**The backend no longer takes a process lock.** Since the first SANE
revision the backend took a `flock` around `sane_open`/`sane_close` on the
same file as the Python driver's `ProcessLock`, so that the two could not
hold the device at once. The SANE maintainer asked (2026-10-14) why a
backend should do that; no other genesys ASIC needs it, and the situation
it guards against (one host running both this driver and the backend) is
the author's alone. The lock is removed from the backend entirely.
`sane/gl126_lock.{h,cpp}` is now `sane/gl126_mark.{h,cpp}` and holds only
the magazine state file, with the same file hardening (`O_NOFOLLOW`,
regular-file check, temp-file-and-`rename` write). 66 lines leave
`genesys.cpp`.

**Consequence for a host that runs both.** The Python driver keeps its own
`ProcessLock`, but the backend no longer honours it. Do not run `of135i`
commands, including the read-only `status` and `doctor`, while a SANE
frontend (digiKam, `scanimage`) has the scanner open.
[docs/hardware-safety.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.3/docs/hardware-safety.md)
and the install notes say so.

**Nothing bench-specific is left in the backend.** The state file moved
from `/tmp/of135i-07b3-1436.lock.magazine` to
`/tmp/sane-gl126-07b3-1436.magazine` (override `SANE_GL126_MAGAZINE_MARK`),
and the test-only poll cap `OF135I_SANE_POLL_CAP_MS` is now
`SANE_GL126_TEST_POLL_CAP_MS` (still read only when the USB interface is a
mock and the library runs in testing mode). A state file written by an
earlier build is simply not seen by this one; the hardware is re-read
before any motor move, so the worst case is being asked to reseat the
magazine once.

**`.desc` status `:basic`.** The maintainer called the submission "fairly
experimental", which is fair for a backend tested on one unit; the
`genesys.desc` entry now says `:basic` with the comment "Experimental:
developed and tested on a single unit."

**Submission revision v8 is on the merge request.** v8 is v7 rebased onto
upstream `59d255866` (nothing new upstream touches `backend/genesys`) plus
the changes above, as five commits. The owner pushed it to the merge
request on 2026-10-16; the fork pipeline passed all eight jobs
(2955733976), the merge request shows no conflicts, its "Known
limitations" list was rewritten (the lock and state-file items became one
item about the state file), and a reply was posted in the maintainer's
thread. The exact series is in
[sane/wp3-package/](https://github.com/cgillinger/opticfilm135i-linux/tree/v0.2.3/sane/wp3-package).
The merge request is **not merged**; nothing here should be read as
acceptance by the SANE project.

## What did not change

- The Python driver: no code change at all.
- Everything the backend sends to the scanner: scan profiles, calibration,
  positioning, PARK, the load and eject flows, the cold start. The lock
  never produced USB traffic, which is why this release was not run on
  hardware.
- The supported hardware and its limits: one unit, the strip holder, as
  described in the v0.2.0 notes.

## Verification

- Offline only. No scanner contact; the last hardware tests (97 to 99)
  were on v0.2.2's code, which this release does not change on the wire.
- The offline suite: 393 passed, 0 skipped (`tools/release_check.py`,
  FULL VERIFICATION; v0.2.2 had 401: the lock suite's fifteen tests became
  the mark suite's seven). `tests/test_sane_lock.py` became
  `tests/test_sane_mark.py` (7 tests). The backend suites pass against the
  v8 build and against the development build.
- The v8 series built standalone with 0 warnings and 105 gl126 symbols
  (v7: 111, the six lock functions); upstream's `genesys_unit_tests` pass;
  the local replay of the SANE CI jobs is green apart from a known local
  `escl_test` stop; both package reconstruction paths (bundle, `git am`)
  give the same tree `ab97f45f`.

## Changes since v0.2.2

The full list is the `v0.2.3` section of
[CHANGELOG.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.3/CHANGELOG.md).
