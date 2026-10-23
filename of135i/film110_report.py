"""The 110 digitize report in holder language (docs/film-110.md section 15).

Everything here is pure geometry and text over data the driver has already
established by other means. Nothing in this module numbers or identifies a
photograph: identity stays with `film110.decide_orientation`. The joint
placement suggestion (`plan_next_pass`) is content-agnostic arithmetic over
positions that were identified by content elsewhere.

CONVENTION (Christian's wording, binding): apertures 1-6 and bars 1-5; bar k
lies between aperture k and aperture k+1; the end walls are "the end wall at
aperture 1" and "the end wall at aperture 6". Directions are always "towards
aperture 1" / "towards aperture 6". Distances are image lengths with mm in
brackets. Position 0 mm is aperture 1's start and positions grow towards
aperture 6 (`film110.global_position_mm`).
"""
from __future__ import annotations

import math

from .holder import FILM_110, STRIP

# The ONE place that says which physical holder end aperture 1 is. The owner
# has not yet confirmed it; flip this text (and nothing else) if it is the
# other end. All legends are built from it.
APERTURE_1_END = "the holder end that enters the scanner first"

N_APERTURES = STRIP.frames
SUGGEST_MARGIN_MM = 2.0            # a rescued image keeps this far from both aperture edges
SUGGEST_COMFORT_MM = 5.0           # margins above this count as equal when two placements are compared (else a 50 mm move beats a 15 mm one for 1 mm more margin)
SUGGEST_RANGE_MM = 60.0
SUGGEST_STEP_MM = 0.5
FRONT_TOLERANCE_MM = 1.0           # = film110.FILM_START_TOLERANCE_MM: a frame position may begin this far before the strip's end


def legend() -> str:
    """The one-line convention every printed report starts with."""
    return ("Holder: apertures 1-6 and bars 1-5 are counted from aperture 1, "
            f"{APERTURE_1_END}; bar N lies between aperture N and aperture N+1; "
            "the end walls are at aperture 1 and aperture 6; directions are "
            "always \"towards aperture 1\" or \"towards aperture 6\".")


# ------------------------------------------------------------------ geometry


def aperture_span(k: int) -> tuple:
    """(start, end) mm of aperture k (1-based) along the holder."""
    return ((k - 1) * STRIP.pitch_mm, (k - 1) * STRIP.pitch_mm + STRIP.aperture_mm)


def plastic_spans() -> list:
    """[(kind, index, start, end)] of the plastic between and around the
    apertures, in holder order: the end wall at aperture 1, bars 1-5, the
    end wall at aperture 6."""
    wall = STRIP.pitch_mm - STRIP.aperture_mm
    out = [("end_wall", 1, -wall, 0.0)]
    for k in range(1, N_APERTURES):
        a1 = aperture_span(k)[1]
        out.append(("bar", k, a1, k * STRIP.pitch_mm))
    out.append(("end_wall", N_APERTURES, aperture_span(N_APERTURES)[1], N_APERTURES * STRIP.pitch_mm))
    return out


def holder_limits() -> tuple:
    """The outer limits strip material can reach, mm."""
    return plastic_spans()[0][2], plastic_spans()[-1][3]


def locate_window(start_mm: float, length_mm: float = None) -> dict:
    """Where an image window [start, start+length] lies in holder terms:
    {"kind": "aperture", "index": k} when it lies wholly inside aperture k,
    else the plastic span that splits it (the first one it overlaps)."""
    length = FILM_110.image_mm[0] if length_mm is None else length_mm
    end = start_mm + length
    for k in range(1, N_APERTURES + 1):
        a0, a1 = aperture_span(k)
        if start_mm >= a0 and end <= a1:
            return {"kind": "aperture", "index": k}
    for kind, idx, p0, p1 in plastic_spans():
        if p1 > start_mm and p0 < end:
            return {"kind": kind, "index": idx}
    return {"kind": "aperture", "index": 1 if start_mm < 0 else N_APERTURES}


