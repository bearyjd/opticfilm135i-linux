# Offline checks — the reproducible, hardware-free regression gate

Every check here runs without a scanner and without network access. It
is the reproducibility answer the project's updated course asks for: a
documented set of checks, their dependencies, their commands and their
expected results, that any commit can be held to. The substantiated
mechanism is the local commands below. A GitHub Actions workflow
(`.github/workflows/offline-checks.yml`) runs part of them on every push
to master and every pull request -- see "The GitHub Actions workflow" at
the end for what it covers and what it leaves to the local run.

Nothing here talks to USB. The suites that would drive hardware are the
scanning tests, and those are not offline — they live in
`docs/test-log.md` as hardware runs.

## Dependencies

| For | Needs |
|---|---|
| the Python suites | Python 3.10+, and `numpy`, `pillow`, `tifffile`, `pyusb`, `pytest` (the repo venv has them: `.venv/`) |
| the generator check | Python only |
| the compiler-based suites | a C/C++ compiler (`g++`) on `PATH`; they print `SKIPPED` and pass trivially without one |
| the three backend suites | additionally a built `libsane-genesys.so` and `SANE_BACKENDS_DIR` (see below) — **not** in CI |

## The checks

### 1. The full local gate

```
.venv/bin/python tools/release_check.py
```

Runs every offline suite and reports each as **PASS** (ran every test,
all passed), **PARTIAL** (ran, but skipped some of its tests), **SKIP**
(ran none of its tests -- a precondition such as a compiler or a built
backend is absent) or **FAIL**. It does **not** count a mandatory suite
as done just because its script exited cleanly: a skipped test is a test
that was not run, and a suite reporting zero tests run proved nothing
(that is a FAIL, never a pass). Verdict and exit code:

- **FULL VERIFICATION PASSED** (exit 0) only when every suite ran every
  one of its tests and all passed -- nothing skipped anywhere;
- **PARTIAL** (exit 1) when a mandatory suite skipped any test, entirely
  or in part; **FAILED** (exit 1) when any suite failed;
- **LIMITED RUN PASSED** (exit 0) under `--no-backend`, which excuses
  skips in the three backend suites only -- a skip in a core or compiler
  suite is still PARTIAL. `--allow-dirty` drops the clean-checkout
  requirement.

The parser and the verdict are pure functions pinned by
`tests/test_release_check.py` (mixed passed/skipped, entirely skipped,
zero tests, normal success, failure, and the `--no-backend` scope).

Substantiated: a full local run on 2026-11-05, against a built backend,
reported **FULL VERIFICATION PASSED, 739 tests, 0 skipped** (the first such run, on 2026-09-15, had 338). Run it where the backend is
built (the development clone) for full verification, or with
`--no-backend` for the buildless subset.

### 2. Generated-table consistency

```
.venv/bin/python tools/gen_sane_tables.py --check
```

Regenerates `sane/gl126_tables.{cpp,h}` into memory and fails if the
checked-in files differ. Guards against a hand-edit of the generated
tables, and against the generator and the committed output drifting
apart. Expected: `generated SANE tables are up to date`.

### 3. Python offline suites (no compiler, no backend)

Each is a standalone script that prints `N tests passed` and exits 0:

```
for t in test_safety test_cold_reseat test_calibrate test_hwblock \
         test_park test_offline test_diag test_dpi test_ir \
         test_image_probe test_aperture_crop test_overscan \
         test_dual_overscan test_slides test_sweep test_calibration_cache \
         test_film110 test_film110_digitize test_film110_report \
         test_film110_sideband test_vendor_isolation test_watch_events \
         test_buttons test_release_check test_poll test_white_blockage \
         test_cal_wait_scanstop test_cross_unit_check \
         test_cr_mismatch_details test_trace_recorder \
         test_position_offset test_recrop test_album \
         test_py310_syntax test_cold_homing_rounds test_diag_readback_regs \
         test_load_probe test_replay_harness; do
    .venv/bin/python tests/run_suite.py $t || exit 1
done
```

The list is the "core" group of `SUITES` in `tools/release_check.py`,
which is the authority if the two ever differ.

