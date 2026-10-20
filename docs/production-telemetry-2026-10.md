# Production telemetry, 2026-09-15 to 2026-10-18: what 711 frames say about the driver

Status: analysis, offline, 2026-10-20. The data is the driver's own
per-strip manifest and per-frame `.diag.json` sidecars from the owner's
digitisation workflow (a separate, private project that calls
`of135i digitize`). Nothing identifying is reproduced here: no paths,
project names, subjects or people; only the driver's own transport and
calibration fields. The workflow's notes to the driver side (de-identified
by that project before hand-over) are summarised where they add an
observation the telemetry alone does not carry.

## 1. The data set

| | |
|---|---|
| Strips (`manifest.jsonl` rows) | 193 (180 ok, 13 failed) |
| Frames with a per-frame entry | 711 |
| Frames with a `.diag.json` still on disk | 652 |
| Sessions (distinct days) | 24, over five weeks |
| Profile | dual 3600 dpi with IR, every strip (`--no-clean` on 16 of 25 early frames, then throughout) |
| Driver versions | v0.1.2 through v0.2.2 |

Every production strip is the dual-light profile. The plain-3600 path,
the other dual profiles and the SANE backend carry no production
telemetry at all; what follows applies to `digitize` on the dual path
unless stated.

## 2. Findings, with what changed because of them

### 2.1 Aperture coverage: the visible channel is the wrong place to look for the edge when film hides it

9 of 711 frames failed with `no aperture found in profile`; 8 of the 9
were frame 1 of their strip. The strips were otherwise fine (the other
frames verified), and the workflow's own inspection found the same
cause each time: the film's end sat in the opening (a black film end
filling most of it, or a dark picture next to an empty, saturated part
of the opening), so the red profile had no single lit-to-plastic edge
pair to find. On both failed frames still on disk, the driver's own
`measure_coverage` run on the **IR** overscan found the opening at the
first attempt (margins 0.70/0.93 mm and 2.54/0.87 mm); IR is blind to
the picture, so the picture cannot hide the edge.

The workflow had built a rescue tool for exactly this (re-measure on IR,
sanity-check the aperture length against the strip's verified siblings,
cut the products from the overscan files). That is the driver's job.

**Change (offline, this revision):** on the dual paths coverage is now
measured on the IR pass first and on the visible frame only if IR does
not verify; the verdict records its `source`. Plain paths are unchanged.
The 647 verified frames on disk are the regression set: on verified
frames the IR edge and the visible edge are expected to agree to within
a line or two (checked on a random sample, see §4).

One case where the IR rule verifies what the visible rule refused: on the
film-end frame above (IR margins 2.54/0.87 mm) the first 1.8 mm of the
opening is opaque in IR too, so IR finds an opening 34.2 mm long and
verifies it. The edges are real, but the crop would be short. A verified
frame whose margin exceeds 2.0 mm (telemetry maximum 1.10 mm,
`aperture_crop.MAX_PLAUSIBLE_MARGIN_MM`) is therefore flagged with
`coverage_warning` in the manifest entry and on stderr, never failed:
the workflow's own sibling-length check decides what to do with it.

### 2.2 Coverage says "the opening is in the picture", not "there is film in it"

Observed by the workflow on 2026-10-18: a strip of two frames scanned as
four. Slots 3 and 4 were empty, light straight through, and the driver
reported `coverage_verified = true`, `reason = ok`, with normal-looking
margins (0.50/0.88 mm). In the raw frame all three channels read above
60000 of 65535 across the whole opening. Film, even unexposed base,
never does that: the orange mask holds blue far below saturation. So an
empty slot has a perfect aperture and passes.

Consequences downstream: the workflow measures a strip's film base and
density range over all its frames, and an empty frame would set both at
the sensor ceiling; the empty frames had to be excluded by hand. And
every empty frame costs a full cycle (55 to 66 s, one position move,
one park return).

**Change (offline, this revision):** after a verified coverage,
`digitize` measures the share of saturated pixels inside the opening and
records `film_present` / `saturated_share`; an empty frame writes no
`fN.tiff` (the overscan files stay), is reported as `empty aperture`,
and is not a coverage failure. By default the strip stops after an empty
frame that follows at least one frame with film (`--no-stop-at-empty`
scans the whole request); an empty slot 1 never stops the strip, since
a strip can be seated short of the stop.

