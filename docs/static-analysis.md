# Static analysis of the vendor software (interoperability)

## Purpose

The standalone Python driver is built from our own USB captures. Where a
capture cannot answer a question (why a register is written, what condition
a wait is really for, what order a step depends on), the vendor's Windows
software may be analysed statically, for interoperability only, to find the
answer. This document is the policy for how such knowledge is recorded and
kept apart from the SANE backend.

## Rules

- Findings are facts only: register values, conditions, orderings. No
  vendor code excerpts, ever, and no code structure is copied.
- Each finding gets an ID (SA-001, SA-002, ...) and a row in the table below.
- Findings that become driver values live in `of135i/vendor_derived.py`,
  each with a comment pointing to its ID here.
- Every finding is a hypothesis until confirmed on hardware.
- `of135i/vendor_derived.py` must never be imported, directly or
  transitively, by `tools/gen_sane_tables.py`, and nothing under `sane/`
  may reference it. `tests/test_vendor_isolation.py` enforces this.

## Promotion

1. Static finding: recorded in the table below, status `hypothesis`.
2. Hypothesis in `of135i/vendor_derived.py`, used by the standalone driver
   only.
3. Own USB capture plus a hardware test, logged in `docs/test-log.md`.
4. Confirmed: the value moves into the normal capture-derived module, with
   the capture as its source; the SANE tables are regenerated; the table
   row is marked `confirmed` and names the test. A refuted finding is marked
   `refuted` and removed from `vendor_derived.py`.

## Findings

| ID | Question | Finding | Source (binary + function/address) | Status | Test |
|---|---|---|---|---|---|
| SA-001 | What does the vendor wait for after the cold-start homing round (our settle check wants reg 0x32 = 0x1f, always reads 0x1d; Test 79)? | Nothing: reg 0x32 bit 0x02 is a host-driven control output. The static finding pointed back to our own captures, which show the vendor SETTING the bit (0x1d -> 0x1f) between move 2 and move 3 of every round; our hand-written cold round omits that write. | Scanapi_x64.dll 1.4.12 (vendor driver DLL), 0x180086250; confirmed in our captures `01-init` (2026-08-29) and the 2026-09-07 latched-magazine start | confirmed in captures; driver fix pending hardware | |
| SA-002 | What distinguishes a button press from a magazine insert after the bare 0x48 interrupt on EP 0x83 (Test 81)? | The interrupt only says "look"; the register state tells the events apart (reg 0x101 latched vs loose; reg 0x31 bits 0x48 as a candidate insert marker). | Scanapi_x64.dll 1.4.12 (vendor driver DLL), 0x180137760 (event thread) + our Test 81 captures | hypothesis | |

## SA-001 -- state after the cold-start homing round

Question: what does the vendor wait for after the cold-start homing round?
Our settle check wants reg 0x32 = 0x1f but always reads 0x1d (Test 79).

Static finding: the driver's per-model configuration names reg 0x32 bit
0x02 as the loader "home" line with polarity 0. The DLL's routine for it
(0x180086250) reads reg 0x32, sets or clears bit 0x02 according to a
parameter and the polarity, and writes the value back. The bit is a
host-driven control output, not a status the hardware sets. No comparison
against 0x1f (reg 0x32) or 0xbb (reg 0x35) as a wait condition exists
anywhere in the DLL.

What the static finding led to in our own captures (the evidence): in both
vendor starts we have on record -- `01-init` (2026-08-29) and the
2026-09-07 start with a latched magazine -- every homing round does, between
move 2 and move 3, a read of reg 0x32 (0x1d) followed by a write of 0x1f:
the vendor sets bit 0x02 again before the eject move. The capture-replayed
JOG table (`of135i/tables_load.py`) contains this write; the hand-written
`_cold_homing_round()` does not. Test 79's "transcription slip" is therefore
a write that was dropped, not a wrong condition. The reg 0x35/0x32/0x32
reads ~200 ms apart after the round are the vendor's idle event polling,
which continues indefinitely; the vendor does not wait there.

Status: the fact is capture-evidenced, so it does not live in
`vendor_derived.py` and may go to both implementations. The driver fix (add
the read-modify-write before move 3; the SANE program follows through the
generator) changes a motor sequence and needs a cold-start hardware run
before it counts as done.

## SA-002 -- button press versus magazine insert

Question: what distinguishes a button press from a magazine insert after
the bare 0x48 interrupt on EP 0x83 (Test 81)?

Static findings (vendor DLL):

- The DLL picks per-chip register helpers by the USB bcdDevice (0x0702 on
  this unit).
- Its event thread (0x180137760) reads EP 0x83 with a 50 ms timeout and,
  for PID 0x1436 only, additionally polls reg 0x35 bit 0x40 and reg 0x32
  bit 0x40. Its loader block reads reg 0x31 (bit 0x08), reg 0x100 (bit 0x02)
  and reg 0x101 (bit 0x08 = magazine present, the bit we already use).

Cross-check against our own Test 81 captures (capture evidence):

- Idle polling reads 0x31/0x32/0x100/0x101 about every 270 ms.
- Reg 0x101 = 0xf8 with the magazine latched (after LOAD), 0xd8 present but
  loose (after eject); bit 0x01 is set while the motor runs (d9/f9/d1/f1).
- Press events happened with 0x101 = 0xf8 (latched) and were followed by an
  eject; insert events happened with 0x101 = 0xd8 and were followed by LOAD
  (reg 0x0f = 0x01).
- At both inserts reg 0x31 went 0xfe -> 0xb6 (bits 0x48 cleared, the same
  bits as the interrupt byte), n = 2.
- One of five events (37.9 s in the three-cycle capture) is unexplained
  (loose magazine, 0x31 stayed 0xfe).

Hypothesis: the interrupt only says "look"; the state tells the events
apart. Latched (0x101 & 0x20) means a press = eject request; loose and
present (0x101 = 0xd8) means an insert = load. Reg 0x31 bits 0x48 are a
candidate direct insert marker.

Test: a read-only watcher run (`tools/watch_events.py`), to be logged as a
test-log entry later. Nothing has been run yet. Constants:
`REG101_LATCHED_BIT`, `REG101_PRESENT_BIT`, `REG101_MOTOR_BIT`,
`REG31_INSERT_MARK_MASK`, `WATCH_REGS` in `of135i/vendor_derived.py`.
