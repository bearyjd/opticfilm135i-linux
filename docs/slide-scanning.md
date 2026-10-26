# Slide-holder scanning and the dark_b fallback

Two additions to `of135i scan`, both verified offline only
(`tests/test_slides.py`, in `tools/release_check.py`). Neither has run on
hardware yet.

## 1. `--slide N` / `--slides SPEC`: the mounted-slide holder

```
of135i scan --slides 1-4 --ir [--dpi 3600] [--overscan MM] -o roll.tiff
of135i scan --slide 2 --ir -o roll.tiff
```

- Exactly one of `--frame`, `--frames`, `--slide`, `--slides` is accepted.
- Dual-light profiles only: add `--ir`, or use any `--dpi` other than 3600.
  Plain 3600 covers 26.5 mm laterally and clips a mounted slide (Test 85),
  so it is refused before the device is opened. `--film 110` is refused too.
- **Output naming:** every slide gets the `-f<N>` suffix, `--slide N`
  included, so existing consumers keep working:
  `<stem>-f<N>.overscan.tiff`, `<stem>-f<N>-ir.overscan.tiff`,
  `<stem>-f<N>.diag.json`. The IR overscan frame is **always** written for a
  slide, with or without `--ir` (600/1200/2400/7200 always run the dual
  engine). `--ir` (and `--no-clean`) only decide whether the visible frame
  is IR dust-cleaned. There is no aperture-registered `<stem>-f<N>.tiff`
  for a slide: the full overscan pair is the product.
- **Coverage:** not measured. The strip coverage detector looks for the
  strip holder's crossbars, and a slide holder has none (Tests 84 and 85
  reported "no aperture found" and exited 4). The sidecar records
  `"coverage": {"applicable": false, "verified": null, ...}`. The overscan
  frames are always written.
- **Exit status:** 0 when every requested slide was scanned and written.
  "Coverage not applicable" is not a failure. Genuine failures keep their
  codes: 2 for a usage or geometry refusal (nothing was sent to the
  scanner), and 1 or 130 for a hardware or session failure.
- **Overscan:** the default is `holder.SLIDE_OVERSCAN_MM` = 1.5 mm per side.
  The opening is 38.05 mm long, while the strip window is sized around a
  ~36 mm aperture. A real scan of opening 1 through strip frame 1 left only
  ~0.6 mm of plastic per side. The slide grid comes from a single load plus
  that scan, and opening 1 is extrapolated (±0.1 mm). 1.5 mm is twice the
  strip margin and more than 4× the position uncertainty (±0.24 mm load to
  load). It is capped from above by slide 4's travel check (section 2). At
  600 dual one chunk is 49 visible lines = 588 steps, so the trailing side
  rounds up in large steps: 2.0 mm and 2.5 mm put slide 4 at 71677 and
  71535, both refused. 1.5 mm keeps every profile at or below 71315.
  `--overscan` overrides the default; a value that would pass the ceiling
  is refused before the device is opened.

### Positioning: same path, different fiducial

A slide scan runs the strip path's own phases in the same order:
calibration, `POSITION` (mode 0x18 to a FEEDL), scan, `PARK`. Only the
injected FEEDL and the line count differ
(`docs/slide-holder-analysis.md` section 5). `Scanner.scan(slide=True)`
passes `holder.SLIDE_FIDUCIAL` and `holder.SLIDE` to the existing dual
geometry through `holder.slide_dual_geometry()`. `slide=False` is the
default, so strip scans take exactly the code path they took before.

The base is the measured, corrected strip positioning, not the retired
`feedl_for_frame()` grid. `feedl_for_frame(1)` is 6746; the driver
actually commands 6519 for strip frame 1 at 600 dual (the A+C geometry,
`STRIP_FIDUCIAL`), and that is the value which framed opening 1 on
hardware.

- Test 84's "600-dual coordinate" is FEEDL + 12·line. The model
  coordinate is that minus K = 1764·6/2 = 5292. Cross-check: strip
  frame 1 at 600 dual is (1439.84 − 212.6) + 5292 = 6519, Test 84's ledger
  value.
- Strip frame 1's leading edge is 1439.84 u in the model, which is
  23.748 mm in Test 84's coordinate. Slide opening 1 starts at 23.5 mm, so
  the shift is −0.248 mm (−70.5 u).
- Slide opening n starts at `STRIP_FIDUCIAL.leading_hwdpi(1) − 70.5 u +
  (n−1)·17744.88 u` (62.6 mm) and is 38.05 mm long. Each profile then
  derives its FEEDL the same way it does for strips: the start plus K.
  Consecutive slide FEEDLs differ by 17744.88 (±1 for rounding).

Commanded FEEDL for each slide at the default 1.5 mm, with FEEDL + wire
travel in brackets (ceiling 71490; pinned in
`test_slide_ledger_default_every_profile`):