These cover the safety model, calibration maths, the park state machine,
the image path, the DPI and IR logic, the overscan/coverage geometry and
the bulk-digitisation workflow — the driver's own logic, all without
hardware.

`test_trace_recorder` uses fake USB devices and the upstream scan wire
digests, exercising recording on/off, private exclusive files, short and
failed transfers, recording failures and the converter. It needs no pytest.
The suite is also mandatory in `tools/release_check.py` and included in
the provided workflow.

For an explicitly requested hardware session, setting
`OF135I_TRACE=/new/path/run.jsonl` enables the diagnostic recorder; unset
or empty leaves it off. It observes transfers through `GuardedDevice`,
including interrupt reads, without adding commands, retries or sleeps.
Synchronous file writes do add host overhead, so recorded timing includes
that overhead. It has run on hardware once (Test 117 in
docs/test-log.md: one 600 dpi scan traced, one not, alike in calibration
and timing on a slow-paced host), which is not a timing validation. Files
are created
exclusively as 0600; existing files and symlinks are refused. One warning
disables recording after an error while USB I/O continues; Ctrl-C while
recording disables it too and never replaces a failed transfer's error.
If Ctrl-C lands while a failed transfer is being recorded, it survives only
as a note on that USB exception: code that catches and retries the USB
error loses the Ctrl-C.
Bulk-IN and interrupt reads record the requested size (`want`) and mark
short reads `short:N`. One recorder is opened per `UsbIo`, so a process
that opens the device more than once records only the first session
(later opens warn); the recorder is not thread-safe.

A traced run is slower between transfers, so it can hide timing problems.
Use the trace to compare what was sent, not to show that timing is
unchanged. The control-read pace is the example: 0.4 ms per read on one
host against 3.9 ms on another made issue #35 invisible on the slower one
(docs/cal-scanstop-gate.md). Note also that the default
`--cal-wait-scanstop enforce` adds its status reads to a trace; compare
against `off` only when you want the vendor-capture wire.

If the same `OF135I_TRACE` value reaches several processes, only the first
records, because the file then exists and later opens warn and do not record (for
example under `tools/cross_unit_check.py` only the first `doctor` is
recorded). A per-process file name could come later.

Convert an existing recording entirely offline:

```
.venv/bin/python tools/trace_to_compiled.py run.jsonl -o run.trace.json.gz
```

The converter requires the recorder's `{"format": ...}` header and
validates every record. It ignores a damaged final line (interrupted
write); any other damaged or malformed line is reported on stderr and the
conversion is refused unless `--skip-damaged` is given. It skips
failed transfers, short OUT/control-IN transfers and interrupt records;
bulk-IN keeps the actual returned length plus `want`. Timestamps follow
`compile_trace.py` (cw/cr/bo at submission, bi at completion). The output
is written to a private temp file and hard-linked into place, which is
atomic: the output appears complete or not at all. On filesystems without
hard links (such as exFAT or some FUSE/SMB mounts) it is copied to an
exclusively created 0600 file instead; a failed copy removes that file,
but a hard kill or power loss mid-copy can leave a truncated one. An
existing output or symlink is refused.

Exit codes: 0 converted; 1 conversion refused or output not written
(damaged lines without `--skip-damaged`, existing output or symlink,
write error); 2 input unreadable or not an OF135I_TRACE recording
(missing or wrong `{"format": ...}` header), or a command-line usage error.

Only the first 64 bytes of control and bulk-OUT payloads are recorded;
bulk-IN image data is never recorded. A longer payload is never padded or
cut: its op gets `data`/`resp` `null`, the full `len` and
`"partial": true`. A record whose `data` is not strict hex or not exactly
`min(len, 64)` bytes counts as damaged.

Converter output is diagnostic only: transfers are dropped (failed, short
OUT/control-IN, interrupt reads, damaged lines), so every converted op
carries `"src": "of135i-trace/1"`, and the file is wrapped as
`{"format": "of135i-trace-converted/1", "ops": [...]}` (a dict, not a bare
list), so deleting every `src` key still leaves a file replay refuses.
`tools/replay_trace.py` refuses,
before opening the device, any trace containing an op with `src`, a
partial op, or a control/bulk write whose data is not strict hex (pairs
of hex digits, no whitespace). Committed captures in `traces/` carry no
`src` key and still pass; loaders that read ops by key ignore it.

