# v0.2.5 — the calibration polls stop waiting for words that never come

Release notes for the `v0.2.5` tag (2026-10-20) of the
Plustek OpticFilm 135i Linux driver. The software is **unofficial**, not
affiliated with, endorsed by or supported by Plustek, and was developed and
tested by one person on a single OpticFilm 135i unit. Previous release
notes:
[v0.2.4](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.5/docs/release-notes-v0.2.4.md).

## What this release is

One change to the Python driver, hardware-verified the same evening on
ten production strips, released on its own so that v0.2.4's larger set
and this timing change can be told apart.

**The non-strict calibration polls accept the data-ready bit.** The
production telemetry showed that 93 % of frames waited out one to seven
polls at exactly the driver's 1.0 s floor, about 9 s per strip, never
followed by a failure: at those status-word sites the scanner answers
with the captured word's data-ready bit set but never with the exact
captured word. The driver now accepts a live word with that bit set at
the sites whose captured settled word carries it — the criterion the
SANE backend has used at the same sites since its first calibration run
— and the floor for these non-strict polls is 0.3 s instead of 1.0 s.
Strict motor-completion waits are unchanged. Every non-strict poll's
outcome and settle time is recorded in the diag sidecar (`poll_settle`,
`poll_settle_max_s`).

**Measured** ([Test 100](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.5/docs/test-log.md)):
on ten production strips (37 frames) every one of the polls settled on
the first read, 4 ms; the calibration phases that used to take 1.1 s
when the word did not come take 0.11–0.27 s; position, scan and park
times, gain, offset and coverage are unchanged against the production
medians; about 9 s saved per strip.

## What did not change

- Everything sent to the scanner: the same reads and writes in the same
  order; only how long the driver waits for a status word it will not
  get, and which word it accepts.
- The SANE backend (merge request !1032, revision v8, still under
  review): it already behaved this way.
- The supported hardware and its limits: one unit, the strip holder.

## Verification

- Hardware: Test 100 (one strip, the four acceptance criteria) and the
  nine strips that followed it on the same code, all verified.
- The offline suite: 424 passed, 0 skipped (`tools/release_check.py`,
  FULL VERIFICATION; v0.2.4 had 414 — `tests/test_poll.py` is new).

## Changes since v0.2.4

The full list is the `v0.2.5` section of
[CHANGELOG.md](https://github.com/cgillinger/opticfilm135i-linux/blob/v0.2.5/CHANGELOG.md).
