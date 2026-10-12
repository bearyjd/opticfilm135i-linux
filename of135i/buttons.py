"""Front-panel button codes (measured, Test 97 in docs/test-log.md).

Our own measurement, not vendor-derived (so not in vendor_derived.py).
Register 0x31 idles at 0xfe; a press clears bits of mask 0x68 for about
0.3-0.9 s. The cleared bits are the button code: Eject 0x48, Positive
0x20, Negative 0x40, Custom 0x28. Once the scanner is initialised
(reg 0x01 = 0x22) each press also delivers one byte on EP 0x83 equal to
the same code. A fifth button (Power) is untested.
"""

from __future__ import annotations

BUTTON_MASK = 0x68

BUTTON_CODES = {
    0x48: "eject",
    0x20: "positive",
    0x40: "negative",
    0x28: "custom",
}


def decode_interrupt(byte: int) -> str:
    """Name of the button for an EP 0x83 interrupt byte."""
    return BUTTON_CODES.get(byte, f"unknown-0x{byte:02x}")


def decode_reg31(value: int) -> str | None:
    """Name of the button currently held according to reg 0x31, or None
    when idle (no bit of 0x68 cleared)."""
    code = ~value & BUTTON_MASK
    if code == 0:
        return None
    return decode_interrupt(code)
