# v0.2.2 — the cold-start fix, the button API, and submission revision v7

Release notes for the `v0.2.2` tag (2026-10-13) of the
Plustek OpticFilm 135i Linux driver. The software is **unofficial**, not
affiliated with, endorsed by or supported by Plustek, and was developed and
tested by one person on a single OpticFilm 135i unit. Previous release
notes:
[v0.2.1](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.2/docs/release-notes-v0.2.1.md).

## What this release is

The first functional change since v0.2.0, and the first finding from
static analysis of the vendor's Windows driver to reach the code — through
a barrier that requires our own capture or measurement to confirm it first.

**The cold start writes one register it had dropped.** The vendor's cold
homing round performs a read-modify-write of reg 0x32 (set bit 0x02)
between its second and third motor move. Our hand-written round, in the
Python driver and in the generated SANE program alike, did not. The
consequence was small and visible: the settle check after each round read
0x1d instead of 0x1f, and every cold start logged three "settle poll did
not reach" warnings, then continued. With the write restored the settle
read is 0x1f on the first poll in both implementations, the warnings are
gone, and the cold start is about 0.75 s shorter (Test 98). The static
analysis pointed at the register; the two existing captures of the vendor
driver show the write in both, which is what admitted the change
([docs/static-analysis.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.2/docs/static-analysis.md),
SA-001).

**The front-panel buttons are readable.** Measured on the unit (Test 97):
a press clears bits of reg 0x31 for 0.3–0.9 s, and the cleared bits are
the button — Eject 0x48, Positive 0x20, Negative 0x40, Custom 0x28. Once
the scanner is initialised the same byte also arrives as an interrupt on
EP 0x83. Inserting the magazine produces no interrupt; it is seen by
polling reg 0x32. The driver exposes this as `of135i.buttons` and
`Scanner.poll_buttons()` (read-only, works on a cold scanner), the command
`of135i buttons` prints presses, and `of135i watch` ejects when the Eject
button is pressed with a magazine present — the only button that does
anything. All four buttons were named correctly from a cold scanner
through the shipped command (Test 99). The Power button is untested.

**A barrier for vendor-derived findings.** Findings from analysis of the
vendor's binaries are recorded in `of135i/vendor_derived.py` and in
`docs/static-analysis.md`; a test asserts that the SANE table generator
never imports that module. Nothing enters the SANE backend unless our own
captures or measurements confirm it.
[LICENSING.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.2/LICENSING.md)
describes the reasoning.

**Submission revision v7 is on the merge request.** Upstream SANE moved
again and reverted its scan-exposure-time feature, which the series had
touched in two places; merge request !1032 showed a conflict. v7 is v6
rebased onto upstream `a8676a6f7` (one conflict, two forced one-line
edits, nothing in the protocol) plus a fifth commit with the cold-start
fix. The owner pushed it to the merge request on 2026-10-13 and the
conflict is gone. The merge request's GitLab pipeline on v7 passed all eight jobs (2943836952) and the merge request is mergeable; still under review, not merged. The exact series is in
[sane/wp3-package/](https://github.com/cgillinger/opticfilm135i-linux/tree/v0.2.2/sane/wp3-package).
The merge request is **not merged**; nothing here should be read as
acceptance by the SANE project.

## What did not change

- Scan profiles, calibration, image handling, positioning, PARK, the load
  and eject flows. The only motor-sequence change is the one restored
  register write in the cold homing round, which moves nothing.
- The supported hardware and its limits: one unit, the strip holder, as
  described in the v0.2.0 notes.

## Verification

- Hardware: Test 97 (buttons, read-only register watch), Test 98 (the
  cold-start fix in the Python driver and in the installed SANE backend,
  load and eject normal), Test 99 (the button API, all four buttons).
- The offline suite: 401 passed, 0 skipped (`tools/release_check.py`,
  FULL VERIFICATION). The three backend suites pass against the v7 build
  and against the development build.
- The v7 series built standalone with 0 warnings and 111 gl126 symbols;
  upstream's `genesys_unit_tests` pass; the local replay of the SANE CI
  jobs is green apart from a known local `escl_test` stop. The v7 build
  itself has not been run on hardware; the cold-start table it carries was
  (Test 98, development build).

## Changes since v0.2.1

The full list is the `v0.2.2` section of
[CHANGELOG.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.2/CHANGELOG.md).
