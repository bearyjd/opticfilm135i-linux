"""Offline eject-outcome regressions over the real guarded transport."""
import pytest
from test_safety import FakeUsbDevice, make_scanner, cli_over, _cli, fast_time


def test_stuck_eject_is_failure_at_cli():
    fake = FakeUsbDevice(reg01=0x22)
    fake.status_high = lambda f: 0xf9 if f.pulses else None
    with cli_over(fake):
        code, stdout, stderr = _cli(["eject"])
    assert code == 1
    assert "ejected" not in stdout
    assert fake.pulses == 1
    assert fake.out_log[-1]["data"] == b"\x09\x00"


def test_clear_sensor_is_never_reported_as_ejected():
    fake = FakeUsbDevice(reg01=0x22, magazine=False)
    with cli_over(fake):
        code, stdout, stderr = _cli(["eject"])
    assert code == 0
    assert "ejected" not in stdout
    assert fake.out_count == 0


def test_busy_then_idle_eject_reports_completed(monkeypatch):
    import time
    ticks = iter(i * 0.001 for i in range(10000))
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(time, "sleep", lambda _: None)
    fake = FakeUsbDevice(reg01=0x22)
    words = iter((0xd9, 0xf9, 0xf8))
    fake.status_high = lambda f: next(words) if f.pulses else None
    scanner = make_scanner(fake)
    assert scanner.eject() == "completed"
    assert fake.pulses == 1
    assert fake.out_log[-1]["data"] == b"\x09\x00"
    assert scanner.last_eject["completion"]["busy_seen"] is True


@pytest.mark.parametrize("outcome", [None, "unexpected"])
def test_unknown_eject_outcome_is_not_reported_as_completed(capsys, outcome):
    from of135i import cli
    cli._report_eject(outcome)
    captured = capsys.readouterr()
    assert "ejected" not in captured.out
    assert "status word read idle" not in captured.out


def test_film110_abort_with_skip_does_not_claim_strip_was_ejected(tmp_path):
    from test_film110_digitize import (_content, _run, _holder_apertures,
                                      _starts_for, _strip_spec, _starts)
    with _content():
        rc, *_ = _run(tmp_path, _holder_apertures(_starts_for(3.5), None))
        assert rc == 0
        other = _strip_spec(_starts(3.5 + 6.4, 4), "lo", ids=[5, 6, 7, 8])
        rc, record, scans, stdout, stderr = _run(
            tmp_path, _holder_apertures(other, None),
            eject_outcome="skipped-no-magazine")
    # Current film110 behavior preserves unnumbered products as a valid
    # pass even when numbering could not be established (exit 5).
    assert rc == 5 and record["status"] == "ok"
    assert record["numbering"] == "unidentified"
    assert "UNNUMBERED" in stderr
    assert "The strip was ejected" not in stderr
    assert "eject NOT run" in stderr


def test_digitize_manifest_records_eject_failure_stage(tmp_path):
    from of135i import digitize, safety
    from test_film110_digitize import _content, _run, _holder_apertures, _starts_for
    with _content():
        with pytest.raises(safety.SafetyError, match="simulated eject failure"):
            _run(tmp_path, _holder_apertures(_starts_for(3.5), None),
                 eject_error=safety.SafetyError("simulated eject failure"))
    record = digitize.read_manifest(tmp_path)[-1]
    assert record["status"] == "failed"
    assert record["stage"] == "eject"
    assert record["per_frame"]


@pytest.mark.parametrize("failure_kind", ["usb", "interrupt"])
def test_failed_second_eject_clears_previous_completed_diagnostic(failure_kind):
    import usb.core
    from of135i import safety
    fake = FakeUsbDevice(reg01=0x22)
    scanner = make_scanner(fake)
    with fast_time():
        assert scanner.eject() == "completed"
    previous = scanner.last_eject
    assert previous["outcome"] == "completed"
    failure = usb.core.USBError("sensor read failed") if failure_kind == "usb" else KeyboardInterrupt()
    fake.fault = lambda event: failure if (event["kind"] == "ctrl_in"
                                          and event["wv"] == 0x018e
                                          and event["wi"] == 0x0122) else None
    before = (fake.out_count, fake.pulses)
    with fast_time(), pytest.raises(type(failure)):
        scanner.eject()
    assert scanner.session.state is safety.SessionState.FAILED
    assert (fake.out_count, fake.pulses) == before
    assert scanner.last_eject is not previous
    assert scanner.last_eject["outcome"] is None
    assert scanner.last_eject["magazine_sensor"] is None
    assert scanner.last_eject["completion"] is None
    before = (fake.out_count, fake.in_count)
    with pytest.raises(safety.SessionFailedError):
        scanner.eject()
    assert (fake.out_count, fake.in_count) == before
