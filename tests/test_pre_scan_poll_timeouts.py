"""Poll timeouts logged while a frame is PREPARED reach its sidecar.

Real logs show two "poll ... timed out after 0.3s ... -- continuing" warnings before the first
frame while the sidecar says ``poll_timeouts: 0``: the counter was zeroed when the scan began.
The scan's own count keeps its meaning; the earlier ones get ``pre_scan_poll_timeouts``.

Fake USB device only (tests/test_safety.py).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from test_safety import FakeUsbDevice, _cal_buffers, fast_time, make_scanner  # noqa: E402

DETAIL = {"wv": "0x018e", "wi": "0x0122", "last": "d855", "want": "f855"}


def _prepared(stale: int):
    scanner = make_scanner(FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers()))
    scanner._diag_poll_timeouts = stale  # left over from an earlier operation
    scanner.initialize()
    return scanner


def test_initialize_starts_its_own_count():
    with fast_time():
        assert _prepared(7)._diag_poll_timeouts == _prepared(0)._diag_poll_timeouts


def test_timeouts_before_the_scan_are_reported_separately():
    with fast_time():
        scanner = _prepared(0)
        own = scanner._diag_poll_timeouts
        scanner._diag_poll_timeouts += 2  # two polls time out during preparation
        scanner._diag_poll_timeout_details.extend([dict(DETAIL), dict(DETAIL)])
        scanner.scan(frame=1)
    diag = scanner.last_diag
    assert diag["pre_scan_poll_timeouts"] == own + 2
    assert diag["pre_scan_poll_timeout_details"][-2:] == [DETAIL, DETAIL]
    # the scan's own counter keeps its meaning: what timed out during the scan
    assert DETAIL not in diag["poll_timeout_details"]
    assert diag["poll_timeouts"] == len(diag["poll_timeout_details"])


def test_dual_scan_reports_them_too():
    with fast_time():
        scanner = make_scanner(FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers()))
        scanner.initialize(ir=True)
        own = scanner._diag_poll_timeouts
        scanner._diag_poll_timeouts += 1
        scanner.scan(frame=1, ir=True)
    assert scanner.last_diag["pre_scan_poll_timeouts"] == own + 1


def test_a_second_frame_does_not_inherit_the_first_frames_count():
    with fast_time():
        scanner = _prepared(0)
        scanner._diag_poll_timeouts += 3
        scanner.scan(frame=1)
        scanner.initialize()
        own = scanner._diag_poll_timeouts
        scanner.scan(frame=2)
    assert scanner.last_diag["pre_scan_poll_timeouts"] == own


if __name__ == "__main__":
    ran = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} OK")
            ran += 1
    print(f"{ran} tests passed")


def test_diag_reset_moves_the_count_aside_and_clears_the_rest():
    scanner = make_scanner(FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers()))
    scanner._diag_poll_timeouts = 4
    scanner._diag_poll_timeout_details = [dict(DETAIL)]
    scanner._diag_cr_mismatches = 2
    scanner._diag_phase_seconds = {"park": 1.0}
    scanner._diag_warmup = {"attempts": 1}
    scanner._diag_reset()
    assert scanner._diag_pre_scan_poll_timeouts == 4
    assert scanner._diag_pre_scan_poll_timeout_details == [DETAIL]
    assert scanner._diag_poll_timeouts == 0 and scanner._diag_poll_timeout_details == []
    assert scanner._diag_cr_mismatches == 0 and scanner._diag_phase_seconds == {}
    assert scanner._diag_warmup is None and scanner._diag_park_waits is None


def test_whole_strip_sweep_reports_them_too():
    with fast_time():
        scanner = make_scanner(FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers()))
        scanner.initialize(ir=True)
        own = scanner._diag_poll_timeouts
        scanner._diag_poll_timeouts += 2
        scanner._diag_poll_timeout_details.extend([dict(DETAIL), dict(DETAIL)])
        scanner.sweep(1, 2, dpi=600, sink=lambda _chunk: None)
    assert scanner.last_diag["pre_scan_poll_timeouts"] == own + 2
    assert scanner.last_diag["pre_scan_poll_timeout_details"][-2:] == [DETAIL, DETAIL]


def test_plain_sweep_reports_them_too():
    # sweep(ir=False) is withdrawn (Test 112), so drive _sweep_plain directly
    # over the fake bus, as _scan_plain's own test does through scan().
    with fast_time():
        scanner = _prepared(0)
        own = scanner._diag_poll_timeouts
        scanner._diag_poll_timeouts += 1
        with scanner._operation("sweep"):
            scanner._sweep_plain(1, 2, overscan_mm=0.0, sink=lambda _chunk: None)
    assert scanner.last_diag["pre_scan_poll_timeouts"] == own + 1
