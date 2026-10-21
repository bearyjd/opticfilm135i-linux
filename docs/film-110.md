# 110 (Pocket Instamatic) support in the strip holder

Status: **offline-implemented**, 2026-09-19, **reviewed and corrected
2026-09-20** (§9.1: the crop cut 0.1–0.4 mm of picture on the trailing
edges because the edge refinement assumed a clear surround; §5: image
identity across placements was not implemented). `--film 110` runs on
saved data and on synthetic fixtures (`tests/test_film110.py`, in
`tools/release_check.py`'s core group) and has been checked against one
real hardware strip's saved scans (`tools/film110_check.py`, private
fixtures). **Hardware verification passed 2026-09-20: Test 87, a second
strip from another camera, end to end with no manual cropping** (§9.2;
acceptance plan in docs/film-110-proposal.md §6). Supported on n = 2. Nothing here
changes the motor side: positioning, waits, calibration and the FEEDL/
overscan geometry are exactly as for 35 mm (docs/film-110-proposal.md
§2/§5).

## 1. What was measured (n = 1 strip, 2026-09-19, Test 86)

A four-frame 110 colour-negative strip scanned in the standard 35 mm
strip holder, three placements, one load each (docs/test-log.md Test 86).

| Quantity | Value | How measured |
|---|---|---|
| Film width | 16.0 mm (15.7–16.0 across 3 loads) | lateral edge of the film band, 600 dpi survey |
| Image, along transport | 17.2 mm (17.0–17.5, four images) | 1 mm rulers, 3600 dpi frames |
| Image, lateral | 13.0 mm (one image read 13.7; unexplained, see proposal §4.3). **The second strip's gate is 13.3–13.7 mm wide with a 0.5 mm fogged margin on each side that is *lighter* than the picture (§9.2)** — the lateral edge is the camera's, not the format's | same |
| Frame pitch | 25.5 mm (25.0/25.8/25.7 between consecutive images) | 600 dpi survey |
| Perforation | one rectangular hole per frame, ~1.5 x 2 mm, punched 0.6–2.1 mm inside the film edge that faces the open side of the aperture (the edge itself stays continuous through the hole); hole trailing edge 2.3 mm before the next image start, previous image end 3.4 mm before the hole leading edge | 600 dpi rulers, column profiles |
| Dark printed border | ~1.2 mm wide along the perforated edge, 0.8–2.0 mm inside it, reading 0.066 x air (the rail plastic reads 0.033 x air) | column profiles |
| Lateral position | film against the lower rail; far edge ~8 mm inside the 24 mm aperture; stable to ±0.1 mm across three loads | column profiles |
| Image lateral offset | image begins 2.0 mm inside the perforated edge (2.4 read by eye on the rulers; 2.0 fits the detector's tracked edge against the hand-read image edges on all four 3600 dpi frames) | 3600 dpi rulers + detector |

These agree with nominal 110 figures (13 x 17 mm image, 16 mm film); the
pitch and perforation lead are **this strip's own measurements** and are
not yet confirmed general (project rule: never build on one reference —
see Limitations below).

`of135i/holder.py` carries this as `FILM_110`, a `Film` record consumed
only by the detector (`of135i/film110.py`); it is image-side geometry,
not holder/positioning geometry, and never feeds a FEEDL, a wait or a
calibration step.

## 2. Why one placement is not enough

The aperture pitch is 38 mm (36 mm opening + ~2 mm bar); the film pitch
is 25.5 mm. With image 1 aligned to aperture 1, successive images sit at
phases 0, 25.5, 13, 0.5, 26, 13.5, 1, … mm inside their apertures. An
image needs a phase under about 18.8 mm to be whole; every third image
lands under a bar and cannot be recovered from that placement — the bar
is opaque.

## 3. The two-placement protocol (visual, no measuring)

- **Placement A** — image 1's left edge at aperture 1's left edge, a
  hair of clear film visible (exactly like loading a 35 mm strip).
  Yields images 1, 3, 4 (and 6, 7, …) whole; the bar between images 1
  and 2 splits image 2.
- **Placement B** — shift the strip half a film pitch (12.5 mm) so bar 1
  falls between images 1 and 2. Yields images 2, 4 (and 5, 8, …) whole.

## 4. The sag rule

A 16 mm strip is held only where it passes under plastic. A strip end
left free inside an aperture sags slightly out of the focal plane —
measured on image 4 of the test strip (Laplacian sharpness, three bands
along the frame):

| Placement | End's support | Sharpness (three bands) |
|---|---|---|
| A — end free in the aperture | unsupported | 2004 / 1775 / 1556 (softening) |
| B — end under the next bar | supported | 2780 / 3765 / 3399 (grain resolved) |

So take the strip's **last** image in the placement where its end sits
under a bar, and the **first** image in the placement where its start
does. The detector flags this (`FrameFind.free_end_near`) but cannot fix
it — it is a physical fact about the holder, not something software
corrects.

## 5. Numbering and image identity

An image's number is its **position on the strip**, derived from where
it was found and which placement the strip is in — given with
`--placement A|B`, or, when it is not given, **inferred from the first
numbered image's position** (below):

    position  = (aperture − 1) × 37.947 mm + image start inside the aperture
    number    = round((position − phase) / 25.5 mm) + 1
    phase(A)  = 3.5 mm      (image 1's start inside aperture 1, §3)
    phase(B)  = 3.5 + 12.75 mm

So the same photograph gets the same number whichever placement,
resolution (the arithmetic is in millimetres) or subset of apertures it
was scanned in — that is what makes `a-image4.tiff` and `b-image4.tiff`
the *same* picture. Worked example from the 2026-09-19 strip (positions
as the detector measured them after the 2026-09-20 correction):

| Placement | Aperture | Image start | Position | Number | Residual |
|---|---|---|---|---|---|
| A | 1 | 3.53 mm | 3.5 mm | 1 | 0.0 mm |
| A | 1 | 28.85 mm (split by bar 1/2) | 28.9 mm | 2 | −0.2 mm |
| A | 2 | 16.17 mm | 54.1 mm | 3 | −0.4 mm |
| A | 3 | 3.70 mm | 79.6 mm | 4 | −0.4 mm |
| B | 1 | 15.73 mm | 15.7 mm | 1 | −0.5 mm |
| B | 2 | 3.26 mm | 41.2 mm | 2 | −0.6 mm |
| B | 2 | 28.74 mm (split by bar 2/3) | 66.7 mm | 3 | −0.6 mm |
| B | 3 | 16.32 mm | 92.3 mm | 4 | −0.5 mm |

The evening's first, unruled placement (strip 13 mm into aperture 1,
image 1 at 17.0 mm) is **not** at either phase (residual 13.5 mm against
A) and is refused — see below. The rounding tolerates ±6 mm, so the
visual placement rule of §3 has ample slack, and a 0.3 mm pitch error
per frame (this strip's pitch is n = 1) would take ~20 frames to reach
it.

**Inferred placement.** The two phases are 12.75 mm apart on a 25.5 mm
film pitch, so the data itself says which placement a strip is in:
`film110.infer_placement` computes the position residual against A's and
B's phase grid and returns the closer one. A residual above
`PLACEMENT_INFER_TOLERANCE_MM` (4.0 mm; the farthest an image can be from
both is 6.4 mm, midway at about 9.9 mm) is *ambiguous*: that image is
refused ("lies between the two placements' phases (A off by …, B off by …)
-> give --placement A or B"), nothing is written for it, and the next image
may still decide. The first numbered image decides for the whole command;
every later image is checked against that placement exactly as below. An
explicit `--placement` is the override: it is never second-guessed by the
inference, and an image off its phase is still refused. The decided
placement is printed on the deciding aperture's line ("placement A
(inferred)" / "(given)") and reported by `scan` in a summary line and by
`digitize` in the roll record (§11).

Rules, all enforced by the CLI (`_film110_process_aperture`, shared by
`scan` and `digitize`):

- A whole image and a trailing split (an image running under the next
  bar) are numbered; a leading split (the tail of the previous aperture's
  trailing split, predicted from the hole after it) is reported but
  never numbered — it was numbered where it started, if that aperture
  was scanned.
- **Phase check.** An image more than 6 mm off the declared placement's
  grid means the strip is not where the operator said: the aperture line
  says so, no 110 product is written for it (the aperture product and the
  overscan are, as always), and the command exits 5 after finishing the
  other apertures. Re-seat the strip or declare the right placement.
- **No overwrite.** A 110 product that already exists is never
  overwritten: the aperture line says so, nothing is written for that
  image (neither the visible crop nor its IR sidecar), and the command
  exits 5. Use one `-o` stem per placement (§6). This is what keeps
  `a-image4.tiff` (the free-end, softer copy) and `b-image4.tiff` (under
  the bar, sharp) both on disk so the operator can choose per photograph
  (§4).
- The detector's per-aperture `FrameFind.index` stays what it is — the
  order within that aperture — and is not the strip number.

**`scan` does not tie A and B together:** the identity is the number, the
pairing is the operator's two stems, and choosing the final product per
photograph is the operator's step (sag rule, §4). `digitize --film 110`
(§11) is the integrated route: one roll directory, one product per
photograph, the sag rule applied automatically.

## 6. Commands

Survey (existing 600 dpi run, not a new mode — prints the per-aperture
verdicts, writes only the aperture/overscan products as today, since
`--overscan` is on by default):

```
of135i scan --frames 1-6 --dpi 600 --film 110 --placement A --ir --no-clean -o a-svep600.tiff
```

Production, once the survey says which apertures/placement to use — one
`-o` stem per placement:

```
of135i scan --frames <whole-image-apertures> --dpi 3600 --ir --film 110 --placement A --eject -o a.tiff
# re-seat the strip (§3, placement B), load, then
of135i scan --frames <whole-image-apertures> --dpi 3600 --ir --film 110 --placement B --eject -o b.tiff
```

Two strips in one load (§12): `of135i digitize --out ~/scans/boxA --film 110
--strips 2` (second run: add `--roll N`) writes roll N for apertures 1–3 and
roll N+1 for apertures 4–6. `scan` always handles one strip.

`--placement` is optional with `--film 110` (inferred from the first image,
§5) and refused without it. The products are then `a-image1.tiff`, `a-image3.tiff`, `a-image4.tiff`,
`b-image2.tiff`, `b-image4.tiff`, … — the number is the photograph (§5),
the stem is the placement.

## 7. Products written

`--film 110` never changes what the aperture pipeline already writes:
the full overscan frame(s) and the aperture-registered product (`out`,
`<stem>-ir.tiff`) are written exactly as for `--film 135`. In addition,
for every 110 image the detector finds **whole** inside a verified
aperture, it writes:

- `<stem>-image<N>.tiff` — the 110 image, visible, oriented the same way
  as the aperture product (`--positive`/`--rotate` applied identically).
- `<stem>-image<N>-ir.tiff` — the same crop of the IR channel, with
  `--ir` (same line/column indices as the visible crop, so the two stay
  registered — the dual scan path already guarantees this on the shared
  grid dust removal runs on).

`digitize --film 110` writes its photographs differently (`image<N>.tiff`
in the roll directory, §11).

A **split** image is reported (which bar, and "take it in placement B"
/ "A") but no file is written for it — the aperture product and
overscan remain the only record, exactly as an unverified-coverage frame
today writes no aperture-registered product either.

An existing `<stem>-image<N>.tiff` is **never overwritten** and an image
off the declared placement's phase is **never numbered** — both are
reported on the aperture line and make the command exit 5 (§5). The
aperture product and the overscan are written regardless, so a refused
110 product loses nothing that was scanned.

Integrated route (§11): two runs, one load each, into the same roll
directory; the placement is inferred, the products are `image<N>.tiff`:

```
of135i digitize --out ~/scans/boxA --film 110            # placement A
# re-seat the strip (§3, placement B), then, same roll:
of135i digitize --out ~/scans/boxA --film 110 --roll 1   # placement B
```

With `--strips 2` (§12) the same files are written per roll: roll N holds
slot 1's `a-f1…a-f3` / `b-f1…b-f3` and `image<K>.tiff`, roll N+1 holds slot 2's
`a-f4…a-f6` / `b-f4…b-f6` and its own `image<K>.tiff`, K counted per strip.

## 8. How the detector reads an aperture

`of135i/film110.py`, pure numpy, in millimetres so it runs on the 600 dpi
survey and the 3600 dpi production frames alike:

1. Column classes from column medians: air (≥ 0.95 x air), plastic
   (≤ 0.05 x air, but only in runs ≥ 2 mm wide, so the film's own dark
   printed border stays inside the band), film between. No film columns:
   the aperture is empty.
2. The film band is the longest run of film columns, 16 ± 1 mm wide,
   adjacent to air on the perforated side.
3. Film presence per line (median over the band interior below 0.85 x
   air) gives a strip end inside the aperture, if any.
4. The film's air-facing edge is located on **every line** (first column
   below 0.6 x air, scanning in from the air side) — the free edge of a
   16 mm strip wandered 1–2 mm along one aperture on this strip — and a
   hole is a line whose zone 0.3–3.0 mm inside *that line's* edge reaches
   air level. Hole runs of 0.8–4 mm (0.3 mm when cut by the aperture
   boundary) are perforations.
5. Every hole predicts the image after it (hole end + 2.3 mm) and the
   image before it (hole start − 3.4 mm); predictions closer than half a
   pitch are the same image and are merged. A window that is neither
   denser than clear film nor varying along its length is clear film, not
   an image, and is dropped.
6. The strip may be inserted either way round, which swaps the 2.3/3.4
   mm figures; both assignments are refined and the one the data confirms
   on more along-transport edges wins (a tie keeps the normal one).
7. Each edge is refined within ±0.5 mm of its prediction. The
   **direction of the step is read from the data, not assumed**: the
   median level 0.3–1.0 mm inside the predicted edge against the same
   band outside it (no refinement below 8 % contrast). On 110 film the
   picture is surrounded on all four sides by the pre-exposed dark
   printed border, so leaving the picture gets *darker* — the opposite
   of a 35 mm-like clear surround. The strongest step in that direction
   wins if it is ≥ 3 x the neighbourhood's median step, and the edge is
   then walked outward to the foot of the transition (≤ 0.6 mm, while
   the step stays ≥ 10 % of the peak), so the soft camera-gate ramp is
   inside the crop. Along transport this is done on the per-line median
   of the band interior; laterally on the per-column median over the
   image's own lines, on **both** sides — the rail side stopping 0.4 mm
   short of the rail's bright rim, the perforated side kept 1.0 mm inside
   the tracked film edge so the film's own rim and the hole never enter
   the window. **Laterally the step's direction is not assumed at all**
   (§9.2: the first strip's side border is darker than the picture, the
   second's is lighter): the candidates are the local maxima of |step|
   in the window that reach half the window's strongest step and 3 x the
   neighbourhood's median step; the **innermost** candidate whose levels
   0.1–0.4 mm on either side differ by ≥ 4 % wins — a border's outer
   edge (border → clear film) can be the stronger step, picture-content
   steps have picture on both sides and fail the contrast test — and is
   walked outward to its foot. `crop` never pads past the film band.
8. A find is *whole* with ≥ 0.3 mm inside both aperture boundaries,
   *split* (leading/trailing) otherwise; *free_end_near* when a strip end
   lies within one pitch. The CLI grows a whole image's crop by 0.25 mm
   on every side before writing it — a safety margin of dark border
   around a correctly found edge (§9.1), not a correction for a biased
   one.

## 9. Results on the real strip (offline, 22 apertures, 2026-09-19)

`tools/film110_check.py` over every aperture-registered frame of Test
86: 14 at 600 dpi (three placements) and 8 at 3600 dpi.

- **Verdicts: 22 of 22 as read by eye.** Every whole image is found
  whole, every bar-split image is reported split on the right side, the
  four empty apertures are empty, both strip ends inside an aperture are
  reported (13.2 / 12.0 mm starts, 25.4 mm end) with the free-end flag on
  the adjacent image, and every aperture resolved to the normal
  orientation.
- **Edges**: see §9.1 — the 2026-09-19 figures ("0.2–0.5 mm inside
  the eye reading, a soft gate edge") were the symptom of a detector
  defect, not a property of the film.

### 9.1 Does the crop lose picture? (Astra's review, measured 2026-09-20)

The review asked for proof that no visible part of the photograph is
cut, not just a `whole` verdict. Measured offline on the four finished
images of Test 86: each hand-cropped image was located inside its source
frame by normalised cross-correlation (0.97–0.99 at 1/8 scale), and a
density profile was taken across every edge (mean over the middle 80 %
of the perpendicular extent, 0.1 mm steps).

**Finding: the crop did cut picture.** On image 1's trailing edge the
product ended 0.15 mm before the picture's transition began, with
picture texture in the lost band (visible in the snippet); image 3's
trailing edge lost ≤ 0.08 mm, image 4's leading edge sat mid-ramp. The
cause was not a soft gate edge: `_refine_edge` assumed the surround is
*brighter* than the picture (true for clear 35 mm film, and for the
synthetic fixtures) and searched for a step of that sign. On the real
110 negative the picture (density 0.3–0.8) sits in a dark printed
border (density 1.1) on all four sides, so the only brightening step
near an edge is the fall-off of the thin bright gate-edge halo just
*inside* the picture — the refinement locked onto that, 0.1–0.4 mm
inside the true edge, on every along-transport edge of every frame, and
the 3 x median-step test did not catch it because a near-flat
neighbourhood has a tiny median. The 0.25 mm pad hid most of it.

**Fix** (§8 step 7): the step direction is read from the levels either
side of the predicted edge; the perforated-side lateral edge is refined
too (its 2.0 mm model offset sat 0.2 mm inside the picture's foot on all
four frames). **Result**, `tools/film110_check.py --edge-check`, which
re-measures every edge independently of the detector (the foot = the
start of the first flat plateau beyond the picture, at another level or
reached through a step; a flat sky can trip it into a false LOSS, which
errs towards a human look, never towards a missed cut):

| Frame | line0 | line1 | col0 | col1 | Size (mm) |
|---|---|---|---|---|---|
| image 1 (A, aperture 1) | +0.19 | +0.24 | +0.26 | +0.25 | 17.07 x 13.11 |
| image 3 (A, aperture 2) | +0.24 | +0.25 | +0.25 | +0.24 | 17.19 x 13.09 |
| image 2 (B, aperture 2) | +0.24 | +0.26 | +0.25 | +0.24 | 17.26 x 13.12 |
| image 4 (B, aperture 3) | +0.24 | +0.26 | +0.22 | +0.26 | 16.80 x 13.12 |

Margins are how far the padded product edge lies *outside* the
independently measured foot: every edge is within 0.06 mm of its foot
before the pad, so the 0.25 mm pad is now pure safety (dark border, no
picture). Sizes match the nominal 17.2 x 13.0 mm; image 4 reads 16.8 mm
in all three placements it was scanned in, so that is the frame. The 22
aperture verdicts of Test 86 are unchanged by the fix, and the same
aperture at 600 and 3600 dpi agrees within 0.05 mm.
- **What failed on the way, and why it matters for a second strip:** a
  hole search fixed next to the aperture-wide median film edge failed at
  3600 dpi (the edge wanders); a hole modelled as a break in the film
  edge failed everywhere (the hole is inside the edge, behind a rim);
  a clear-base level taken from the 90th percentile of the film failed on
  an aperture with a fogged leader and one image (little clear film to
  estimate from). All three are structural and are what steps 4, 5 and
  8 above encode.

### 9.2 The second strip (Test 87, 2026-09-20): a lighter side border

A four-frame strip from another 110 camera, placed by the §3 rule with
image 1 starting 1.0 mm into aperture 1 (2.5 mm earlier than the first
strip's 3.5 mm — well inside the ±6 mm phase tolerance; the numbering
gave 1, 3, 4 directly). Survey verdicts A: image 1 whole, image 2 split
by bar 1/2, image 3 whole, image 4 whole with the strip end free at
25.4 mm (flagged), aperture 4 empty; B: image 1 whole with the free
start flagged, image 2 whole, image 3 split, image 4 whole with the end
under bar 3 — exactly the first strip's pattern, all 8 survey verdicts
and both productions (apertures 1–2 in A, 2–3 in B; coverage verified,
ejected) as the rule predicts, and the same photograph numbered the
same in `a-` and `b-` products.

**The first products cut 0.2–0.5 mm of picture on the rail side of
three images and 0.2 mm on the perforated side of one**, caught by
`--edge-check`. This film differs from the first in one thing a
one-strip model cannot know: beside the picture, on both sides, lies a
~0.5 mm fogged margin of density ~0.8 that is **lighter** than the
picture (density 0.9–1.05 on these dense frames), where the first
strip's printed border (1.13) is darker than its pictures (0.5–0.7).
The lateral refinement had assumed a fixed step direction and checked
contrast in bands around the *prediction*, which for a prediction 0.4 mm
off straddled border and picture: it then found nothing and fell back
to the 13.0 mm model, or locked onto a picture-content step. On the way
to the fix two wrong readings were made and are kept here: the check
tool's foot was walking through that light border to the rail (its
scatter threshold was inflated by the ramp itself), which made the
picture look 14 mm wide "with no side border", and a "picture runs to
the rail" rule built on that reading was implemented and then removed
once the profiles were read properly — a uniform strip of one density
on both sides of every frame is a border, and the close-ups agree.

Fix (§8 step 7): direction-free lateral refinement, innermost contrasted
step; the check tool's foot is now the first plateau beyond the picture.
Re-measured on all eight production frames of both strips: every edge at
or outside the foot (worst +0.08 mm, image 4's perforated side, where
the border and the picture share a density and only the step between
them marks the edge); the first strip's four frames unchanged. Sizes on
this strip: 17.3 x 13.3, 16.9 x 13.4, 17.1 x 13.7, 16.9 x 13.4 mm. The
products were re-cut from the saved aperture frames; both earlier cuts
are kept beside them as evidence.

## 10. Limitations

- **Lateral size and border polarity are the camera's and the film's,
  not the format's**: 13.1 mm inside a darker border on the first strip,
  13.3–13.7 mm inside a lighter one on the second. The model's 13.0 mm
  is only where the search starts; the crop follows the data (§8 step
  7). A fixed "13.0 ± 0.3 mm" is therefore not an acceptance criterion —
  the edge check is.

- **n = 1 strip.** Pitch 25.5, the 2.3/3.4 mm hole offsets, the 2.0 mm
  lateral offset and the 0.6 mm rim are one strip's. Refinement absorbs
  ±0.3 mm of model error, not ±1 mm. Test 87 — a second strip through
  the full protocol with no manual cropping — is the acceptance step
  before "110 support" is claimed anywhere public.
- **The perforated edge must face the open side of the aperture** (it
  does when the strip lies against the lower rail as in Test 86). With
  the holes under the rail the detector reports "perforations hidden"
  and writes no 110 products; the aperture products are unaffected.
- **Edge safety is 0.25 mm of border, n = 1 strip.** After the
  2026-09-20 fix the refined edge lies within 0.06 mm of the measured
  foot on all 16 production edges; the pad is a margin, not a
  correction. Test 87 re-measures this with `film110_check.py
  --edge-check` on the second strip: any negative margin is a defect to
  fix in the detector, never by widening the pad.
- **Placement phases are one operator's, n = 1 strip** (3.5 mm for A,
  measured 3.53/3.58; B derived as A + 12.75, measured 15.7). The ±6 mm
  rounding tolerance covers the visual rule's slack; a strip that reads
  off-phase is refused, not misnumbered.
- **`leading_continuation` over-reports on 110.** It flags an aperture
  whose first millimetre is darker than clear film as "continuing a
  split image"; on this film the dark printed border does that too, so
  placement A's aperture 1 (image 1 at 3.5 mm) reads as continuing.
  The line is informational only — numbering and products never use it
  (a real continuation is the `leading` split predicted from the hole).
- **Positive (slide) 110 film and B&W 110** are untested; the detector
  works on light levels, the IR clean path assumes a dye-image negative
  as for 135.
- **Placement inference is one operator's phases, n = 2 strips.** A strip
  seated 4–9 mm off both phases (visibly "between" A and B) is refused, not
  guessed; give `--placement`. A strip seated so far off that it lands near
  the *other* phase is inferred as that placement — the numbering is then
  consistent with where the strip actually is, which is what matters, but
  the advice "take it in the other placement" refers to the inferred one.
- **Two strips per load (§12) is offline-tested only on synthetic
  fixtures** (Test 103 pending): at most four images per strip, slot 2's
  numbering follows its first detected image, an aperture holding pieces of
  both strips on opposite lateral sides is not readable by the detector, and
  an image can be split by a bar in both placements of slot 2.
- **`digitize --film 110` is offline-tested only** (synthetic apertures
  through the real command with a fake scanner); the hardware check is
  Test 102 (§11). Its limits: no mid-run resume, a failed run of one
  placement can only be redone with `--force` (which clears both
  placements), and the diag sidecar of a frame written under the provisional
  stem keeps that stem in its own `output` field. The empty-aperture stop
  rule and `--no-stop-at-empty` are the 135 ones.

## 11. `digitize --film 110` — the two-run roll

*Superseded in part by §13 (2026-10-22): a roll is no longer limited to
two runs, the placement no longer refuses a run, and from the second run
on the numbering comes from the roll's own images, not from the holder's
phase. The protocol below still describes the first run and the two
seatings that deliver every image of a four-image strip; file names are
now `p<k>-f<N>.tiff` per pass instead of `a-`/`b-`.*

*Implemented offline 2026-10-21; hardware check pending (Test 102).*

A 110 strip takes two placements (§3), so a roll takes two `digitize`
runs. Both write into the **same** roll directory; the first run picks the
roll number as usual, the second names it with `--roll N`. Each run is
what `digitize` always is — one load, `--frames` (default 1-4), eject —
and uses the same pipeline, coverage, film-presence check, overscan files,
diag and `cal.npz`; the 110 detector then runs on each verified,
aperture-registered crop (one detector call per aperture, the same
detection, inference, numbering and cropping code as `scan --film 110`).

**Files.** Frame files carry the placement as a prefix so the runs cannot
collide: `a-f1.tiff`, `a-f1-ir.tiff`, `a-f1.overscan.tiff`,
`a-f1-ir.overscan.tiff`, `a-f1.diag.json`, `a-f1.cal.npz`,
`a-f1-preview.tiff` (`b-f…` for placement B). The **photographs** are
`image<N>.tiff` and `image<N>-ir.tiff` (with `--ir`), N = position on the
strip (§5), one file per photograph for the whole roll, written only for
WHOLE images, cropped and padded like `scan`'s, oriented like the frame's
main negative (rotated if asked, never inverted).

**Provisional stem.** When `--placement` is not given, the placement is
known only after the first numbered image is detected, which happens while
the frame's files are being written. Until then a frame is written under the
stem `x-f<N>`; as soon as a frame decides the placement, every `x-` frame so
far is renamed (`digitize.rename_film110_frame`, called in one place in the
command, before the next frame is scanned). If the run ends with nothing
found anywhere (every aperture empty, or only ambiguous images) the files
keep the `x-` stem and the record says `placement: null`. The `.diag.json`
written for a frame under the provisional stem keeps that stem in its own
`output` field; it is not rewritten. A frame whose own detection decides the
placement is renamed before its sidecar is written, so its sidecar is right.

**Never overwritten, one exception.** An existing `image<N>.tiff` is kept
(recorded on the image entry as `refused`, and listed in the roll's
`problems`; the exit code stays 0). The exception is the sag rule (§4): when
the manifest — or this run — shows the existing copy was delivered with
`free_end_near: true` and the new one is not, the supported copy replaces
it and the entry says `replaced_free_end_copy: true`. Everything the scan
hook refuses (an image off the placement's phase, an ambiguous one) is also
refused here, recorded per image, never a failure of the roll.

**Roll record** (a line in `manifest.jsonl`, per run), in addition to the
usual fields:

| Field | Meaning |
|---|---|
| `film` | `"110"` |
| `placement` | `"A"`, `"B"` or `null` (nothing decided it) |
| `placement_given` | true when `--placement` was passed |
| `images` | one entry per numbered image: `number`, `aperture`, `whole`, `split` (`null`/`"trailing"`; `"leading"` only for a two-strip slot 2's first image, §12), `free_end_near`, `main`, `ir` (paths, `null` when not written), `size_mm` (`[along, lateral]` for whole images, else `null`), `replaced_free_end_copy`, and `refused` (only when nothing was written) |
| `problems` | the refusal messages of this run |
| `advice` | `{"other_placement": "B", "images": [2, 4]}`: the numbers the other placement is still needed for — split in this placement and not delivered by any run, or delivered in this run with the strip end free (§4, the other placement's supported copy replaces them); `images` is empty when nothing is missing |

`per_frame` entries are as for 135 (they describe apertures).

**Guard and resume.** The second run on a roll is accepted without
`--force` when the directory holds only the *other* placement's files (or
provisional `x-` files) and the roll's last manifest record is a 110 run.
The same placement twice is refused before any hardware (exit 2) with a
message naming it; if the placement is not given and the strip reads as one
the roll already holds, the run stops at the deciding frame, ejects, exits
5, records the roll failed, and overwrites nothing. A roll with output that
is not a recorded 110 run is refused as for 135. `--force` clears both
placements' files and the photographs (exact names only) and redoes the
roll. `digitize.roll_is_done` for a 110 roll: the last record is `ok`,
found a placement and has an empty `advice.images`, or both placements
have an `ok` record; a run that found nothing is not done. `next_roll` is
unchanged — a new strip is a new roll.

**Test 102 (hardware, pending):** a 110 strip through the two runs with
`--film 110` and no `--placement`; expected: placement inferred correctly
in both, `image<N>.tiff` for every photograph exactly once, the free-end
copy of the last image replaced by the B copy, `advice` right after run A
and empty after run B, `film110_check.py --edge-check` clean on the
products.

## 12. Two strips per load — `digitize --film 110 --strips 2`

*Implemented offline 2026-10-21; first hardware pass the same evening
(Test 103: two strips, placement A twice, placement B not yet run). The
operator's rules on how a strip must lie and three detector/guard rules
below came out of that pass; everything else is still fixture-tested only
(`tests/test_film110_digitize.py`).*

The strip holder has six apertures (pitch 37.947 mm, opening 36 mm); two
110 strips of up to four images each (about 102 mm plus leader) fit at
once, so one load, one calibration and one eject can carry both.

**The operator's rule.**

- **Placement A:** strip 1 pushed against the holder's LEFT end (its image
  1 at aperture 1's left edge — exactly §3's placement A); strip 2 pushed
  against the holder's RIGHT end (its last image ends at aperture 6's right
  edge; its start then lies somewhere in aperture 4, depending on its
  length and leader).
- **Placement B:** both strips moved half a film pitch (12.75 mm) towards
  the middle, strip 1 to the right and strip 2 to the left, by eye.
- **The edge with the small holes faces the aperture's open side**, away
  from the rail the strip lies against. Holes against the rail are hidden
  and the aperture reads "no perforation found" — nothing is numbered
  (Test 103: one strip lay right and one wrong in each pass; the wrong one
  gave nothing). To turn a strip, turn it in its plane (the images then
  run the other way along the holder and the numbers count from the
  other short side); never flip it over.
- **Both film edges stay visible.** A strip pushed under the rail is
  accepted while no more than `film110.RAIL_HIDDEN_MAX_MM` (3.0 mm) of it
  is hidden (`ApertureFind.rail_hidden_mm`; Test 103 measured 0.6–2.1 mm),
  but the picture's own margin on that side is only ~1 mm: beyond it the
  CLI warns that the picture is clipped by the rail and asks for the strip
  to be nudged towards the open side.

**Slots.** Strip 1 occupies apertures 1–3 (*slot 1*), strip 2 apertures 4–6
(*slot 2*): `film110.SLOT_APERTURES = 3`, `film110.slot_of(aperture, strips)`.
A strip of more than four images does not fit beside another one; use
`--strips 1` for it. For a load with only one strip also use `--strips 1`
(a slot that finds no film at all is still recorded, below).

**Numbering.**

- *Slot 1* numbers exactly as a lone strip (§5): phase from the holder's
  left end, the placement inferred from its first numbered image (or given
  with `--placement`).
- *Slot 2* is anchored by the data, not by a fixed phase, because its
  position depends on the strip's length and leader. Its first numbered
  image — the lowest position in the slot — is the strip's image 1, and
  every later image is numbered by its distance from that one in film
  pitches (`film110.image_number_anchored`: `round(distance / 25.5) + 1`).
  A *leading* split in the slot's first aperture (an image that started
  under the bar before aperture 4) is image 1, recorded with `whole:
  false, split: "leading"` and no product; the other placement delivers
  it. Slot 2 has no placement inference of
  its own: its frame files and record carry the run's placement label, and
  `placement_source` says where that came from (`"given"`, `"slot 1
  inferred"`, or `"run order"`). Consequence: if the detector misses the
  strip's real first image in one placement, the numbering of that run is
  shifted by one against the other placement's — the eyes of Test 103 must
  check that the same photograph has the same number in both runs.
- *Leading splits, both slots* (Test 103). An image whose start lies
  within `WHOLE_MARGIN_MM` (0.3 mm) of the aperture's leading edge is a
  leading split (image 1 sat at −0.1 mm, image 4 at +0.03 mm in aperture
  3). It is numbered like any image and dropped only when the previous
  aperture already numbered that image (as its trailing split) — the
  number decides, not the aperture order. A leading *prediction* whose
  visible end edge the data did not confirm (`FrameFind.refined["line1"]`
  false) is the strip's first hole predicting an image before itself, i.e.
  the leader: ignored, not a problem (every real leading image of Test 103
  had its end edge confirmed).
- *Run order.* When slot 1 numbered nothing and `--placement` is not given
  (slot 1 empty), the run is labelled by the manifest: `"A"` when roll N has
  no 110 record yet, else `"B"` (`placement_source: "run order"`).

**Two rolls per run.** Roll N holds slot 1, roll N+1 slot 2 (`N` = `--roll`
or the next free number), each with its own directory, its own frame files
(the HOLDER aperture number stays in the name: roll N+1 has `a-f4.tiff`,
`a-f5.tiff`, `a-f6.tiff`), its own `image<K>.tiff` products (K counted per
strip) and its own manifest record: `frames` = that slot's apertures, plus
`strips: 2`, `slot`, `companion_roll`, `placement_source`, `film_found`
(docs/driver-design.md). The second run is `digitize --film 110 --strips 2 --roll
N` and fills both rolls' other placement. `--frames` defaults to `1-6`; a
given `--frames` must contain at least one aperture of each slot, else exit
2 before any hardware. The guard is applied to BOTH rolls before hardware
(a roll with records must be half of a two-strip pair with the other as its
companion; the same-placement-twice and finished-roll refusals of §11 apply
to each); `--force` clears both rolls.

A slot that finds no film at all, or film but no image, still gets its
record — `status: "ok"`, `images: []`, the placement as labelled,
`film_found` as seen. Such a roll is never "done": a roll without a single
numbered image is not finished whatever its records say about placement
(Test 103: slot 2's strip with its holes against the rail was recorded
"ok, placement A, nothing left" and the roll counted as done, so the other
placement's run was refused), and the other placement's run is accepted on
it. Its number is still not reused. One-strip loads should use `--strips
1`, which leaves no empty roll behind.

**Empty apertures.** The stop rule of §11 is per slot: an empty aperture
after film in slot 1 skips the rest of slot 1's apertures but never stops
the run (slot 2 may hold film); in slot 2 it skips the rest of slot 2
(`skipped_frames`/`stop_reason` on that roll's record). Empty apertures
before a slot's first film never stop anything. `--no-stop-at-empty` scans
everything. `scan` is unchanged — it scans one strip.

**Where the strips meet (what the code does about it).** Strip 1's last
bar and strip 2's first image can fall in the same two apertures:

- In slot 1's last aperture (3), a *trailing* split — an image running
  under the bar towards slot 2 — is the next strip's leading image: a
  strip of up to four images ends before that bar in either placement. It
  is reported ("the next strip's leading image, counted there"), never
  numbered in slot 1, never decides the placement, and is not a problem.
  The detector also predicts an image *before* the next strip's first hole,
  where only strip 1's trailing leader or the gap lies; it is recognised
  by sharing that hole with the split and dropped (reported).
- In slot 2's first aperture (4), the same first-hole prediction can put a
  ghost image before the strip's film begins. An image predicted more than
  `film110.FILM_START_TOLERANCE_MM` (1.0 mm) before a free film start
  (`ApertureFind.film_line_start`) is dropped; otherwise it would become the
  anchor and shift every number of the strip by one.
- Not handled, because the detector cannot tell (and the spec forbids
  hacking around it): an aperture that holds pieces of BOTH strips lying on
  opposite lateral sides — a strip 1 leader against the lower rail and a
  strip 2 start against the upper one in the same aperture. The detector
  reads one film band per aperture and reports "no perforation found" or
  nothing for such an aperture; nothing is lost that was needed in the
  fixtures (the images of both strips lie in other apertures or are delivered
  by the other placement), but that is not guaranteed in general. A
  ghost-before-first-hole *inside* aperture 4 that is not before a free film
  start (the film continues from aperture 3) is geometrically the same as
  a real leading split of image 1; what separates them is the end edge —
  a real image has one where the prediction says, the leader has none
  (the leading-split rule above).
- A strip's images that are split in BOTH placements stay undelivered: slot
  1's phase is fixed by the holder, so §3's guarantee (every image whole in
  A or B) holds there; slot 2's phase depends on the strip's length and
  leader, and with an unlucky phase (the fixtures have one) an image is cut
  by a bar in both. It stays on the roll's `advice`; the roll still counts
  as done once both placements have an `ok` record.

**Either rail.** The strips may lie against either rail, and the two may
differ: nothing in the slot logic, numbering, cropping or products assumes
which one. The detector handles a film band on either lateral side of the
aperture (`test_perforated_edge_on_high_side`) and both hole orientations
(`test_reversed_orientation_hole_after_image`); the fixtures of the
two-strip suite run all four combinations (`lo/hi`, `hi/lo`, `lo/lo`,
`hi/hi`) and require identical numbering. It works as long as the perforated
edge faces the opening; with the perforations hidden under a rail the
detector reports "perforations hidden" (§10) and no 110 products are
written for that aperture.

**Limits.** At most four images per strip; offline-tested only; no real
data; the hardware check is Test 103 — a two-strip load through both
placements, eyes on: the same photograph has the same number in A and B for
both strips, no ghost image in apertures 3 and 4, no image shifted by the
anchor, `film110_check.py --edge-check` clean on the products.

## 13. Free passes — any seating, slide or turn (2026-10-22, offline)

*Implemented offline after Test 103 (Christian's decision: "utred fri
placering hellre än mm-instruktion"); hardware check pending (Test 104).*

**The contract.** Every `digitize --film 110 [--strips 2] --roll N` run on
an existing roll is a *pass* (`pass` in the record; frame files
`p1-f…`, `p2-f…`, …) and is accepted as long as the roll is not done. The
placement is read from the data and recorded (`A`, `B` or `free`) but never
refuses a run; the same seating twice is allowed and changes nothing. Per
photograph the best copy wins — whole and supported, then whole with the
strip end free, then split — and a copy that is not better is `kept`
(information on the image entry, not a problem). A roll is **done** when
no number has only been seen split (`advice.missing` empty) and no
delivered copy has the strip end free (`advice.free_end` empty); the
operator may stop earlier. `advice.suggest` is a measured recommendation
("image 2: move 5.7 mm further in", "image 4: no seating puts the strip
end under plastic"), advice not a requirement; the old `advice.images` /
`other_placement` keys stay one release for the application. Exit codes
are unchanged (0, 2 refused before hardware, 4 coverage, 5 numbering could
not be established — nothing written, the frame files kept).

**Numbering from pass 2: recognise, never guess.** Pass 1 numbers as §5/§12.
Every later pass holds its sightings until the slot's last aperture and
then matches them against the newest earlier record of the roll. The film
is periodic, so positions alone cannot tell a move of *d* from one of
*d* ± 25.5 mm, and a strip turned 180° in its plane lies on the same grid
as one that slid: positions are therefore never trusted on their own.
Instead every sighting — whole or split — carries where it lies
(`position_mm`), which part of the image it shows (`extent_mm`, mm from
the image's own start) and its crop bounds; the earlier pass's sightings
are re-read from its frame files, and each (new, old) pair that overlaps
by at least 6 mm is compared on that part — as it is (SLIDE by *d* =
*p* − *r*) and rotated 180° (TURNED about *c* = *p* + *r*). Pairs that
agree on *d* (or *c*) form a hypothesis; what is physically possible (every
recorded image must still lie in the slot after the move) and the strip's
own free end, when seen in both passes (`film_start_mm` / `film_end_mm`),
prune the hypotheses; the best remaining one wins only with a mean
correlation of at least 0.5 and 0.15 above every other. Otherwise exit 5.
Test 103 measured 0.74–0.96 for the same photograph against at most 0.52
for different ones. A sighting predicted before the roll's first image
that is not whole is the leader (ignored); a *whole* image there is a
problem (the earlier pass missed the strip's first image). Products of a
turned pass are rotated back, so every `image<N>.tiff` of a roll has pass
1's orientation; `orientation` (`same`/`turned`, cumulative against pass
1) and `numbering` (`protocol`/`content`) are in the record.

**The operator.** Holes towards the open side, both film edges visible
(§12), then: *place the strips so that in two passes you maximise the
support at the edge, and make sure every image gets one pass without a
bar across it* (the application's instruction, same words). Between passes
a strip may be slid by any amount or turned end for end in its plane;
never flipped over. A pass whose images cannot be recognised against the
earlier ones stops without writing: seat the strip so that something seen
before is seen again.

**Detector note.** The plausibility test that drops a window of clear film
(bright AND flat) now references the brightest film in the aperture
(`CLEAR_BASE_PERCENTILE` 99, was 90): an aperture holding only printed
border and pictures had read its brightest picture as the clear base and
dropped a bright, flat picture. Identical results on all 24 archived real
110 apertures.
