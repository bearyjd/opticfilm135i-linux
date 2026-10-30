# Capture replay harness

Offline tool that plays the DEVICE side of a recorded USB capture into the
driver and diffs what the driver sends against what the recorded host sent.
While a replay runs, the driver is given only the harness's own fake device
object; the guard described under "Guarantees and limits of the guard"
makes the usual ways of reaching real hardware raise.

The harness reads a capture path you give it at run time. No capture,
register table or recorded value ships with it; the tests use small
hand-written synthetic captures only. Keep your own captures out of the
repository.

## Usage

    python3 tools/replay_harness.py CAPTURE.jsonl --op scan --dpi 600 --out OUTDIR

`--op` is one of `cold_init`, `initialize`, `vendor_startup`, `load_magazine`,
`scan` (initialize, then a scan: calibration phases first). `OUTDIR` must not
exist. It receives:

- `diff.txt`: `ORDER` (ops only on one side) and `VALUE` (register value,
  bulk-OUT content) lines, then a summary line. Register level only.
- `summary.json`: whether the run halted, the exception type and message,
  transfer count and divergence counts by kind. A halt is also the final
  line of `diff.txt` (`# HALTED Type: message`).
- `divergences.jsonl`: every point where the driver asked something the
  recording could not answer (see below).
- `trace.jsonl`: the driver's own `OF135I_TRACE` recorder output
  (`of135i-trace/1`), and `compiled.trace.json`, the same converted with
  `tools/trace_to_compiled.py`, for an external annotated-trace diff tool
  if you have one.

Exit code: 0 for a complete run and for a documented measurement-check halt
(`EXPECTED_HALT_TYPES` in `tools/replay_harness.py`: `LampWarmupError`,
`CalibrationError`; the replay serves zeros for image data, so these are the
normal end of a `scan`). Any other halt exits 1. A malformed capture row
makes loading fail with a `ValueError` naming the row (line number) before
anything is run.

The exit summary prints driver transfer count, divergence counts by kind and
`halted` (the first driver exception, if any; the run ends there).

## Capture format

One JSON object per USB transfer, as written by a usbmon decoder:
`endpoint`, `setup` = `[bmRequestType, bRequest, wValue, wIndex, wLength]`
(control only), `payload_hex`, `payload_length`, `payload_sha256` (bulk OUT),
`returned` (bulk IN), `status`, `submit_packet`. Standard/class requests and
failed control transfers are dropped; empty interrupt reads (idle timeouts)
are dropped.

## Guarantees and limits of the guard

While `run()` executes, `usb.core.find`, `usb.core.Device.__init__` and
`usb.backend.libusb1.get_backend` are replaced by functions that raise
`AssertionError`; they are restored afterwards. `run()` also refuses (with a
`TypeError`) to drive anything but `FakeVendorDevice`: the device is checked
before the operation starts and again when it ends, and a swapped-in object
is replaced by the fake before the session closes so it is never disposed.

Not covered:

- Import time is not guarded. `tools/replay_harness.py` imports the driver
  (and `usb.core`) before the guard is installed. This is safe because the
  driver does only `import usb.core` at module level: it calls no find or
  open at import time, and the libusb backend is loaded lazily.
- Transfers through a device object that a caller obtained elsewhere and
  passes to the driver by other means are not intercepted; the guard blocks
  discovery and construction, not every possible use of a handle that
  already exists.
- The check on the fake is a type check at the two points above, not a
  per-transfer check.

## How replies are matched

- A cursor walks the recorded events. A driver control read gets the next
  recorded reply with the same `(bmRequestType, bRequest, wValue, wIndex)` at
  or after the cursor, but never past the next recorded OUT transfer (state
  change). The cursor moves past the reply used.
- A driver control/bulk OUT is matched forward, skipping at most `--window`
  recorded OUT events. Same data: silent. Same request, other data:
  `write_data_differs`. No match: `write_unmatched` / `bulk_out_unmatched`.
  Bulk-OUT content is compared by length and SHA-256.
- A read with no recorded reply gets the last value served for that request,
  else an idle value (the start-state register reads `--start-reg01`, default
  idle-homed; anything else zeros with the ack byte), and logs
  `read_unanswered`.
- Bulk IN is never invented: zeros of the requested size, logged as
  `bulk_in_zeros`. Interrupt reads replay recorded events, else time out.

`time.sleep`, `time.monotonic` and `time.perf_counter` run on a virtual clock
for the duration of the run, so poll loops reach their deadlines instantly.

## Limits (what a replay cannot show)

- Timing: the clock is virtual; trace timestamps and poll windows mean
  nothing. Do not read timing differences from the diff.
- Motor physics and sensors: a status reply is what the vendor saw at that
  point of its own sequence. If the driver diverges, later replies no longer
  correspond to the state the driver caused.
- Image data: all bulk-IN data is zeros, so any step that inspects a
  measurement (white/dark level, lamp warm-up) fails or takes its failure
  path. A `scan` replay therefore normally halts at the first measurement
  check; the transfers before it are still compared.
- The matcher is heuristic (forward windows, per-request order). After the
  first divergence, later ones may be consequences. Read the first ones.
- The built-in diff is register-level and unnamed. Per-operation grouping,
  field names and timing need an external annotation tool run on
  `compiled.trace.json`; none is included.