### 2.3 Every benign poll mismatch costs exactly one second

| poll_timeouts per frame | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|---|---|---|---|---|---|---|---|---|
| frames | 46 | 107 | 206 | 181 | 83 | 25 | 3 | 1 |

93 % of frames time out at least one non-strict poll. The signatures
are the three documented as benign since 2026-09-05 and nothing else:
`9c55` where the capture settled at `ad55` (1130 observations), `9c55`
against `bd55` (384), `8155` against `9555` (30, the reg 0x32 polls).
Not one of these 1544 cases was followed by any failure; the scan,
PARK and eject completed every time.

The cost is clean: calibration time per frame fits
`6.16 s + 0.93 s × poll_timeouts` (the non-strict poll floor is 1.0 s,
`_poll_one`), and the per-phase minima show where it lands:
`cal_dark_a/b`, `cal_gain_check_a/b` and `cal_white` each take 0.12 to
0.29 s when the poll matches and 1.1 to 1.3 s when it does not, in 41 to
59 % of frames each. Mean cost about 2.2 s per frame, 9 s per strip,
roughly 4 % of a four-frame strip's 240 s.

What the telemetry cannot say is *when* the status word would have
settled had the poll waited longer, because a timed-out poll logs only
its last value. It also cannot say why the same site matches on one
frame and reads `9c55` for a full second on the next. The SANE backend,
on the same unit, polls the data-ready bit at these sites (the
`PollDataReady` classification, hardware-verified from Test 48 on) and
does not report the stall, so the state word's class is not what the
data path depends on.