def where_text(where: dict) -> str:
    if where["kind"] == "aperture":
        return f"aperture {where['index']}"
    if where["kind"] == "bar":
        return f"bar {where['index']}"
    return f"the end wall at aperture {where['index']}"


def where_of_entry(entry: dict) -> dict:
    """Holder location of one manifest `images` entry (or a blank): a whole
    image (and a blank) is in its aperture; a split one is at the bar or end
    wall that splits it."""
    k = int(entry["aperture"])
    split = entry.get("split")
    if entry.get("whole") is False or split:
        if split == "leading":
            return ({"kind": "end_wall", "index": 1} if k == 1
                    else {"kind": "bar", "index": k - 1})
        return ({"kind": "end_wall", "index": N_APERTURES} if k == N_APERTURES
                else {"kind": "bar", "index": k})
    return {"kind": "aperture", "index": k}


# -------------------------------------------------------------------- amounts


def amount_text(shift_mm: float) -> str:
    """"half an image (about 13 mm)": the move in quarter image lengths."""
    pitch = FILM_110.pitch_mm
    q = round(abs(shift_mm) / pitch * 4) / 4
    mm = int(round(q * pitch))
    whole = int(q)
    frac = q - whole
    names = {0.25: "a quarter", 0.5: "half", 0.75: "three quarters"}
    if q == 0:
        return "no distance (about 0 mm)"
    if whole == 0:
        txt = f"{names[frac]} of an image" if frac != 0.5 else "half an image"
    elif frac == 0:
        txt = "one image" if whole == 1 else f"{whole} images"
    else:
        noun = "an image" if whole == 1 else f"{whole} images"
        txt = f"{noun} and {names[frac]}" if frac != 0.5 else f"{noun} and a half"
    return f"{txt} (about {mm} mm)"


def quarter_images(shift_mm: float) -> float:
    return round(abs(shift_mm) / FILM_110.pitch_mm * 4) / 4


# ---------------------------------------------------------------- pass report


def _outcome(e: dict) -> str:
    if e.get("whole") is not True or e.get("refused"):
        return "not_delivered"
    if e.get("kept"):
        return "kept"
    if e.get("replaced_free_end_copy"):
        return "better"
    if e.get("main"):
        return "new"
    return "not_delivered"


def _state(e: dict) -> str:
    if e.get("whole") is not True:
        return "split"
    return "whole_free_end" if e.get("free_end_near") else "whole_supported"


def build_pass_report(images: list, blanks: list) -> list:
    """One entry per sighting of this pass, ordered along the holder from
    aperture 1: {"number", "where", "state", "outcome", "located_by",
    "position_mm"} (+ "uncertain" when the number is a guess). A blank frame
    position has state "blank" and never a product."""
    rows = []
    for e in images:
        if e.get("number") is None:
            continue
        row = {"number": e["number"], "where": where_of_entry(e),
               "state": _state(e), "outcome": _outcome(e),
               "located_by": e.get("located_by") or "holes",
               "position_mm": e.get("position_mm")}
        if e.get("uncertain"):
            row["uncertain"] = True
        rows.append(row)
    for b in blanks:
        rows.append({"number": b["number"],
                     "where": {"kind": "aperture", "index": int(b["aperture"])},
                     "state": "blank", "outcome": "not_delivered",
                     "located_by": b.get("located_by") or "frame",
                     "position_mm": b.get("position_mm")})
    rows.sort(key=lambda r: (r["position_mm"] if r["position_mm"] is not None else 1e9))
    return rows


_OUTCOME_WORDS = {"new": "delivered", "better": "delivered, better than the earlier copy",
                  "kept": "the earlier copy was kept", "not_delivered": "not delivered"}