The mounted-slide and DPI-aware cleaning regressions also run explicitly
under pytest in CI (the standalone suites above remain in place):

```
python -m pytest -q tests/test_slides.py tests/test_ir.py tests/test_offline.py
```

The safety suite also runs explicitly under pytest, covering incomplete
calibration acquisitions and both gain-check phases with capture enabled
and disabled. This collects every test in the file:

```
python -m pytest -q tests/test_safety.py
```

The experimental vendor startup has a separate pytest suite, including
parameterized refusals, exact sanitized write-transcript checks and motor
deadline/failure checks. It never opens a physical USB device:

```
python -m pytest -q tests/test_vendor_startup.py
```

The eject outcome and deadline suites check truthful CLI reporting,
manifest failure stages, stale diagnostics and strict completion deadlines
with synthetic USB devices and clocks:

```
python -m pytest -q tests/test_eject_outcome.py tests/test_eject_deadline.py
```

The status-word diagnostics suite checks the named-bit decoder, the
eject sensor-clear interpretation and the travel-margin diagnostic; the
wire-baseline suite pins, by digest, the exact transfer sequence of the
default plain, dual, slide, eject and cold-start flows, so a diagnostic
change that alters traffic fails:

```
python -m pytest -q tests/test_poll.py tests/test_status_decode.py tests/test_wire_baseline.py
```

The calibration read-back guard suite checks the "acquisition not
observed" signature (engine stopped, buffer empty after a timed-out
read-back poll), that the default `log` mode only records it and leaves
the wire sequence identical, that the opt-in `fail` mode stops before the
phase's next transfer with the session FAILED, and the CLI flag:

```
python -m pytest -q tests/test_cal_readback_guard.py
```

The opt-in POSITION motor-idle suite checks that the default positioning
wait is today's class check (wire sequence unchanged), that
`--position-require-motor-idle` additionally requires the motor-enable
bit clear on every profile (a class-F word with that bit set fails the
scan before SCAN starts, session FAILED), and that the budget is the same
in both modes:

```
python -m pytest -q tests/test_position_motor_idle.py
```

The cold-start move outcome suite checks that a timed-out cold-start motor
move names its outcome (`ended-off-sensor`, `still-busy`,
`no-busy-observed`, `malformed`, `other`) in the error and the session
failure record with the timing and wire sequence unchanged, that f855
stays the only success value, and that the opt-in
`--cold-move-early-fail` stops a move that ran and then stayed stopped off
the home sensor for 1 s (`ColdMoveEndedOffSensorError`, session FAILED,
no further move):

```
python -m pytest -q tests/test_cold_move_outcome.py
```

The opt-in `--cold-homing-rounds N` (experimental, unverified on hardware)
is checked to leave the default traffic byte-identical, to skip only the
extra rounds for N = 1 or 2, to reject invalid values before any transfer
and to reach every command that can run the cold start:

```
python -m pytest -q tests/test_cold_homing_rounds.py
```

The transfer recorder suite (described above) also runs under pytest:

```
python -m pytest -q tests/test_trace_recorder.py
```

The calibration integrity suite checks the default-on stale-read
telemetry: per calibration acquisition the re-arm gap, the previous
acquisition's post-clear status reads (decoded), the execute-pulse-to-data
time, `served_fast` (below `CAL_SERVED_FAST_S`, an inferred 10 ms),
`acq_not_observed`, identical per-channel means and `stale_suspect`; the
per-frame `cal_integrity` verdict (`clean` / `degraded`) with one warning
when degraded; and that switching the telemetry off or on changes neither
the transfer sequence nor any poll-loop clock or sleep call:

```
python -m pytest -q tests/test_cal_integrity.py
```

