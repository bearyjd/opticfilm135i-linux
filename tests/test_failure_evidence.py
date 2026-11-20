"""A failed scan leaves evidence: what the USB error was, how far the scan got, which frames
of a multi-frame run finished -- in the error text and in a ``.fail.json`` record.

Fake USB device only (tests/test_safety.py); nothing here opens a real device.
"""
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

import usb.core

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from of135i import cli, diag  # noqa: E402
from test_safety import (FakeUsbDevice, SessionState, _assert_power_cycle_message,  # noqa: E402
                         _cal_buffers, _cli, _usb_timeout, cli_over, patched)

BASE = ["--cal-wait-scanstop", "off"]


def _gone() -> usb.core.USBError:
    return usb.core.USBError("No such device (it may have been disconnected)", errno=19)


def _fail_in_scan_of_frame(fake, opened, finished, frame, make):
    """Arm ``fake`` to raise ``make()`` on an image read of ``frame`` (1-based order)."""
    def fault(ev):
        if (ev["kind"] == "bulk_in" and opened and opened[-1].session.phase == "scan"
                and len(finished) == frame - 1):
            return make()
        return None
    fake.fault = fault


def _run(argv, frame, make):
    """Run the CLI over a fake that fails in the scan pass of the ``frame``-th frame."""
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    finished = []
    with cli_over(fake) as opened, \
            patched(cli, "_finish_plain_scan", lambda *a, **k: finished.append(1)):
        _fail_in_scan_of_frame(fake, opened, finished, frame, make)
        code, so, se = _cli(argv)
    return code, so, se, opened[0], fake


def test_classify_usb_error_tells_a_lost_device_from_a_stall():
    gone = diag.classify_usb_error(_gone())
    assert gone["kind"] == "device_gone" and gone["errno"] == 19
    assert "bus" in gone["meaning"]
    stall = diag.classify_usb_error(_usb_timeout())
    assert stall["kind"] == "timeout" and stall["errno"] == 110
    assert diag.classify_usb_error(usb.core.USBError("Pipe error", errno=32))["kind"] == "pipe"
    assert diag.classify_usb_error(usb.core.USBError("I/O", errno=5))["kind"] == "io_error"
    assert diag.classify_usb_error(usb.core.USBError("odd", errno=71))["kind"] == "usb_error"
    assert diag.classify_usb_error(usb.core.USBError("no errno"))["errno"] is None
    assert diag.classify_usb_error(ValueError("not usb")) is None


def test_failure_path_sits_beside_the_frame():
    assert diag.failure_path("dir/x.tiff") == str(Path("dir/x.fail.json"))


def test_second_frame_failure_names_the_finished_frame_and_leaves_a_record():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "tray.tiff")
        code, so, se, scanner, fake = _run(
            ["scan", "--frames", "1-2", "-o", out, *BASE], 2, _gone)
        assert code == 1, (code, se)
        assert scanner.session.state is SessionState.FAILED
        assert "frame 2 failed" in se and "finished before it: 1" in se, se
        assert "device_gone" in se and "errno 19" in se, se
        _assert_power_cycle_message(se)
        record = json.loads(Path(diag.failure_path(cli._frame_output(out, 2))).read_text())
        assert record["frame"] == 2 and record["frames_finished"] == [1]
        assert record["frames_requested"] == [1, 2]
        assert record["usb"]["kind"] == "device_gone" and record["usb"]["errno"] == 19
        assert record["session"]["state"] == "failed"
        assert record["session"]["failure"]["phase"] == "scan"
        progress = record["progress"]
        assert progress["phase"] == "scan"
        assert 0 <= progress["op_index"] < progress["ops_total"]
        assert progress["bulk_in_bytes"] == 0 and progress["bulk_in_reads"] == 0
        assert record["exception"].startswith("USBError")
        # the frame that finished keeps its own sidecar; the failed one has none
        assert Path(diag.sidecar_path(cli._frame_output(out, 1))).exists()
        assert not Path(diag.sidecar_path(cli._frame_output(out, 2))).exists()
        # nothing was sent after the failure
        assert scanner.session.failure["writes"] == fake.out_count


def test_first_frame_failure_says_no_frame_finished():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "x.tiff")
        code, so, se, scanner, fake = _run(
            ["scan", "--frame", "1", "-o", out, *BASE], 1, _usb_timeout)
        assert code == 1, (code, se)
        assert "frame 1 failed" in se and "finished before it: none" in se, se
        assert "timeout" in se and "errno 110" in se, se
        record = json.loads(Path(diag.failure_path(out)).read_text())
        assert record["frames_finished"] == [] and record["usb"]["kind"] == "timeout"


def test_no_diag_writes_no_record_but_still_explains():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "x.tiff")
        code, so, se, scanner, fake = _run(
            ["scan", "--frames", "1-2", "--no-diag", "-o", out, *BASE], 2, _gone)
        assert code == 1
        assert "frame 2 failed" in se and "device_gone" in se
        assert list(Path(td).glob("*.fail.json")) == []


def test_ctrl_c_keeps_its_exit_code_and_also_leaves_a_record():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "x.tiff")
        code, so, se, scanner, fake = _run(
            ["scan", "--frame", "1", "-o", out, *BASE], 1, KeyboardInterrupt)
        assert code == 130, (code, se)
        record = json.loads(Path(diag.failure_path(out)).read_text())
        assert record["usb"] is None and record["exception"].startswith("KeyboardInterrupt")