def format_pass_report(report: list) -> list:
    """The report as lines, grouped per aperture / bar / end wall in holder
    order."""
    groups: dict = {}
    for r in report:
        w = r["where"]
        key = (w["kind"], w["index"])
        groups.setdefault(key, []).append(r)

    def sort_key(key):
        kind, idx = key
        if kind == "aperture":
            return 2 * idx
        if kind == "bar":
            return 2 * idx + 1
        return -1 if idx == 1 else 2 * N_APERTURES + 2

    lines = []
    for key in sorted(groups, key=sort_key):
        parts = []
        for r in groups[key]:
            unc = " (number uncertain)" if r.get("uncertain") else ""
            if r["state"] == "blank":
                parts.append(f"frame position {r['number']} is blank (no picture, no file)")
            elif r["state"] == "split":
                parts.append(f"image {r['number']} is split here{unc} ({_OUTCOME_WORDS[r['outcome']]})")
            else:
                free = ", strip end free" if r["state"] == "whole_free_end" else ""
                parts.append(f"image {r['number']} whole{free}{unc} ({_OUTCOME_WORDS[r['outcome']]})")
        lines.append(f"  {where_text({'kind': key[0], 'index': key[1]})}: " + "; ".join(parts))
    return lines


# ------------------------------------------------------- frame positions (E)


def strip_bounds(sightings: list, ends: tuple, turned: bool,
                 pitch: float = None, length: float = None,
                 tol: float = FRONT_TOLERANCE_MM) -> tuple:
    """(m_front, m_rear): the first and last frame position NUMBER the strip
    holds, from the strip ends seen in THIS pass; None for an end not seen.

    `sightings` = [(number, start_mm)] of this pass (images and blanks, the
    window start in this pass's holder coordinates), `ends` = (low, high)
    strip end positions in the same coordinates (None = not seen), `turned`
    = the strip lies turned against pass 1 (numbers fall along the holder).
    A frame position exists when its whole window lies between the ends,
    give or take `tol`. The front end is the one at the low numbers: the low
    holder end for a strip as in pass 1, the high one when turned. Pure
    geometry over positions the numbering has already fixed."""
    pitch = FILM_110.pitch_mm if pitch is None else pitch
    length = FILM_110.image_mm[0] if length is None else length
    e_lo, e_hi = ends
    eps = 1e-6
    if not sightings:
        return None, None

    def nearest(x):
        return min(sightings, key=lambda t: abs(t[1] - x))

    front = rear = None
    if not turned:
        if e_lo is not None:
            n, p = nearest(e_lo)
            front = math.ceil(n + (e_lo - tol - p) / pitch - eps)
        if e_hi is not None:
            n, p = nearest(e_hi)
            rear = math.floor(n + (e_hi + tol - length - p) / pitch + eps)
    else:
        if e_hi is not None:
            n, p = nearest(e_hi)
            front = math.ceil(n + (p + length - e_hi - tol) / pitch - eps)
        if e_lo is not None:
            n, p = nearest(e_lo)
            rear = math.floor(n + (p - e_lo + tol) / pitch + eps)
    return front, rear


def window_of(number: int, ref: tuple, turned: bool) -> float:
    """Start (mm, this pass's coordinates) of the window of `number`, on the
    film grid through the sighting `ref` = (number, start_mm)."""
    n, p = ref
    s = -1.0 if turned else 1.0
    return p + s * (number - n) * FILM_110.pitch_mm


# ------------------------------------------------------------- joint suggestion


def _inside_limits(points, shift, lo, hi) -> bool:
    for x in points:
        y = x + shift
        if y < lo and not (x < lo and shift > 0):
            return False
        if y > hi and not (x > hi and shift < 0):
            return False
    return True


