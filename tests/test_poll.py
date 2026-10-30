"""Non-strict poll acceptance and floor (of135i.device._poll_one).

Production telemetry (docs/production-telemetry-2026-10.md section 2.3):
93 % of 652 frames timed out one to seven non-strict polls, each at the
1.0 s floor, on `9c55` against a captured `ad55`/`bd55` (extended status,
wIndex 0x0122) and on the reg 0x32 polls, never followed by a failure.
This suite pins the rules that replaced that: the data-ready bit accepts
the live word where the capture had it set, the non-strict floor is
NONSTRICT_POLL_FLOOR_S, strict completion waits are unchanged, and every
non-strict poll's outcome is recorded for the sidecar.

Pure logic against a fake control endpoint: no USB, no scanner.
"""
from __future__ import annotations

import os
import sys
import time
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from of135i import device, safety  # noqa: E402
from of135i.device import NONSTRICT_POLL_FLOOR_S, Scanner  # noqa: E402
from of135i.tables import Op  # noqa: E402


class _Dev:
    """Fake pyusb device: answers ctrl_transfer from a list of hex words,
    the last one repeated forever."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def ctrl_transfer(self, bm, br, wv, wi, length, timeout=None):
        self.calls += 1
        r = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        return bytes.fromhex(r)


def _scanner(replies) -> Scanner:
    s = Scanner.__new__(Scanner)
    s.io = types.SimpleNamespace(dev=_Dev(replies))
    s._diag_poll_timeouts = 0
    s._diag_poll_timeout_details = []
    s._diag_poll_settle = []
    s._diag_poll_settle_max_s = 0.0
    s._cal_bufempty_waits = []
    s.session = types.SimpleNamespace(start_reg01="0x22", snapshot=lambda: {})
    return s


def _status_poll(want: str, wi: int = 0x0122, dur: float = 0.016) -> Op:
    return Op("poll", 0.02, bm=0xC0, br=0x04, wv=0x018E, wi=wi, length=2,
              resp=bytes.fromhex(want), dur=dur)


def _reg32_poll(want: str) -> Op:
    return Op("poll", 0.008, bm=0xC0, br=0x04, wv=0x008E, wi=0x3222, length=2,
              resp=bytes.fromhex(want), dur=0.004)


def _timed(fn):
    t0 = time.monotonic()
    fn()
    return time.monotonic() - t0


# ------------------------------------------------------------ data-ready

def test_dataenb_bit_accepts_live_word_where_capture_had_it():
    # Captured bd55 (bit 0 set); the unit reads 9c55 twice, then cd55
    # (class C, bit 0 set -- what the backend sees at this site).
    s = _scanner(["9c55", "9c55", "cd55"])
    s._poll_one(_status_poll("bd55"))
    assert s.io.dev.calls == 3
    assert s._diag_poll_timeouts == 0
    assert s._diag_poll_settle[-1]["how"] == "dataenb"
    assert s._diag_poll_settle[-1]["got"] == "cd55"


def test_dataenb_rule_needs_the_captured_bit_too():
    # Captured dc55 (bit 0 clear): a live 9d55 with bit 0 set is NOT
    # accepted on that ground, and the class differs -> runs to the floor.
    s = _scanner(["9d55"])
    elapsed = _timed(lambda: s._poll_one(_status_poll("dc55")))
    assert s._diag_poll_timeouts == 1
    assert s._diag_poll_settle[-1]["how"] == "timeout"
    assert NONSTRICT_POLL_FLOOR_S * 0.8 <= elapsed < NONSTRICT_POLL_FLOOR_S + 0.3


def test_dataenb_rule_is_for_wIndex_0x0122_only():
    s = _scanner(["9d55"])
    s._poll_one(_status_poll("bd55", wi=0x0022))
    assert s._diag_poll_timeouts == 1


# ------------------------------------------------------------ the floor

def test_nonstrict_floor_is_short_and_recorded():
    # The production case: 9c55 for ever against bd55.
    s = _scanner(["9c55"])
    elapsed = _timed(lambda: s._poll_one(_status_poll("bd55")))
    assert s._diag_poll_timeouts == 1
    assert s._diag_poll_timeout_details == [
        {"wv": "0x018e", "wi": "0x0122", "last": "9c55", "want": "bd55"}]
    assert NONSTRICT_POLL_FLOOR_S * 0.8 <= elapsed < NONSTRICT_POLL_FLOOR_S + 0.3
    assert elapsed < 1.0, "the old 1.0 s floor is gone for non-strict polls"
    e = s._diag_poll_settle[-1]
    assert e["how"] == "timeout" and abs(e["s"] - elapsed) < 0.05
    assert s._diag_poll_settle_max_s >= NONSTRICT_POLL_FLOOR_S * 0.8


def test_captured_duration_still_scales_the_budget():
    # 3x a long captured duration beats the floor.
    s = _scanner(["9c55"])
    elapsed = _timed(lambda: s._poll_one(_status_poll("bd55", dur=0.2)))
    assert 0.55 <= elapsed < 0.9


def test_strict_floor_unchanged_at_one_second():
    s = _scanner(["9c55"])
    t0 = time.monotonic()
    try:
        s._poll_one(_status_poll("e855"), strict=True)
    except safety.StrictPollTimeoutError:
        pass
    else:
        raise AssertionError("strict poll must raise on timeout")
    assert time.monotonic() - t0 >= 1.0
    assert s._diag_poll_timeouts == 1
    assert s._diag_poll_settle == [], "strict polls are not recorded as settle entries"


# ------------------------------------------------------------ existing rules keep working

def test_exact_match_on_first_read_is_recorded_as_exact():
    s = _scanner(["bd55"])
    s._poll_one(_status_poll("bd55"))
    assert s.io.dev.calls == 1
    e = s._diag_poll_settle[-1]
    assert e["how"] == "exact" and e["s"] < 0.05


def test_state_class_match_is_recorded_as_class():
    s = _scanner(["d455"])
    s._poll_one(_status_poll("dc55"))
    assert s._diag_poll_timeouts == 0
    assert s._diag_poll_settle[-1]["how"] == "class"


def test_reg32_mask_is_recorded_as_reg32():
    s = _scanner(["8d55"])          # 0x95 with bit 3 cleared
    s._poll_one(_reg32_poll("9555"))
    assert s._diag_poll_timeouts == 0
    assert s._diag_poll_settle[-1]["how"] == "reg32"


def test_settle_list_is_capped_per_scan():
    s = _scanner(["bd55"])
    for _ in range(device._POLL_SETTLE_CAP + 10):
        s._poll_one(_status_poll("bd55"))
    assert len(s._diag_poll_settle) == device._POLL_SETTLE_CAP


if __name__ == "__main__":
    import inspect

    fns = [f for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for f in fns:
        f()
    print(f"{len(fns)} tests passed")