**Change (separate branch, not merged until one strip has been scanned
with it):** at these sites a status word with the data-ready bit set is
accepted (the backend's criterion), the non-strict floor is shortened,
and the settle time of every non-strict poll is recorded in the diag
sidecar so the next production strips measure the distribution.
Hardware plan: docs/test-log.md Test 100.

### 2.4 The time budget, and where the motor moves

Per frame, medians over 652 frames (dual 3600 with IR):

| phase | frame 1 | frame 2 | frame 3 | frame 4 |
|---|---|---|---|---|
| position (s) | 1.80 | 3.77 | 5.93 | 8.09 |
| scan (s) | 40.2 | 40.2 | 40.2 | 40.2 |
| park, wait B (s) | 3.59 | 5.75 | 7.90 | 10.06 |
| total (s) | 55.0 | 58.0 | 62.2 | 66.3 |

Calibration is 6 to 11 s per frame (§2.3). Position and park are
deterministic to the hundredth of a second (p5 = p95), no park wait ever
timed out, and the park's wait B grows with the frame number because the
semantic PARK returns the carriage from wherever the frame was: that
return, four times per strip, is the repeated motor movement. A
four-frame strip spends about 240 s, of which 160 s is the scan itself,
32 s calibration, 28 s park returns and 20 s positioning.

This is the quantified case for the whole-strip sweep already listed in
docs/ROADMAP.md: one calibration, one position, one sweep, one park. On
these numbers it removes about 70 s per four-frame strip and three park
returns, and it is the only way to get the 1.9 mm inter-frame gaps into
the raw data, which the workflow needs as a true film-base sample (its
current base estimate is taken from picture content and moves by 3 %
in the median, 14 % at the 90th percentile, when the frame that carries
the estimate is dropped). Not started; it needs a hardware A/B and crop
work, and the owner's go.

### 2.5 Per-frame calibration is justified by the data; the session cache never runs in production

Gain codes over 711 frames: red 44 to 46, green 32 to 34, blue 38 to 39;
offset 266 ± 1 in every channel. Within a session the white reference
falls by 3 to 4 % in red and 5 to 6 % in green from first to last frame
(blue about 1 %), and the gain codes follow it by one or two codes. The
lamp dims as it warms; the calibration tracks it. There is no case for
reusing a gain or shading measurement across a dual session without a
drift check.

The per-session calibration cache (2026-09-17) applies to the plain
path only, by design, and the production profile is dual: 645 frames
report `calibration = fresh`, none `cached`. It has had no effect on any
production scan and is documented as such; it stays for the plain path.

### 2.6 Column pattern on thin negatives, and the shading measurement

The workflow found horizontal bands in finished pictures from thin
negatives (seven 8-bit levels peak to peak in a stretched sky). After
the 90° rotation it applies, a horizontal band in the picture is a
constant **column** in the raw frame, i.e. one sensor pixel over the
whole transport. Measured in the raw green channel: per-column residual
1.5 to 2.5 % of level against a 4-column filter, not periodic, and the
column pattern correlates only 0.2 to 0.45 between frames of the same
session, so a fixed map from the pictures themselves explains only 10
to 35 % of a frame's residual and was rejected.

The driver's side of the answer: per-pixel shading **is** done, by the
scanner, from the per-frame white measurement (`cal_shading_measure`
reads 128 lines, the uploaded table is the 45 856-byte per-pixel
offset/gain field, docs/sane-hook4-shading.md). A per-frame shading
measurement that is itself noisy would imprint a different column
pattern on every frame, which is what the low inter-frame correlation
looks like. That is a hypothesis, not a measurement.

**Change (offline, this revision):** the raw calibration data of every
fresh calibration (white line, dark lines, the 128-line shading and
verify measurements, the uploaded tables) is written next to the diag
sidecar as `<stem>.cal.npz` (`--no-cal-data` to skip), so the
hypothesis can be tested on production data without touching the
scanner: the column noise of the 128-line mean, its frame-to-frame
correlation, and whether a session flat field built from several
measurements removes the pattern. Averaging more lines in the shading
measurement would be the fix if it holds; that is a wire change
(Python and the backend share the program) and needs an A/B.

### 2.7 Three strips lost to a closed stdout

Three strips (2026-09-15 and 2026-09-18) are recorded failed with
`BrokenPipeError(32, 'Broken pipe')` at stage `scan`; two had already
completed frame 1 with verified coverage. A print to a stdout pipe
whose reader had gone away raised inside the scanning session, which
then ended without PARK. No recurrence in the 190 strips since, so the
reader side was fixed, but the driver's console output must not be able
to end a scan.

**Change (offline, this revision):** stdout and stderr are wrapped so a
broken pipe silences further output instead of raising.

## 3. What the telemetry confirms and does not change

- **Overscan sizing.** Verified margins over 647 frames: leading 0.43 to
  1.08 mm (median 0.73), trailing 0.55 to 1.10 mm (median 0.79),
  aperture length 35.71 to 36.20 mm. Not one margin under 0.3 mm. The
  0.75 mm default against the ±0.5 mm design case of
  docs/holder-position-design.md holds over about 190 loads: observed
  spread ±0.33 mm. Nothing to change.
- **Transport.** Position and park budgets are never approached;
  `dark_b_substituted` never fired; no session started from any state
  but `idle-homed` (the load flow precedes every strip); no lamp
  warm-up retry was ever needed (one measurement every frame).
- **`cr_mismatches`** 6 to 43 per frame (median 24): the control-read
  mismatches documented as benign; no action.

## 4. Checks run for this analysis

- Manifest and sidecar aggregation: Python over all `manifest.jsonl`
  rows and `.diag.json` files on disk (read-only).
- The two failed frames on disk: `measure_coverage` on their IR
  overscan (verified) and visible overscan (not verified), reproducing
  the stored verdict.
- IR-versus-visible edge agreement on 16 random verified frames from
  nine strips (read-only, `measure_coverage` on both overscan files):
  IR verified 16/16, visible 16/16. Leading-edge difference (IR minus
  visible) within 1 line on 9 frames, 1 to 6 lines on 6, and 19 lines
  (0.13 mm, IR earlier) on one; trailing-edge difference within 1 line
  on 10, 1 to 5.3 lines on 6. Where they differ the visible edge sits
  inside the IR edge: the film's own dark rim next to the plastic moves
  the visible crossing inwards, the IR crossing is the plastic itself.
  So the IR-first rule changes a verified crop by at most about 0.1 mm
  and only ever towards the holder edge. The empty-slot share
  (`saturated_share`) was 0.0000 on all 16 frames with film, against
  > 0.9 for an empty slot.

## 5. Parked

- **Blue gain against the film base** (suggested by the workflow: blue
  carries about a quarter of red's code range through the orange mask).
  Not adopted: the cast the workflow measures is tone-dependent (yellow
  highlights, blue shadows, consistent with dye fading) and gain does
  not change that; a film-dependent gain breaks the film-independent
  calibration (Test 60); and if blue is light-limited rather than
  read-noise-limited, exposure, not gain, is the lever. Would need a
  hardware A/B against a measured noise floor before it is worth a
  design.
