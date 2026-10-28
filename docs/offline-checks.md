# Offline checks — the reproducible, hardware-free regression gate

Every check here runs without a scanner and without network access. It
is the reproducibility answer the project's updated course asks for: a
documented set of checks, their dependencies, their commands and their
expected results, that any commit can be held to. The substantiated
mechanism is the local commands below. A GitHub Actions workflow file is
also provided (`.github/workflows/offline-checks.yml`) but has not been
activated and no CI run has occurred -- see "The GitHub Actions workflow"
at the end.

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

Substantiated: a full local run on 2026-09-15, against a built backend,
reported **FULL VERIFICATION PASSED, 338 tests, 0 skipped** (a later run the same day, after the parser fix and the cold-start change). Run it where the backend is
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
for t in test_safety test_calibrate test_hwblock test_park test_offline \
         test_diag test_dpi test_ir test_image_probe test_aperture_crop \
         test_overscan test_dual_overscan; do
    .venv/bin/python tests/$t.py || exit 1
done
```

These cover the safety model, calibration maths, the park state machine,
the image path, the DPI and IR logic, the overscan/coverage geometry and
the bulk-digitisation workflow — the driver's own logic, all without
hardware.

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

The transfer recorder suite checks the opt-in `OF135I_TRACE=<path>`
recorder: one record per control/bulk transfer (timestamp, direction,
setup fields, status, first 64 data bytes of control and bulk-OUT
transfers, only the length of bulk-IN data), complete lines flushed per
record, file mode 0600, an existing file or symlink refused, any
recording failure logs one warning and never touches scanner I/O, and
with the variable set or unset the wire fingerprints equal the stored
baseline. `tools/trace_to_compiled.py TRACE.jsonl` converts a recording
to the compiled `*.trace.json[.gz]` op format:

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

The calibration stop-gate suite checks the EXPERIMENTAL opt-in
`--cal-wait-scanstop enforce` (unverified on hardware): after the SCAN-clear
that closes a calibration execute pulse, and before the next OUT transfer,
register 0x100 bit 0 and then register 0x101 bit 0 must read clear in
2-byte replies with the 0x55 acknowledge, under one shared deadline of the
existing non-strict poll floor; at the deadline `CalibrationError` is raised
before any further write. It also checks that the default `off` leaves the
transfer sequence identical to the stored baseline:

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

### 4. Compiler-based suites (need `g++`, no backend)

```
for t in test_sane_mark test_sane_ops test_sane_geometry test_sane_install; do
    .venv/bin/python tests/$t.py || exit 1
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

## The GitHub Actions workflow (provided, not yet activated)

A workflow file, `.github/workflows/offline-checks.yml`, is provided to
run checks 2-4 (the generator check, the twelve Python suites and the four
compiler-based suites) on push and pull request, installing only the four
Python packages, using the distribution's `g++`, building no backend and
touching no hardware.

It has **not been activated, and no CI run has occurred.** Publishing a
workflow file to this repository needs a token carrying the `workflow`
scope, which is the owner's to grant; until then the file sits ready and
the substantiated mechanism is the local commands above. **No green CI is
claimed.** Once enabled, the workflow would cover the driver's logic, the
generated tables and the backend's wire-level and lock behaviour, but
never an installed backend (check 5) or the scanner.