def test_a_record_that_cannot_be_written_does_not_hide_the_failure():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "x.tiff")

        def refuse(path, data):
            raise OSError("disk full")

        with patched(diag, "write_sidecar", refuse):
            code, so, se, scanner, fake = _run(
                ["scan", "--frame", "1", "-o", out, *BASE], 1, _gone)
        assert code == 1 and "frame 1 failed" in se and "device_gone" in se, se
        assert "could not write" in se


def test_a_scanner_that_cannot_report_progress_still_fails_with_the_real_error():
    """Evidence gathering must never replace the failure (a test double, or a
    driver state the report did not foresee)."""
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "x.tiff")

        def broken(self):
            raise AttributeError("no progress here")

        from of135i.device import Scanner
        with patched(Scanner, "progress", broken):
            code, so, se, scanner, fake = _run(
                ["scan", "--frame", "1", "-o", out, *BASE], 1, _gone)
        assert code == 1 and "USBError" in se and "No such device" in se, se
        assert "could not describe the failure of frame 1" in se
        _assert_power_cycle_message(se)


def test_progress_counts_the_image_reads_before_the_failure():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "x.tiff")
        fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
        seen = []
        with cli_over(fake) as opened:
            def fault(ev):
                if ev["kind"] == "bulk_in" and opened and opened[-1].session.phase == "scan":
                    seen.append(1)
                    if len(seen) == 4:
                        return _gone()
                return None
            fake.fault = fault
            code, so, se = _cli(["scan", "--frame", "1", "-o", out, *BASE])
        assert code == 1
        progress = json.loads(Path(diag.failure_path(out)).read_text())["progress"]
        assert progress["bulk_in_reads"] == 3 and progress["bulk_in_bytes"] > 0
        assert "3 image read(s)" in se


def test_log_format_has_a_clock_only_when_asked():
    assert cli._log_format({}) == "%(levelname)s %(name)s: %(message)s"
    timed = cli._log_format({"OF135I_LOG_TIME": "1"})
    assert timed.startswith("%(asctime)s.%(msecs)03d ")
    assert timed.endswith("%(levelname)s %(name)s: %(message)s")
    assert cli._log_format({"OF135I_LOG_TIME": "0"}) == cli._log_format({})
    line = logging.Formatter(timed, datefmt=cli.LOG_DATEFMT).format(
        logging.LogRecord("of135i", logging.INFO, __file__, 1, "hello", None, None))
    assert line.endswith("INFO of135i: hello") and line[2] == ":" and "." in line.split()[0]


if __name__ == "__main__":
    ran = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"{name} OK")
            ran += 1
    print(f"{ran} tests passed")


def _succeed(argv):
    """Run the CLI over a fake that never faults (image finishing stubbed)."""
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    with cli_over(fake), patched(cli, "_finish_plain_scan", lambda *a, **k: None):
        return _cli(argv)


def test_a_later_good_run_removes_the_old_failure_record():
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "x.tiff")
        argv = ["scan", "--frame", "1", "-o", out, *BASE]
        code, so, se, scanner, fake = _run(argv, 1, _gone)
        assert code == 1 and Path(diag.failure_path(out)).exists()
        code, so, se = _succeed(argv)
        assert code == 0, (code, se)
        assert Path(diag.sidecar_path(out)).exists()
        assert not Path(diag.failure_path(out)).exists()


def test_a_failure_record_that_cannot_be_removed_is_logged_not_hidden(caplog):
    with tempfile.TemporaryDirectory() as td:
        out = str(Path(td) / "x.tiff")
        argv = ["scan", "--frame", "1", "-o", out, *BASE]
        _run(argv, 1, _gone)
        real_unlink = Path.unlink

        def refuse(self, *a, **k):
            if self.name.endswith(".fail.json"):
                raise PermissionError("read-only")
            return real_unlink(self, *a, **k)

        with patched(Path, "unlink", refuse), caplog.at_level(logging.WARNING, "of135i"):
            code, so, se = _succeed(argv)
        assert code == 0, (code, se)
        assert "fail.json" in caplog.text and "read-only" in caplog.text


class _Chain:
    @staticmethod
    def wrap(inner: BaseException) -> Exception:
        try:
            try:
                raise inner
            except BaseException as e:
                raise RuntimeError("calibration failed") from e
        except RuntimeError as outer:
            return outer


def test_wrapped_usb_errors_are_classified_by_their_cause():
    gone = diag.classify_usb_error(_Chain.wrap(_gone()))
    assert gone["kind"] == "device_gone" and gone["errno"] == 19
    stall = diag.classify_usb_error(_Chain.wrap(_usb_timeout()))
    assert stall["kind"] == "timeout" and stall["errno"] == 110


def test_wrapped_usb_error_found_through_context_too():
    try:
        try:
            raise _gone()
        except usb.core.USBError:
            raise ValueError("while handling")  # noqa: B904 -- implicit __context__ is the point
    except ValueError as e:
        assert diag.classify_usb_error(e)["kind"] == "device_gone"


def test_the_exception_itself_wins_over_its_cause():
    try:
        try:
            raise _gone()
        except usb.core.USBError as e:
            raise _usb_timeout() from e
    except usb.core.USBError as top:
        assert diag.classify_usb_error(top)["kind"] == "timeout"


def test_a_cyclic_cause_chain_terminates():
    a, b = RuntimeError("a"), RuntimeError("b")
    a.__cause__, b.__cause__ = b, a
    assert diag.classify_usb_error(a) is None


def test_a_chain_without_usb_errors_is_not_usb():
    assert diag.classify_usb_error(_Chain.wrap(ValueError("plain"))) is None


def test_usb_kinds_are_keyed_by_errno_constants():
    import errno
    assert set(diag._USB_ERROR_KINDS) == {
        errno.ENODEV, errno.ETIMEDOUT, errno.EPIPE, errno.EIO}