The calibration stop-gate suite checks `--cal-wait-scanstop`, default
`enforce` (docs/cal-scanstop-gate.md, "Decision", has the evidence and its
limits).
After the SCAN-clear
that closes a calibration execute pulse, and before the next OUT transfer,
register 0x100 bit 0 and then register 0x101 bit 0 must read clear in
2-byte replies with the 0x55 acknowledge, under one shared deadline of the
existing non-strict poll floor; at the deadline `CalibrationError` is raised
before any further write. It also checks that an explicit `off` leaves the
transfer sequence identical to the stored baseline, and that the true
default (unpinned, through `scan` and `digitize`) is that sequence plus
only the register 0x100/0x101 status reads. `tests/conftest.py` pins the
gate off for every other suite (including `tests/test_wire_baseline.py`,
whose digests are unchanged and describe the `off` sequence), because their
fakes script their own status replies and clocks:

```
python -m pytest -q tests/test_cal_wait_scanstop.py
```

The calibration buffer-empty gate suite checks the EXPERIMENTAL opt-in
`--cal-wait-bufempty enforce` (unverified on hardware): at a calibration
read-back poll whose captured word has the buffer-empty bit (0x40) clear,
only a 2-byte reply with the 0x55 acknowledge and that bit clear ends the
poll; the budget is the poll's own; at its deadline `CalibrationError` is
raised before the bulk read; image scan, strict and other status polls are
untouched; and `off` keeps the transfer sequence identical:

```
python -m pytest -q tests/test_cal_wait_bufempty.py
```

The fail-closed calibration integrity suite checks the EXPERIMENTAL
extension of `--cal-readback-guard fail` (unverified on hardware): the scan
stops with `CalibrationError` when any calibration acquisition so far is
`stale_suspect` or has identical per-channel means, checked after the dark
pair (before any dark_b substitution or default-offset fallback), after the
gain checks and after the shading verify (before POSITION), in the plain
and dual profiles; `log` and `off` keep today's behaviour and traffic:

```
python -m pytest -q tests/test_cal_integrity_fail.py
```

### 4. Compiler-based suites (need `g++`, no backend)

```
for t in test_sane_mark test_sane_ops test_sane_geometry test_sane_install; do
    .venv/bin/python tests/run_suite.py $t || exit 1
done
```

Each builds a small standalone probe (or, for `test_sane_install`, a
stand-in library) with `g++`/`cc` and exercises it: the magazine mark file and
its hardened file handling, the op programs' byte-for-byte
wire-equality against the Python driver, the geometry ledger, and the
installer's staging behaviour. Without a compiler they self-skip.

### 5. Backend suites (need a built backend — local only, not CI)

```
SANE_BACKENDS_DIR=/path/to/sane-backends \
  .venv/bin/python tests/test_sane_open_params.py
SANE_BACKENDS_DIR=/path/to/sane-backends \
  .venv/bin/python tests/test_sane_calibration_cache.py
SANE_BACKENDS_DIR=/path/to/sane-backends \
  .venv/bin/python tests/test_sane_magazine.py
```

These drive the real `sane_open → sane_control_option → sane_start` flow
through genesys's own testing mode (which cannot reach USB), so they need
`libsane-genesys.so` built from a sane-backends checkout carrying the
GL126 changes. They **auto-skip** when that build is absent, printing the
reason. They are deliberately outside CI: building sane-backends there
would add an autotools toolchain and a clone of the upstream tree for
three suites whose logic the op and geometry suites already cover on the
wire. Run them in the development clone, or against a recreated WP-3
package build (`sane/wp3-package/README.md` shows how).

## The GitHub Actions workflow

`.github/workflows/offline-checks.yml` runs on every push to master, on
every pull request and on manual dispatch. It runs check 2 (the
generator check) and then `tools/release_check.py --no-backend`, that
is every suite of checks 3 and 4 from the one list in
`tools/release_check.py`, installing only the four Python packages,
using the runner's `g++`, building no backend and touching no hardware.
A suite added to that list runs in CI without a change to the workflow.

A green CI run is LIMITED RUN PASSED: the three backend suites
(check 5) skip there, so it says less than FULL VERIFICATION PASSED
from a local run against a built backend, and neither says anything
about an installed backend or the scanner.
