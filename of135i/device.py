"""Scan sequencing for the Plustek OpticFilm 135i (GL126).

`Scanner` drives the phase list in tables.py (generated from the
verified 3600 dpi single-frame trace, see driver/gen_tables.py and
driver-design.md's "Scan sequence" section) over a UsbIo transport,
replaying each phase's FULL verbatim op stream -- not just its
register batches -- and injecting the calibration.py values at the
documented injection points.

2026-08-30 rework: hardware A/B testing proved that executing only
the register batches + a poll subset (as this module used to) is
insufficient -- the verbatim replayer (replay_trace.py), which
executes the full op stream (all single control reads, exact op
ordering, captured dt pacing), measures correct calibration levels
where the slimmed execution saturates. Some of the skipped reads/
pacing evidently carry real state transitions on the device (lamp
settling etc.), so `_exec_ops` below reproduces replay_trace.py's
executor semantics (see its module docstring) exactly, phase by
phase, plus the hardware-discovered engine-busy wait after every
execute pulse.

Hardware safety (safety.py, docs/hardware-safety.md): every public
method that can write to the scanner -- initialize(), cold_init(),
scan(), eject(), home(), park_semantic(), load_magazine() -- runs as
a *hardware operation* (`_operation()`). The first operation of a
session verifies the start state (reg 0x01 must read 0x22, or 0x00
for the cold-init path only) before its first write; an exception
escaping any operation, KeyboardInterrupt included, marks the
session failed and no recovery command of any kind is sent
afterwards. The low-level executor `_exec_ops()` refuses to run
outside an operation, so nothing can bypass this by calling it
directly. Read-only methods (is_magazine_present, check_start_state)
never write.
"""

from __future__ import annotations

import importlib
import logging
import math
import os
import time
from contextlib import contextmanager
from datetime import datetime, timezone

import numpy as np

from . import calibrate, diag, holder, image as image_mod, safety, tables, tables_base, tables_ir
from .safety import (
    OperationNotAllowedError, UnejectableStateError, SessionState, StartState, UnsafeStartStateError,
)
from .tables import Op, Phase
from .usbio import EP_BULK_IN, EP_BULK_OUT, InterruptOverflowError, Of135iError, UsbIo

log = logging.getLogger("of135i")

# Resolutions with a compiled dual-light (alternating IR/visible line)
# phase set: 3600 -> tables_ir (trace 04), the others -> tables_dpi<N>
# (the 2026-09-02 QuickScan captures, see tools/gen_tables.py's
# compile_dual). 3600 is the only resolution that also has a plain
# (visible-only) phase set (tables.py).
DUAL_DPIS = (600, 1200, 2400, 3600, 7200)
SUPPORTED_DPIS = DUAL_DPIS


def dual_tables(dpi: int):
    """The dual-light table module for `dpi` (imported on first use --
    the 7200 dpi module alone is a few thousand lines)."""
    if dpi == 3600:
        return tables_ir
    if dpi not in DUAL_DPIS:
        raise Of135iError(f"no scan profile for {dpi} dpi (have {', '.join(map(str, SUPPORTED_DPIS))})")
    return importlib.import_module(f".tables_dpi{dpi}", __package__)


def _tables_for(dpi: int, ir: bool):
    """Phase-set module for a scan: the plain 3600 dpi set unless IR is
    requested or the resolution only exists as a dual-light capture."""
    if dpi == 3600 and not ir:
        return tables
    return dual_tables(dpi)

# replay_trace.py's pacing: sleep a captured inter-op gap when it
# exceeds this threshold, capped so a real outlier can't stall a run.
_PACE_THRESHOLD = 0.05
_PACE_CAP = 2.0


_has_execute_pulse = safety.has_execute_pulse


# ---------------------------------------------------- per-scan shading diag

def _parse_shading_offsets(payload: bytes) -> np.ndarray:
    """Recover the per-pixel offset field of a calibrate.shading_table()/
    shading_table2()-style payload (126 payload + 2 zero trailer
    (offset, gain) u16 LE pairs per full 512 B block, final block
    unpadded), mirroring tests/test_calibrate.py's _parse_shading_blocks.
    Returns an (n_pixels, 3) float64 array (pixel-interleaved RGB, same
    order as the image line format). Diagnostics only -- does not feed
    back into calibrate.py or the scan flow."""
    offsets: list[int] = []
    i, n = 0, len(payload)
    while i < n:
        remaining = n - i
        if remaining >= 512:
            block, n_payload = payload[i:i + 512], 126
            i += 512
        else:
            block, n_payload = payload[i:i + remaining], remaining // 4
            i += remaining
        pairs = np.frombuffer(block, dtype="<u2").reshape(-1, 2)
        offsets.extend(pairs[:n_payload, 0].tolist())
    arr = np.array(offsets, dtype=np.float64)
    if arr.size == 0 or arr.size % 3 != 0:
        raise ValueError(f"shading payload does not hold a whole number of RGB pixels ({arr.size} values)")
    return arr.reshape(-1, 3)


def _shading_stats(payload: bytes) -> dict:
    """Per-scan diagnostics summary of one shading-table upload payload:
    per-channel offset mean/min/max when the block format parses
    cleanly, else just the payload length (see diag.py/device.py's
    module docs -- this never raises, and never touches calibrate.py)."""
    try:
        px = _parse_shading_offsets(payload)
        return {
            "per_channel_offset": [
                {"mean": float(px[:, ch].mean()), "min": float(px[:, ch].min()), "max": float(px[:, ch].max())}
                for ch in range(3)
            ],
        }
    except Exception as e:
        return {"len_bytes": len(payload), "note": f"could not parse per-channel stats: {e}"}


# Lamp warmup retry: if gain_codes returns all-maxed (0x3F) on every
# channel, the lamp is likely cold (insufficient white-line levels for
# meaningful calibration).  Wait and re-measure up to this many times.
# Lamp warmup (see _gain_with_warmup): after a cold start the first
# white-line measurement can be so dark that every channel's gain code
# clips at maximum (0x3f). Test 11 (2026-09-04) showed 3 x 5 s was not
# enough; Tests 17-21 (2026-09-05) showed the load flow's ~60 s of lamp
# time makes the first scan fine. The real warmup curve has NOT been
# measured. The measurement IS the probe: a scan from bare power-on with
# a generous `--warmup-budget` records every white-line measurement with
# its time in the sidecar (warmup_peak_history / warmup_measurement_
# times_s) and completes once the lamp is stable -- no separate tool,
# no extra sequence. Until that has been run the budget below is a
# bounded default, not a measured value. Set Scanner.warmup_budget_s to
# change it per run (CLI: --warmup-budget).
_WARMUP_BUDGET_S = 60.0       # total wall-clock budget, hard upper bound
_WARMUP_INTERVAL_S = 5.0      # between white-line measurements
_WARMUP_STABLE_PCT = 3.0      # consecutive peaks must agree within this

# Floor under a NON-strict poll's budget (3x the captured duration,
# _poll_one). Was 1.0 s from 2026-09-02 to 2026-10-20. Production
# telemetry (docs/production-telemetry-2026-10.md section 2.3, 652
# frames): 93 % of frames timed out one to seven of these polls, each at
# exactly the floor, on the three documented benign signatures and
# nothing else, never followed by a failure; when the poll did match it
# matched within the phase's first ~10 ms. The accepted-word rules in
# _poll_one (data-ready bit, state class, reg 0x32 mask) are what end a
# poll early; this floor only bounds the cases none of them covers.
# Strict completion waits (motor moves) are NOT under this floor.
# Hardware check: docs/test-log.md Test 100.
NONSTRICT_POLL_FLOOR_S = 0.3
# Per-scan cap on recorded poll settle entries (diag "poll_settle").
_POLL_SETTLE_CAP = 48

# Calibration acquisition validity. In these phases every bulk-IN chunk
# routed into a measurement buffer must return exactly the captured chunk
# length; a short chunk raises CalibrationError right there, before the
# phase's next op, so the session is FAILED and nothing further is sent.
# Same rule as the SANE op-program runner, which fails closed with
# ShortBulk on these phases (tools/gen_sane_tables.py docstring,
# tests/test_sane_ops.py test_short_bulk_fails_closed). Before this rule
# a short dark/white read silently fed offset_codes()/gain_codes() a
# partial measurement (reshape(-1, 3) accepts any whole pixel count) and
# a short shading read kept writing the rest of its phase before the
# fixed reshape failed. Image (scan) and drain reads are NOT covered:
# their tolerance is unchanged. The dark_b residual substitution (Test
# 32) and the --dark-fallback option are content rules on a complete
# buffer and are unchanged.
CAL_ACQUISITION_PHASES = frozenset({
    "cal_dark_a", "cal_dark_b", "cal_white",
    "cal_gain_check_a", "cal_gain_check_b",
    "cal_shading_measure", "cal_shading_verify",
})

# Calibration read-back "acquisition not observed" guard (Scanner.
# cal_readback_guard). At the read-back sites the capture settled the
# extended status word with the motor-enabled bit (0x01) set and the
# buffer-empty bit (0x40) clear. A poll there that has already timed out
# under its unchanged budget, whose last well-formed reply shows the
# motor-enabled bit clear and the buffer-empty bit set, means the engine
# stopped with nothing buffered: the measurement the next bulk read
# collects was most likely never acquired. That reading is an inference
# from hardware logs and vendor-app traffic, so the default only records
# it ("log": acq_not_observed on the poll_settle entry, wire unchanged);
# "fail" (opt-in) raises CalibrationError before the phase's next
# transfer; "off" is the previous behaviour exactly. The guard never
# accepts anything and never changes a poll budget.
# "fail" also refuses on the calibration integrity telemetry (below): see
# Scanner._cal_integrity_refuse. EXPERIMENTAL, unverified on hardware.
CAL_READBACK_GUARD_MODES = ("off", "log", "fail")
CAL_READBACK_GUARD_DEFAULT = "log"

# Calibration stale-read telemetry (Scanner.cal_integrity_telemetry,
# default on; diagnostics only, see Scanner._cal_note_*). Per calibration
# acquisition (one execute pulse in a CAL_ACQUISITION_PHASES phase) the
# driver records, from transfers it already makes plus time.perf_counter
# (no poll loop uses that clock): the gap from the previous acquisition's
# SCAN-clear write to this execute pulse, the previous acquisition's
# existing post-clear reads of register 0x100 (bit 0: data enable) and
# register 0x101 (bit 0: motor enable), the time from the execute pulse
# to the end of the last bulk read, and the read-back signature. In
# hardware logs, calibration reads that returned stale data were re-armed
# while the previous acquisition was still running and completed within
# a few milliseconds of the execute pulse; fresh reads of the same size
# took tens of milliseconds. CAL_SERVED_FAST_S separates the two; it is
# INFERRED from those logs (stale at most ~3.5 ms, fresh at least ~24 ms),
# not a measured device limit. Nothing here sends, delays or decides.
CAL_SERVED_FAST_S = 0.010

# Calibration stop gate (Scanner.cal_wait_scanstop). EXPERIMENTAL,
# unverified on hardware, opt-in; it delays the next calibration execute
# pulse, so it is motor-adjacent. In hardware logs the stale calibration
# reads (see CAL_SERVED_FAST_S) were re-armed while the previous
# acquisition still showed data-enable (register 0x100 bit 0) and
# motor-enable (register 0x101 bit 0) set; vendor-app traffic waits for
# both to clear first. "enforce": after the SCAN-clear (register 0x01
# written with bit 0 clear) that closes an execute pulse in a
# CAL_ACQUISITION_PHASES phase, and before the next OUT transfer of any
# kind, poll register 0x100 until bit 0 is clear, then register 0x101
# until bit 0 is clear. Only a 2-byte reply with the 0x55 acknowledge
# counts. Both waits share ONE deadline, NONSTRICT_POLL_FLOOR_S (no new or
# longer budget); at that deadline CalibrationError is raised before any
# further write. "off" (default): no read, sleep or clock call is added.
CAL_WAIT_SCANSTOP_MODES = ("off", "enforce")

# Calibration buffer-empty gate (Scanner.cal_wait_bufempty). EXPERIMENTAL,
# unverified on hardware, opt-in. At the calibration read-back polls
# (extended status word, wIndex 0x0122) the capture settled with the
# buffer-empty bit (0x40) clear; the non-strict poll also ends on the
# motor-enable bit alone or on a state-class match, so the bulk read can
# start before anything was buffered. Vendor-app traffic waits for the
# buffer-empty bit to clear first. "enforce": at such a site whose captured
# word is a well-formed 2-byte word with bit 0x40 clear, inside a
# CAL_ACQUISITION_PHASES phase, only a 2-byte reply with the 0x55
# acknowledge and bit 0x40 clear ends the poll. The budget is the poll's
# existing one; at its deadline CalibrationError is raised before the bulk
# read. "off" (default): today's rules exactly.
CAL_WAIT_BUFEMPTY_MODES = ("off", "enforce")


# Semantic PARK (see park_semantic() and docs/replay-analysis.md's
# "Conversion order" step 1): timeout and poll interval for the two
# condition waits that replace the captured trace's pacing.
#
# Raised 2026-09-17 when semantic became the CLI/library default (was
# 15/30 s, sized only from the plain-3600 frame-1-4 parks). Test 61
# (docs/test-log.md, hardware, 2026-09-10) measured full park duration
# under verbatim pacing across every table: dual 600/1200/2400/7200
# ~65-67 s, IR3600 ~14 s, plain 3600 ~5 s. Semantic has never been
# hardware-timed on dual/IR (only plain, Test 52-77's SANE runs and the
# 3.7-3.8 s plain figure above), so the split between Wait A (carriage-
# home) and Wait B (status-word-idle after the return) for those tables
# is unknown -- each budget below is sized to cover the longest observed
# total (dual's ~67 s) ALONE, so a timeout is never spurious regardless
# of which wait ends up carrying most of that time. This can only
# lengthen the fail-closed bound, never shorten it (Test 52-77's plain
# parks finished in 3.7-3.8 s, far inside either budget).
_PARK_WAIT_TIMEOUT = 90.0
# Wait B (status word idle after the carriage return): sized the same way
# as Wait A above, plus headroom over the dual ~65-67 s maximum.
_PARK_WAIT_B_TIMEOUT = 100.0
# Both budgets above were set from parks after frames 1-4 on the plain
# table's own timing; dual/IR headroom is carried by the raised constants
# themselves (comment above), not by this scale. A park returns the
# carriage from wherever the frame was, so the return grows with the
# frame's distance from home: frame 6 is 1.55x frame 4. The two waits are
# therefore ALSO scaled by the same linear factor POSITION uses
# (position_timeout_scale of the frame last positioned to), which can only
# lengthen them -- the factor is never below 1, so a park after frame 1 is
# bounded exactly at the constants above. Fail-closed is unchanged; only
# the patience is.
_PARK_POLL_INTERVAL = 0.02

# Loader-homing rounds cold_init() runs by default; also the maximum of the
# opt-in Scanner.cold_homing_rounds setting.
COLD_HOMING_ROUNDS_DEFAULT = 3

# cold_init()'s "engine reached the done class" wait, at both of its sites
# (the opening one in _cold_init_body and the per-round one in
# _cold_homing_round). Non-raising: poll_status_word logs and continues.
#
# 1.5 s, not the 15 s it was until 2026-09-13. Measured on hardware that
# day across two cold starts (docs/test-log.md Test 77): the OPENING wait
# never settles at all -- the status word reads 0x48 and stays there for
# the full budget, ~1900 polls, first == last -- because at power-on the
# engine is not in the done class and does not enter it until the first
# homing move has run. The PER-ROUND wait, the same mask and target, then
# settles on its FIRST read in 4 ms, every round of every run. So the
# opening 15 s was waiting for something that cannot happen yet, and the
# per-round one needs a fraction of a second; 1.5 s is ~375x the observed
# settle and still removes 13.5 s from the ~40 s an operator waits at
# load. The wait is best-effort either way: a shorter budget cannot fail
# the sequence, it only stops waiting sooner.
COLD_READY_TIMEOUT = 1.5

# _cold_homing_round()'s closing settle check, waiting for reg 0x35 = 0xbb
# and reg 0x32 = 0x1f. Non-raising: it warns and continues.
#
# 0.25 s, not the 1.5 s (30 rounds of 50 ms) it was until 2026-09-13.
# Reg 0x35 reaches 0xbb on the FIRST read, every round of every run ever
# logged. Reg 0x32 never reaches 0x1f -- it reads 0x1d, static, in all
# nine observations across three cold starts (docs/test-log.md Tests 77,
# 78).
#
# Cause (docs/static-analysis.md SA-001, Test 79): 0x1f has bit 0x02 set,
# and until 2026-10-12 this round dropped the vendor's write that sets
# that bit again between move 2 and move 3 (our own captures show it in
# every round), so the last reg 0x32 write before the settle CLEARED it
# and 0x1f was unreachable. The write is now in place, so reg 0x32 is
# expected to read 0x1f on the first settle read; confirmed on hardware
# (Test 98: first settle read 0x1f, no settle warning in three rounds).
# The constants below are deliberately unchanged (one
# variable at a time). The motor completion that precedes this settle has
# already confirmed the move finished, so this is a secondary check, not
# the one that proves the transport is done.
COLD_SETTLE_TIMEOUT = 0.25
COLD_SETTLE_INTERVAL = 0.05

# _cold_motor_move()'s completion wait: the status word must read
# 0xf855 (done class, loader sensor bit set, 0x55 ack) after each of the
# nine loader-profile moves. STRICT since 2026-09-15 -- a timeout raises
# StrictPollTimeoutError, the cold start stops, nothing further is
# written, the session is FAILED and a power cycle is the only way on.
#
# Why strict, when the ready and settle polls around it are not: this is
# the only wait that separates one motor start from the next. After a
# timeout the sequence would otherwise write 0x09/0x0f and start the next
# move (up to eight more before the closing reg 0x01 = 0x22 check) on an
# engine whose previous move is not known to have finished. No hardware
# property makes that safe, and the one time a command was sent on top
# of a running engine the firmware hung (2026-09-04, power cycle). The
# lenient form was a replay habit, not evidence: the completions have
# settled at 0xf855 in 1.0-1.9 s (busy 0xd9 first) in every cold start
# ever logged, latched magazine or not (docs/test-log.md Tests 45, 51,
# 77, 78, 79) -- the latched case's "timeout every time" was the OPENING
# ready poll (still best-effort, see COLD_READY_TIMEOUT) and the reg 0x32
# settle, never a completion. So the strict form changes no observed
# run; it changes only what happens in a case never seen. Budget 30 s
# unchanged (~15x the observed move). The C++ backend's cold-start
# program takes this budget from here (tools/gen_sane_tables.py) and
# runs the same nine waits as PollMasked, fail-closed.
COLD_MOVE_TIMEOUT = 30.0

# Opt-in early failure of a cold-start motor move (Scanner.
# cold_move_early_fail, CLI --cold-move-early-fail): once the motor-enable
# bit has been seen set after the move's execute pulse, this long a run of
# well-formed replies with the motor-enable and home-sensor bits both
# clear ends the wait as a failure (ColdMoveEndedOffSensorError) instead
# of waiting out COLD_MOVE_TIMEOUT. A stopped motor is how moves end in
# vendor-app traffic; stopped off the sensor, the exact f855 this wait
# requires does not come. Nothing new is accepted.
COLD_MOVE_IDLE_WINDOW_S = 1.0


class _ColdMoveWatch:
    """Read history of one cold-start motor completion wait (on_read hook
    of UsbIo.poll_status_word). Classifies a timeout; with ``early_fail``
    raises ColdMoveEndedOffSensorError (see COLD_MOVE_IDLE_WINDOW_S).
    Uses only the clock value the poll loop already took.

    ``t`` is taken BEFORE its read, so it is a lower bound on when that
    reply was read. The window therefore starts at the ``t`` of the read
    AFTER the first stopped-off-sensor reply (an upper bound on when that
    reply was read), and a slow transfer can only lengthen the observed
    stop, never shorten it."""

    def __init__(self, early_fail: bool):
        self.early_fail = early_fail
        self.busy_seen = False
        self.last = b""
        self._idle_pending = False
        self._idle_since = None

    @staticmethod
    def _well_formed(raw: bytes) -> bool:
        return len(raw) == 2 and raw[1] == 0x55

    def __call__(self, raw: bytes, t: float) -> None:
        self.last = raw
        if self._idle_pending:
            self._idle_since, self._idle_pending = t, False
        ok = self._well_formed(raw)
        if ok and raw[0] & 0x01:
            self.busy_seen = True
        if not (self.busy_seen and ok and not raw[0] & 0x09):
            self._idle_since, self._idle_pending = None, False
            return
        if self._idle_since is None:
            self._idle_pending = True
            return
        if self.early_fail and t - self._idle_since >= COLD_MOVE_IDLE_WINDOW_S:
            raise safety.ColdMoveEndedOffSensorError(
                "motor stopped off the home sensor", last=raw,
                want=bytes.fromhex("f855"), outcome="ended-off-sensor")

    def outcome(self) -> str:
        raw = self.last
        if not self._well_formed(raw):
            return "malformed"
        if raw[0] & 0x01:
            return "still-busy"
        if not self.busy_seen:
            return "no-busy-observed"
        if not raw[0] & 0x08:
            return "ended-off-sensor"
        return "other"


# Masked completion test for the LOAD flow (docs/test-log.md Test 14,
# docs/load-analysis.md). The status word (reg 0x101 high byte, ack
# 0x55 low byte) is compared against the captured completion value
# under LOAD_STATUS_MASK, so the check requires BOTH the finished state
# class (upper nibble) AND the loader-sensor bit 0x08 in its captured
# state (feed: CLEAR, cassette pulled past the sensor; traverse: SET,
# cassette back in front of it), plus the busy bit 0x01 clear and the
# never-observed bit 0x02 clear. Only bit 0x04 is masked out: it is
# session-variable in every vendor capture (f0/f4 after the feed,
# d8/dc after the traverse) and carries no completion meaning.
#
# Not an exact match (0xf055/0xd855 rejected the correct vendor load),
# not a range or "upper nibble only" (0xf855 -- feed finished but the
# cassette still in front of the sensor -- would pass), and not the
# sensor bit alone (a still-running or wrong-class engine would pass).
LOAD_STATUS_MASK = 0xFB


# POSITION (the per-frame carriage move before SCAN): the captured
# completion is 0xf455 and a still-moving transport reads class 0xD
# (Test 18: 0xd555 after 4.9 s on the 39026-step move to frame 4, and
# the scan then started on a moving transport -- the frame held mostly
# frame 3). Only the state class is required here: the loader-sensor
# bit is not a positioning signal, and bit 0x01 is not proven to be a
# motor flag in the scan states (the SCAN phase settles at 0xad with
# it set).
POSITION_STATUS_MASK = 0xF0

# Opt-in (Scanner.position_require_motor_idle, CLI
# --position-require-motor-idle): the POSITION wait additionally requires
# the motor-enable bit (0x01) clear. In vendor-app traffic a class-F word
# with that bit set (f9) is a busy step of a move, and the vendor app
# waits for the bit to clear after every positioning move. Strictly
# tighter than POSITION_STATUS_MASK against the captured f455; budget,
# floor and FEEDL scale are unchanged. Off by default because it can turn
# a run that passes today into a FAILED session (hardware A/B pending).
POSITION_STATUS_MASK_IDLE = 0xF1


def status_matches(got: bytes, want: bytes, mask: int) -> bool:
    """True when status-word reply ``got`` completes like captured
    reply ``want`` under ``mask`` on the status byte. The ack byte
    must match exactly; a short or malformed reply never matches."""
    if len(got) != 2 or len(want) != 2 or got[1] != want[1]:
        return False
    return (got[0] & mask) == (want[0] & mask)


def load_status_matches(got: bytes, want: bytes) -> bool:
    """status_matches() under LOAD_STATUS_MASK (see above)."""
    return status_matches(got, want, LOAD_STATUS_MASK)