def plan_next_pass(known: dict, needed: dict, ends: list,
                   end_beyond: dict, now: dict, extra: dict = None) -> "dict | None":
    """ONE placement for the next pass, in holder mm (slides the whole strip).

    known      {number: start_mm} of every frame position the strip is known to
               hold (measured in this pass, or on the film grid through one);
    extra      {number: start_mm} of needed positions never seen (grid only;
               they do not limit the shift);
    needed     {number: kind} the positions still to be delivered, kind in
               "split" | "free_end" | "unseen";
    ends       strip ends seen in this pass (mm);
    end_beyond {number: mm} for a "free_end" image: the strip end beyond it;
    now        {number: text} where it lies now ("split by bar 1").

    Search shifts in [-60, +60] mm, step 0.5, that keep every known window and
    seen end inside the holder. A needed image is rescued when its shifted
    window lies inside one aperture with at least SUGGEST_MARGIN_MM to both
    aperture edges; a free-end image only if, in addition, the strip end
    beyond it lies on plastic. Most rescued, then largest minimum margin, then
    smallest |shift|. Returns the `advice.next` dict (+ "text") or None."""
    if not needed:
        return None
    L = FILM_110.image_mm[0]
    lo, hi = holder_limits()
    windows = dict(extra or {})
    windows.update(known)
    points = []
    for p in known.values():
        points += [p, p + L]
    points += list(ends)
    best = None
    steps = int(round(SUGGEST_RANGE_MM / SUGGEST_STEP_MM))
    for i in range(-steps, steps + 1):
        s = i * SUGGEST_STEP_MM
        if not _inside_limits(points, s, lo, hi):
            continue
        rescued, margins, targets = [], [], []
        for n, kind in sorted(needed.items()):
            p = windows.get(n)
            if p is None:
                continue
            x0, x1 = p + s, p + s + L
            hit = None
            for k in range(1, N_APERTURES + 1):
                a0, a1 = aperture_span(k)
                m = min(x0 - a0, a1 - x1)
                if m >= SUGGEST_MARGIN_MM:
                    hit = (k, m)
                    break
            if hit is None:
                continue
            if kind == "free_end":
                e = end_beyond.get(n)
                span = None if e is None else next(
                    ((kd, ix) for kd, ix, a, b in plastic_spans() if a <= e + s <= b), None)
                if span is None:
                    continue
                rescued.append(n)
                margins.append(hit[1])
                targets.append({"number": n, "aperture": hit[0],
                                "end_on": (f"bar {span[1]}" if span[0] == "bar"
                                           else f"end wall at aperture {span[1]}")})
                continue
            rescued.append(n)
            margins.append(hit[1])
            targets.append({"number": n, "aperture": hit[0]})
        if not rescued:
            continue
        key = (len(rescued), min(min(margins), SUGGEST_COMFORT_MM), -abs(s))
        if best is None or key > best[0]:
            best = (key, s, rescued, targets)
    if best is None:
        return None
    _key, s, rescued, targets = best
    remaining = sorted(n for n in needed if n not in rescued)
    direction = "towards aperture 6" if s > 0 else "towards aperture 1"
    bits = []
    for t in targets:
        n = t["number"]
        a0, a1 = aperture_span(t["aperture"])
        centred = abs((windows[n] + s + L / 2) - (a0 + a1) / 2) <= 2.0
        bit = (f"image {n} ({now.get(n, 'not yet delivered')}) lies "
               f"{'in the middle of' if centred else 'inside'} aperture {t['aperture']}")
        if t.get("end_on"):
            bit += (f" with the strip's end against the {t['end_on']}"
                    if t["end_on"].startswith("end wall")
                    else f" and the strip's end rests on {t['end_on']}")
        bits.append(bit)
    if s == 0:
        text = ("Next pass: keep the strip where it is; " + " and ".join(bits) + ".")
    else:
        text = (f"Next pass: slide the strip {amount_text(s)} {direction}, so that "
                + " and ".join(bits) + ".")
    if remaining:
        text += " Another pass will then still be needed for " + \
                ", ".join(f"image {n}" for n in remaining) + "."
    return {"shift_mm": round(float(s), 1), "direction": direction,
            "images": quarter_images(s), "rescues": rescued, "targets": targets,
            "remaining": remaining, "another_pass_after": bool(remaining),
            "text": text}
