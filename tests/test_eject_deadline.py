"""Focused eject deadline regressions with an independently driven clock."""
import time
import pytest
import usb.core
from of135i import safety
from test_safety import FakeUsbDevice, make_scanner


@pytest.mark.parametrize("reply_time", [10.0, 11.0])
def test_eject_cannot_confirm_at_or_after_deadline(monkeypatch, reply_time):
    clock = [0.0]
    timeouts = []
    class TimedEject(FakeUsbDevice):
        def ctrl_transfer(self, bm, br, wv=0, wi=0, data_or_wLength=None, timeout=None):
            result = super().ctrl_transfer(bm, br, wv, wi, data_or_wLength, timeout)
            if bm == 0xc0 and wv == 0x018e and wi == 0x0122 and self.pulses:
                timeouts.append(timeout)
                clock[0] = reply_time
                return b"\xf8\x55"
            return result
    fake = TimedEject(reg01=0x22)
    scanner = make_scanner(fake)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda _: None)
    with pytest.raises(safety.SafetyError):
        scanner.eject()
    assert scanner.session.state is safety.SessionState.FAILED
    assert scanner.last_eject["outcome"] is None
    assert scanner.last_eject["completion"]["timed_out"] is True
    assert timeouts and all(isinstance(t, int) and 0 < t <= 10000 for t in timeouts)
    assert fake.pulses == 1
    assert fake.out_log[-1]["data"] == b"\x09\x00"


def test_eject_deadline_exhausted_before_next_read(monkeypatch):
    clock = [0.0]
    timeouts = []
    class TimedEject(FakeUsbDevice):
        def ctrl_transfer(self, bm, br, wv=0, wi=0, data_or_wLength=None, timeout=None):
            if bm == 0xc0 and wv == 0x018e and wi == 0x0122 and self.pulses:
                assert not timeouts, "read started after the deadline"
                timeouts.append(timeout)
                clock[0] = 0.1
                return b"\xf9\x55"
            return super().ctrl_transfer(bm, br, wv, wi, data_or_wLength, timeout)
    fake = TimedEject(reg01=0x22)
    scanner = make_scanner(fake)
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(time, "sleep", lambda _: clock.__setitem__(0, 11.0))
    with pytest.raises(safety.EjectIncompleteError):
        scanner.eject()
    assert len(timeouts) == 1
    assert isinstance(timeouts[0], int) and 0 < timeouts[0] <= 10000
    assert scanner.session.state is safety.SessionState.FAILED
    assert scanner.last_eject["completion"]["reads"] == 1
    assert fake.pulses == 1
    assert fake.out_log[-1]["data"] == b"\x09\x00"


@pytest.mark.parametrize("failure", [usb.core.USBTimeoutError("eject status timeout"), KeyboardInterrupt()])
def test_eject_status_exception_propagates_without_later_write(monkeypatch, failure):
    monkeypatch.setattr(time, "monotonic", lambda: 0.0)
    monkeypatch.setattr(time, "sleep", lambda _: None)
    class FailedStatus(FakeUsbDevice):
        def ctrl_transfer(self, bm, br, wv=0, wi=0, data_or_wLength=None, timeout=None):
            if bm == 0xc0 and wv == 0x018e and wi == 0x0122 and self.pulses:
                assert isinstance(timeout, int) and 0 < timeout <= 10000
                raise failure
            return super().ctrl_transfer(bm, br, wv, wi, data_or_wLength, timeout)
    fake = FailedStatus(reg01=0x22)
    scanner = make_scanner(fake)
    with pytest.raises(type(failure)):
        scanner.eject()
    assert scanner.session.state is safety.SessionState.FAILED
    assert scanner.last_eject["outcome"] is None
    assert fake.out_log[-1]["data"] == b"\x0f\x01"
    before = (fake.out_count, fake.in_count)
    with pytest.raises(safety.SessionFailedError):
        scanner.eject()
    assert (fake.out_count, fake.in_count) == before
