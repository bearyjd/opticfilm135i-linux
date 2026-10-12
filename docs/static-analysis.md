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
| | | | | | |