# Semantic PARK's completion after the carriage-return write (0x02=0x30):
# PARK_COMPLETE -- the status word reads the way every OBSERVED successful
# park ended, not merely "some idle state". Evidence, PARK-specific only
# (docs/park-completion-analysis.md): captured parks end in f8 (trace 03)
# and e8 (traces 04, 600, 1200, 2400, 7200, load-only); our own verbatim
# parks (Tests 18-21, 20+ parks) end in e8/ec as read at the next frame's
# start. Busy during the return: d1, 81, a1, a5, a9 (private capture).
#   0x80 required set   -- set in every observed park end
#   0x40 required set   -- set in every observed park end, clear in a1/a5/a9/81
#   0x20 required set   -- set in EVERY observed park end (e8, ec, f8); the
#                          class-D values d8/dc were observed only after a
#                          LOAD and never as a park end, so class D (and C,
#                          never observed at all) is NOT accepted here
#   0x10 ignored        -- varies between observed park ends (f8 vs e8)
#   0x08 ignored        -- the loader sensor (vendor INI: LoaderSensorReg =
#                          0x101,0x08): magazine presence, a documented
#                          semantic that is always "present" in a scan
#                          session; set in every observed park end, ignored
#                          on that documented meaning, not on variation
#   0x04 ignored        -- varies between observed park ends (e8 vs ec)
#   0x02 required clear -- never observed set anywhere; set = unknown state
#   0x01 required clear -- busy: set in every busy value during the return
# Not the LOAD mask (sensor bit in a captured state) and not the POSITION
# mask (class F only): different semantics. A park that ends outside this
# rule on hardware is an UNOBSERVED state and stops fail-closed; that is
# what the pending hardware A/B is for.
PARK_COMPLETE_REQUIRED_MASK = 0xE3
PARK_COMPLETE_REQUIRED_VALUE = 0xE0
PARK_COMPLETE_ACK = 0x55


def park_complete_status_matches(reply) -> bool:
    """True when a raw status-word reply (wValue 0x018e, wIndex 0x0122)
    reads like every observed successful PARK end -- semantic PARK's
    Wait B rule. Anything but a 2-byte reply with the 0x55 ack and
    (byte & PARK_COMPLETE_REQUIRED_MASK) == PARK_COMPLETE_REQUIRED_VALUE
    is a rejection: short, empty, long, malformed, busy (a1, a9, 81,
    d1, d5 ...), scanning (9c, ad, bd), the class-D/C values (d8, dc,
    c8, cc) that were never observed after a park, and unknown-bit
    values."""
    try:
        b = bytes(reply)
    except Exception:
        return False
    if len(b) != 2 or b[1] != PARK_COMPLETE_ACK:
        return False
    return (b[0] & PARK_COMPLETE_REQUIRED_MASK) == PARK_COMPLETE_REQUIRED_VALUE


# eject(): what happened, stated honestly (Scanner.eject()'s return value;
# Scanner.last_eject holds the detail). EJECT_COMPLETED: the eject move
# was commanded and the status word then read idle. EJECT_SKIPPED_NO_
# MAGAZINE: the loader sensor read clear and NOTHING was sent -- not an
# eject (the sensor is only trustworthy before the first initialize() of
# a power cycle, docs/hardware-safety.md, so after a scan this may be a
# wrong reading). A move whose completion is never confirmed is not an
# outcome: it raises safety.EjectIncompleteError (session FAILED).
EJECT_COMPLETED = "completed"
EJECT_SKIPPED_NO_MAGAZINE = "skipped-no-magazine"
# What a clear loader-sensor reading can mean (last_eject["interpretation"]
# and the CLI text for EJECT_SKIPPED_NO_MAGAZINE, whose value is kept for
# API compatibility). A clear bit 0x08 is ambiguous: no holder in the slot,
# or a holder that has been fed past the sensor. The driver cannot tell
# the two apart without moving the transport, and it does not move.
EJECT_SENSOR_CLEAR_INTERPRETATION = "sensor-clear: no holder, or holder fed past the sensor"
# Completion budget after the eject's GO. Unchanged value: the vendor's
# own poll reads d9 -> f9 -> f8 over ~0.9 s (protocol-notes.md pass 14).
EJECT_WAIT_TIMEOUT = 10.0
EJECT_POLL_INTERVAL = 0.02
# Completion rule, unchanged from the original eject: status byte has
# 0x20 set and busy 0x01 clear. Added only: the reply must be 2 bytes
# with the 0x55 ack -- a short or malformed reply never confirms (the old
# read_status() took byte 0 of anything, and 0 of an empty reply).
EJECT_DONE_MASK = 0x21
EJECT_DONE_VALUE = 0x20


#: Named bits of register 0x101 (high byte of the extended status word,
#: wValue 0x018e / wIndex 0x0122), in our own words, as observed in
#: hardware logs and vendor-app traffic. Bits without an established
#: meaning are recorded raw under their mask.
STATUS_WORD_BITS = (
    ("motor_enabled", 0x01),   # the engine is driving the motor
    ("bit_0x02", 0x02),
    ("lamp_on", 0x04),
    ("loader_sensor", 0x08),   # loader/home sensor
    ("bit_0x10", 0x10),
    ("bit_0x20", 0x20),
    ("buffer_empty", 0x40),    # read buffer holds no data
    ("power_bit", 0x80),
)


def decode_status_word(reply) -> dict | None:
    """Name the bits of a raw extended status-word reply (2 bytes: the
    register 0x101 value, then the 0x55 ack). Returns {name: 0 or 1} for
    every entry of STATUS_WORD_BITS, or None for anything else (short,
    empty, long, bad ack). Pure host-side decoding for diagnostics: no
    driver decision reads it and it never raises on a malformed reply."""
    try:
        b = bytes(reply)
    except Exception:
        return None
    if len(b) != 2 or b[1] != 0x55:
        return None
    return {name: int(bool(b[0] & mask)) for name, mask in STATUS_WORD_BITS}


def format_status_bits(bits: dict | None) -> str:
    """One-line ``name=v`` rendering of decode_status_word() output for
    log and error messages ("undecodable" for None)."""
    if bits is None:
        return "undecodable"
    return " ".join(f"{k}={v}" for k, v in bits.items())


def cal_stale_suspect(rec: dict) -> bool:
    """True when a calibration acquisition record (Scanner.
    _cal_acquisitions entry) was re-armed while the previous acquisition
    still read as running AND its data came back fast or its read-back
    poll saw no acquisition. An unknown prev_running (None) is never
    suspect. Pure function."""
    return bool(rec.get("prev_running") is True
                and (rec.get("served_fast") or rec.get("acq_not_observed")))


def cal_integrity_verdict(acquisitions: list[dict], dark_b_substituted: bool,
                          dark_b_fallback) -> tuple[str, list[str]]:
    """("clean" | "degraded", reasons) for one frame's calibration:
    degraded on any stale_suspect acquisition, the dark_b fallback or the
    dark_b substitution. Pure function, diagnostics only."""
    reasons = []
    for a in acquisitions:
        if a.get("stale_suspect"):
            gap, data = a.get("rearm_gap_s"), a.get("go_to_data_s")
            reasons.append(
                f"{a.get('phase')}: stale read suspected (re-armed "
                f"{'?' if gap is None else f'{gap * 1000:.1f}'} ms after the previous "
                f"clear while the previous acquisition still read as running; data "
                f"{'?' if data is None else f'{data * 1000:.1f}'} ms after the execute "
                f"pulse{'; acquisition not observed' if a.get('acq_not_observed') else ''})")
    if dark_b_fallback:
        reasons.append(f"dark_b residual: fallback {dark_b_fallback}")
    if dark_b_substituted:
        reasons.append("dark_b residual: substituted with this session's healthy dark_b")
    return ("degraded" if reasons else "clean"), reasons


def eject_complete_status_matches(reply) -> bool:
    """True when a raw status-word reply (wValue 0x018e, wIndex 0x0122)
    confirms the eject move finished: 2 bytes, ack 0x55, (byte & 0x21)
    == 0x20. Anything else -- short, empty, long, bad ack, busy (d9, f9)
    -- is not a confirmation."""
    try:
        b = bytes(reply)
    except Exception:
        return False
    if len(b) != 2 or b[1] != 0x55:
        return False
    return (b[0] & EJECT_DONE_MASK) == EJECT_DONE_VALUE


def check_line_count(n_lines: int) -> int:
    """Return ``n_lines`` if it can be encoded as a scan line count; raise
    otherwise. Called immediately before the count is encoded on every
    profile. The upper bound is the 24-bit register (as before); the lower
    bound (>= 1) is a purely additional guard: no valid geometry produces
    fewer than one line, and a negative count would otherwise encode as a
    huge two's-complement value (e.g. -490 -> 0xFFFE16)."""
    if n_lines < 1:
        raise Of135iError(f"line count {n_lines} is not a positive scan length")
    if n_lines > 0xFFFFFF:
        raise Of135iError(f"line count {n_lines} does not fit the 24-bit register")
    return n_lines


def position_timeout_scale(t, feedl: int) -> float:
    """How much longer than the captured POSITION move (frame 1 of the
    table's capture) a move of ``feedl`` steps may take: the completion
    budget is 3x the captured duration times this. Linear in FEEDL,
    never below 1 (frame 4 at 3600 dpi: 39026/6743 = 5.8x, ~28 s)."""
    return max(1.0, feedl / t.feedl_for_frame(1))


def load_completion_target() -> int:
    """The status word the vendor capture settled on after the LOAD
    flow's traverse: the last coalesced status-word poll in
    tables_load.LOAD (0xdc55 in the clean-load capture). Derived from
    the table so it cannot drift from it; compared under
    LOAD_STATUS_MASK via load_status_matches()."""
    from . import tables_load
    polls = [op for op in tables_load.LOAD.ops if op.kind == "poll" and op.wv == 0x018E]
    if not polls or len(polls[-1].resp) != 2:
        raise RuntimeError("tables_load.LOAD has no final status-word poll")
    return int.from_bytes(polls[-1].resp, "big")


# Diagnostic: when OF135I_DUMP_CAL is set to a directory, a Scanner
# accumulates the raw dark_a/dark_b calibration buffers and per-bulk-read
# transfer metadata in memory across a session, and writes them (with
# checksums) once, on __exit__ -- after the batch or after a failure.
# It adds no USB transaction and does not change the op sequence; no file
# I/O or subprocess runs between frames (dark_b-collapse investigation,
# docs/test-log.md Test 29/30).
DUMP_CAL_ENV = "OF135I_DUMP_CAL"


class _CalCapture:
    """In-memory accumulation of raw calibration buffers and per-transfer
    read records for one Scanner session. Nothing is hashed or written to
    disk until :meth:`Scanner._flush_cal_capture` at __exit__, so the
    batch's inter-frame timing is untouched. Memory is bounded: only the
    dark_a/dark_b buffers are kept (not white/shading/image), and past
    ``max_bytes`` further buffers are dropped with a recorded count."""

    def __init__(self, out_dir: str, max_bytes: int = 64 * 1024 * 1024):
        self.out_dir = out_dir
        self.max_bytes = max_bytes
        self.frame: int | None = None
        self.buffers: list[dict] = []   # {name, frame, phase, raw}
        self.reads: list[dict] = []     # per-bulk-read transfer record
        self._seq = 0
        self._bytes = 0
        self.dropped_buffers = 0

    def set_frame(self, frame: int | None) -> None:
        self.frame = frame

    def note_read(self, phase, requested, returned, t_before, t_after,
                  exception) -> None:
        """One bulk-IN read. ``returned`` is the byte count, or None when
        an exception made the received length unknown (distinct from a
        genuine zero-byte read, which records returned=0)."""
        self._seq += 1
        self.reads.append({
            "seq": self._seq, "phase": phase, "frame": self.frame,
            "requested": requested, "returned": returned,
            "t_before": t_before, "t_after": t_after,
            "exception": exception,
        })

    def note_buffer(self, name, phase, raw) -> None:
        raw = bytes(raw)
        if self._bytes + len(raw) > self.max_bytes:
            self.dropped_buffers += 1
            return
        self._bytes += len(raw)
        self.buffers.append({"name": name, "frame": self.frame,
                             "phase": phase, "raw": raw})


def check_cold_homing_rounds(n):
    """Return ``n`` if it is a valid cold-homing round count (an int,
    1..COLD_HOMING_ROUNDS_DEFAULT), else raise ValueError. Pure: no I/O."""
    if type(n) is not int or not 1 <= n <= COLD_HOMING_ROUNDS_DEFAULT:
        raise ValueError(
            f"cold_homing_rounds must be an int from 1 to "
            f"{COLD_HOMING_ROUNDS_DEFAULT}, got {n!r}")
    return n