| profile | slide 1 | slide 2 | slide 3 | slide 4 |
|---|---|---|---|---|
| 600 dual | 6236 (17996) | 23981 (35741) | 41726 (53486) | 59471 (71231) |
| 1200 dual | 6272 (18080) | 24017 (35825) | 41762 (53570) | 59507 (71315) |
| 2400 dual | 6260 (17948) | 24005 (35693) | 41750 (53438) | 59495 (71183) |
| 3600 dual | 6255 (17951) | 24000 (35696) | 41745 (53441) | 59490 (71186) |
| 7200 dual | 6256 (17944) | 24001 (35689) | 41746 (53434) | 59491 (71179) |

### Dust cleaning at each resolution

IR cleaning scales its detection, border exclusion, mask dilation and
inpainting footprints from the accepted 3600 dpi calibration to each
profile's physical sampling. The 2400 dpi profile is anisotropic
(3600 dpi across, 2400 dpi along), so each axis scales separately.
This prevents low-resolution specks from expanding into a near-solid
mask. The default 3600 dpi result remains unchanged; these checks use
synthetic offline fixtures, not new hardware runs. `--no-clean` preserves
the uncleaned visible frame while still writing the IR overscan frame.

## 2. Safety reasoning for slides

- Every slide position is validated before the device is opened (CLI), and
  again in the public `Scanner.scan(slide=True)` before the scan operation
  starts, so a refusal there sends no command and leaves the session
  unfailed. The
  slide number must be 1–4 (`check_frame` against `holder.SLIDE`). The
  model end position and the FEEDL must be inside `FEEDL_CEILING` = 71490,
  the same checks the strip path makes.
- **One extra check, slide path only:** the commanded FEEDL plus the
  wire's whole travel, `feedl + wire_lines·(7200/dpi)/2`, must also be
  ≤ 71490. This is the travel bound as `docs/holder-position-design.md`
  states it ("FEEDL + window travel"). For dual profiles it lies K
  (~18.7 mm) beyond the model end that the strip path checks. Which of the
  two is the true carriage extreme has not been established from
  software. Slide 4 is the furthest window the driver has ever been asked
  for, so the slide path requires both bounds to hold. With the default
  1.5 mm all four slides pass on every profile (worst case 71315). Larger
  explicit overscans are refused for slide 4, e.g. 2.0 mm at 600 dpi or
  3.0 mm at 3600/7200 dpi.
- **Strip-path finding, not changed here:** by the same FEEDL + travel
  measure, strip frame 6 at 600 dual already exceeds 71490 with an
  explicit `--overscan 2.5` (71520) or 2.0 (71662). The strip check
  accepts both, because it checks the model end only. Only the default
  0.75 mm case (71428) has run on hardware (Test 84).
- No existing check was changed or relaxed. The strip path does not get
  the stricter check, so strip behaviour stays bit-for-bit the same.

## 3. `--dark-fallback {strict,default}`

`strict` is the default and behaves exactly as before. When dark_b is
residual (Test 32) and no healthy dark_b has been measured this session,
the scan fails closed with `CalibrationError` before the white
measurement, and the scanner needs a power cycle.

`default` is opt-in. In that one case only, the driver logs a WARNING and
uses `calibrate._OFFSET_DEFAULT` = (0x010B, 0x010A, 0x010B), the vendor's
own codes, as the AFE offset. The A10 later-frame substitution (a residual
dark_b replaced by this session's healthy one) is unchanged. A residual
buffer is never remembered as healthy.

The fallback is recorded as `"dark_b_fallback": "default_offset_codes"`
in `last_diag`, and therefore in the `.diag.json`. Otherwise the field is
`null`. The value is carried through the plain path's calibration cache,
so a later cached frame says so too. `offset_codes` in the sidecar and in
`.cal.npz` are the codes actually used. The sidecar's
`cli.dark_fallback` records the mode.

**Why this is safe and honest:**

- dark_b has exactly one computational consumer in both the plain and the
  dual path: `calibrate.offset_codes(dark_a, dark_b)`. That is the
  two-point bracket (dark_a at offset code 0x80, dark_b at 0xFF) that
  produces AFE offset registers 5/6/7. Every other use is a record:
  `dark_b_mean`, the `cal_data` array, the calibration capture dump and
  the cache.
- `offset_codes()` already returns these same default codes when the
  bracket slope is abnormal. Measured calibrations land within 1–2 codes
  of them (Test 84: 266/265/266; Test 85: 0x010a/0x0109/0x010a).
- One code is ~18 counts of 65535 (bracket slope 17.6–18 counts per code),
  so a 1–2 code error moves the black level by ~0.03–0.06 % of full scale.
- The motor and phase sequence is identical; only three injected register
  values differ. Continuing a scan after a residual dark_b is already
  accepted behaviour on later frames (A10).
