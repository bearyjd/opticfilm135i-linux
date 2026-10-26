# Experimental vendor startup

`Scanner.vendor_startup(experimental=True)` and the isolated
`of135i vendor-startup --experimental` command implement the observed vendor
application startup: mode-1c traverse 71490, followed by mode-18 JOG moves
6690, 6690 and 3090. The implementation has **offline verification only**.
Hardware activation needs a separately approved, bounded trial after VM USB
ownership has been handed back. No hardware trial accompanied this change.

The opt-in acknowledges the experimental operation; it is not proof of a reset
or physical holder position. The entry gate requires a fresh writing session,
an exact live two-byte register 01 reply 2255, and an exact two-byte F055
status reply, including ACK55, before the first vendor OUT. The normal open
path verifies register01 before USB configuration; startup reads it again
before the F055 gate, even if the session already cached an ARMED verdict.
Both live gates precede vendor/register writes. Prior operations,
prior write attempts, read-only, COLD00, FAILED, REFUSED and unknown states are
refused without startup writes. A COLD power-on state remains unsupported by
this operation: the capture did not record register 01 before its first write.

The sequence preserves all 63 captured control/bulk writes through motor
disable, including eight identical 512-byte loader slopes, the full first JOG
profile, register31 toggles before traverse, and extended-register configuration
114/115=80 (`40/04/0183/0000`, payload `14801580`). The two EEPROM selectors
`00000100` then `40000100` each precede ACK and a 64-byte read at
`c0/04/008a/000f`. These select read windows; they are not established EEPROM
programming commands. EEPROM response contents are neither retained nor used.
Before the traverse GO, register32 must match the captured 1F before writing
1D to clear bit02; status must be exact D055 (including presence bit08 clear),
and **two fresh successful register100 reads must each have bit02 clear**.
The register100 checks are vendor branch predicates established by DLL analysis
and correlated with capture packets7535/7537. Captured read/modify/write inputs
for registers31/32/35 are gated before their dependent writes. Other register
values may vary; every control reply must have the proper length and ACK.
Read-only diagnostic register sweeps and background interrupt monitoring are
omitted. Named phases use the existing guarded executor for writes and validate
protocol reads with the same pacing rules, without changing normal execution.

After every GO the operation must observe one of that move's captured busy
words before accepting its exact completion: D855 for traverse, F855 for each
JOG. This prevents a stale F855 from completing a subsequent move. The traverse
deadline is 21 seconds (rounded up from three times the measured 6.8873s);
JOG deadlines are three times their measured 1.6047/1.6047/0.8890s. Status
reads use a USB timeout bounded by the remaining phase budget. The deadline
is checked before each read and after its reply; completion at or after the
deadline is refused. Status class leniency is not used. Timeout, malformed completion status, USB errors,
interruptions and short writes terminate the session; no recovery or later
motor writes follow.

“Startup sequence complete” reports protocol completion only. It establishes
neither physical release nor a loaded holder. The command stops there and
does not scan, load, initialize or fall back to another operation. Existing
initialize, cold-init, eject, home and default load paths are unchanged.

The sanitized fixture `tests/fixtures/vendor_startup_writes.json` was frozen
directly from scanner-only transactions in a capture whose SHA256 is
`dc6f2ca6b69c88f445f5e07805ad2fd48c12c1e8e601066a91fe72754cf8c4eb`.
It contains protocol writes and slope hashes, without raw PCAP, EEPROM
responses, image payloads or other-device traffic. Tests need no private
capture or vendor binary and never open a physical USB device.

Run the offline startup regression suite with:

```
python -m pytest -q tests/test_vendor_startup.py
```

The offline CI workflow runs this suite separately from the existing
standalone and compiler checks. Passing it establishes protocol behavior
against synthetic USB responses and the sanitized fixture, not physical
scanner behavior.