class Scanner:
    """Drives the of135i scan sequence over a UsbIo transport."""

    _cal_readback_guard = CAL_READBACK_GUARD_DEFAULT
    _diag_readback_regs = False
    _position_require_motor_idle = False
    _cold_move_early_fail = False
    _cal_integrity_telemetry = True
    _cal_wait_scanstop = "off"
    _cal_wait_bufempty = "off"

    @property
    def cal_wait_bufempty(self) -> str:
        """"off" (default) or "enforce": see CAL_WAIT_BUFEMPTY_MODES.
        EXPERIMENTAL, unverified on hardware."""
        return self._cal_wait_bufempty

    @cal_wait_bufempty.setter
    def cal_wait_bufempty(self, mode: str) -> None:
        if not isinstance(mode, str) or mode not in CAL_WAIT_BUFEMPTY_MODES:
            raise ValueError(
                f"cal_wait_bufempty must be one of {CAL_WAIT_BUFEMPTY_MODES}, got {mode!r}")
        self._cal_wait_bufempty = mode
    # Stop-gate state (CAL_WAIT_SCANSTOP_MODES): an execute pulse seen in
    # the current calibration phase, and the phase whose SCAN-clear armed
    # the gate (None = not armed). Only ever set in "enforce" mode.
    _scanstop_go_phase: str | None = None
    _scanstop_armed: str | None = None
    _scanstop_t_clear = 0.0
    _scanstop_polled_s = 0.0

    @property
    def cal_wait_scanstop(self) -> str:
        """"off" (default) or "enforce": see CAL_WAIT_SCANSTOP_MODES.
        EXPERIMENTAL, unverified on hardware."""
        return self._cal_wait_scanstop

    @cal_wait_scanstop.setter
    def cal_wait_scanstop(self, mode: str) -> None:
        if not isinstance(mode, str) or mode not in CAL_WAIT_SCANSTOP_MODES:
            raise ValueError(
                f"cal_wait_scanstop must be one of {CAL_WAIT_SCANSTOP_MODES}, got {mode!r}")
        self._cal_wait_scanstop = mode

    @property
    def cal_integrity_telemetry(self) -> bool:
        """Default True: record per-acquisition calibration timing and the
        per-frame cal_integrity verdict (see CAL_SERVED_FAST_S). Observer
        only; False records nothing. Either way the transfers, sleeps and
        poll-loop clock reads are identical."""
        return self._cal_integrity_telemetry

    @cal_integrity_telemetry.setter
    def cal_integrity_telemetry(self, on: bool) -> None:
        if not isinstance(on, bool):
            raise ValueError(f"cal_integrity_telemetry must be a bool, got {on!r}")
        self._cal_integrity_telemetry = on

    _cold_homing_rounds = COLD_HOMING_ROUNDS_DEFAULT

    @property
    def cold_homing_rounds(self) -> int:
        """EXPERIMENTAL, unverified on hardware, motor-affecting, opt-in:
        how many loader-homing rounds cold_init() runs (1..3). Default 3
        keeps the wire traffic exactly as before. Fewer rounds skip only
        the extra rounds; the pre-move prep, resync and each round's own
        moves are unchanged. In vendor-app traffic with the holder at the
        sensor, one set of three jogs was observed."""
        return self._cold_homing_rounds

    @cold_homing_rounds.setter
    def cold_homing_rounds(self, n: int) -> None:
        self._cold_homing_rounds = check_cold_homing_rounds(n)

    @property
    def cold_move_early_fail(self) -> bool:
        """Opt-in: end a cold-start motor wait early, as a failure, when the
        motor has stopped off the home sensor (see COLD_MOVE_IDLE_WINDOW_S).
        Default False: the full COLD_MOVE_TIMEOUT wait."""
        return self._cold_move_early_fail

    @cold_move_early_fail.setter
    def cold_move_early_fail(self, on: bool) -> None:
        if not isinstance(on, bool):
            raise ValueError(f"cold_move_early_fail must be a bool, got {on!r}")
        self._cold_move_early_fail = on

    @property
    def position_require_motor_idle(self) -> bool:
        """Opt-in: POSITION completion also requires the motor-enable bit
        clear (POSITION_STATUS_MASK_IDLE). Default False: today's rule."""
        return self._position_require_motor_idle

    @position_require_motor_idle.setter
    def position_require_motor_idle(self, on: bool) -> None:
        if not isinstance(on, bool):
            raise ValueError(f"position_require_motor_idle must be a bool, got {on!r}")
        self._position_require_motor_idle = on

    def _position_status_mask(self) -> int:
        return (POSITION_STATUS_MASK_IDLE if self._position_require_motor_idle
                else POSITION_STATUS_MASK)

    @property
    def diag_readback_regs(self) -> bool:
        """Opt-in, read-only: after a calibration read-back poll times out,
        read register 0x100 and register 0x101 once each and record the
        decoded values on the poll records and in the log. Default False:
        the transfer sequence is unchanged."""
        return self._diag_readback_regs

    @diag_readback_regs.setter
    def diag_readback_regs(self, on: bool) -> None:
        if not isinstance(on, bool):
            raise ValueError(f"diag_readback_regs must be a bool, got {on!r}")
        self._diag_readback_regs = on

    @property
    def cal_readback_guard(self) -> str:
        """"off", "log" (default) or "fail": see CAL_READBACK_GUARD_MODES."""
        return self._cal_readback_guard

    @cal_readback_guard.setter
    def cal_readback_guard(self, mode: str) -> None:
        if mode not in CAL_READBACK_GUARD_MODES:
            raise ValueError(
                f"cal_readback_guard must be one of {CAL_READBACK_GUARD_MODES}, "
                f"got {mode!r}")
        self._cal_readback_guard = mode

    def __init__(self, io: UsbIo):
        self.io = io
        self._base_initialized = False
        # Set by initialize(), cleared by scan(): the vendor re-runs its
        # PREP/AFE_BASE equivalent before every frame, and a scan
        # without it is not a verified sequence.
        self._prepared_for_scan = False
        # Set by initialize(prep=False): the vendor's device-open
        # sequence (tables_load.OPEN) has been replayed this session.
        self._vendor_open = False
        # How far from home the last POSITION move went, as the same
        # linear factor POSITION scales its own completion budget by.
        # The park that follows returns the carriage from there, so its
        # two waits are scaled by it too. 1.0 until a frame is
        # positioned to, which is also the value for frame 1.
        self._park_scale = 1.0

        # Hardware-safety session (safety.py). A real UsbIo brings its
        # own; a duck-typed transport (tests) gets one attached here,
        # with its device handle wrapped in the same write-gating proxy,
        # so the guard cannot be skipped by constructing Scanner over
        # something other than UsbIo.
        session = getattr(io, "session", None)
        if not isinstance(session, safety.HardwareSession):
            session = safety.HardwareSession()
            try:
                io.session = session
            except AttributeError:
                pass
            dev = getattr(io, "dev", None)
            if dev is not None and not isinstance(dev, safety.GuardedDevice):
                io.dev = safety.GuardedDevice(dev, session)
        self.session: safety.HardwareSession = session

        # Per-scan diagnostics (see diag.py). last_diag is populated by
        # scan()/_scan_dual() on every call (overwritten, not
        # accumulated); the _diag_* attributes below are the recording
        # points _run_phase/_exec_ops/_poll_one/_gain_with_warmup write
        # into, reset at the start of each scan() / _scan_dual() call.
        # None of this adds or changes any USB operation -- it only
        # observes values/timings the driver already computes.
        self.last_diag: dict | None = None
        # Per-pixel calibration measurements of the LAST fresh calibration
        # (numpy arrays, for the `<stem>.cal.npz` export; see cli
        # _write_diag_sidecar). None when the last scan measured nothing
        # new (calibration cache hit). Pure host-side bookkeeping: it
        # keeps references to buffers already read, adds no USB traffic.
        self.last_cal_data: dict | None = None
        self._last_white: np.ndarray | None = None
        self._diag_phase_seconds: dict[str, float] = {}
        self._diag_poll_timeouts: int = 0
        self._diag_cr_mismatches: int = 0
        self._diag_poll_timeout_details: list[dict] = []
        self._diag_poll_settle: list[dict] = []
        self._diag_poll_settle_max_s: float = 0.0
        # Calibration acquisition records of the current scan (see
        # CAL_SERVED_FAST_S); keys starting with "_" are host timestamps
        # and recording state, stripped from the diag.
        self._cal_acquisitions: list[dict] = []
        # Stop-gate waits of the current scan (cal_wait_scanstop).
        self._cal_scanstop_waits: list[dict] = []
        # Buffer-empty waits of the current scan (cal_wait_bufempty enforce).
        self._cal_bufempty_waits: list[dict] = []
        self._diag_warmup: dict | None = None
        # Set by a completed load_magazine(): the transport is at the
        # vendor's scan reference position (the load's traverse). A
        # session that started cold must load before it scans (Test 22).
        self._loaded_this_session = False
        # Lamp warmup budget for this scanner object (see _WARMUP_BUDGET_S).
        self.warmup_budget_s: float = _WARMUP_BUDGET_S
        self._diag_park_waits: dict | None = None
        # Record of the last eject() call (outcome, loader-sensor reading
        # and whether it was taken after an initialize() in this session,
        # and the completion wait: reads, seconds, first/last status word,
        # whether a busy word was seen, timed out). Survives every exit of
        # the wait -- timeout, USB error, Ctrl-C -- like _diag_park_waits.
        # Host-side bookkeeping only; adds no USB traffic.
        self.last_eject: dict | None = None

        # PARK phase implementation switch (see park_semantic() and
        # docs/replay-analysis.md): "semantic" (default since 2026-09-17)
        # issues the same motor sequence as "verbatim" -- the captured
        # PARK op stream, byte for byte, register for register -- and
        # only replaces the captured trace's fixed pacing with real
        # read-modify-write plus condition waits (park_complete_status_
        # matches(): (byte & 0xe3) == 0xe0). That condition is hardware-
        # confirmed, not just offline-derived: the SANE backend's PARK
        # program (sane/gl126.cpp) was built from these same
        # park_semantic steps and has run in every SANE scan since
        # Test 52 (docs/test-log.md). Measured 3.7-3.8 s against
        # verbatim's captured 13.6-16.9 s (docs/park-completion-
        # analysis.md) -- semantic finishes as soon as the hardware
        # reports done instead of waiting out the capture's pauses.
        # "verbatim" remains selectable (CLI --park verbatim) for A/B
        # comparison; tools/hwblock.py keeps its own --park default for
        # that purpose.
        self.park_mode: str = "semantic"

        # Calibration-buffer capture (see _CalCapture / DUMP_CAL_ENV):
        # active only when the env var names an output directory. Held for
        # the whole session so a batch accumulates in memory and flushes
        # once, on __exit__.
        _cal_dir = os.environ.get(DUMP_CAL_ENV)
        self._cal_capture: _CalCapture | None = (
            _CalCapture(_cal_dir) if _cal_dir else None)

        # Last healthy dark_b of this session, for substituting a residual
        # one on a later batch frame (Test 32/33). A dark measurement is
        # frame-independent, so reusing a healthy one is a proven fix, not
        # a cover-up. Reset naturally per Scanner (a new session = None).
        self._last_good_dark_b: np.ndarray | None = None
        # What to do when dark_b is residual AND no healthy one exists yet
        # this session (_dark_b_for_offsets): "strict" (default) fails
        # closed with CalibrationError, exactly as before; "default" (opt-
        # in, CLI --dark-fallback default) uses the vendor's own offset
        # codes calibrate._OFFSET_DEFAULT and records that in last_diag.
        self.dark_fallback: str = "strict"

        # Per-session calibration cache (see _scan_plain, _calibration_
        # rewrite(), docs/driver-design.md's calibration-cache section).
        # Vendor evidence: docs/protocol-notes.md pass 14 + its
        # frame-4-rewrite addendum -- SilverFast scanning frames 3, 4, 1
        # in one session runs full calibration on 3 and 1 but has frame 4
        # skip it entirely, re-applying frame 3's AFE gain codes with a
        # short rewrite instead (the uploaded shading table survives PARK
        # in scanner RAM, so it needs no re-upload either). "recalibrate"
        # forces today's full-calibration-every-frame behaviour (CLI:
        # --recalibrate); the cache is otherwise used by DEFAULT since
        # 2026-09-17.
        #
        # None = no cached calibration yet (or invalidated). Set to a dict
        # {"key": (dpi, dual), "gain": (r,g,b), "offset": (r,g,b), plus
        # the calibration-descriptive diag fields (dark means, warmup
        # summary, shading stats) so a cache-hit's diag sidecar stays
        # informative} immediately after a full calibration sequence
        # succeeds -- see _scan_plain. Cleared (never trusted by a later
        # frame): on any exception escaping a scan (a failed scan must
        # never leave a cache the next frame relies on -- see the
        # try/except around position/scan/park in _scan_plain), by
        # eject() and load_magazine() (the transport state calibration
        # was measured against no longer holds), and implicitly by a
        # (dpi, dual) key mismatch. A dual scan's own full calibration
        # (never itself cached -- see _scan_dual) ALSO clears this: its
        # shading upload overwrites whatever plain shading table is in
        # scanner RAM, which would silently invalidate a same-key plain
        # cache from earlier in the same session.
        self.recalibrate: bool = False
        self._cal_cache: dict | None = None

    @classmethod
    def open(cls) -> "Scanner":
        return cls(UsbIo.open())

    def close(self) -> None:
        self.io.close()

    def __enter__(self) -> "Scanner":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Flush captured calibration buffers (host I/O only, no USB, no
        # recovery), then close. The flush runs whether the block exited
        # normally or by exception, so a failed scan's partial buffers are
        # still written; a flush error is logged, never allowed to mask
        # the original exception or trigger recovery.
        try:
            self._flush_cal_capture()
        except Exception as e:
            log.warning("cal-capture flush failed (ignored): %s", e)
        # Close only. Deliberately NO park/home/eject/initialize here:
        # if the block is left by an exception the hardware state is
        # unknown and the only valid recovery is a power cycle.
        self.close()

    def _flush_cal_capture(self) -> None:
        """Write the session's captured raw calibration buffers + transfer
        records to OF135I_DUMP_CAL, computing checksums/stats now (after
        the hardware run). No-op if capture is off or empty."""
        cap = self._cal_capture
        if cap is None or (not cap.buffers and not cap.reads):
            return
        meta = {
            "dumped_utc": datetime.now(timezone.utc).isoformat(),
            "host": diag._collect_host(),
            "park_mode": self.park_mode,
            "dropped_buffers": cap.dropped_buffers,
            "max_bytes": cap.max_bytes,
        }
        base = f"cal-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f')}"
        # Name buffers uniquely per (frame, phase, name), preserving order.
        named: dict = {}
        for i, b in enumerate(cap.buffers):
            key = f"{b['name']}-f{b['frame']}-{i}"
            named[key] = b["raw"]
        path = diag.dump_calibration_buffers(
            cap.out_dir, base, meta, named, reads=cap.reads)
        log.info("flushed %d calibration buffer(s), %d read record(s) to %s",
                 len(named), len(cap.reads), path)

    # ------------------------------------------------------- safety gate

    def check_start_state(self) -> StartState:
        """Read-only: verify the scanner's start state for this session
        (reg 0x01, read once, strictly) and return the classification.
        Raises safety.UnsafeStartStateError -- with zero writes -- if
        the state is neither idle-homed (0x22) nor cold (0x00), or if
        it cannot be read. Idempotent: later calls return the verdict.
        """
        return safety.verify_start_state(self.io)

    @contextmanager
    def _operation(self, name: str, *, cold_ok: bool = False):
        """Run a public hardware operation.

        Outermost operation of a session: verifies the start state
        first (no writes happen before that), and refuses anything but
        the cold-init path (`cold_ok` operations: initialize/eject run
        cold_init() themselves; cold_init itself) on a cold scanner.
        Nested operations (initialize -> cold_init, scan -> park) run
        inside the outer one without re-checking -- the transient
        engine states inside a session are expected. Any exception
        escaping the block marks the session FAILED (safety.
        HardwareSession.operation); nothing is sent afterwards.
        """
        session = self.session
        if not session.operations:
            self.check_start_state()
            if session.state is SessionState.COLD and not cold_ok:
                raise OperationNotAllowedError(
                    f"{name}() refused: the scanner is in the cold start state "
                    f"(reg 0x01 = 0x00) and only initialize()/eject() -- which run the "
                    f"cold-init path first -- or cold_init() itself may write to it. "
                    f"{safety.NO_COMMANDS_SENT}",
                    observed=session.start_reg01, session=session.snapshot())
        with session.operation(name):
            yield

    def session_report(self) -> dict:
        """JSON-serializable snapshot of the safety session (state,
        write/execute counters, phase, failure record)."""
        return self.session.snapshot()

    # ------------------------------------------------------- op execution

    def _exec_ops(self, ops: list[Op], strict_polls: frozenset[int] = frozenset(),
                  strict_mask: int = LOAD_STATUS_MASK,
                  poll_timeout_scale: float = 1.0) -> list[bytes]:
        """Execute a verbatim op list with replay_trace.py semantics:

          - cw: ctrl_transfer verbatim; if it's a wv=0x82 buffer
            descriptor, flush/(re)start the current measurement buffer
            (read descriptors start a new one, write descriptors just
            flush); if it's a wv=0x83 register batch containing the
            execute pulse (0x0f=0x01), wait for the engine-idle bit
            afterwards.
          - cr: perform the read; log (don't raise) on a captured-
            response mismatch.
          - poll: poll until the final captured response, timing out
            after max(3*dur, 10s); log-and-continue.
          - bo: bulk write verbatim.
          - bi: bulk read, routed into the current measurement buffer
            (if any).
          - dt pacing: sleep dt before the op when it exceeds
            _PACE_THRESHOLD, capped at _PACE_CAP.

        Returns the list of collected read buffers, in the order their
        buffer-read descriptors were encountered (most phases produce
        0 or 1; "scan" produces one per image chunk plus the trailing
        drain).

        Refuses to run outside a hardware operation (see _operation):
        this is the only way op streams reach the wire, and every
        caller must therefore be a guarded public method.
        """
        self.session.write_allowed_or_final()
        if not self.session.operations:
            raise OperationNotAllowedError(
                "_exec_ops() called outside a hardware operation; use the public "
                "Scanner methods (initialize/scan/eject/load_magazine). Nothing was sent.",
                session=self.session.snapshot())
        dev = self.io.dev
        collected: list[bytes] = []
        cur: bytearray | None = None

        def flush() -> None:
            nonlocal cur
            if cur is not None:
                collected.append(bytes(cur))
                cur = None

        for idx, op in enumerate(ops):
            if op.dt > _PACE_THRESHOLD:
                time.sleep(min(op.dt, _PACE_CAP))

            if op.kind == "cw":
                if op.wv == 0x0082 and op.br == 0x04 and len(op.data) == 8:
                    flush()
                    if op.wi != 1:
                        cur = bytearray()
                if self._scanstop_armed is not None:
                    self._cal_wait_scan_stop()
                dev.ctrl_transfer(op.bm, op.br, op.wv, op.wi, op.data)
                if self._cal_wait_scanstop != "off" and op.wv == 0x0083:
                    self._scanstop_note_write(op.data)
                if self._cal_integrity_telemetry and op.wv == 0x0083:
                    self._cal_note_write(op.data)
                # NOTE: no engine-idle wait here. The verbatim replayer
                # never waits and works (verified on hardware twice,
                # 2026-08-30): the trace's own polls carry the timing,
                # and during the image pass the scanner streams while
                # bulk reads drain it -- blocking until idle overflows
                # the scanner's buffer and yields stale RAM instead of
                # live data. _wait_engine_idle() remains in _motor_run
                # for our own out-of-trace moves (home/eject).
            elif op.kind == "cr":
                got = bytes(dev.ctrl_transfer(op.bm, op.br, op.wv, op.wi, op.length))
                if (self._cal_integrity_telemetry and op.wv == 0x018E
                        and op.wi in (0x0022, 0x0122)):
                    self._cal_note_status_read(op.wi, got)
                if got != op.resp:
                    self._diag_cr_mismatches += 1
                    log.debug(
                        "cr wv=%#06x wi=%#06x: got %s want %s -- continuing",
                        op.wv, op.wi, got.hex(), op.resp.hex(),
                    )
            elif op.kind == "poll":
                if (self._scanstop_armed is not None and op.wv == 0x018E
                        and op.wi == 0x0022):
                    # The phase's own post-clear poll of register 0x100 is
                    # part of the stop gate's budget (enforce mode only).
                    _t0 = time.monotonic()
                    try:
                        self._poll_one(op, strict=idx in strict_polls, mask=strict_mask,
                                       timeout_scale=poll_timeout_scale)
                    finally:
                        self._scanstop_polled_s += time.monotonic() - _t0
                else:
                    self._poll_one(op, strict=idx in strict_polls, mask=strict_mask,
                                   timeout_scale=poll_timeout_scale)
            elif op.kind == "bo":
                if self._scanstop_armed is not None:
                    self._cal_wait_scan_stop()
                dev.write(EP_BULK_OUT, op.data, timeout=5000)
            elif op.kind == "bi":
                cap = self._cal_capture
                if cap is None:
                    data = dev.read(EP_BULK_IN, op.length, timeout=60000)
                else:
                    # Same read call as above; only wrapped in timing +
                    # a transfer record. On failure, keep the partial
                    # buffer and re-raise (returned=None marks the length
                    # unknown, distinct from a zero-byte read).
                    _t0 = time.monotonic()
                    try:
                        data = dev.read(EP_BULK_IN, op.length, timeout=60000)
                    except BaseException as e:
                        cap.note_read(self.session.phase, op.length, None,
                                      _t0, time.monotonic(), repr(e))
                        if cur is not None:
                            cap.note_buffer(f"partial:{self.session.phase}",
                                            self.session.phase, bytes(cur))
                        raise
                    cap.note_read(self.session.phase, op.length, len(data),
                                  _t0, time.monotonic(), None)
                if self._cal_integrity_telemetry:
                    self._cal_note_bulk()
                if (cur is not None and len(data) != op.length
                        and self.session.phase in CAL_ACQUISITION_PHASES):
                    if cap is not None:
                        cap.note_buffer(f"partial:{self.session.phase}",
                                        self.session.phase, bytes(cur) + bytes(data))
                    raise safety.CalibrationError(
                        f"calibration bulk read in phase {self.session.phase} returned "
                        f"{len(data)} of {op.length} bytes; the measurement is incomplete "
                        f"and was not used. The scan stops here, before the phase's next "
                        f"transfer. {safety.NO_RECOVERY_ATTEMPTED} "
                        f"{safety.POWER_CYCLE_INSTRUCTION}",
                        session=self.session.snapshot())
                if cur is not None:
                    cur.extend(data)
            else:
                raise ValueError(f"unknown op kind {op.kind!r}")

        flush()
        return collected

    def _poll_one(self, op: Op, strict: bool = False, mask: int = LOAD_STATUS_MASK,
                  timeout_scale: float = 1.0) -> None:
        """Poll until the captured settled value (with the documented
        leniencies) or the deadline. ``strict``: for completion waits an
        operation must not continue past (load_magazine's two motor-
        completion polls, scan's POSITION move): a status-word poll
        (wValue 0x018e) must satisfy status_matches() under ``mask``
        (LOAD_STATUS_MASK: state class AND loader-sensor bit;
        POSITION_STATUS_MASK: state class), any other poll must match
        exactly, and a timeout raises safety.StrictPollTimeoutError
        instead of logging. ``timeout_scale`` stretches the budget for
        a move longer than the captured one (position_timeout_scale)."""
        want = op.resp
        # Most captured polls settled in < 0.03 s; the previous 10 s
        # floor added ~200 s of wasted timeouts per scan when dynamic
        # register bits (magazine presence, sensor state) differed from
        # the capture. Strict completion waits keep a 1 s floor; the
        # non-strict state checks get NONSTRICT_POLL_FLOOR_S (see the
        # constant for the production telemetry behind it). Real motor
        # waits (POSITION dur ~1.6 s) still get 3× their captured time.
        floor = 1.0 if strict else NONSTRICT_POLL_FLOOR_S
        timeout = max(3 * op.dur * timeout_scale, floor)
        t_start = time.monotonic()
        deadline = t_start + timeout
        dev = self.io.dev
        if (not strict and self._cal_wait_bufempty == "enforce"
                and self._bufempty_site(op, want)):
            self._poll_bufempty(op, want, t_start, deadline, timeout)
            return
        while True:
            got = bytes(dev.ctrl_transfer(op.bm, op.br, op.wv, op.wi, op.length))
            if (self._cal_integrity_telemetry and op.wv == 0x018E
                    and op.wi in (0x0022, 0x0122)):
                # Some tables read a post-clear register through a poll.
                self._cal_note_status_read(op.wi, got)
            if got == want:
                if strict:
                    log.info("completion poll settled %s (exact) after %.2fs",
                             got.hex(), time.monotonic() - t_start)
                else:
                    self._note_poll_settle(op, got, want, t_start, "exact")
                return
            if strict:
                if op.wv == 0x018E and status_matches(got, want, mask):
                    log.info("completion poll settled %s (captured %s) after %.2fs",
                             got.hex(), want.hex(), time.monotonic() - t_start)
                    return
                if time.monotonic() > deadline:
                    self._diag_poll_timeouts += 1
                    raise safety.StrictPollTimeoutError(
                        f"completion poll wv={op.wv:#06x} wi={op.wi:#06x} did not reach the "
                        f"captured value: last {got.hex()}, want {want.hex()}"
                        f"{f' (mask {mask:#04x}55)' if op.wv == 0x018E else ''} "
                        f"after {timeout:.1f}s. "
                        f"The hardware did not complete this step; the operation stops here. "
                        f"{safety.NO_RECOVERY_ATTEMPTED} {safety.POWER_CYCLE_INSTRUCTION}",
                        last=got, want=want, observed=self.session.start_reg01,
                        session=self.session.snapshot())
                time.sleep(0.004)
                continue
            # Extended status word at wIndex 0x0122 (reg 0x101 in the high
            # byte). Bit 0x01 of reg 0x101 is the motor-enabled bit (the
            # engine is driving the motor; see STATUS_WORD_BITS); the
            # separate data-enable flag lives in bit 0 of reg 0x100, not
            # here. The settle label stays "dataenb" for sidecar
            # compatibility. Where the capture settled with this bit set
            # (ad55, bd55, a955: the calibration read-back sites), a live
            # word with that bit set is accepted whatever its state class
            # (the acceptance rule itself is unchanged). The SANE
            # backend polls exactly this bit at these sites (PollDataReady,
            # hardware-verified since Test 48); production telemetry
            # (docs/production-telemetry-2026-10.md section 2.3) showed the
            # driver instead timing out on 9c55-versus-ad55/bd55 in 93 % of
            # frames, one second each, never followed by a failure.
            if (op.wv == 0x018E and op.wi == 0x0122
                    and len(got) >= 1 and len(want) >= 1
                    and (want[0] & 0x01) and (got[0] & 0x01)):
                self._note_poll_settle(op, got, want, t_start, "dataenb")
                return
            # Status-word polls (0x018e): the upper nibble of the high
            # byte encodes the state class (0x9=busy, 0xD=transitioning,
            # 0xF=done); the lower nibble carries session-variable bits
            # (magazine sensor, lamp state) that legitimately differ from
            # the capture.  Accept an upper-nibble match so the poll
            # completes when the hardware reaches the right state class
            # instead of timing out on an irrelevant bit difference.
            if (op.wv == 0x018E and len(got) >= 1 and len(want) >= 1
                    and (got[0] & 0xF0) == (want[0] & 0xF0)):
                self._note_poll_settle(op, got, want, t_start, "class")
                return
            # Reg 0x32 polls (wIndex 0x3222): bits 3-4 (0x18) reflect
            # loader-sensor and transport state that vary with magazine
            # presence; mask them out so a poll doesn't time out just
            # because the magazine is in a different position than the
            # capture session had (hardware-verified 2026-09-02: bit 3
            # is the loader sensor, bit 4 co-varies).
            if (op.wv == 0x008E and op.wi == 0x3222
                    and len(got) >= 1 and len(want) >= 1
                    and (got[0] & ~0x18 & 0xFF) == (want[0] & ~0x18 & 0xFF)):
                self._note_poll_settle(op, got, want, t_start, "reg32")
                return
            if time.monotonic() > deadline:
                self._diag_poll_timeouts += 1
                detail = None
                if len(self._diag_poll_timeout_details) < 20:
                    detail = {
                        "wv": f"0x{op.wv:04x}", "wi": f"0x{op.wi:04x}",
                        "last": got.hex(), "want": want.hex(),
                    }
                    self._diag_poll_timeout_details.append(detail)
                entry = self._note_poll_settle(op, got, want, t_start, "timeout")
                log.warning(
                    "poll wv=%#06x wi=%#06x timed out after %.1fs "
                    "(last %s, want %s) -- continuing",
                    op.wv, op.wi, timeout, got.hex(), want.hex(),
                )
                if self._diag_readback_regs and self._is_cal_readback_site(op, want):
                    self._diag_read_regs(entry, detail)
                if self._cal_acquisition_not_observed(op, got, want):
                    self._on_cal_acquisition_not_observed(got, want, entry)
                return
            time.sleep(0.004)

    def _bufempty_site(self, op: Op, want: bytes) -> bool:
        """A buffer-empty gate site (CAL_WAIT_BUFEMPTY_MODES): extended
        status word (wValue 0x018e, wIndex 0x0122), captured as a 2-byte
        word with ack 0x55 and bit 0x40 clear, in a calibration acquisition
        phase. Pure check."""
        return (op.wv == 0x018E and op.wi == 0x0122 and len(want) == 2
                and want[1] == 0x55 and not want[0] & 0x40
                and getattr(self.session, "phase", None) in CAL_ACQUISITION_PHASES)

    def _poll_bufempty(self, op: Op, want: bytes, t_start: float, deadline: float,
                       timeout: float) -> None:
        """The enforced buffer-empty poll: only a 2-byte reply with ack 0x55
        and bit 0x40 clear, received by the poll's own deadline, ends it.
        Every read is bounded by the time left. At the deadline raise
        CalibrationError, before the phase's bulk read."""
        dev = self.io.dev
        got = None
        reads = 0
        while True:
            remaining = deadline - time.monotonic()
            if got is not None and remaining <= 0:
                late = True          # no transfer is started once the budget is spent
            else:
                got = bytes(dev.ctrl_transfer(op.bm, op.br, op.wv, op.wi, op.length,
                                              max(1, math.ceil(max(0.0, remaining) * 1000))))
                reads += 1
                if self._cal_integrity_telemetry:
                    self._cal_note_status_read(op.wi, got)
                late = time.monotonic() > deadline
                if not late and len(got) == 2 and got[1] == 0x55 and not got[0] & 0x40:
                    self._note_poll_settle(op, got, want, t_start, "bufempty_clear")
                    self._note_bufempty_wait("clear", t_start, reads, got)
                    return
            if late:
                self._diag_poll_timeouts += 1
                detail = None
                if len(self._diag_poll_timeout_details) < 20:
                    detail = {"wv": f"0x{op.wv:04x}", "wi": f"0x{op.wi:04x}",
                              "last": got.hex(), "want": want.hex()}
                    self._diag_poll_timeout_details.append(detail)
                entry = self._note_poll_settle(op, got, want, t_start, "timeout")
                self._note_bufempty_wait("timeout", t_start, reads, got)
                if entry is not None and self._cal_acquisition_not_observed(op, got, want):
                    entry["acq_not_observed"] = True
                if self._diag_readback_regs:
                    self._diag_read_regs(entry, detail)
                phase = getattr(self.session, "phase", None)
                log.warning("calibration buffer-empty gate in phase %s: no buffered "
                            "data within %.1fs (last %s, captured %s)", phase, timeout,
                            got.hex(), want.hex())
                raise safety.CalibrationError(
                    f"calibration buffer-empty gate in phase {phase}: the buffer-empty "
                    f"bit did not clear within the poll's budget of {timeout:.1f}s "
                    f"(last status word {got.hex()}, captured {want.hex()}). The "
                    f"measurement was not read and the scan stops here, before the "
                    f"bulk read (--cal-wait-bufempty enforce). "
                    f"{safety.NO_RECOVERY_ATTEMPTED} {safety.POWER_CYCLE_INSTRUCTION}",
                    session=self.session.snapshot())
            time.sleep(0.004)

    def _note_bufempty_wait(self, outcome: str, t_start: float, reads: int,
                            got: bytes) -> None:
        """Record one enforced buffer-empty wait in the diag
        (``cal_bufempty_waits``). ``engaged`` is False only when the first
        read already cleared. Records what the poll already read."""
        self._cal_bufempty_waits.append({
            "phase": getattr(self.session, "phase", None), "outcome": outcome,
            "wait_s": round(time.monotonic() - t_start, 4), "reads": reads,
            "last_value": got.hex(), "engaged": not (outcome == "clear" and reads == 1)})

    def _note_poll_settle(self, op: Op, got: bytes, want: bytes, t_start: float,
                          how: str) -> dict | None:
        """Record how a NON-strict poll ended (diag sidecar ``poll_settle``):
        ``how`` is "exact", "dataenb", "class", "reg32", "bufempty_clear"
        or "timeout", ``s``
        the seconds from the first read. Capped per scan so the sidecar
        stays small; ``poll_settle_max_s`` is kept over all of them. An
        extended status-word poll (wValue 0x018e, wIndex 0x0122) also
        records ``bits``: decode_status_word() of the last reply (None if
        malformed). Returns the entry, or None once the cap is reached.
        Pure bookkeeping, no USB traffic."""
        s = time.monotonic() - t_start
        if s > self._diag_poll_settle_max_s:
            self._diag_poll_settle_max_s = s
        if len(self._diag_poll_settle) < _POLL_SETTLE_CAP:
            entry = {
                "wv": f"0x{op.wv:04x}", "wi": f"0x{op.wi:04x}",
                "want": want.hex(), "got": got.hex(), "how": how, "s": round(s, 4),
            }
            if op.wv == 0x018E and op.wi == 0x0122:
                entry["bits"] = decode_status_word(got)
            self._diag_poll_settle.append(entry)
        else:
            entry = None
        if (how == "timeout" and self._cal_integrity_telemetry
                and self._readback_signature(op, got, want)):
            self._cal_note_not_observed()
        return entry

    def _is_cal_readback_site(self, op: Op, want: bytes) -> bool:
        """True for a calibration read-back poll: extended status word
        (wValue 0x018e, wIndex 0x0122) captured with the motor-enabled bit
        set, inside a calibration acquisition phase. Pure check."""
        return (op.wv == 0x018E and op.wi == 0x0122 and len(want) >= 1
                and bool(want[0] & 0x01)
                and getattr(self.session, "phase", None) in CAL_ACQUISITION_PHASES)

    def _diag_read_regs(self, entry: dict | None, detail: dict | None) -> None:
        """Opt-in diagnostic (diag_readback_regs): one IN read of register
        0x100 and one of register 0x101 after a read-back poll timed out,
        recorded as ``regs`` on the poll_settle entry and on the matching
        poll_timeout_details record, and logged. Reads only, through the
        same device path as every other read; no sleep or clock call.
        A failing read is logged once and ignored: it never changes
        control flow."""
        regs: dict = {}
        failures: list[str] = []
        for name, wi in (("reg100", 0x0022), ("reg101", 0x0122)):
            try:
                raw = bytes(self.io.dev.ctrl_transfer(0xC0, 0x04, 0x018E, wi, 2))
            except Exception as e:
                failures.append(f"{name}: {type(e).__name__}: {e}")
                regs[name] = {"error": type(e).__name__}
                continue
            if len(raw) != 2 or raw[1] != 0x55:
                regs[name] = None
            elif name == "reg100":
                regs[name] = {"raw": f"{raw[0]:02x}", "data_enable": raw[0] & 0x01}
            else:
                regs[name] = {"raw": f"{raw[0]:02x}", "bits": decode_status_word(raw)}
        if failures:
            log.warning("diagnostic register read failed (ignored): %s",
                        "; ".join(failures))
        if entry is not None:
            entry["regs"] = regs
        if detail is not None:
            detail["regs"] = regs
        r100, r101 = regs.get("reg100"), regs.get("reg101")
        log.warning(
            "read-back timeout registers: reg100=%s data_enable=%s reg101=%s (%s)",
            r100.get("raw") if r100 else None,
            r100.get("data_enable") if r100 else None,
            r101.get("raw") if r101 else None,
            format_status_bits(r101.get("bits")) if r101 else "unavailable",
        )

    def _cal_acquisition_not_observed(self, op: Op, got: bytes, want: bytes) -> bool:
        """True when a timed-out non-strict poll matches the read-back
        signature (CAL_READBACK_GUARD_MODES comment): guard not "off",
        an extended status-word poll (wValue 0x018e, wIndex 0x0122), a
        captured 2-byte word with ack 0x55, bit 0x01 set and bit 0x40
        clear, a last reply with ack 0x55, bit 0x01 clear and bit 0x40
        set, inside a calibration acquisition phase. A malformed reply
        never matches. Pure check, no USB traffic."""
        if self._cal_readback_guard == "off":
            return False
        return self._readback_signature(op, got, want)

    def _readback_signature(self, op: Op, got: bytes, want: bytes) -> bool:
        """The read-back signature of _cal_acquisition_not_observed()
        without the guard-mode check (the telemetry records it in every
        mode). Pure check, no USB traffic."""
        if op.wv != 0x018E or op.wi != 0x0122:
            return False
        if len(want) != 2 or want[1] != 0x55 or not want[0] & 0x01 or want[0] & 0x40:
            return False
        if len(got) != 2 or got[1] != 0x55 or got[0] & 0x01 or not got[0] & 0x40:
            return False
        return getattr(self.session, "phase", None) in CAL_ACQUISITION_PHASES

    def _on_cal_acquisition_not_observed(self, got: bytes, want: bytes,
                                         entry: dict | None) -> None:
        """Record the read-back signature; in "fail" mode refuse here,
        before the phase's next transfer (no bulk read, no write)."""
        if entry is not None:
            entry["acq_not_observed"] = True
        phase = getattr(self.session, "phase", None)
        log.warning("calibration read-back in phase %s: acquisition not observed "
                    "(last %s, captured %s)", phase, got.hex(), want.hex())
        if self._cal_readback_guard == "fail":
            raise safety.CalibrationError(
                f"calibration acquisition not observed in phase {phase}: engine "
                f"stopped, buffer empty (status word {got.hex()}, captured "
                f"{want.hex()}). The measurement was not read and the scan stops "
                f"here, before the phase's next transfer (--cal-readback-guard "
                f"fail). {safety.NO_RECOVERY_ATTEMPTED} {safety.POWER_CYCLE_INSTRUCTION}",
                session=self.session.snapshot())

    # ------------------------------------------- calibration stop gate

    def _scanstop_note_write(self, data: bytes) -> None:
        """A register write that reached the device ("enforce" only): an
        execute pulse in a calibration acquisition phase opens an
        acquisition; a later write of register 0x01 with bit 0 clear in
        the same phase arms the gate. Pairs are taken in wire order."""
        phase = getattr(self.session, "phase", None)
        if phase not in CAL_ACQUISITION_PHASES:
            self._scanstop_go_phase = None
            return
        for i in range(0, len(data) - 1, 2):
            reg, val = data[i], data[i + 1]
            if reg == 0x0F and val == 0x01:
                self._scanstop_go_phase = phase
            elif reg == 0x01 and not val & 0x01 and self._scanstop_go_phase == phase:
                self._scanstop_go_phase = None
                self._scanstop_armed = phase
                self._scanstop_t_clear = time.monotonic()
                self._scanstop_polled_s = 0.0

    @staticmethod
    def _bit0_clear(raw: bytes) -> bool:
        """A 2-byte reply with the 0x55 acknowledge and bit 0 clear."""
        return len(raw) == 2 and raw[1] == 0x55 and not raw[0] & 0x01

    def _cal_wait_scan_stop(self) -> None:
        """The armed stop gate (CAL_WAIT_SCANSTOP_MODES), run before the
        next OUT transfer: register 0x100 bit 0 clear, then register 0x101
        bit 0 clear. ONE deadline for both registers: NONSTRICT_POLL_FLOOR_S
        from the gate's start, less the time the phase's own post-clear poll
        of register 0x100 already spent (that budget is not granted twice);
        every read is bounded by the time left
        and a reply counts only if it arrived by the deadline. Reads only;
        at the deadline CalibrationError is raised before any write. The
        deadline is not taken from the SCAN-clear itself: legitimate host
        gaps can follow a clear (the lamp warm-up re-runs the white phase
        after an interval sleep), and they must not turn into refusals."""
        phase = self._scanstop_armed
        self._scanstop_armed = None
        dev = self.io.dev
        t_start = time.monotonic()
        deadline = t_start + max(0.0, NONSTRICT_POLL_FLOOR_S - self._scanstop_polled_s)
        rec = {"phase": phase, "reads_100": 0, "reads_101": 0,
               "last_100": None, "last_101": None, "outcome": None, "wait_s": None,
               "since_clear_s": None}
        self._cal_scanstop_waits.append(rec)
        for wi, key in ((0x0022, "100"), (0x0122, "101")):
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._scanstop_refuse(rec, phase, key, t_start)
                try:
                    got = bytes(dev.ctrl_transfer(0xC0, 0x04, 0x018E, wi, 2,
                                                  max(1, math.ceil(remaining * 1000))))
                except Exception as e:
                    if time.monotonic() >= deadline:
                        self._scanstop_refuse(rec, phase, key, t_start, cause=e)
                    raise
                rec[f"reads_{key}"] += 1
                rec[f"last_{key}"] = got.hex()
                if time.monotonic() > deadline:
                    self._scanstop_refuse(rec, phase, key, t_start)
                if self._bit0_clear(got):
                    break
                time.sleep(0.004)
        now = time.monotonic()
        rec["outcome"] = "clear"
        rec["wait_s"] = round(now - t_start, 4)
        rec["since_clear_s"] = round(now - self._scanstop_t_clear, 4)

    def _scanstop_refuse(self, rec: dict, phase, key: str, t_start: float,
                         cause: BaseException | None = None) -> None:
        """Record the stop gate's timeout and raise CalibrationError."""
        now = time.monotonic()
        last = rec.get(f"last_{key}")
        rec["outcome"] = "timeout"
        rec["wait_s"] = round(now - t_start, 4)
        rec["since_clear_s"] = round(now - self._scanstop_t_clear, 4)
        log.warning("calibration stop gate after phase %s: register 0x%s bit 0 not "
                    "seen clear (last %s) by the deadline", phase, key, last)
        raise safety.CalibrationError(
            f"calibration scan-stop gate after phase {phase}: the previous "
            f"acquisition is still running (register 0x{key} last reply {last}, "
            f"bit 0 not seen clear within {NONSTRICT_POLL_FLOOR_S}s of the "
            f"scan-clear). Nothing further was sent (--cal-wait-scanstop enforce). "
            f"{safety.NO_RECOVERY_ATTEMPTED} {safety.POWER_CYCLE_INSTRUCTION}",
            session=self.session.snapshot()) from cause

    # ------------------------------------- calibration stale-read telemetry
    # Observers called from _exec_ops/_note_poll_settle AFTER a transfer
    # the driver makes anyway (see CAL_SERVED_FAST_S). They read only
    # time.perf_counter, never send, sleep or raise.

    def _cal_open_acquisition(self) -> dict | None:
        """The current phase's acquisition whose execute pulse was seen and
        whose SCAN-clear was not, else None."""
        accs = getattr(self, "_cal_acquisitions", None)
        if not accs:
            return None
        a = accs[-1]
        if a["phase"] != getattr(self.session, "phase", None) or "_t_clear" in a:
            return None
        return a

    def _cal_note_write(self, data: bytes) -> None:
        """Register write: an execute pulse in a calibration acquisition
        phase opens a record; the first SCAN-clear (register 0x01 = 0x02)
        after it is timed."""
        try:
            phase = getattr(self.session, "phase", None)
            accs = getattr(self, "_cal_acquisitions", None)
            if phase not in CAL_ACQUISITION_PHASES or accs is None:
                return
            pairs = [(data[i], data[i + 1]) for i in range(0, len(data) - 1, 2)]
            if safety.has_execute_pulse(data):
                t = time.perf_counter()
                prev = accs[-1] if accs else None
                rec = {"phase": phase, "_t_go": t, "rearm_gap_s": None,
                       "prev_phase": None, "prev_post": None, "prev_running": None,
                       "go_to_data_s": None, "served_fast": False,
                       "acq_not_observed": False, "identical_channel_means": None}
                if prev is not None and "_t_clear" in prev:
                    post = self._cal_post_words(prev)
                    rec["prev_phase"] = prev["phase"]
                    rec["rearm_gap_s"] = round(t - prev["_t_clear"], 6)
                    rec["prev_post"] = post
                    if post is not None:
                        rec["prev_running"] = bool(post.get("data_enabled")
                                                   or post.get("motor_enabled"))
                accs.append(rec)
            elif (0x01, 0x02) in pairs:
                a = self._cal_open_acquisition()
                if a is not None:
                    a["_t_clear"] = time.perf_counter()
        except Exception as e:      # telemetry must never reach scanner I/O
            log.debug("calibration telemetry: write not recorded (%s)", e)

    def _cal_note_status_read(self, wi: int, got: bytes) -> None:
        """The first read of register 0x100 (wIndex 0x0022) and of register
        0x101 (wIndex 0x0122) after an acquisition's SCAN-clear."""
        accs = getattr(self, "_cal_acquisitions", None)
        if not accs:
            return
        a = accs[-1]
        key = "_post_100" if wi == 0x0022 else "_post_101"
        if ("_t_clear" in a and key not in a
                and a["phase"] == getattr(self.session, "phase", None)):
            a[key] = bytes(got)

    @staticmethod
    def _cal_post_words(rec: dict) -> dict | None:
        """Decode a record's post-clear reads; None when neither is
        well-formed (2 bytes, 0x55 ack). A missing or malformed register
        is recorded as None and contributes nothing to prev_running."""
        out: dict = {}
        words = []
        r100, r101 = rec.get("_post_100"), rec.get("_post_101")
        if r100 is not None and len(r100) == 2 and r100[1] == 0x55:
            out["reg_0x100"] = f"{r100[0]:02x}"
            out["data_enabled"] = r100[0] & 0x01
            words.append(f"data-enable {'on' if r100[0] & 0x01 else 'off'}")
        bits = decode_status_word(r101) if r101 is not None else None
        if bits is not None:
            out["reg_0x101"] = f"{r101[0]:02x}"
            out["motor_enabled"] = bits["motor_enabled"]
            out["status_bits"] = bits
            words.append(f"motor-enable {'on' if bits['motor_enabled'] else 'off'}")
        if not out:
            return None
        out["words"] = ", ".join(words)
        return out

    def _cal_note_bulk(self) -> None:
        """A bulk read completed: execute pulse to the end of the open
        acquisition's latest bulk read."""
        a = self._cal_open_acquisition()
        if a is not None:
            s = time.perf_counter() - a["_t_go"]
            a["go_to_data_s"] = round(s, 6)
            a["served_fast"] = s < CAL_SERVED_FAST_S

    def _cal_note_not_observed(self) -> None:
        a = self._cal_open_acquisition()
        if a is not None:
            a["acq_not_observed"] = True

    def _cal_note_channel_means(self, phase_name: str, raw: bytes) -> None:
        """identical_channel_means on the phase's last record: the three
        per-channel sums of the (N, 3) little-endian 16-bit buffer are
        exactly equal (integer arithmetic). Buffers already read; host-only."""
        if not self._cal_integrity_telemetry:
            return
        for a in reversed(self._cal_acquisitions):
            if a["phase"] == phase_name:
                if raw and len(raw) % 6 == 0:
                    sums = np.frombuffer(raw, dtype="<u2").reshape(-1, 3).sum(
                        axis=0, dtype=np.int64)
                    a["identical_channel_means"] = bool(sums[0] == sums[1] == sums[2])
                return

    def _cal_integrity_diag(self, dark_b_substituted: bool, dark_b_fallback) -> dict:
        """cal_integrity / cal_integrity_reasons / cal_acquisitions for the
        diag of a fresh calibration; cal_integrity None when the telemetry
        is off."""
        if not self._cal_integrity_telemetry:
            return {"cal_integrity": None, "cal_integrity_reasons": [],
                    "cal_acquisitions": []}
        accs = []
        for a in self._cal_acquisitions:
            pub = {k: v for k, v in a.items() if not k.startswith("_")}
            pub["stale_suspect"] = cal_stale_suspect(pub)
            accs.append(pub)
        verdict, reasons = cal_integrity_verdict(accs, dark_b_substituted, dark_b_fallback)
        return {"cal_integrity": verdict, "cal_integrity_reasons": reasons,
                "cal_acquisitions": accs}

    def _cal_integrity_refuse(self, checkpoint: str) -> None:
        """--cal-readback-guard fail only (EXPERIMENTAL, unverified on
        hardware): raise CalibrationError when any calibration acquisition
        recorded so far is stale_suspect (cal_stale_suspect) or has
        identical per-channel means. Called after the dark pair (before any
        dark_b substitution or default-offset fallback), after the
        gain-check pair and after the shading verify (before POSITION).
        Needs the integrity telemetry records (default on); with the
        telemetry off there is nothing to judge. Host-side only."""
        if self._cal_readback_guard != "fail" or not self._cal_integrity_telemetry:
            return
        reasons = self._cal_integrity_fail_reasons()
        if not reasons:
            return
        log.warning("calibration integrity check failed %s: %s", checkpoint,
                    "; ".join(reasons))
        raise safety.CalibrationError(
            f"calibration integrity check failed {checkpoint}: {'; '.join(reasons)}. "
            f"The measurement is not trusted: no dark_b substitution and no default "
            f"offsets are used, and the scan stops here (--cal-readback-guard fail). "
            f"{safety.NO_RECOVERY_ATTEMPTED} {safety.POWER_CYCLE_INSTRUCTION}",
            session=self.session.snapshot())

    def _cal_integrity_fail_reasons(self) -> list[str]:
        """The reasons --cal-readback-guard fail refuses on, from this
        scan's acquisition records (empty with the telemetry off)."""
        if not self._cal_integrity_telemetry:
            return []
        reasons = []
        for a in self._cal_acquisitions:
            if cal_stale_suspect(a):
                reasons.append(
                    f"{a['phase']}: stale (re-armed while the previous acquisition was "
                    f"still running; "
                    f"{'acquisition not observed' if a.get('acq_not_observed') else 'data served fast'})")
            if a.get("identical_channel_means") is True:
                reasons.append(f"{a['phase']}: identical per-channel means")
        return reasons

    @staticmethod
    def _warn_cal_integrity(frame, integ: dict) -> None:
        """ONE integrity summary warning per frame when its calibration is
        degraded (the existing per-event warnings, e.g. the dark_b residual
        one, are unchanged and may precede it)."""
        if integ.get("cal_integrity") == "degraded":
            log.warning("calibration integrity DEGRADED for frame %s: %s "
                        "(diagnostic only; the scan continued unchanged)",
                        frame, "; ".join(integ.get("cal_integrity_reasons") or []))

    def _run_phase(self, phase: Phase, strict_polls: frozenset[int] = frozenset(),
                   strict_mask: int = LOAD_STATUS_MASK, poll_timeout_scale: float = 1.0,
                   **inject) -> list[bytes]:
        """Execute a phase's full op stream (with injections applied),
        returning its collected read buffers.

        Timed into self._diag_phase_seconds[phase.name] (accumulated,
        in case a phase name is ever run more than once per scan) for
        the per-scan diagnostics -- see diag.py and scan()/_scan_dual().
        """
        ops = phase.patched(**inject) if inject else phase.ops
        self.session.phase = phase.name
        t0 = time.monotonic()
        try:
            return self._exec_ops(ops, strict_polls=strict_polls, strict_mask=strict_mask,
                                  poll_timeout_scale=poll_timeout_scale)
        finally:
            dt = time.monotonic() - t0
            self._diag_phase_seconds[phase.name] = self._diag_phase_seconds.get(phase.name, 0.0) + dt

    # -------------------------------------------------------------- park

    def _park(self, t, ir: bool = False) -> None:
        """Dispatch on self.park_mode: "verbatim" replays `t.PARK`'s
        captured op stream exactly as before (byte-identical, see
        test_scan_sequence_matches_trace/test_dpi.py's
        test_scan_sequences -- every dpi's own table module has its
        own PARK data, hence `t` is the caller's actual table module);
        "semantic" runs park_semantic(t), which takes its table-
        specific constants (the two 0x8b control-write payloads and
        the optional 0x19=0x00 write) from that same `t.PARK`. Called
        at the end of scan()/_scan_dual() in place of the former bare
        self._run_phase(<tables>.PARK)."""
        if self.park_mode == "verbatim":
            self._run_phase(t.PARK)
        elif self.park_mode == "semantic":
            self.park_semantic(t, ir=ir)
        else:
            raise ValueError(f"unknown park_mode {self.park_mode!r} (want 'verbatim' or 'semantic')")

    def park_semantic(self, t=None, ir: bool = False) -> None:
        """Semantic replacement for the captured PARK phase (see
        docs/replay-analysis.md's "Conversion order" step 1 and PARK's
        row in the phase-by-phase table).

        The captured PARK op stream is a real park/teardown sequence
        followed by five identical repetitions of the vendor app's
        idle-loop heartbeat (36=fc, 3a=00, 36=fc, 33=0e, read/write-
        back reg 0x32, a 2 s pause, read reg 0x35, poll reg 0x32) --
        the repetitions are the vendor polling for completion, not a
        hardware requirement. This reproduces the same writes with:

          - real read-modify-write on regs 0x15 (clear bit 0x10),
            0x32 (write back what was read, unchanged) and 0x35
            (clear bit 0x40) instead of the captured constants, which
            are only correct on the capture unit/session;
          - Wait A: poll reg 0x35 until bit 0x40 is set (replaces the
            captured 0.74 + 2.06 s pacing before the RMW clear);
          - Wait B: poll the status word (reg 0x101) until it reads
            like every observed successful park end --
            park_complete_status_matches(): bits 0x80/0x40/0x20 set,
            busy bit 0x01 and never-seen bit 0x02 clear, 0x10/0x04
            (vary between observed park ends) and 0x08 (loader sensor)
            ignored; class D/C never accepted (docs/park-completion-
            analysis.md). Until 2026-09-05 this waited for
            reg 0x32 to equal the captured 0x95 (masked 0x18), which
            Test 23 showed to be a session-variable value (0xb5 on
            hardware, 0x81/0x95/0x8d/0x15 across captures);
          - exactly ONE idle-loop round afterwards (heartbeat only,
            no 2 s pause), instead of five.

        Both waits are bounded (_PARK_WAIT_TIMEOUT / _PARK_WAIT_B_TIMEOUT,
        raised 2026-09-17 to cover Test 61's dual/IR park durations) and
        FAIL CLOSED: a timeout records the wait in the diagnostics and
        raises safety.StrictPollTimeoutError inside the "park"
        operation -- the session is FAILED, nothing further is written
        (no heartbeat, no RMW clear), a power cycle is required. Wait A
        follows the carriage-return write (0x02=0x30): a transport that
        has not reported home by then must not be handed to the next
        frame's absolute POSITION move (Test 18's frame-4 lesson).
        Earlier revisions logged and continued here; changed 2026-09-05
        (P3 review) before any hardware A/B.

        The table-specific constants come from `t.PARK` itself (`t`
        defaults to tables_ir when `ir` else tables): the 0x8b
        control-write payloads (wIndex 0x0b: 0c000100 in the 3600 dpi
        captures, 22000100 in the 600-7200 dpi captures; wIndex 0x0f:
        e0ff/c0ff/f8ff/feff/fcff/f0ff -- varies per dpi and IR, meaning
        unknown, kept verbatim per table) and whether a 0x19=0x00 write
        follows the 0x32 write-back (every dual-light table has it, the
        plain 3600 table does not). Everything else in PARK is
        identical across all six tables.

        Records total elapsed time into self._diag_phase_seconds
        ["park"] and wait details into self._diag_park_waits (see
        scan()/_scan_dual()'s last_diag "park_waits" key).
        """
        if t is None:
            t = tables_ir if ir else tables
        park_ops = t.PARK.ops
        ctrl_8b = [(op.wi, bytes(op.data)) for op in park_ops
                   if op.kind == "cw" and op.wv == 0x008B]
        has_0x19 = any(
            op.kind == "cw" and op.wv == 0x0083
            and any(op.data[i] == 0x19 for i in range(0, len(op.data), 2))
            for op in park_ops
        )
        if len(ctrl_8b) != 2:
            raise Of135iError(
                f"park_semantic: expected 2 control writes with wValue 0x8b in "
                f"{t.__name__}.PARK, found {len(ctrl_8b)}"
            )
        with self._operation("park"):
            self._park_semantic_body(ctrl_8b, has_0x19)

    def _park_semantic_body(self, ctrl_8b, has_0x19) -> None:
        self.session.phase = "park"
        waits = {
            "a_seconds": None, "a_timed_out": False,
            "b_seconds": None, "b_timed_out": False, "b_last": None,
        }
        # The wait record survives every exit -- timeout, USB error,
        # Ctrl-C -- so the sidecar/report shows how far the park got.
        self._diag_park_waits = waits
        try:
            self._park_semantic_steps(ctrl_8b, has_0x19, waits)
        finally:
            self._diag_park_waits = waits

    def _park_semantic_steps(self, ctrl_8b, has_0x19, waits: dict) -> None:
        t0 = time.monotonic()
        dev = self.io.dev
        # The carriage returns from wherever the frame was, so both waits
        # get the same linear stretch POSITION's own budget got (>= 1.0;
        # exactly 1.0 for frame 1 and for a park with no preceding
        # position, so nothing that was verified gets a shorter bound).
        park_scale = max(1.0, float(getattr(self, "_park_scale", 1.0)))
        waits["scale"] = park_scale

        # ---- real park/teardown sequence (captured ops 0-55) -----------
        dev.ctrl_transfer(0x40, 0x0C, 0x8D, 0, b"\x00")
        self.io.write_regs([(0x03, 0x30)])
        self.io.write_regs([(0x03, 0x20)])
        self.io.write_regs([(0x01, 0x22)])
        self.io.write_regs([(0x3A, 0x00)])

        v15 = self.io.read_reg(0x15)
        self.io.write_regs([(0x15, v15 & ~0x10 & 0xFF)])

        self.io.write_regs([(0x02, 0x30)])
        self.io.write_regs([(0x36, 0xFC), (0x3A, 0x00), (0x36, 0xFC), (0x33, 0x0E)])
        self.io.write_regs([(0x03, 0x10)])
        self.io.write_regs([(0x03, 0x00)])

        for wi, payload in ctrl_8b:
            dev.ctrl_transfer(0x40, 0x04, 0x8B, wi, payload)

        v32 = self.io.read_reg(0x32)
        self.io.write_regs([(0x32, v32)])
        if has_0x19:
            self.io.write_regs([(0x19, 0x00)])

        # ---- Wait A: reg 0x35 bit 0x40 set, then clear it (RMW) ---------
        _t = time.monotonic()
        deadline = _t + _PARK_WAIT_TIMEOUT * park_scale
        v35 = self.io.read_reg(0x35)
        while not (v35 & 0x40):
            if time.monotonic() > deadline:
                waits["a_timed_out"] = True
                waits["a_seconds"] = time.monotonic() - _t
                self._diag_park_waits = waits
                raise safety.StrictPollTimeoutError(
                    f"park_semantic: wait A (reg 0x35 bit 0x40, carriage home after 0x02=0x30) "
                    f"did not complete within {_PARK_WAIT_TIMEOUT * park_scale:.0f}s: last 0x{v35:02x}. The "
                    f"transport state is unknown; the park stops here. "
                    f"{safety.NO_RECOVERY_ATTEMPTED} {safety.POWER_CYCLE_INSTRUCTION}",
                    last=bytes([v35]), want=bytes([0x40]), observed=self.session.start_reg01,
                    session=self.session.snapshot())
            time.sleep(_PARK_POLL_INTERVAL)
            v35 = self.io.read_reg(0x35)
        waits["a_seconds"] = time.monotonic() - _t
        log.debug(
            "park_semantic: wait A (reg 0x35 bit 0x40) done in %.3fs (last 0x%02x, timed_out=%s)",
            waits["a_seconds"], v35, waits["a_timed_out"],
        )
        self.io.write_regs([(0x35, v35 & ~0x40 & 0xFF)])

        # ---- Wait B: the status word reads PARK_COMPLETE after the
        # carriage return (park_complete_status_matches: bits 0x80/0x40/
        # 0x20 set, 0x02/0x01 clear; docs/park-completion-analysis.md).
        # Replaces the former target
        # "reg 0x32 == 0x95 masked 0x18", a session-variable value that
        # stopped Test 23 on 0xb5. Bounded by _PARK_WAIT_B_TIMEOUT; a
        # timeout, a malformed reply that never clears, a USB error or
        # Ctrl-C ends the operation here -- nothing is written after.
        _t = time.monotonic()
        deadline = _t + _PARK_WAIT_B_TIMEOUT * park_scale
        reply = bytes(dev.ctrl_transfer(0xC0, 0x04, 0x018E, 0x0122, 2))
        while not park_complete_status_matches(reply):
            if time.monotonic() > deadline:
                waits["b_timed_out"] = True
                waits["b_seconds"] = time.monotonic() - _t
                waits["b_last"] = reply.hex()
                self._diag_park_waits = waits
                raise safety.StrictPollTimeoutError(
                    f"park_semantic: wait B (status word PARK_COMPLETE after the carriage return: "
                    f"(byte & {PARK_COMPLETE_REQUIRED_MASK:#04x}) == {PARK_COMPLETE_REQUIRED_VALUE:#04x}, "
                    f"ack {PARK_COMPLETE_ACK:#04x}) did not complete within {_PARK_WAIT_B_TIMEOUT * park_scale:.0f}s: "
                    f"last {reply.hex() or '(empty)'}. The park stops here. "
                    f"{safety.NO_RECOVERY_ATTEMPTED} {safety.POWER_CYCLE_INSTRUCTION}",
                    last=reply, want=bytes([PARK_COMPLETE_REQUIRED_VALUE, PARK_COMPLETE_ACK]),
                    observed=self.session.start_reg01, session=self.session.snapshot())
            time.sleep(_PARK_POLL_INTERVAL)
            reply = bytes(dev.ctrl_transfer(0xC0, 0x04, 0x018E, 0x0122, 2))
        waits["b_seconds"] = time.monotonic() - _t
        waits["b_last"] = reply.hex()
        log.debug("park_semantic: wait B (status word PARK_COMPLETE) done in %.3fs (last %s)",
                  waits["b_seconds"], reply.hex())

        # ---- one idle-loop round (heartbeat), no captured 2 s pause -----
        self.io.write_regs([(0x36, 0xFC), (0x3A, 0x00), (0x36, 0xFC), (0x33, 0x0E)])
        v32b = self.io.read_reg(0x32)
        self.io.write_regs([(0x32, v32b)])

        self._diag_park_waits = waits
        dt = time.monotonic() - t0
        self._diag_phase_seconds["park"] = self._diag_phase_seconds.get("park", 0.0) + dt

    # -------------------------------------------------------------- init

    def initialize(self, ir: bool = False, dpi: int = 3600, prep: bool = True) -> None:
        """Full device initialization.

        First (once per session) the power-on base register table + AFE
        base values -- what the vendor driver programs at device open;
        without these the scanner keeps whatever state the previous
        session left, which breaks calibration (observed 2026-08-30 as
        a saturated white measurement after a VM session). Then the
        pre-scan phases from the capture (tables.PREP + AFE_BASE), full
        op stream.

        Called once per frame in a batch. The vendor never rewrites the
        power-on table between frames -- only the PREP/AFE_BASE
        equivalent (protocol-notes.md pass 14) -- so the base table is
        written on the first call only.

        Cold start: the session's start-state check (safety.py) reads
        reg 0x01 once before the first write. 0x22 (idle-homed) is the
        state every successful run to date started from; 0x00 (fresh
        power-on, never homed) makes this run the vendor's cold-start
        sequence first, exactly once per session -- and nothing else
        is accepted: any other value, or an unreadable one, refuses
        the whole session with zero writes.

        ``prep=False``: the vendor's device-open state for the magazine
        flow -- replays tables_load.OPEN (clean-load capture ops 37-88:
        chip-id ack, the app-start register table with the LOADER motor
        profile, acks, AFE enable, EEPROM reads, AFE reset, AFE base
        values) verbatim instead of BASE_INIT_PAIRS + AFE values, after
        cold_init() if the scanner is cold. BASE_INIT_PAIRS carries the
        scan motor profile (0x7e/0x7f) and the jog's first move takes
        its profile from the table: on top of BASE_INIT_PAIRS it ran at
        scan speed (Test 16). No scan preparation is run; scan() still
        requires a prep=True initialize() in the same session, which
        then writes BASE_INIT_PAIRS as usual.
        """
        # Validate parameters before entering the operation, so a bad
        # argument is a plain error and not a hardware-session failure.
        t = _tables_for(dpi, ir)
        with self._operation("initialize", cold_ok=True):
            if not prep:
                from . import tables_load
                if self.session.state is SessionState.COLD:
                    log.info("initialize: reg 0x01=0x00 -- cold-start state, running cold_init() first")
                    self.cold_init()
                self.session.phase = "vendor_open"
                self._run_phase(tables_load.OPEN)
                self._vendor_open = True
                return
            if not self._base_initialized:
                if self.session.state is SessionState.COLD:
                    log.info(
                        "initialize: reg 0x01=0x00 -- cold-start state (fresh "
                        "power-on), running cold_init() first",
                    )
                    self.cold_init()
                self.session.phase = "base_init"
                self.io.write_regs(tables_base.BASE_INIT_PAIRS)
                for adr, val in tables_base.AFE_BASE_PAIRS:
                    self.io.write_regs([(0x51, adr), (0x5D, 0x00), (0x5E, val)])
                self._base_initialized = True
            # IR mode: trace 04's own prep carries IR-LED setup that the
            # plain prep lacks (a dim IR pass was observed without it,
            # 2026-08-30) -- run the matching phase set. Other resolutions:
            # their own prep/afe_base (the base register table is dpi-
            # dependent: regs 0x3b/0x3c, and the sensor timing at 7200).
            self._run_phase(t.PREP)
            self._run_phase(t.AFE_BASE)
            self._prepared_for_scan = True

    # ---------------------------------------------------------- cold-start

    def cold_init(self) -> None:
        """Vendor cold-start sequence (USB capture 01-init.pcap).

        A freshly power-cycled scanner has never been homed: reg 0x01
        reads without the vendor's "ready" bit (0x20) set, and
        initialize()'s usual power-on table assumes an already-homed
        transport (0x01=0x22, 0x32=0x1f -- see driver-design.md/
        protocol-notes.md). This reproduces the vendor's own recovery
        from that state: a GL chip handshake, the cold-start register
        table (COLD_INIT_PAIRS -- the loader motor speed profile,
        not the scan one), an AFE bring-up (access enable, EEPROM
        read, reset, base values), an interrupt/sensor toggle, and
        three rounds of loader homing moves (3 moves each), with the
        register table + AFE sequence rewritten between rounds.

        Called automatically by initialize()/eject() on a cold scanner;
        can also be called directly, but ONLY from the cold start state
        (reg 0x01 = 0x00, see safety.py): on an already-homed scanner,
        or one in any other state, it refuses with zero writes -- re-
        homing a magazine-loaded transport is not a verified sequence.
        Leaves the scanner in the same homed state initialize()
        otherwise assumes, but does NOT itself write BASE_INIT_PAIRS --
        COLD_INIT_PAIRS carries the loader (not scan) motor profile, so
        initialize() still runs its normal power-on table write
        afterwards.

        Two kinds of wait, deliberately different (docs/test-log.md,
        "Offline 2026-09-15"): the opening ready poll, the per-round
        ready poll and the settle check are best-effort -- they log and
        continue, and on a latched magazine (or, for the opening one, on
        every cold start) they time out and must not stop the sequence.
        Each of the NINE motor completions is STRICT: it is the only
        thing between one motor start and the next, and a timeout there
        ends the cold start with nothing further written (see
        COLD_MOVE_TIMEOUT). When the sequence completes, reg 0x01 is
        read again (strictly) and must be 0x22 -- COLD_INIT_PAIRS
        writes 0x01=0x22 and the idle bit 0x20 sets when the last
        homing move completes -- for the session to be armed for normal
        operations. Anything else is a new observation: the session is
        refused there, no further command is sent, and the operator is
        asked to power-cycle (and to record the value).
        """
        # Precondition BEFORE the operation (a refusal here is not a
        # hardware failure): the read-only start check, then COLD-only.
        self.check_start_state()
        if self.session.state is not SessionState.COLD or self.session.cold_init_done:
            raise OperationNotAllowedError(
                "cold_init() is only permitted from the cold start state (reg 0x01 = "
                f"0x00) and only once per session; session is {self.session.state.value}"
                f"{' (cold_init already done)' if self.session.cold_init_done else ''}. "
                f"{safety.NO_COMMANDS_SENT}",
                observed=self.session.start_reg01, session=self.session.snapshot())
        with self._operation("cold_init", cold_ok=True):
            self.session.phase = "cold_init"
            self._cold_init_body()
            self.session.phase = "cold_init:verify"
            val01 = self.io.read_reg(0x01, strict=True)
            if val01 != safety.REG01_IDLE:
                self.session.refuse(
                    f"cold_init completed but reg 0x01 = {val01:#04x} instead of "
                    f"{safety.REG01_IDLE:#04x}", val01)
                raise UnsafeStartStateError(
                    f"cold_init completed but the scanner reads reg 0x01 = {val01:#04x}, "
                    f"not the expected idle state {safety.REG01_IDLE:#04x}. No further "
                    f"commands were sent. This is a new observation -- please record it. "
                    f"{safety.POWER_CYCLE_INSTRUCTION}",
                    observed=val01, session=self.session.snapshot())
            self.session.arm_after_cold_init(val01)
            log.info("cold_init: complete, reg 0x01 = %#04x -- session armed", val01)

    def _cold_init_body(self) -> None:
        log.info("cold_init: starting vendor cold-start sequence")

        # ---- 1: GL chip handshake --------------------------------------
        chip_id = bytes(self.io.dev.ctrl_transfer(0xC0, 0x0C, 0x008A, 0x26FE, 1))
        log.info("cold_init: chip id %s", chip_id.hex())
        self.io.dev.ctrl_transfer(0x40, 0x0C, 0x008B, 0x26FE, b"")

        # ---- 2: status word, poll until ready ---------------------------
        word = self.io.read_status_word()
        log.info("cold_init: initial status word %#06x", word)
        self.io.poll_status_word(mask=0xF000, value=0xF000, timeout=COLD_READY_TIMEOUT)

        # ---- 3-7: cold register table + end_access + AFE bring-up ------
        self._cold_write_table_and_afe()

        # ---- 8: interrupt/sensor setup -----------------------------------
        val31 = self.io.read_reg(0x31)
        self.io.write_regs([(0x31, val31 & 0x7F)])
        val31 = self.io.read_reg(0x31)
        self.io.write_regs([(0x31, (val31 | 0x80) & 0xFF)])

        # ---- 9: up to three rounds of loader homing (3 moves each) --------
        rounds = self._cold_homing_rounds
        for round_n in range(1, rounds + 1):
            log.info("cold_init: homing round %d/%d", round_n, rounds)
            self.session.phase = f"cold_init:homing-{round_n}"
            self._cold_homing_round()
            if round_n < rounds:
                self._cold_write_table_and_afe()

        log.info("cold_init: homing done")

    def _cold_write_table_and_afe(self) -> None:
        """Steps 3-7 of cold_init(): the cold register table, end-of-
        access acks, AFE access enable + EEPROM read (logged only, not
        yet consumed), AFE reset, and AFE base values. Re-run between
        each of the three homing rounds in cold_init()'s step 9, per
        the capture."""
        self.io.write_regs(tables_base.COLD_INIT_PAIRS)

        self.io.end_access(0x8C, 16)
        self.io.end_access(0x8C, 19)

        # AFE access enable.
        self.io.write_regs([(0x0B, 0x64)])
        self.io.write_regs([(0x13, 0x0F)])
        self.io.write_regs([(0x0B, 0x6C)])

        # EEPROM read sequence -- logged for now, not yet used.
        self.io.end_access(0x8B, 7)
        self.io.dev.ctrl_transfer(0x40, 0x04, 0x008B, 0x0009, bytes(4))
        eeprom1 = bytes(self.io.dev.ctrl_transfer(0xC0, 0x04, 0x008A, 0x0010, 3))
        self.io.dev.ctrl_transfer(0x40, 0x04, 0x008B, 0x000B, b"\x00\x00\x01\x00")
        eeprom2 = bytes(self.io.dev.ctrl_transfer(0xC0, 0x04, 0x008A, 0x000F, 19))
        log.info("cold_init: EEPROM data: %s / %s", eeprom1.hex(), eeprom2.hex())

        # AFE reset.
        self.io.write_regs([(0x03, 0x10)])
        self.io.write_regs([(0x03, 0x00)])

        # AFE base values.
        for adr, val in tables_base.AFE_BASE_PAIRS:
            self.io.write_regs([(0x51, adr), (0x5D, 0x00), (0x5E, val)])

    def _cold_motor_move(self, feedl: int, full_speed_regs: bool,
                         label: str = "loader move", final_jog: bool = False) -> None:
        """One loader-profile motor move within a cold_init() homing
        round: enable, feed-length + motor params (the full 19-register
        loader speed profile when `full_speed_regs`, otherwise just the
        6 move-specific registers -- COLD_INIT_PAIRS already primed the
        speed profile at the top of the round), the loader slope table
        uploaded to both scanner-RAM addresses (same table _motor_run/
        eject() use), execute, then wait for completion (status word
        0xf855, per the capture) -- STRICTLY: this wait is what stands
        between this motor start and the next one, so a timeout ends the
        cold start with nothing further written (see COLD_MOVE_TIMEOUT).
        `label` names the move in the failure message; `final_jog` marks
        move 3/3 of a round, whose ended-off-sensor failure is reported
        as a possible release of a latched holder, cause not established (wording
        only -- still a failure, nothing else changes)."""
        self.io.write_regs([(0x09, 0x08)])
        move_regs = [
            (0x02, 0x18), (0xAE, 0x00), (0xAF, 0xFF),
            (0x3D, 0x00), (0x3E, (feedl >> 8) & 0xFF), (0x3F, feedl & 0xFF),
        ]
        if full_speed_regs:
            self.io.write_regs(list(tables_base.LOADER_SPEED_PAIRS) + move_regs)
        else:
            self.io.write_regs(move_regs)
        for addr in (0x1000C000, 0x10010000):
            self.io.buf_write(addr, tables_base.SLOPE_TABLE_LOADER)
        self.io.write_regs([(0x0F, 0x01)])
        watch = _ColdMoveWatch(self._cold_move_early_fail)
        try:
            self.io.poll_status_word(mask=0xFFFF, value=0xF855,
                                     timeout=COLD_MOVE_TIMEOUT, strict=True,
                                     on_read=watch)
        except safety.StrictPollTimeoutError as e:
            early = isinstance(e, safety.ColdMoveEndedOffSensorError)
            outcome = e.outcome if early else watch.outcome()
            how = (f"stopped off the home sensor (motor-enable and home-sensor bits "
                   f"clear for {COLD_MOVE_IDLE_WINDOW_S:.1f}s after it ran; "
                   f"--cold-move-early-fail)" if early else
                   f"did not complete within {COLD_MOVE_TIMEOUT:.0f}s")
            released = outcome == "ended-off-sensor" and final_jog
            tail = (safety.RELEASED_LIKELY_ADVICE if released else
                    f"The transport state is unknown and the cold start stops here -- no "
                    f"further motor move is started. This is a new observation -- please "
                    f"record it. {safety.NO_RECOVERY_ATTEMPTED} {safety.POWER_CYCLE_INSTRUCTION}")
            raise type(e)(
                f"cold_init: {label} (feed 0x{feedl:04x}) {how}: status word {e.last.hex()} "
                f"({format_status_bits(decode_status_word(e.last))}), want f855, "
                f"outcome={outcome}. {tail}",
                last=e.last, want=e.want, outcome=outcome, released_likely=released,
                observed=self.session.start_reg01,
                session=self.session.snapshot()) from None

    def _cold_homing_round(self) -> None:
        """One of cold_init()'s three homing rounds: pre-move sensor/
        int-ack prep, move 1 (feed 6690, minimal regs), a resync
        interstitial + move 2 (feed 6690, full speed-profile rewrite),
        move 3 (eject 3090), then a settle poll on regs 0x35/0x32.

        On the wire these three moves are the vendor's app-start jog
        (feed 0x1a22, feed 0x1a22, eject 0x0c12 -- the same bytes as
        tables_load.JOG), run three times with the cold table between
        rounds. Earlier revisions called them 8730/4620: the same bytes
        read in the other order (0x221a/0x120c), written swapped again
        in _cold_motor_move -- byte-identical output, misleading names."""
        FEEDL_1_2 = 0x1A22   # feed 6690
        FEEDL_3 = 0x0C12     # eject 3090

        # ---- pre-move setup -----------------------------------------
        val32 = self.io.read_reg(0x32)
        self.io.write_regs([(0x32, val32 & ~0x02 & 0xFF)])
        self.io.read_status_word()
        self.io.write_regs([(0x36, 0xFC)])
        self.io.write_regs([(0x33, 0x8E)])
        val32 = self.io.read_reg(0x32)
        self.io.write_regs([(0x32, (val32 | 0x02) & 0xFF)])
        self.io.read_status_word()

        # ---- move 1 ----------------------------------------------------
        self._cold_motor_move(FEEDL_1_2, full_speed_regs=False,
                              label=f"{self.session.phase} move 1/3 (feed)")

        # ---- resync between move 1 and move 2 --------------------------
        self.io.write_regs([(0x09, 0x00)])
        val32 = self.io.read_reg(0x32)
        self.io.write_regs([(0x32, val32)])
        val35 = self.io.read_reg(0x35)
        self.io.write_regs([(0x35, val35 & ~0x40 & 0xFF)])
        self.io.read_reg(0x32)
        self.io.poll_status_word(mask=0xF000, value=0xF000, timeout=COLD_READY_TIMEOUT)
        val32 = self.io.read_reg(0x32)
        self.io.write_regs([(0x32, val32 & ~0x02 & 0xFF)])
        w = self.io.read_status_word()
        if w != 0xF055:
            log.debug("cold_init: resync status word %#06x (want 0xf055)", w)
        w = self.io.read_status_word()
        if w != 0xF855:
            log.debug("cold_init: resync status word %#06x (want 0xf855)", w)

        # ---- move 2 (full speed-profile rewrite) ------------------------
        self._cold_motor_move(FEEDL_1_2, full_speed_regs=True,
                              label=f"{self.session.phase} move 2/3 (feed)")

        # ---- between move 2 and move 3: set bit 0x02 of reg 0x32 again ----
        # The vendor does this in every round of both starts we have
        # captured (01-init, 2026-09-07; also in the capture-replayed JOG
        # table, tables_load.py). It was missing from this hand-written
        # round (docs/static-analysis.md SA-001): without it the last
        # reg 0x32 write is the `& ~0x02` above and the settle check below
        # can never see 0x1f (Test 79).
        val32 = self.io.read_reg(0x32)
        self.io.write_regs([(0x32, (val32 | 0x02) & 0xFF)])

        # ---- move 3 ------------------------------------------------------
        self._cold_motor_move(FEEDL_3, full_speed_regs=False,
                              label=f"{self.session.phase} move 3/3 (eject)",
                              final_jog=True)

        # ---- settle: motor disable, poll 0x35/0x32 until stable ---------
        self.io.write_regs([(0x09, 0x00)])
        val35 = val32 = None
        for _ in range(max(1, round(COLD_SETTLE_TIMEOUT / COLD_SETTLE_INTERVAL))):
            val35 = self.io.read_reg(0x35)
            val32 = self.io.read_reg(0x32)
            if val35 == 0xBB and val32 == 0x1F:
                break
            time.sleep(COLD_SETTLE_INTERVAL)
        else:
            log.warning(
                "cold_init: settle poll did not reach 0x35=0xbb/0x32=0x1f "
                "(last 0x35=%#04x 0x32=%#04x) -- continuing",
                val35, val32,
            )

    def _wait_engine_idle(self, timeout: float = 90.0) -> None:
        """Wait until the scan/motor engine is idle again.

        Discovered on hardware 2026-08-30 (resolves open question 5 in
        protocol-notes.md): bit 0x20 of reg 0x01 CLEARS while the
        engine executes (0x22 -> 0x02 observed during homing) and sets
        again on completion. Waiting on this bit replaces mimicking the
        capture's poll timing, which raced the hardware and returned
        stale buffer data. Applied after every cw wv=0x83 batch
        containing the execute pulse (0x0f=0x01), before any
        subsequent buffer reads -- see _exec_ops.
        """
        try:
            self.io.wait_reg(0x01, 0x20, timeout=timeout, mask=0x20)
        except Of135iError as e:
            log.warning("engine-idle wait: %s -- continuing", e)

    def poll_buttons(self, strict_interrupts: bool = False) -> list[str]:
        """Read-only: return the front-panel buttons seen right now.

        Drains EP 0x83 (short timeout) and reads reg 0x31 once; never
        writes, so it works in a read-only session and in the cold state
        (reg 0x01 = 0x00, where no interrupts flow but reg 0x31 still
        shows a press). Interrupt events come first, then a press
        currently held according to reg 0x31 if not already reported;
        the result is deduplicated. Names are from of135i.buttons
        (Test 97). A press lasts at least 0.3 s, so a caller polling at
        about 10 Hz catches it via reg 0x31 even without interrupts; the
        same held press shows up in consecutive polls, so callers that
        act on presses must edge-detect. If EP 0x83 is in the overflow
        state the interrupt part is skipped (reg 0x31 still works), or
        InterruptOverflowError is raised when ``strict_interrupts``.
        """
        from . import buttons

        found: list[str] = []
        events = self.io.drain_events(timeout_ms=50)
        if events == "overflow" and strict_interrupts:
            raise InterruptOverflowError()
        if isinstance(events, list):
            for ev in events:
                name = buttons.decode_interrupt(ev)
                if name not in found:
                    found.append(name)
        held = buttons.decode_reg31(self.io.read_reg(0x31))
        if held is not None and held not in found:
            found.append(held)
        return found

    def is_magazine_present(self) -> bool:
        """Check the loader sensor (vendor INI: LoaderSensorReg=0x101,0x08).

        Bit 0x08 set = a magazine is physically PRESENT in the slot
        (hardware-verified 2026-09-02: 0xe0 without, 0xe8 with; Test 12
        2026-09-04: also set for a loose, unlatched magazine with the
        orange LED, read on a cold, unwritten register). It says nothing
        about the magazine being fed in or latched, and is unreliable
        after the base register table has been written.
        """
        val = self.io.read_ext_reg(0x101)
        return bool(val & 0x08)

    # -------------------------------------------------------------- motor

    def _motor_run(self, mode: int, feedl: int) -> None:
        """Stand-alone motor move, modelled on the vendor's eject in the
        SilverFast capture (protocol-notes.md pass 14, eject addendum):
        sensor/int-ack prep (33=8e, 32=8f), 09=08, batch 02=mode
        ae/af 3d/3e/3f=FEEDL (24-bit, hi/mid/lo), the positioning slope
        table uploaded to 0x1000c000 and 0x10010000, 0f=01 executes,
        wait, 09=00.

        The slope-table uploads are NOT optional: without them the
        motor runs against whatever curve happens to be in scanner RAM.
        That worked while a positioning move had left the table there
        and stalled with a grinding noise once a scan pass had
        overwritten it (eject after a 4-frame batch, 2026-09-02;
        cf. pass 13 on the naked load feed)."""
        self.io.write_regs([(0x33, 0x8E)])
        self.io.write_regs([(0x32, 0x8F)])
        self.io.write_regs([(0x09, 0x08)])
        self.io.write_regs([
            (0x02, mode), (0xAE, 0x00), (0xAF, 0xFF),
            (0x3D, (feedl >> 16) & 0xFF),
            (0x3E, (feedl >> 8) & 0xFF),
            (0x3F, feedl & 0xFF),
        ])
        for addr in (0x1000C000, 0x10010000):
            self.io.buf_write(addr, tables.SLOPE_TABLE_POSITION)
        self.io.write_regs([(0x0F, 0x01)])
        # No motor-busy bit is confirmed yet (driver-design.md open
        # items / protocol-notes.md pass 4 "Motor/feed" note): reg 0x01
        # was observed unchanged across a real homing move on hardware.
        # Poll it briefly so a deployed driver at least has somewhere
        # to add a real busy check later, but don't block indefinitely.
        time.sleep(0.1)
        self._wait_engine_idle()
        self.io.write_regs([(0x09, 0x00)])

    def home(self) -> None:
        """Mode 0x30, FEEDL=1 execute -- historically taken for a homing
        move (pass 3/4). Pass 14 showed mode 0x30 is the SCAN PASS: this
        runs the engine for whatever line count regs 0x25-0x27 hold, and
        the vendor flow has no homing command at all (positioning is an
        absolute mode-0x18 feed from wherever the carriage is). Not used
        by the scan flow, the CLI or any tool; kept only as a guarded
        entry point (it is NOT a recovery command)."""
        with self._operation("home"):
            self.session.phase = "home"
            self._motor_run(0x30, 1)

    def _strict_status_polls(self, phase: Phase) -> frozenset[int]:
        """Indices of a phase's status-word polls (wValue 0x018e): the
        motor completions, run strictly under load_status_matches()."""
        return frozenset(i for i, op in enumerate(phase.ops)
                         if op.kind == "poll" and op.wv == 0x018E)

    def vendor_startup(self, experimental: bool = False) -> None:
        """Explicit, experimental vendor startup; offline verified only.

        Requires a fresh ARMED session, a fresh exact reg01=2255 read,
        and exact F055 before any vendor OUT. The packet 6813 reg101
        bit08 preflight must read clear before the base-register write.
        Mechanical state and COLD eligibility remain unverified.
        Four moves: mode1c traverse71490, then mode18 6690/6690/3090.
        Completion reports protocol progress, never physical release/load.
        """
        from . import tables_startup

        session = self.session
        session.write_allowed_or_final()
        if not experimental:
            raise OperationNotAllowedError(
                "vendor_startup() requires explicit experimental=True. "
                f"{safety.NO_COMMANDS_SENT}", session=session.snapshot())
        if session.operations_started or session.operations or session.writes_attempted:
            raise OperationNotAllowedError(
                "vendor_startup() requires a fresh session with no earlier operations or writes.",
                session=session.snapshot())
        pre = tables_startup.CONFIGURE.ops[tables_startup.PRESENCE_PREFLIGHT_INDEX]
        first_reg_write = next(i for i, op in enumerate(tables_startup.CONFIGURE.ops)
                               if op.kind == "cw" and op.wv == 0x0083)
        if not (pre.kind == "cr" and pre.bm == 0xc0 and pre.br == 4 and pre.wv == 0x018e
                and pre.wi == 0x0122 and pre.length == 2
                and tables_startup.PRESENCE_PREFLIGHT_INDEX < first_reg_write):
            raise OperationNotAllowedError(
                "vendor_startup() table invariant failed: presence preflight read is not the "
                f"reg101 read before the first register write. {safety.NO_COMMANDS_SENT}",
                session=session.snapshot())
        with self._operation("vendor_startup"):
            session.write_allowed()  # rejects read-only; COLD is refused by _operation
            session.phase = "vendor_startup_entry"
            got = bytes(self.io.dev.ctrl_transfer(0xc0, 4, 0x008e, 0x0122, 2))
            if got != b'\x22\x55':
                raise OperationNotAllowedError(
                    f"vendor_startup() requires exact live reg01 2255, got {got.hex()}. "
                    f"{safety.NO_COMMANDS_SENT}", session=session.snapshot())
            got = bytes(self.io.dev.ctrl_transfer(0xc0, 4, 0x018e, 0x0122, 2))
            if got != b'\xf0\x55':
                raise OperationNotAllowedError(
                    f"vendor_startup() requires exact pre-write F055, got {got.hex()}. "
                    f"{safety.NO_COMMANDS_SENT}", session=session.snapshot())
            self._run_vendor_startup_phase(tables_startup.CONFIGURE)
            for phase, busy, done, budget in tables_startup.MOTOR_PHASES:
                self._run_vendor_startup_phase(phase)
                self._wait_vendor_startup_motor(busy, done, budget)
            self._run_vendor_startup_phase(tables_startup.DISABLE)
            log.info("vendor_startup: startup sequence complete; mechanical position unverified")

    def _run_vendor_startup_phase(self, phase: Phase) -> None:
        """Validate startup replies before any dependent write.

        Preserve shared execution behavior for existing operations. Captured
        RMW inputs are gated rather than overwriting different live bits.
        Informational values may vary; lengths/ACKs must always be valid.
        """
        from . import tables_startup

        self.session.phase = phase.name
        t0 = time.monotonic()
        try:
            for index, op in enumerate(phase.ops):
                if op.kind not in ("cr", "poll"):
                    self._exec_ops([op])
                    continue
                if op.dt > _PACE_THRESHOLD:
                    time.sleep(min(op.dt, _PACE_CAP))
                got = bytes(self.io.dev.ctrl_transfer(op.bm, op.br, op.wv, op.wi, op.length))
                ack_reply = (op.br == 4 and op.wv in (0x008e, 0x018e)
                             and op.length == 2)
                if len(got) != op.length or (ack_reply and got[1] != 0x55):
                    raise OperationNotAllowedError(
                        f"vendor startup control reply malformed at {op.wv:#06x}/{op.wi:#06x}: "
                        f"{got.hex()}; stopping.", session=self.session.snapshot())
                # ACK55, slope completion02 and pre-traverse D055 are exact.
                if op.kind == "poll" and got != op.resp:
                    raise OperationNotAllowedError(
                        f"vendor startup protocol check failed at {op.wv:#06x}/{op.wi:#06x}: "
                        f"got {got.hex()}, expected {op.resp.hex()}; stopping.",
                        session=self.session.snapshot())
                if op.wv == 0x008e and op.wi in (0x3122, 0x3222, 0x3522):
                    if got != op.resp:
                        raise OperationNotAllowedError(
                            f"vendor startup captured read/modify/write input changed at "
                            f"{op.wi:#06x}: got {got.hex()}, expected {op.resp.hex()}; stopping.",
                            session=self.session.snapshot())
                # Conservatively require live reg101 bit08 clear at packet 6813:
                # a set bit can enter an unimplemented vendor cancel/drain path,
                # subject to additional configuration and helper predicates.
                # Stop before base write 6825; see docs/vendor-startup.md.
                if (phase is tables_startup.CONFIGURE
                        and index == tables_startup.PRESENCE_PREFLIGHT_INDEX
                        and got[0] & tables_startup.PRESENCE_BIT):
                    raise OperationNotAllowedError(
                        f"vendor startup refused: live register101 bit08 set at the vendor "
                        f"presence preflight ({got.hex()}); a possible vendor cancel/drain path is "
                        f"not implemented. No base-register write or motor GO was sent. "
                        f"{safety.NO_RECOVERY_ATTEMPTED}", session=self.session.snapshot())
                # DLL eligibility: TWO fresh reg100 reads, each bit02 clear.
                if phase is tables_startup.TRAVERSE and op.wv == 0x018e and op.wi == 0x0022:
                    if got[0] & 0x02:
                        raise OperationNotAllowedError(
                            "vendor startup traverse refused: live register100 bit02 is set; "
                            "no motor GO was sent.", session=self.session.snapshot())
                if op.resp and got != op.resp:
                    self._diag_cr_mismatches += 1
                    log.debug("startup informational read %04x/%04x: got %s, captured %s",
                              op.wv, op.wi, got.hex(), op.resp.hex())
        finally:
            elapsed = time.monotonic() - t0
            self._diag_phase_seconds[phase.name] = self._diag_phase_seconds.get(phase.name, 0.0) + elapsed

    def _wait_vendor_startup_motor(self, busy: frozenset[bytes], done: bytes,
                                   budget: float) -> None:
        """Require a captured busy word after EACH GO before exact done.

        Consecutive JOGs share F855 completion, so a stale F855 must never
        complete a later move. All reads share one bounded phase deadline.
        """
        deadline = time.monotonic() + budget
        observed_busy = False
        got = b''
        while True:
            remaining_ms = int((deadline - time.monotonic()) * 1000)
            if remaining_ms < 1:
                raise safety.StrictPollTimeoutError(
                    f"vendor startup motor deadline exhausted before next status read. "
                    f"{safety.NO_RECOVERY_ATTEMPTED}",
                    last=got, want=done, session=self.session.snapshot())
            got = bytes(self.io.dev.ctrl_transfer(
                0xc0, 4, 0x018e, 0x0122, 2, timeout=remaining_ms))
            if len(got) != 2 or got[1] != 0x55:
                raise OperationNotAllowedError(
                    f"vendor startup motor status malformed: {got.hex()}; stopping.",
                    session=self.session.snapshot())
            if time.monotonic() >= deadline:
                raise safety.StrictPollTimeoutError(
                    f"vendor startup motor did not reach busy then {done.hex()} within "
                    f"{budget:.1f}s; last {got.hex()}. {safety.NO_RECOVERY_ATTEMPTED}",
                    last=got, want=done, session=self.session.snapshot())
            observed_busy |= got in busy
            if observed_busy and got == done:
                return
            time.sleep(0.004)

    def jog_magazine(self) -> None:
        """Run the vendor's app-start jog (tables_load.JOG, clean-load
        capture ops 88-170): interrupt/sensor acks, motor enable, feed
        6690, 0x35=0xbb, feed 6690, eject 3090, motor disable -- with
        the loader profile and slope tables, verbatim.

        Why: every vendor load that engaged the cassette was preceded
        by this jog (eject-from-loaded 2026-09-02, load-only
        2026-08-30, clean-load 2026-09-05), and every load of ours
        that did not engage lacked it (Tests 11b/12b/13/15: feed done,
        loader-sensor bit still set, magazine loose). The jog's own
        feeds never clear the sensor bit (0xf855 after each move);
        it positions the mechanism for the load that follows. The
        vendor runs it with the magazine loose in the slot, right
        after its device-open sequence (initialize(prep=False) =
        tables_load.OPEN, LOADER motor profile in the table: the jog's
        first move takes 0x7e/0x7f from it),
        and then has the operator take the magazine out and reinsert
        it to the stop before load_magazine().

        Its four status-word polls are strict under the masked test
        (done class, sensor bit SET, busy clear; captured 0xf855): a
        move that does not complete stops the flow, fails the session
        and requires a power cycle. NOT yet hardware-verified as part
        of a load.
        """
        from . import tables_load
        self.session.write_allowed_or_final()
        if not (self._base_initialized or self._vendor_open):
            raise OperationNotAllowedError(
                "jog_magazine() requires initialize() first in this session. "
                f"{safety.NO_COMMANDS_SENT}", session=self.session.snapshot())
        with self._operation("jog_magazine"):
            self._run_phase(tables_load.JOG, strict_polls=self._strict_status_polls(tables_load.JOG))
            word = self.io.read_status_word()
            log.info("jog_magazine: complete, status word %#06x", word)

    def load_magazine(self) -> None:
        """Run the vendor's standalone magazine insert flow
        (tables_load.LOAD, from the 2026-09-05 clean-load capture, Test
        14): loader-sensor ack, the engaging feed with the vendor's full
        register block, the prescan traverse and one idle housekeeping
        cycle. The cassette must already be pushed in by hand, fully,
        to the stop (Test 14: the vendor loads from there).

        The vendor's FIRST load of a power-on is always preceded by
        jog_magazine() -- initialize(prep=False) -> jog -> operator
        reinserts -> load -- and every load of ours attempted WITHOUT a
        jog since power-on, or from an otherwise undefined transport
        state, has failed to engage the cassette (Tests 11b-15). That is
        not true of every load in a session, though: a vendor USB
        capture made 2026-09-25 (docs/protocol-notes.md Pass 14 addendum
        4) shows the vendor's own load of the NEXT strip, after an eject
        button press later in the SAME power-on, running with no jog at
        all -- just this table again -- and latching normally.
        of135i.loadflow's ``next_strip`` mode
        (``of135i load --next-strip``) relies on that: it calls this
        method directly, with no preceding jog_magazine() call, and is
        refused up front if the session is COLD. This method itself does
        not track whether a jog ran -- only initialize() having run
        first is enforced below. Requires initialize() first in this
        session; tools/load_magazine.py runs the whole first-load flow.

        Completion is verified, not assumed, at three points, each with
        the masked test load_status_matches() (state class AND loader-
        sensor bit 0x08, mask LOAD_STATUS_MASK -- see the module-level
        note): the feed's completion poll must reach the done class
        with the sensor bit CLEAR (captured 0xf455: the cassette was
        pulled past the sensor; 0xec55/0xf855 fail), the traverse's
        poll the done class with the sensor bit SET (captured 0xdc55;
        0xcc55 fails), and after the replay the status word is read
        once more and must still match the traverse target
        (load_completion_target()), else LoadIncompleteError. A poll
        that does not get there stops the flow right there (safety.
        StrictPollTimeoutError: a feed that did not engage the cassette
        never gets a traverse). Either failure marks the session
        FAILED: the transport state is then unknown and the magazine
        may not be latched. No recovery is attempted; a power cycle is
        required.

        Even a completed load only sets the vendor's "loaded"
        indication (blue LED); the loader sensor reports presence,
        not latching (docs/hardware-safety.md). This table has NOT yet
        been hardware-verified as a load.
        """
        from . import tables_load
        # A new load is a new piece of film in the gate -- any cached
        # calibration (see __init__'s _cal_cache comment) was measured
        # against whatever was there before and must not be reused.
        self._cal_cache = None
        self.session.write_allowed_or_final()
        if not (self._base_initialized or self._vendor_open):
            raise OperationNotAllowedError(
                "load_magazine() requires initialize() first in this session. "
                f"{safety.NO_COMMANDS_SENT}", session=self.session.snapshot())
        expected = load_completion_target()
        with self._operation("load_magazine"):
            # TODO(next-strip, deferred 2026-09-25): tables_load.LOAD's
            # second op writes reg 0x32 as the literal constant 0x1d
            # (captured from a fresh, jog-then-reinsert load). The
            # 2026-09-25 vendor capture (docs/protocol-notes.md Pass 14
            # addendum 4) shows the vendor writing this register as a
            # read-modify-write, read & ~0x02 (the same clear-bit-0x02 ack
            # jog_magazine()/eject() already do inline), in BOTH situations
            # it was seen in (0x1f->0x1d fresh, 0x9f->0x9d after a scan,
            # bits 0x80/0x10 preserved) -- so a literal 0x1d is only
            # byte-correct when the read happens to be 0x1f. Phase.injections/patched() (of135i/tables.py) is the
            # existing per-op override mechanism and would fit this
            # (byte injection at LOAD.ops[1], offset 1), but tables_load.py
            # is auto-generated (tools/gen_load_table.py) with NO support
            # for emitting injections -- hand-adding one here would be
            # silently lost on the next regeneration unless the generator
            # is extended too, which is out of scope for next-strip mode.
            # LOAD stays byte-identical (the constant 0x1d) for now; it is
            # exactly what Scanner.load_magazine() replays 7/7 hardware-
            # verified from the post-jog position. Test 89 (2026-09-25):
            # next-strip engaged on the first poll with reg 0x32 reading
            # 0xbf beforehand, so the literal did not matter for the grip
            # (n=1). If a later next-strip load fails at this feed, this
            # is still the first place to look.
            self._run_phase(tables_load.LOAD,
                            strict_polls=self._strict_status_polls(tables_load.LOAD))
            self.session.phase = "load_verify"
            word = self.io.read_status_word()
            if not load_status_matches(word.to_bytes(2, "big"), expected.to_bytes(2, "big")):
                raise safety.LoadIncompleteError(
                    f"magazine load did NOT complete: status word {word:#06x}, expected "
                    f"{expected:#06x} under mask {LOAD_STATUS_MASK:#04x}55 (the vendor capture's "
                    f"completion class with the loader-sensor bit). The transport "
                    f"state is unknown and the magazine may not be latched. "
                    f"{safety.NO_RECOVERY_ATTEMPTED} {safety.POWER_CYCLE_INSTRUCTION}",
                    status_word=word, expected=expected, observed=self.session.start_reg01,
                    session=self.session.snapshot())
            log.info("load_magazine: complete, status word %#06x", word)
            self._loaded_this_session = True

    def eject(self) -> str:
        """Eject the film magazine the way the vendor driver does it from
        a LOADED magazine (its app-start "jog": feed 6690, eject 3090 --
        load-only capture ops 151-169, ramval ops 736-760; protocol-
        notes.md pass 14 eject addendum):

            33=8e, 32=<read>|02, 09=08,
            1c=00 02=18 3d/3e/3f=3090 7d=00 7e=75 7f=30 8a-92=00 ae=00 af=ff,
            loader slope table -> 0x1000c000 and 0x10010000,
            0f=01, busy-wait, 09=00

        The post-scan eject variant (06-eject: positioning slope table,
        fast speed regs left over from the scan flow) stalled twice on
        2026-09-02 when issued from a loaded/parked transport; this
        variant is the one that freed the magazine each time via the
        vendor software.

        Safety guards:
        - Start-state guard (safety.py): a session that reads anything
          but 0x22/0x00 in reg 0x01 is refused before any write.
        - If no magazine is present (loader sensor bit 0x08 clear),
          log a notice and return immediately — no motor commands.
          (The sensor is only trustworthy before the first initialize()
          of the scanner's power cycle -- docs/hardware-safety.md.)
        - Cold start (0x00, never homed): run cold_init() first —
          ejecting from an unhomed state is undefined.
        - Base-table-only state (regs 0x3b/0x3c read 0xff/0xff: BASE_INIT
          written with no scan phase after it): refused read-only with
          UnejectableStateError. The eject stalled twice from that state
          (Test 44) and no vendor flow ejects from it (Test 46).

        Honest outcome (2026-10): returns EJECT_COMPLETED only when the
        status word confirmed the move finished, EJECT_SKIPPED_NO_MAGAZINE
        when nothing was sent because the sensor read clear (recorded in
        last_eject with whether that reading came after an initialize()
        in this session, i.e. from the unreliable sensor). A completion
        that is never confirmed raises safety.EjectIncompleteError after
        the eject's usual closing write (0x09=0x00, sent exactly as
        before): the session is FAILED and nothing else follows -- no
        retry, no second move, no park. Formerly that case was logged
        and the caller reported "ejected".
        """
        # The film is leaving the gate -- any cached calibration (see
        # __init__'s _cal_cache comment) no longer applies to whatever
        # comes next, refused or not.
        self._cal_cache = None
        sensor_after_init = bool(self._base_initialized or self._vendor_open)
        with self._operation("eject", cold_ok=True):
            self.last_eject = {"outcome": None, "magazine_sensor": None,
                               "sensor_after_initialize": sensor_after_init,
                               "completion": None}
            present = self.is_magazine_present()
            self.last_eject["magazine_sensor"] = present
            if not present:
                log.info("eject: no magazine detected -- nothing sent%s",
                         " (sensor read after initialize(): unreliable)"
                         if sensor_after_init else "")
                self.last_eject["outcome"] = EJECT_SKIPPED_NO_MAGAZINE
                self.last_eject["interpretation"] = EJECT_SENSOR_CLEAR_INTERPRETATION
                return EJECT_SKIPPED_NO_MAGAZINE
            if self.session.state is SessionState.COLD:
                log.info("eject: reg 0x01=0x00 (cold state) — running cold_init() first")
                self.cold_init()
            self._refuse_eject_from_base_table_state()
            self.session.phase = "eject"
            self._eject_body()
            self.last_eject["outcome"] = EJECT_COMPLETED
            return EJECT_COMPLETED

    def _refuse_eject_from_base_table_state(self) -> None:
        """Fail-closed guard before the eject sequence's first write.

        Regs 0x3b/0x3c read 0xff/0xff only in the base-table-only state
        (BASE_INIT_PAIRS written, no PREP/AFE_BASE phase after it -- the
        state the SANE hook-1 open created). The driver's eject stalled
        2/2 from it (Test 44, 2026-09-07); every state the vendor ejects
        from reads 0x00/0x00 (after OPEN/LOAD, cold_init) or 0x00/0x01
        (after scan + PARK). Two register reads, no write. Why the eject
        stalls there is a hypothesis, not established (docs/test-log.md,
        Test 46); this guard only keeps the motor out of that state.
        """
        regs = (self.io.read_reg(0x3B), self.io.read_reg(0x3C))
        if regs == (0xFF, 0xFF):
            raise UnejectableStateError(
                "eject() refused: regs 0x3b/0x3c read 0xff/0xff (the scan-session "
                "base table with no scan phase after it). The driver's eject stalled "
                "from this state (Test 44) and no vendor flow ejects from it. "
                "Recovery: power-cycle the scanner, then `of135i load`. "
                f"{safety.NO_COMMANDS_SENT}",
                regs=regs, session=self.session.snapshot())

    def _eject_body(self) -> None:
        feedl = 3090
        self.io.write_regs([(0x33, 0x8E)])
        self.io.write_regs([(0x32, (self.io.read_reg(0x32) | 0x02) & 0xFF)])
        self.io.write_regs([(0x09, 0x08)])
        self.io.write_regs(
            [(0x1C, 0x00), (0x02, 0x18),
             (0x3D, (feedl >> 16) & 0xFF), (0x3E, (feedl >> 8) & 0xFF), (0x3F, feedl & 0xFF)]
            + [p for p in tables_base.LOADER_SPEED_PAIRS if p[0] != 0x1C]
            + [(0xAE, 0x00), (0xAF, 0xFF)]
        )
        for addr in (0x1000C000, 0x10010000):
            self.io.buf_write(addr, tables_base.SLOPE_TABLE_LOADER)
        self.io.write_regs([(0x0F, 0x01)])
        # Vendor completion poll: status word (wValue 0x018e) reads
        # 0xd9 -> 0xf9 -> 0xf8 over ~0.9 s; done at 0xf8. The 0x008e
        # reg-0x01 engine bit used elsewhere stays clear after an eject
        # (observed 2026-09-02: 90 s timeout on a successful eject).
        # Bound every status read by the remaining 10 s budget and reject
        # replies at or after its deadline before accepting completion.
        # Keep the existing closing 0x09=0x00 write after an unconfirmed
        # wait; transfer exceptions still propagate without further writes.
        wait = {"reads": 0, "seconds": None, "first": None, "last": None,
                "last_bits": None, "busy_seen": False, "timed_out": False}
        if self.last_eject is not None:
            self.last_eject["completion"] = wait
        t0 = time.monotonic()
        deadline = t0 + EJECT_WAIT_TIMEOUT
        try:
            while True:
                remaining_ms = int((deadline - time.monotonic()) * 1000)
                if remaining_ms < 1:
                    wait["timed_out"] = True
                    break
                reply = bytes(self.io.dev.ctrl_transfer(
                    0xC0, 0x04, 0x018E, 0x0122, 2, timeout=remaining_ms))
                wait["reads"] += 1
                wait["last"] = reply.hex()
                if wait["first"] is None:
                    wait["first"] = reply.hex()
                if len(reply) == 2 and reply[0] & 0x01:
                    wait["busy_seen"] = True
                if time.monotonic() >= deadline:
                    wait["timed_out"] = True
                    break
                if eject_complete_status_matches(reply):
                    break
                time.sleep(EJECT_POLL_INTERVAL)
        finally:
            wait["seconds"] = round(time.monotonic() - t0, 3)
            if wait["last"] is not None:
                wait["last_bits"] = decode_status_word(bytes.fromhex(wait["last"]))
        self.io.write_regs([(0x09, 0x00)])
        if wait["timed_out"]:
            last = bytes.fromhex(wait["last"]) if wait["last"] else b""
            raise safety.EjectIncompleteError(
                f"eject did NOT confirm completion: after the eject move's execute pulse the "
                f"status word never read idle ((byte & {EJECT_DONE_MASK:#04x}) == "
                f"{EJECT_DONE_VALUE:#04x}, ack 0x55) within {EJECT_WAIT_TIMEOUT:.0f}s: last "
                f"{last.hex() or '(empty)'} after {wait['reads']} read(s), busy "
                f"{'seen' if wait['busy_seen'] else 'never seen'}. The eject's closing motor-"
                f"disable write (0x09=0x00) was sent as always; nothing else follows. The "
                f"magazine may or may not have been released and the transport state is "
                f"unknown -- this is NOT a completed eject. "
                f"{safety.NO_RECOVERY_ATTEMPTED} {safety.POWER_CYCLE_INSTRUCTION}",
                status_word=int.from_bytes(last, "big") if len(last) == 2 else None,
                reads=wait["reads"], seconds=wait["seconds"],
                observed=self.session.start_reg01, session=self.session.snapshot())

    # -------------------------------------------------- lamp warmup retry

    def _gain_with_warmup(self, cal_white_phase, parse_white):
        """Compute AFE gain codes from a white-line measurement, waiting
        for the lamp when the first measurement says it is not ready.

        `cal_white_phase` is the Phase to run for the white measurement
        (a single-line read, no motor move). `parse_white` is a
        callable(raw_bytes) -> (N, 3) ndarray of the white line(s).

        Rules (fail-closed, see docs/hardware-safety.md):
          - A warm lamp -- the first measurement gives a non-maxed gain
            code on at least one channel -- returns at once, exactly as
            every verified scan to date (one CAL_WHITE run).
          - Otherwise the lamp is warming: measure again every
            _WARMUP_INTERVAL_S and accept only when two CONSECUTIVE
            measurements are non-maxed and their per-channel peaks agree
            within _WARMUP_STABLE_PCT (calibrating on a still-rising lamp
            would fix the gain at a level the lamp then overshoots).
          - The total budget is self.warmup_budget_s (default
            _WARMUP_BUDGET_S), enforced both by the clock and by a hard
            cap on the number of measurements, so the loop cannot run
            forever even with a stopped clock. Exhausting it raises
            safety.LampWarmupError: no scan, no motor command, the
            operation fails like any other.
          - A malformed white buffer (not (N, 3), or empty) or a
            saturated one (any channel peak at 65535 at gain 0 -- never
            seen on this unit, physically implausible) raises
            LampWarmupError immediately. USB errors, timeouts and
            Ctrl-C during the wait propagate untouched.

        Records into self._diag_warmup: "attempts", "gain_history",
        "peak_history", "elapsed_s", "exhausted" (True only on the
        failure path, kept for the sidecar/hwblock fields), and
        "white_mean"/"white_max" of the LAST measurement.
        """
        diag = {"attempts": 0, "gain_history": [], "peak_history": [], "measurement_times_s": [],
                "elapsed_s": 0.0, "exhausted": False, "white_mean": None, "white_max": None}
        self._diag_warmup = diag
        budget = float(self.warmup_budget_s)
        interval = _WARMUP_INTERVAL_S
        max_measurements = 1 + int(budget // interval)
        t0 = time.monotonic()

        def fail(msg: str) -> "safety.LampWarmupError":
            diag["exhausted"] = True
            return safety.LampWarmupError(
                f"{msg} after {diag['attempts']} white-line measurement(s) in "
                f"{diag['elapsed_s']:.1f}s (budget {budget:.0f}s); per-channel peaks "
                f"{diag['peak_history']}. No scan was made and no motor command was sent. "
                f"{safety.NO_RECOVERY_ATTEMPTED} {safety.POWER_CYCLE_INSTRUCTION} Then run "
                f"the magazine load flow first (it gives the lamp about a minute) or raise "
                f"the budget (--warmup-budget).",
                measurements=diag["attempts"], peak_history=list(diag["peak_history"]),
                elapsed_s=diag["elapsed_s"], observed=self.session.start_reg01,
                session=self.session.snapshot())

        def measure():
            white_raw = self._run_phase(cal_white_phase)[0]
            white = parse_white(white_raw)
            self._last_white = white
            diag["attempts"] += 1
            diag["elapsed_s"] = time.monotonic() - t0
            diag["measurement_times_s"].append(round(diag["elapsed_s"], 2))
            if white.ndim != 2 or white.shape[1] != 3 or white.shape[0] == 0:
                raise fail(f"malformed white-line measurement (shape {tuple(white.shape)})")
            w = white.astype(np.float64)
            peaks = [float(np.percentile(w[:, c], 99.9)) for c in range(3)]
            diag["peak_history"].append([round(pk, 1) for pk in peaks])
            diag["white_mean"] = [float(x) for x in w.mean(axis=0)]
            diag["white_max"] = [float(x) for x in w.max(axis=0)]
            if any(pk >= 65535.0 for pk in peaks):
                raise fail("white line saturated at gain 0 (implausible AFE state)")
            codes = calibrate.gain_codes(white, clamp_nonpositive=True)
            diag["gain_history"].append(list(codes))
            maxed = all(c == calibrate._GAIN_MAX_CODE for c in codes)
            return codes, peaks, maxed

        codes, peaks, maxed = measure()
        if not maxed:
            return codes                      # the verified single-measurement path

        log.warning("gain maxed R=G=B=0x3F on the first white line -- lamp not ready; "
                    "waiting up to %.0fs (measuring every %.0fs)", budget, interval)
        prev_peaks, prev_maxed = peaks, True
        while True:
            elapsed = time.monotonic() - t0
            if diag["attempts"] >= max_measurements or elapsed + interval > budget:
                raise fail("lamp did not reach a stable usable level")
            time.sleep(interval)
            codes, peaks, maxed = measure()
            if not maxed and not prev_maxed:
                stable = all(abs(pk - pp) <= _WARMUP_STABLE_PCT / 100.0 * max(pp, 1.0)
                             for pk, pp in zip(peaks, prev_peaks))
                if stable:
                    log.info("lamp warm: gain %s after %d measurements, %.0fs",
                             [hex(c) for c in codes], diag["attempts"], diag["elapsed_s"])
                    return codes
            prev_peaks, prev_maxed = peaks, maxed

    # -------------------------------------------------------------- scan

    def scan(
        self, frame: int = 1, lines: int | None = None, ir: bool = False,
        dpi: int = 3600, overscan_mm: float | None = None,
        slide: bool = False,
    ) -> tuple[bytes, int] | tuple[bytes, int, dict]:
        """Run the full calibration + scan sequence for one frame.

        dpi=3600, ir=False (default): the plain visible-only flow;
        returns (raw_bytes, width) -- raw_bytes is the pixel-interleaved
        RGB16LE image buffer (image.assemble() turns it into an ndarray),
        width is tables.IMAGE_WIDTH. Positioning and window size come
        from the corrected mean mapping (holder.STRIP_FIDUCIAL) with
        ``overscan_mm`` of slack per side (default holder.OVERSCAN_MM)
        -- the A+C production geometry accepted in Test 58. The
        delivered buffer therefore spans the whole aperture plus the
        margins; aperture_crop registers the product on the edges
        found in THIS scan. The historical fixed window (FEEDL grid
        6743/10760, 223 chunks) is no longer a runtime path; it
        remains in the captures, the test log and the SANE tables
        (which migrate separately).

        ir=True, or any other dpi: the dual-light (alternating IR/
        visible line) flow of that resolution's table module
        (dual_tables(dpi); every non-3600 capture is a dual-light one)
        via _scan_dual(), returning (raw_bytes, width, meta) with meta
        {"width": W, "alternating": True, "dpi": dpi} -- see
        image.split_ir() for turning raw_bytes into separate visible/IR
        arrays.

        slide=True: ``frame`` is a mounted-slide holder opening (1-4) and
        the dual window comes from holder.slide_dual_geometry() (the slide
        fiducial model, default overscan holder.SLIDE_OVERSCAN_MM) instead
        of the strip model -- the only difference: the same calibration,
        POSITION, scan and PARK phases run. Dual profiles only (plain
        3600 clips a slide laterally, Test 85), and not with lines=.
        slide=False (default) is the strip path, unchanged.

        Safety: runs as a hardware operation (start-state guard on the
        session's first write; any exception marks the session failed
        and nothing -- no PARK -- is sent afterwards). Requires
        initialize() first in this session, before EVERY scan, as the
        vendor flow does; refuses with zero writes otherwise.
        """
        # Magazine presence: the CLI checks the loader sensor before
        # initialize() (the only point where it's reliable). By the time
        # scan() runs, the base-register writes have changed ext reg
        # 0x101 so re-checking here would be unreliable. Callers that
        # bypass the CLI should verify the magazine is loaded before
        # calling scan().
        #
        # Parameter validation BEFORE the operation: a bad argument is a
        # plain error, not a hardware-session failure.
        self.session.write_allowed_or_final()
        t = dual_tables(dpi) if (ir or dpi != 3600) else None
        if self.park_mode not in ("verbatim", "semantic"):
            raise ValueError(f"unknown park_mode {self.park_mode!r} (want 'verbatim' or 'semantic')")
        if self.dark_fallback not in ("strict", "default"):
            raise ValueError(f"unknown dark_fallback {self.dark_fallback!r} (want 'strict' or 'default')")
        if self.session.cold_init_done and not self._loaded_this_session:
            raise OperationNotAllowedError(
                "scan() after a cold start requires the magazine load flow first "
                "(tools/load_magazine.py): the load's traverse puts the transport at the "
                "vendor's scan reference position, and a scan straight after cold_init reads "
                "a dark white line -- 51 measurements over 295 s in Test 22 -- and can only "
                "end flat or fail. The vendor never scans after a power-on without loading. "
                f"{safety.NO_COMMANDS_SENT}", session=self.session.snapshot())
        if not self._prepared_for_scan:
            raise OperationNotAllowedError(
                "scan() requires initialize() first (before every frame) in this session. "
                f"{safety.NO_COMMANDS_SENT}", session=self.session.snapshot())
        if slide:
            # Validated BEFORE the operation, like every other argument:
            # the slide geometry is dual-only and has no capture grid.
            if t is None:
                raise ValueError(
                    "slide scans need a dual-light profile (ir=True or dpi != "
                    "3600): plain 3600 covers 26.5 mm laterally and clips a "
                    f"mounted slide (Test 85). {safety.NO_COMMANDS_SENT}")
            if lines is not None:
                raise ValueError(
                    "lines= is the strip capture-grid path; slide scans are "
                    f"sized by overscan_mm. {safety.NO_COMMANDS_SENT}")
            if overscan_mm is None:
                overscan_mm = holder.SLIDE_OVERSCAN_MM
            # The whole slide geometry -- slide number, FEEDL, model end
            # and the slide-only travel check -- BEFORE the operation, so a
            # refusal sends nothing and leaves the session unfailed
            # (_scan_dual recomputes the same values after calibration).
            holder.slide_dual_geometry(
                frame,
                dpi=dpi,
                lines_per_chunk=t.LINES_PER_CHUNK,
                colour_crop_lines=image_mod.align_shift(dpi),
                default_wire_lines=t.DEFAULT_LINES,
                overscan_mm=overscan_mm,
            )
        if t is not None:
            # Dual: the production default (lines=None, overscan_mm=None)
            # is the same A+C geometry as plain, in visible lines
            # (holder.dual_overscan_geometry). Explicit lines= remains
            # the documented diagnostic/capture-replay path -- it keeps
            # the historical FEEDL grid and is what the SANE
            # wire-equality tests and long sweeps use; it is not the
            # production path.
            if lines is not None:
                if overscan_mm is not None:
                    raise ValueError("pass either lines= or overscan_mm=, not both")
                if lines > 0xFFFFFF:
                    raise Of135iError(f"line count {lines} does not fit the 24-bit register")
            elif overscan_mm is None:
                overscan_mm = holder.OVERSCAN_MM
        if t is None:
            # Plain 3600: the A+C overscan geometry IS the production
            # path (single runtime truth; docs/holder-position-design.md
            # section 9 step 2, adopted after Test 58's acceptance).
            if lines is not None:
                raise ValueError(
                    "explicit lines= belonged to the retired fixed-window "
                    "plain path; the plain window is sized by overscan_mm "
                    f"around the measured aperture. {safety.NO_COMMANDS_SENT}")
            if overscan_mm is None:
                overscan_mm = holder.OVERSCAN_MM
        with self._operation("scan"):
            self._prepared_for_scan = False
            if self._cal_capture is not None:
                self._cal_capture.set_frame(frame)
            if t is not None:
                return self._scan_dual(t, frame=frame, lines=lines,
                                       overscan_mm=overscan_mm, slide=slide)
            return self._scan_plain(frame=frame, overscan_mm=overscan_mm)

    def _cal_white_line(self) -> np.ndarray:
        """The last warm-up white-line measurement (N, 3) uint16, for the
        calibration data export; empty if none was recorded."""
        if self._last_white is None:
            return np.zeros((0, 3), dtype=np.uint16)
        return np.asarray(self._last_white, dtype=np.uint16)

    def _healthy_dark_b(self, dark_b):
        """Return a trustworthy dark_b and whether it was substituted.

        If ``dark_b`` is residual (Test 32: the device returned the frame's
        own dark_a tail instead of measuring), substitute this session's
        last healthy dark_b -- a dark measurement is frame-independent, so
        this is a proven correction, not a cover-up. A healthy dark_b is
        remembered for later frames. Fails closed (CalibrationError, scan
        FAILED, no motor command) if dark_b is residual and none healthy
        has been seen this session (frame 1 is empirically always healthy,
        so this is a defensive branch)."""
        if not calibrate.dark_is_residual(dark_b):
            self._last_good_dark_b = dark_b
            return dark_b, False
        log.warning("dark_b is residual (Test 32: repeated block, not a "
                    "measurement); substituting this session's healthy dark_b")
        if self._last_good_dark_b is None:
            raise safety.CalibrationError(
                "dark_b is residual and no healthy dark_b has been measured "
                "this session; cannot compute AFE offset. The scan stops "
                f"here. {safety.NO_RECOVERY_ATTEMPTED} "
                f"{safety.POWER_CYCLE_INSTRUCTION}",
                session=self.session.snapshot())
        return self._last_good_dark_b, True

    def _dark_b_for_offsets(self, dark_b):
        """_healthy_dark_b() plus the opt-in fallback. Returns (dark_b,
        substituted, fallback): ``fallback`` is None when the offset codes
        are computed from the bracket as always, or "default_offset_codes"
        when they must be calibrate._OFFSET_DEFAULT instead.

        The fallback applies ONLY when self.dark_fallback == "default" AND
        dark_b is residual AND no healthy dark_b has been measured this
        session -- precisely the case where _healthy_dark_b() would raise.
        Every other case (healthy dark_b, the A10 later-frame substitution,
        strict mode) goes through _healthy_dark_b() unchanged. The residual
        buffer is returned as-is for the records (dark_b_mean, cal_data)
        and is never remembered as healthy.

        Why this is safe: dark_b's only computational consumer is
        calibrate.offset_codes() (the two-point dark bracket, dark_a at
        offset code 0x80, dark_b at 0xFF -> AFE offset regs 5/6/7);
        everything else that touches it is a record. _OFFSET_DEFAULT
        (0x010B, 0x010A, 0x010B) are the vendor's own codes, which
        offset_codes() itself already returns when the bracket slope is
        abnormal, and which measured calibrations land within 1-2 codes of
        (Tests 84/85: 266/265/266 and 0x010a/0x0109/0x010a). One code is
        ~18 counts of 65535 (bracket slope 17.6-18 counts/code), so a
        1-code error shifts the black level by ~0.03 % of full scale. The
        motor/phase sequence is unchanged; the A10 substitution already
        continues a scan after a residual dark_b on later frames."""
        if (self.dark_fallback == "default" and self._last_good_dark_b is None
                and calibrate.dark_is_residual(dark_b)):
            log.warning(
                "dark_b is residual and no healthy dark_b has been measured "
                "this session; --dark-fallback default: using the vendor's "
                "default AFE offset codes %s instead of a measured bracket "
                "(recorded as dark_b_fallback in the .diag.json)",
                ["%#06x" % c for c in calibrate._OFFSET_DEFAULT])
            return dark_b, False, "default_offset_codes"
        dark_b, substituted = self._healthy_dark_b(dark_b)
        return dark_b, substituted, None

    def _calibration_rewrite(self, gain_r: int, gain_g: int, gain_b: int) -> None:
        """Re-apply cached AFE gain codes without a full recalibration.

        Runs tables.CAL_REWRITE (see its module-level comment and
        docs/protocol-notes.md's pass 14 addendum 3 for exactly which ops
        this is and isn't): a lamp toggle, a fixed register rewrite of
        unknown meaning kept verbatim, then the three AFE gain-code
        writes patched with the cached codes, then the same exposure
        block CAL_SHADING_MEASURE/CAL_SHADING_VERIFY already carry. No
        offset rewrite -- the capture this was decoded from does not
        have one; the uploaded shading table is what the vendor's own
        capture shows surviving PARK in scanner RAM.
        """
        self._run_phase(
            tables.CAL_REWRITE,
            gain_r=bytes([gain_r]), gain_g=bytes([gain_g]), gain_b=bytes([gain_b]),
        )

    def _scan_plain(self, frame: int,
                    overscan_mm: float = holder.OVERSCAN_MM) -> tuple[bytes, int]:
        # No homing move here. The vendor flow has none (protocol-notes.md
        # pass 14): positioning below is an absolute mode-0x18 feed that
        # works from wherever the previous frame left the carriage. The
        # home() call that used to sit here executed mode 0x30 -- the
        # scan pass -- with the previous frame's line count still in
        # regs 0x25-0x27, i.e. a full-length engine run before the
        # calibration reads (frame 2+ of a batch calibrated dark,
        # 2026-09-01).

        # ---- per-scan diagnostics: reset recording state (see diag.py) --
        self._diag_phase_seconds = {}
        self._diag_poll_timeouts = 0
        self._diag_cr_mismatches = 0
        self._diag_poll_timeout_details = []
        self._diag_poll_settle = []
        self._diag_poll_settle_max_s = 0.0
        self._cal_acquisitions = []
        self._cal_scanstop_waits = []
        self._cal_bufempty_waits = []
        self._scanstop_go_phase = None
        self._scanstop_armed = None
        self._diag_warmup = None
        self._diag_park_waits = None
        started_utc = datetime.now(timezone.utc).isoformat()
        t_start = time.monotonic()

        # ---- calibration: fresh measurement, or this session's cache ----
        # See docs/protocol-notes.md pass 14 + addendum 3, and the
        # _cal_cache comment in __init__. The cache key is (dpi, dual);
        # this path is always (3600, False) -- dual has its own
        # calibration and is never cached (_scan_dual).
        cal_key = (3600, False)
        cal_data = None          # filled only by a fresh calibration below
        cache = self._cal_cache
        if not self.recalibrate and cache is not None and cache["key"] == cal_key:
            calibration = "cached"
            gain_r, gain_g, gain_b = cache["gain"]
            off_r, off_g, off_b = cache["offset"]
            dark_a_mean = cache["dark_a_mean"]
            dark_b_mean = cache["dark_b_mean"]
            dark_b_substituted = cache["dark_b_substituted"]
            # A cached calibration carries its offset codes' provenance: a
            # frame reusing fallback codes must say so too.
            dark_b_fallback = cache.get("dark_b_fallback")
            warmup = cache["warmup"]
            shading_stats = cache["shading_stats"]
            shading2_stats = cache["shading2_stats"]
            white_dips_diag = None       # tables reused, no white measured
            # The codes in use came from the cached calibration: carry its
            # verdict (no acquisition was made this frame).
            cached_integ = ((cache.get("cal_integrity") or {})
                            if self._cal_integrity_telemetry else {})
            integ = {
                "cal_integrity": cached_integ.get("cal_integrity"),
                "cal_integrity_reasons": [f"cached calibration: {r}" for r in
                                          cached_integ.get("cal_integrity_reasons", [])],
                "cal_acquisitions": [],
            }
            cached_fail = ((cache.get("cal_fail_reasons") or [])
                           if self._cal_integrity_telemetry else [])
            if (self._cal_readback_guard == "fail"
                    and (integ["cal_integrity"] == "degraded" or cached_fail)):
                # A calibration that fail mode would have refused (e.g.
                # measured while the guard was "log") is not reused in fail
                # mode: refuse before the cached rewrite, nothing further
                # is sent.
                why = "; ".join(list(integ["cal_integrity_reasons"]) + cached_fail)
                raise safety.CalibrationError(
                    f"cached calibration is degraded ({why}) and is not reused "
                    f"(--cal-readback-guard fail). {safety.NO_RECOVERY_ATTEMPTED} "
                    f"{safety.POWER_CYCLE_INSTRUCTION}",
                    session=self.session.snapshot())
            log.info("calibration: reusing cached codes (gain R=%#04x G=%#04x B=%#04x, "
                     "offset R=%#06x G=%#06x B=%#06x)", gain_r, gain_g, gain_b,
                     off_r, off_g, off_b)
            self._calibration_rewrite(gain_r, gain_g, gain_b)
        else:
            calibration = "fresh"
            self._last_white = None
            # ---- dark pair (offset bracket, gain=0) ---------------------
            dark_a_raw = self._run_phase(tables.CAL_DARK_A)[0]
            if self._cal_capture is not None:
                self._cal_capture.note_buffer("dark_a", tables.CAL_DARK_A.name, dark_a_raw)
            dark_b_raw = self._run_phase(tables.CAL_DARK_B)[0]
            if self._cal_capture is not None:
                self._cal_capture.note_buffer("dark_b", tables.CAL_DARK_B.name, dark_b_raw)
            self._cal_note_channel_means(tables.CAL_DARK_A.name, dark_a_raw)
            self._cal_note_channel_means(tables.CAL_DARK_B.name, dark_b_raw)
            self._cal_integrity_refuse("after the dark pair")
            dark_a = np.frombuffer(dark_a_raw, dtype="<u2").reshape(-1, 3)
            dark_b = np.frombuffer(dark_b_raw, dtype="<u2").reshape(-1, 3)
            dark_b, dark_b_substituted, dark_b_fallback = self._dark_b_for_offsets(dark_b)

            # ---- white line (gain=0) -> compute AFE gain -----------------
            gain_r, gain_g, gain_b = self._gain_with_warmup(
                tables.CAL_WHITE,
                lambda raw: np.frombuffer(raw, dtype="<u2").reshape(-1, 3),
            )
            log.info("computed gain codes: R=%#04x G=%#04x B=%#04x", gain_r, gain_g, gain_b)

            # ---- gain-check pair (offset bracket, gain=computed) ---------
            gain_a_bufs = self._run_phase(
                tables.CAL_GAIN_CHECK_A,
                gain_r=bytes([gain_r]), gain_g=bytes([gain_g]), gain_b=bytes([gain_b]),
            )
            gain_b_bufs = self._run_phase(tables.CAL_GAIN_CHECK_B)
            for _ph, _bufs in ((tables.CAL_GAIN_CHECK_A, gain_a_bufs),
                               (tables.CAL_GAIN_CHECK_B, gain_b_bufs)):
                if _bufs:
                    self._cal_note_channel_means(_ph.name, _bufs[0])
            self._cal_integrity_refuse("after the gain checks")

            # ---- shading measurement (offset=computed final) -------------
            if dark_b_fallback is None:
                off_r, off_g, off_b = calibrate.offset_codes(dark_a, dark_b)
            else:
                off_r, off_g, off_b = calibrate._OFFSET_DEFAULT
            log.info("offset codes: R=%#06x G=%#06x B=%#06x", off_r, off_g, off_b)
            shading_meas_raw = self._run_phase(
                tables.CAL_SHADING_MEASURE,
                offset_r_hi=bytes([off_r >> 8]), offset_r_lo=bytes([off_r & 0xFF]),
                offset_g_hi=bytes([off_g >> 8]), offset_g_lo=bytes([off_g & 0xFF]),
                offset_b_hi=bytes([off_b >> 8]), offset_b_lo=bytes([off_b & 0xFF]),
            )[0]
            shading_meas = np.frombuffer(shading_meas_raw, dtype="<u2").reshape(128, 3762, 3)
            shading = calibrate.shading_table(shading_meas)

            # ---- upload + verify (re-measure, re-upload once) -------------
            self._run_phase(tables.CAL_SHADING_UPLOAD, shading_table=shading)

            # cal_shading_verify genuinely re-measures then re-uploads a
            # shading table computed from *that* read, so it can't be run
            # as a single _run_phase() call: shading_table2 depends on data
            # only available after the phase's own read completes. Run its
            # ops up to (not including) the re-upload's buffer-write
            # descriptor (split_at), compute, then run the rest patched.
            verify_phase = tables.CAL_SHADING_VERIFY
            self.session.phase = verify_phase.name
            _t0 = time.monotonic()
            verify_raw = self._exec_ops(verify_phase.ops[:verify_phase.split_at])[0]
            verify_meas = np.frombuffer(verify_raw, dtype="<u2").reshape(128, 3762, 3)
            white_guarded, white_dips = calibrate.guard_white_dips(verify_meas)
            if white_dips:
                log.info("white measurement: %d narrow dip(s) guarded before the gain table: %s",
                         len(white_dips), [d.as_dict() for d in white_dips])
            shading2 = calibrate.shading_table2(white_guarded, shading_meas)
            remaining = verify_phase.patched(shading_table2=shading2)[verify_phase.split_at:]
            self._exec_ops(remaining)
            self._diag_phase_seconds["cal_shading_verify"] = (
                self._diag_phase_seconds.get("cal_shading_verify", 0.0) + (time.monotonic() - _t0)
            )
            self._cal_integrity_refuse("after the shading verify")

            dark_a_mean = [float(x) for x in dark_a.astype(np.float64).mean(axis=0)]
            dark_b_mean = [float(x) for x in dark_b.astype(np.float64).mean(axis=0)]
            warmup = self._diag_warmup or {}
            cal_data = {
                "white_line": self._cal_white_line(),
                "dark_a": np.asarray(dark_a, dtype=np.uint16),
                "dark_b": np.asarray(dark_b, dtype=np.uint16),
                "shading_meas": np.asarray(shading_meas, dtype=np.uint16),
                "verify_meas": np.asarray(verify_meas, dtype=np.uint16),
                "shading_table": np.frombuffer(shading, dtype=np.uint8),
                "shading_table2": np.frombuffer(shading2, dtype=np.uint8),
                "gain_codes": np.array([gain_r, gain_g, gain_b], dtype=np.uint16),
                "offset_codes": np.array([off_r, off_g, off_b], dtype=np.uint16),
                "dpi": np.int64(3600),
                "dual": np.bool_(False),
            }
            shading_stats = _shading_stats(shading)
            shading2_stats = _shading_stats(shading2)
            white_dips_diag = [d.as_dict() for d in white_dips]
            integ = self._cal_integrity_diag(dark_b_substituted, dark_b_fallback)

            # Cache immediately on success -- see __init__'s _cal_cache
            # comment and (d) in the design: a LATER failure (position/
            # scan/park below) must not leave this trusted by the next
            # frame, so the try/except below clears it again if anything
            # from here on raises.
            self._cal_cache = {
                "key": cal_key,
                "gain": (gain_r, gain_g, gain_b),
                "offset": (off_r, off_g, off_b),
                "dark_a_mean": dark_a_mean,
                "dark_b_mean": dark_b_mean,
                "dark_b_substituted": dark_b_substituted,
                "dark_b_fallback": dark_b_fallback,
                "warmup": warmup,
                "shading_stats": shading_stats,
                "shading2_stats": shading2_stats,
                "cal_integrity": {k: integ[k] for k in
                                  ("cal_integrity", "cal_integrity_reasons")},
                # What --cal-readback-guard fail would refuse this
                # calibration for (it can be cached in log mode).
                "cal_fail_reasons": self._cal_integrity_fail_reasons(),
            }

        try:
            # ---- geometry: overscan sizes FEEDL and the line/chunk count -
            # The corrected mean mapping (option A, holder.STRIP_FIDUCIAL)
            # plus a leading/trailing margin (option C), docs/holder-
            # position-design.md sections 4-5 -- the single runtime geometry
            # for the plain path since Test 58's acceptance (section 9 step
            # 2). The furthest motor position is range-checked inside
            # overscan_geometry(); the stop FEEDL is checked here as well.
            geom = holder.overscan_geometry(
                frame,
                res_units_per_line=7200 // 3600,
                chunk_lines=tables.IMAGE_CHUNK_LINES,
                colour_crop_lines=image_mod.align_shift(3600),
                overscan_mm=overscan_mm,
            )
            feedl = holder.check_feedl(geom.feedl)
            travel_margin_steps = holder.travel_margin(
                max(feedl, round(geom.end_hwdpi)))
            scan_phase = tables.scan_phase(geom.chunks)
            n_chunks = geom.chunks
            n_lines = tables.scan_lines_for_chunks(geom.chunks)
            log.info("overscan frame %d: FEEDL=%d chunks=%d lines=%d "
                     "margins lead=%.3f trail=%.3f mm", frame, feedl, n_chunks,
                     n_lines, geom.leading_margin_mm, geom.trailing_margin_mm)

            # ---- position: relative feed from current carriage position --
            self._park_scale = position_timeout_scale(tables, feedl)
            log.info("positioning to frame %d (FEEDL=%d)", frame, feedl)
            # Strict completion (class F, budget scaled with FEEDL): never
            # start SCAN on a moving transport (Test 18, frame 4).
            self._run_phase(
                tables.POSITION,
                strict_polls=self._strict_status_polls(tables.POSITION),
                strict_mask=self._position_status_mask(),
                poll_timeout_scale=position_timeout_scale(tables, feedl),
                feedl_hi=bytes([(feedl >> 16) & 0xFF]),
                feedl_mid=bytes([(feedl >> 8) & 0xFF]),
                feedl_lo=bytes([feedl & 0xFF]),
            )

            # ---- scan: 3 slope tables, line count, execute, image data ----
            check_line_count(n_lines)
            buffers = self._run_phase(
                scan_phase,
                lines_hi=bytes([(n_lines >> 8) & 0xFF]),
                lines_lo=bytes([n_lines & 0xFF]),
            )
            # buffers[:n_chunks] are the image data (capture fidelity: the
            # first image descriptor carries wIndex=8, subsequent ones 0 --
            # meaning unknown, baked into tables.py). The trailing buffer
            # (buffers[-1]) is a drain of unclear purpose (not part of the
            # documented image size); already read verbatim above, discarded
            # here.
            image = b"".join(buffers[:n_chunks])

            # ---- park -------------------------------------------------------
            # PARK's own first op is the captured end-of-access control
            # write (cw wv=0x8d) -- no separate call needed here.
            self._park(tables, ir=False)
        except Exception:
            # A calibration this scan cached (or reused) may no longer be
            # trustworthy after a failure this far in -- the transport/
            # engine state a later frame would rely on is unknown (see
            # __init__'s _cal_cache comment, design point (d)). Never
            # leave a cache a failed scan didn't earn.
            self._cal_cache = None
            raise

        self.last_cal_data = cal_data
        self.last_diag = {
            "dpi": 3600,
            "frame": frame,
            "dual": False,
            "lines": n_lines,
            "width": tables.IMAGE_WIDTH,
            "raw_bytes": len(image),
            "chunk_count": n_chunks,
            "overscan_mm": overscan_mm,
            "calibration": calibration,
            "dark_a_mean": dark_a_mean,
            "dark_b_mean": dark_b_mean,
            "dark_b_substituted": dark_b_substituted,
            "dark_b_fallback": dark_b_fallback,
            **integ,
            "cal_scanstop_waits": list(self._cal_scanstop_waits),
            "cal_bufempty_waits": list(self._cal_bufempty_waits),
            "white_mean": warmup.get("white_mean"),
            "white_max": warmup.get("white_max"),
            "gain_codes": [gain_r, gain_g, gain_b],
            "warmup_attempts": warmup.get("attempts"),
            "warmup_gain_history": warmup.get("gain_history"),
            "warmup_exhausted": warmup.get("exhausted"),
            "warmup_peak_history": warmup.get("peak_history"),
            "warmup_measurement_times_s": warmup.get("measurement_times_s"),
            "offset_codes": [off_r, off_g, off_b],
            "shading": shading_stats,
            "shading2": shading2_stats,
            "white_dips": white_dips_diag,
            "feedl": feedl,
            "travel_margin_steps": travel_margin_steps,
            "phase_seconds": dict(self._diag_phase_seconds),
            "total_seconds": time.monotonic() - t_start,
            "poll_timeouts": self._diag_poll_timeouts,
            "cr_mismatches": self._diag_cr_mismatches,
            "poll_timeout_details": list(self._diag_poll_timeout_details),
            "poll_settle": list(self._diag_poll_settle),
            "poll_settle_max_s": round(self._diag_poll_settle_max_s, 4),
            "park_mode": self.park_mode,
            "park_waits": dict(self._diag_park_waits) if self._diag_park_waits is not None else None,
            "started_utc": started_utc,
            "finished_utc": datetime.now(timezone.utc).isoformat(),
            "session": self.session.snapshot(),
        }
        self._warn_cal_integrity(frame, integ)

        return image, tables.IMAGE_WIDTH

    # ----------------------------------------------------------- scan (IR)

    def _scan_dual(self, t, frame: int = 1, lines: int | None = None,
                   overscan_mm: float | None = None,
                   slide: bool = False) -> tuple[bytes, int, dict]:
        """Dual-light (alternating IR/visible line) counterpart of scan(),
        driven by the phases of table module `t` -- tables_ir (3600 dpi,
        trace 04) or a tables_dpi<N> module (the 2026-09-02 captures);
        all compiled by tools/gen_tables.py's compile_dual() to one
        interface.

        Every calibration buffer in this mode is at the RAW sensor
        readout width (t.IMAGE_WIDTH: 5184 at 3600 dpi, not the windowed
        3762 the plain scan uses) and covers ALTERNATING lines -- even
        index = IR pass, odd index = visible pass (see ../cal-data/ir/
        ir-analysis.md and image.split_ir(), which applies the same
        convention to the final image). Measurements that feed a per-
        channel computation (gain, shading) are de-interleaved into
        their halves BEFORE computing. The shading table is uploaded to
        TWO scanner-RAM addresses with a CROSS-CONNECTION: address A
        (0x10014000) is computed from the ODD (visible) measurement
        lines but applied by the scanner hardware to ODD (visible)
        scan lines; address B (0x10034000) from EVEN (IR) measurement
        lines, applied to EVEN (IR) scan lines. The vendor uploads
        were analysed as "A from even, B from odd" (pass 18), which
        correctly identified the SOURCE data but incorrectly assumed
        same-source application; empirical testing (2026-09-03) proved
        the scanner applies A→odd and B→even, matching the pre-pass-18
        code that produced working images. See calibrate.SHADING2_
        TARGET_A/_B for the per-address white-uniformity targets.
        AFE gain/offset (regs 2-7) and FEEDL/line-count injections
        are the same single-register mechanism as the plain scan.

        The line count is programmed as whole image chunks: `lines`
        (alternating lines, default t.DEFAULT_LINES) rounded up to
        t.LINES_PER_CHUNK, and that many chunks are read.
        """
        # No homing move -- see scan().
        W = t.IMAGE_WIDTH
        dpi = t.DPI

        # Dual is never cached (docs/protocol-notes.md pass 14 addendum 3:
        # no vendor capture of a dual-light skip-calibration exists, and
        # its own CAL_SHADING_VERIFY preamble differs from plain's --
        # reported, not guessed; see tables.CAL_REWRITE's module comment).
        # It ALWAYS runs a full calibration below, which uploads its own
        # shading table to scanner RAM -- overwriting whatever the plain
        # path's cache (if any) left there. Clear it unconditionally so a
        # later plain scan in this session never trusts a shading table
        # that is no longer the one it was cached against.
        self._cal_cache = None

        # ---- per-scan diagnostics: reset recording state (see diag.py) --
        self._diag_phase_seconds = {}
        self._diag_poll_timeouts = 0
        self._diag_cr_mismatches = 0
        self._diag_poll_timeout_details = []
        self._diag_poll_settle = []
        self._diag_poll_settle_max_s = 0.0
        self._cal_acquisitions = []
        self._cal_scanstop_waits = []
        self._cal_bufempty_waits = []
        self._scanstop_go_phase = None
        self._scanstop_armed = None
        self._diag_warmup = None
        self._diag_park_waits = None
        started_utc = datetime.now(timezone.utc).isoformat()
        t_start = time.monotonic()

        # ---- dark pair (offset bracket, gain=0) ------------------------
        # Both reads feed offset_codes() (per-channel means, same as the
        # plain path); the doubled buffer size (alternating IR/visible)
        # doesn't matter -- flattened to (N, 3) either way, and the IR
        # and visible dark lines are both dark.
        dark_a_raw = self._run_phase(t.CAL_DARK_A)[0]
        if self._cal_capture is not None:
            self._cal_capture.note_buffer("dark_a", t.CAL_DARK_A.name, dark_a_raw)
        dark_b_raw = self._run_phase(t.CAL_DARK_B)[0]
        if self._cal_capture is not None:
            self._cal_capture.note_buffer("dark_b", t.CAL_DARK_B.name, dark_b_raw)
        self._cal_note_channel_means(t.CAL_DARK_A.name, dark_a_raw)
        self._cal_note_channel_means(t.CAL_DARK_B.name, dark_b_raw)
        self._cal_integrity_refuse("after the dark pair")
        dark_a = np.frombuffer(dark_a_raw, dtype="<u2").reshape(-1, 3)
        dark_b = np.frombuffer(dark_b_raw, dtype="<u2").reshape(-1, 3)
        dark_b, dark_b_substituted, dark_b_fallback = self._dark_b_for_offsets(dark_b)
        self._last_white = None

        # ---- white line (gain=0) -> compute AFE gain -------------------
        # 2 raw lines (alternating): index 0 = IR, index 1 = visible --
        # at the full sensor width (5184 px at every dpi below 7200, not
        # W), hence the reshape by line count rather than by W. Only ONE
        # set of AFE gain registers exists (regs 2/3/4, same as the plain
        # path) -- computed from the VISIBLE line only, since mixing in
        # the IR line's near-flat/bright broadcast values would badly
        # skew the 99.9th-percentile peak calculation.
        gain_r, gain_g, gain_b = self._gain_with_warmup(
            t.CAL_WHITE,
            lambda raw: np.frombuffer(raw, dtype="<u2").reshape(2, -1, 3)[1],
        )
        log.info("computed gain codes (%d dpi dual): R=%#04x G=%#04x B=%#04x", dpi, gain_r, gain_g, gain_b)

        # ---- gain-check pair (offset bracket, gain=computed) -----------
        gain_a_bufs = self._run_phase(
            t.CAL_GAIN_CHECK_A,
            gain_r=bytes([gain_r]), gain_g=bytes([gain_g]), gain_b=bytes([gain_b]),
        )
        gain_b_bufs = self._run_phase(t.CAL_GAIN_CHECK_B)
        for _ph, _bufs in ((t.CAL_GAIN_CHECK_A, gain_a_bufs),
                           (t.CAL_GAIN_CHECK_B, gain_b_bufs)):
            if _bufs:
                self._cal_note_channel_means(_ph.name, _bufs[0])
        self._cal_integrity_refuse("after the gain checks")

        # ---- shading measurement (offset=computed final) ---------------
        if dark_b_fallback is None:
            off_r, off_g, off_b = calibrate.offset_codes(dark_a, dark_b)
        else:
            off_r, off_g, off_b = calibrate._OFFSET_DEFAULT
        log.info("offset codes (%d dpi dual): R=%#06x G=%#06x B=%#06x", dpi, off_r, off_g, off_b)
        shading_meas_raw = self._run_phase(
            t.CAL_SHADING_MEASURE,
            offset_r_hi=bytes([off_r >> 8]), offset_r_lo=bytes([off_r & 0xFF]),
            offset_g_hi=bytes([off_g >> 8]), offset_g_lo=bytes([off_g & 0xFF]),
            offset_b_hi=bytes([off_b >> 8]), offset_b_lo=bytes([off_b & 0xFF]),
        )[0]
        shading_meas = np.frombuffer(shading_meas_raw, dtype="<u2").reshape(t.SHADING_LINES, W, 3)
        shading_meas_ir = shading_meas[0::2]       # even lines = IR pass
        shading_meas_vis = shading_meas[1::2]      # odd lines  = visible pass
        # Address A (0x10014000) is applied by the scanner to ODD
        # (visible) scan lines; address B (0x10034000) to EVEN (IR).
        # The vendor computes table A FROM even (IR) measurements and
        # table B FROM odd (visible) — a cross-connection (see pass 18
        # analysis, protocol-notes.md).  But the old code (pre pass-18)
        # proved empirically that address A corrects VISIBLE and B
        # corrects IR: uploading visible → A and IR → B produced images
        # with 170% dynamic range; the swapped assignment (pass 18)
        # produced flat noise.  So: visible measurement → table A,
        # IR measurement → table B.
        shading_a = calibrate.shading_table(shading_meas_vis, width=W)
        shading_b = calibrate.shading_table(shading_meas_ir, width=W)

        # ---- upload + verify (re-measure, re-upload once) ---------------
        self._run_phase(t.CAL_SHADING_UPLOAD, shading_table_a=shading_a, shading_table_b=shading_b)

        verify_phase = t.CAL_SHADING_VERIFY
        self.session.phase = verify_phase.name
        _t0 = time.monotonic()
        verify_raw = self._exec_ops(verify_phase.ops[:verify_phase.split_at])[0]
        verify_meas = np.frombuffer(verify_raw, dtype="<u2").reshape(t.SHADING_LINES, W, 3)
        # Same cross-connection: table A (visible lines) uses TARGET_A,
        # table B (IR lines) uses TARGET_B — matching the vendor's
        # per-address targets, with the line subsets swapped to the
        # empirically correct assignment.
        white_a, dips_a = calibrate.guard_white_dips(verify_meas[1::2])
        white_b, dips_b = calibrate.guard_white_dips(verify_meas[0::2])
        if dips_a or dips_b:
            log.info("white measurement: narrow dip(s) guarded before the gain tables: A %s B %s",
                     [d.as_dict() for d in dips_a], [d.as_dict() for d in dips_b])
        shading2_a = calibrate.shading_table2_dual(
            white_a, shading_meas_vis, width=W, target=calibrate.SHADING2_TARGET_A)
        shading2_b = calibrate.shading_table2_dual(
            white_b, shading_meas_ir, width=W, target=calibrate.SHADING2_TARGET_B)
        remaining = verify_phase.patched(
            shading_table2_a=shading2_a, shading_table2_b=shading2_b,
        )[verify_phase.split_at:]
        self._exec_ops(remaining)
        self._diag_phase_seconds["cal_shading_verify"] = (
            self._diag_phase_seconds.get("cal_shading_verify", 0.0) + (time.monotonic() - _t0)
        )
        self._cal_integrity_refuse("after the shading verify")

        # ---- position: relative feed from current carriage position ------
        # POSITION uses mode 0x18 (feed) which advances FEEDL steps from
        # wherever the carriage is. In same-DPI sessions the preceding
        # PARK always returns to the same offset, so the fixed FEEDL
        # lands at the correct frame. After a DPI change the PARK end
        # position differs (observed ~1060 rows / 7.5 mm shift at 3600
        # dpi between 2400 and 3600); re-loading the magazine resets
        # the carriage to the load-position reference. A proper homing
        # command would fix this, but requires hardware testing.
        #
        # Geometry: production (lines=None) = the A+C corrected mapping
        # in visible lines (holder.dual_overscan_geometry); explicit
        # lines= = the diagnostic/capture path on the historical grid.
        geom = None
        if slide:
            # Mounted-slide opening `frame` (1-4): the slide fiducial model
            # through the same dual geometry, plus the slide-only travel
            # check (holder.slide_dual_geometry). Everything after this --
            # POSITION, scan, PARK -- is the strip path verbatim.
            if lines is not None:
                raise ValueError("slide scans do not take lines=")
            geom, wire_lines = holder.slide_dual_geometry(
                frame,
                dpi=dpi,
                lines_per_chunk=t.LINES_PER_CHUNK,
                colour_crop_lines=image_mod.align_shift(dpi),
                default_wire_lines=t.DEFAULT_LINES,
                overscan_mm=(overscan_mm if overscan_mm is not None
                             else holder.SLIDE_OVERSCAN_MM),
            )
            feedl = holder.check_feedl(geom.feedl, holder.SLIDE)
            travel_margin_steps = holder.travel_margin(
                holder.slide_travel_end(feedl, wire_lines, dpi))
            log.info("dual slide %d (%d dpi): FEEDL=%d chunks=%d "
                     "wire_lines=%d margins lead=%.3f trail=%.3f mm",
                     frame, dpi, feedl, geom.chunks, wire_lines,
                     geom.leading_margin_mm, geom.trailing_margin_mm)
        elif lines is None:
            geom, wire_lines = holder.dual_overscan_geometry(
                frame,
                dpi=dpi,
                lines_per_chunk=t.LINES_PER_CHUNK,
                colour_crop_lines=image_mod.align_shift(dpi),
                default_wire_lines=t.DEFAULT_LINES,
                overscan_mm=overscan_mm if overscan_mm is not None else holder.OVERSCAN_MM,
            )
            feedl = holder.check_feedl(geom.feedl)
            travel_margin_steps = holder.travel_margin(
                max(feedl, round(geom.end_hwdpi)))
            log.info("dual overscan frame %d (%d dpi): FEEDL=%d chunks=%d "
                     "wire_lines=%d margins lead=%.3f trail=%.3f mm",
                     frame, dpi, feedl, geom.chunks, wire_lines,
                     geom.leading_margin_mm, geom.trailing_margin_mm)
        else:
            feedl = holder.check_feedl(t.feedl_for_frame(frame))
            # Explicit lines= (diagnostic grid): no modelled pass end.
            travel_margin_steps = None
        self._park_scale = position_timeout_scale(t, feedl)
        log.info("positioning to frame %d (FEEDL=%d, %d dpi dual)", frame, feedl, dpi)
        # Strict completion (class F, budget scaled with FEEDL): never
        # start SCAN on a moving transport (Test 18, frame 4).
        self._run_phase(
            t.POSITION,
            strict_polls=self._strict_status_polls(t.POSITION),
            strict_mask=self._position_status_mask(),
            poll_timeout_scale=position_timeout_scale(t, feedl),
            feedl_hi=bytes([(feedl >> 16) & 0xFF]),
            feedl_mid=bytes([(feedl >> 8) & 0xFF]),
            feedl_lo=bytes([feedl & 0xFF]),
        )

        # ---- scan: 3 slope tables, line count, execute, image data ------
        if geom is not None:
            n_chunks = geom.chunks
            n_lines = wire_lines
        else:
            n_chunks = max(1, -(-lines // t.LINES_PER_CHUNK))
            n_lines = n_chunks * t.LINES_PER_CHUNK
        check_line_count(n_lines)
        buffers = self._run_phase(
            t.scan_phase(n_chunks),
            lines_top=bytes([(n_lines >> 16) & 0xFF]),
            lines_hi=bytes([(n_lines >> 8) & 0xFF]),
            lines_lo=bytes([n_lines & 0xFF]),
        )
        # buffers[:n_chunks] are the image data. tables_ir's tail issues
        # a further descriptor that the vendor cancelled with no data
        # (ir-analysis.md); it is not among the collected buffers at all
        # (_exec_ops only appends a buffer on flush, and a cancelled
        # read never gets any bi data to flush).
        image = b"".join(buffers[:n_chunks])

        # ---- park ---------------------------------------------------------
        self._park(t, ir=True)

        integ = self._cal_integrity_diag(dark_b_substituted, dark_b_fallback)
        warmup = self._diag_warmup or {}
        self.last_cal_data = {
            "white_line": self._cal_white_line(),
            "dark_a": np.asarray(dark_a, dtype=np.uint16),
            "dark_b": np.asarray(dark_b, dtype=np.uint16),
            "shading_meas": np.asarray(shading_meas, dtype=np.uint16),
            "verify_meas": np.asarray(verify_meas, dtype=np.uint16),
            "shading_table_a": np.frombuffer(shading_a, dtype=np.uint8),
            "shading_table_b": np.frombuffer(shading_b, dtype=np.uint8),
            "shading_table2_a": np.frombuffer(shading2_a, dtype=np.uint8),
            "shading_table2_b": np.frombuffer(shading2_b, dtype=np.uint8),
            "gain_codes": np.array([gain_r, gain_g, gain_b], dtype=np.uint16),
            "offset_codes": np.array([off_r, off_g, off_b], dtype=np.uint16),
            "dpi": np.int64(dpi),
            "dual": np.bool_(True),
        }
        self.last_diag = {
            "dpi": dpi,
            "frame": frame,
            "dual": True,
            "lines": n_lines,
            "width": W,
            "raw_bytes": len(image),
            "chunk_count": n_chunks,
            "overscan_mm": (overscan_mm if overscan_mm is not None
                            else (holder.SLIDE_OVERSCAN_MM if slide
                                  else holder.OVERSCAN_MM)) if geom is not None else None,
            # Dual has no skip-calibration path (see _scan_dual's opening
            # comment) -- always a fresh measurement.
            "calibration": "fresh",
            "dark_a_mean": [float(x) for x in dark_a.astype(np.float64).mean(axis=0)],
            "dark_b_mean": [float(x) for x in dark_b.astype(np.float64).mean(axis=0)],
            "dark_b_substituted": dark_b_substituted,
            "dark_b_fallback": dark_b_fallback,
            **integ,
            "cal_scanstop_waits": list(self._cal_scanstop_waits),
            "cal_bufempty_waits": list(self._cal_bufempty_waits),
            "white_mean": warmup.get("white_mean"),
            "white_max": warmup.get("white_max"),
            "gain_codes": [gain_r, gain_g, gain_b],
            "warmup_attempts": warmup.get("attempts"),
            "warmup_gain_history": warmup.get("gain_history"),
            "warmup_exhausted": warmup.get("exhausted"),
            "warmup_peak_history": warmup.get("peak_history"),
            "warmup_measurement_times_s": warmup.get("measurement_times_s"),
            "offset_codes": [off_r, off_g, off_b],
            "shading": {"a": _shading_stats(shading_a), "b": _shading_stats(shading_b)},
            "shading2": {"a": _shading_stats(shading2_a), "b": _shading_stats(shading2_b)},
            "white_dips": {"a": [d.as_dict() for d in dips_a], "b": [d.as_dict() for d in dips_b]},
            "feedl": feedl,
            "travel_margin_steps": travel_margin_steps,
            "phase_seconds": dict(self._diag_phase_seconds),
            "total_seconds": time.monotonic() - t_start,
            "poll_timeouts": self._diag_poll_timeouts,
            "cr_mismatches": self._diag_cr_mismatches,
            "poll_timeout_details": list(self._diag_poll_timeout_details),
            "poll_settle": list(self._diag_poll_settle),
            "poll_settle_max_s": round(self._diag_poll_settle_max_s, 4),
            "park_mode": self.park_mode,
            "park_waits": dict(self._diag_park_waits) if self._diag_park_waits is not None else None,
            "started_utc": started_utc,
            "finished_utc": datetime.now(timezone.utc).isoformat(),
            "session": self.session.snapshot(),
        }
        if slide:
            # Only on slide scans, so a strip scan's record is unchanged.
            self.last_diag["holder"] = holder.SLIDE.name
            self.last_diag["slide"] = frame
        self._warn_cal_integrity(frame, integ)

        return image, W, {"width": W, "alternating": True, "dpi": dpi}
