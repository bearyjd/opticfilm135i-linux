"""`of135i load --probe DIR`: read-only register snapshots at three points of
the interactive load flow, plus the offline `probe-diff` helper.

Against the repo's fake USB device only. The guarantees pinned here:
the default load wire traffic is unchanged; with the flag the WRITE sequence
is unchanged and the snapshots are plain control reads; a failing snapshot
never alters the load; a failed session takes no snapshot; file modes; and
probe-diff reads nothing from a device.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_safety import (FakeUsbDevice, SessionState, _cli, cli_over,  # noqa: E402
                         patched, quiet, vendor_like_load_status)

from of135i import cli, loadflow, loadprobe, safety  # noqa: E402

# sha256 of repr(fake.wire_log) / repr(fake.out_log) for the default
# `loadflow.run()` below, recorded from the driver BEFORE --probe existed.
GOLDEN_WIRE_LOG = "211b6bc484785e840d4683c0e638804c382279356d3e199270e23d7710573156"
GOLDEN_OUT_LOG = "dda3d348a2dc5fee2a95cc1d66ccc0e3c60c2f607ef29c4a9003cae199b4c3dc"

READ_REQUESTS = {(0x04, 0x008E), (0x04, 0x018E)}


def _fake(**kw):
    fake = FakeUsbDevice(reg01=0x22, **kw)
    vendor_like_load_status(fake, jog=True)
    return fake


def _run(fake, probe_dir=None, ask=lambda p: ""):
    kw = {} if probe_dir is None else {"probe_dir": str(probe_dir)}
    with cli_over(fake), quiet():
        return loadflow.run(ask=ask, **kw)


def _writes(fake):
    return [e for e in fake.wire_log if e["t"] in ("cw", "bo")]


def _snaps(d):
    out = []
    for name in sorted(os.listdir(d)):
        with open(os.path.join(d, name)) as f:
            out.append((name, json.load(f)))
    return out


# ------------------------------------------------- default is unchanged


def test_default_load_wire_traffic_is_unchanged():
    fake = _fake()
    assert _run(fake) == 0
    assert hashlib.sha256(repr(fake.wire_log).encode()).hexdigest() == GOLDEN_WIRE_LOG
    assert hashlib.sha256(repr(fake.out_log).encode()).hexdigest() == GOLDEN_OUT_LOG


def test_probe_leaves_the_write_sequence_unchanged(tmp_path):
    base = _fake()
    assert _run(base) == 0
    probed = _fake()
    assert _run(probed, tmp_path / "p") == 0
    assert _writes(probed) == _writes(base)
    assert repr(probed.out_log) == repr(base.out_log)
    assert (probed.pulses, probed.out_count) == (base.pulses, base.out_count)
    # ... and the probe really did read extra registers.
    assert probed.in_count > base.in_count


# ------------------------------------------------------ three snapshots


def test_three_snapshots_in_order_with_expected_content(tmp_path):
    fake = _fake()
    fake.button_events.extend([0x48, None])      # seen by the flow after the jog
    d = tmp_path / "p"
    assert _run(fake, d) == 0
    snaps = _snaps(d)
    assert [s["label"] for _, s in snaps] == ["after-jog", "at-stop", "after-load"]
    for name, s in snaps:
        assert name.endswith(".json") and s["label"] in name
        for key in ("timestamp_utc", "status_word", "reg01", "regs", "magazine_present",
                    "interrupt_events_seen_by_flow"):
            assert key in s, (name, key)
        assert set(s["regs"]) == {f"0x{r:02x}" for r in range(0x121)}
        assert s["reg01"] == "0x22"
        assert s["status_word"].startswith("0x")
        assert isinstance(s["magazine_present"], bool)
    # The flow's own drain result is copied, not re-drained.
    assert snaps[0][1]["interrupt_events_seen_by_flow"] == [0x48]
    assert snaps[1][1]["interrupt_events_seen_by_flow"] == []


def test_snapshots_are_taken_at_the_right_points(tmp_path):
    fake = _fake()
    seen = []
    real = loadprobe.collect_snapshot

    def spy(io, label, events):
        seen.append((label, fake.pulses, fake.out_count))
        return real(io, label, events)

    ask_at = []

    def ask(prompt):
        ask_at.append((fake.pulses, fake.out_count, len(seen)))
        return ""

    with patched(loadprobe, "collect_snapshot", spy):
        assert _run(fake, tmp_path / "p", ask=ask) == 0
    assert [s[0] for s in seen] == ["after-jog", "at-stop", "after-load"]
    jog_pulses, jog_out = ask_at[0][0], ask_at[0][1]
    assert jog_pulses == 3
    assert seen[0][1:] == (jog_pulses, jog_out)          # after the jog, before the prompt
    assert ask_at[0][2] == 1                              # after-jog existed before ask
    assert seen[1][1:] == (jog_pulses, jog_out)          # at-stop: no feed/traverse write yet
    assert seen[2][1] == 5 and seen[2][2] == fake.out_count   # after the whole load


# ------------------------------------------------------ snapshots are reads


def test_snapshots_issue_only_the_known_read_requests(tmp_path):
    fake = _fake()
    windows = []
    real = loadprobe.collect_snapshot

    def spy(io, label, events):
        start = len(fake.wire_log)
        outs = fake.out_count
        try:
            return real(io, label, events)
        finally:
            windows.append((fake.wire_log[start:], fake.out_count - outs))

    with patched(loadprobe, "collect_snapshot", spy):
        assert _run(fake, tmp_path / "p") == 0
    assert len(windows) == 3
    for entries, outs in windows:
        assert outs == 0
        assert entries and all(e["t"] == "cr" and (e["br"], e["wv"]) in READ_REQUESTS
                               for e in entries), entries[:3]


def test_a_write_inside_a_snapshot_is_detected(tmp_path):
    """The 'no writes' check above must be able to fail: a snapshot that
    writes one register makes the windowed write count non-zero."""
    fake = _fake()
    counts = []
    real = loadprobe.collect_snapshot

    def writing(io, label, events):
        before = fake.out_count
        io.write_regs([(0x03, 0x00)])
        snap = real(io, label, events)
        counts.append(fake.out_count - before)
        return snap

    with patched(loadprobe, "collect_snapshot", writing):
        _run(fake, tmp_path / "p")
    assert any(counts), "a writing snapshot must be visible to the check"


# ------------------------------------------------- failures never alter load


def test_failing_register_reads_are_recorded_and_the_load_still_succeeds(tmp_path):
    base = _fake()
    assert _run(base) == 0
    fake = _fake()

    def fault(ev):
        if ev["kind"] == "ctrl_in" and ev["wv"] == 0x008E and (ev["wi"] >> 8) == 0x10:
            return RuntimeError("injected read failure")
        if ev["kind"] == "ctrl_in" and ev["wv"] == 0x018E and (ev["wi"] >> 8) == 0x20:
            return RuntimeError("injected ext read failure")
        return None
    fake.fault = fault
    d = tmp_path / "p"
    assert _run(fake, d) == 0
    assert _writes(fake) == _writes(base)
    for _, s in _snaps(d):
        assert "injected read failure" in s["regs"]["0x10"]["error"]
        assert "injected ext read failure" in s["regs"]["0x120"]["error"]
        assert s["regs"]["0x11"] == 0       # the rest still recorded


def test_a_crashing_snapshot_does_not_change_the_load(tmp_path):
    base = _fake()
    assert _run(base) == 0
    fake = _fake()

    def boom(io, label, events):
        raise RuntimeError("snapshot exploded")
    with patched(loadprobe, "collect_snapshot", boom):
        assert _run(fake, tmp_path / "p") == 0
    assert _writes(fake) == _writes(base)
    assert (fake.pulses, fake.out_count) == (base.pulses, base.out_count)


def test_an_unwritable_probe_file_does_not_change_the_load(tmp_path):
    base = _fake()
    assert _run(base) == 0
    fake = _fake()
    def nowrite(*a, **k):
        raise OSError("disk full")
    with patched(loadprobe, "write_snapshot", nowrite):
        assert _run(fake, tmp_path / "p") == 0
    assert _writes(fake) == _writes(base)


# ------------------------------------------------------- failed session


def test_failed_session_takes_no_snapshot(tmp_path):
    fake = _fake()
    from of135i.device import Scanner
    from of135i.usbio import UsbIo
    s = Scanner(UsbIo(fake))
    s.session.fail(RuntimeError("earlier failure"), where="test")
    assert s.session.state is SessionState.FAILED
    before = fake.in_count
    d = tmp_path / "p"
    d.mkdir()
    assert loadflow._probe(s, str(d), "after-jog", []) is False
    assert fake.in_count == before and os.listdir(d) == []


def test_load_failure_means_no_after_load_snapshot(tmp_path):
    fake = FakeUsbDevice(reg01=0x22)
    vendor_like_load_status(fake, jog=True, after_feed=0xFC)    # feed does not engage
    d = tmp_path / "p"
    assert _run(fake, d) == 1
    assert [s["label"] for _, s in _snaps(d)] == ["after-jog", "at-stop"]


# ------------------------------------------------------- files and modes


def test_modes_and_never_overwrite(tmp_path, monkeypatch):
    old = os.umask(0)
    try:
        d = loadprobe.prepare_dir(str(tmp_path / "new"))
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
        monkeypatch.setattr(loadprobe, "_stamp", lambda: "20260101T000000000000Z")
        p1 = loadprobe.write_snapshot(d, "at-stop", {"a": 1})
        p2 = loadprobe.write_snapshot(d, "at-stop", {"a": 2})
        assert p1 != p2
        for p in (p1, p2):
            assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
        assert json.load(open(p1)) == {"a": 1}
        assert json.load(open(p2)) == {"a": 2}
    finally:
        os.umask(old)


def test_real_run_files_are_0600_in_a_0700_dir(tmp_path):
    old = os.umask(0)
    try:
        d = tmp_path / "p"
        assert _run(_fake(), d) == 0
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
        for name in os.listdir(d):
            assert stat.S_IMODE(os.stat(d / name).st_mode) == 0o600
    finally:
        os.umask(old)


# ------------------------------------------------------------- CLI wiring


def test_cli_passes_probe_dir_and_default_is_none(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(loadflow, "run", lambda **kw: calls.append(kw) or 0)
    assert cli.main(["load"]) == 0
    assert calls[-1]["probe_dir"] is None
    assert cli.main(["load", "--probe", str(tmp_path / "x")]) == 0
    assert calls[-1]["probe_dir"] == str(tmp_path / "x")


@pytest.mark.parametrize("other", ["--next-strip", "--double-jog"])
def test_probe_excludes_next_strip_and_double_jog(other, monkeypatch, tmp_path):
    monkeypatch.setattr(loadflow, "run", lambda **kw: pytest.fail("must not run"))
    code, _o, err = _cli(["load", "--probe", str(tmp_path / "x"), other])
    assert code == 2 and "--probe" in err


def test_load_help_shows_probe():
    with pytest.raises(SystemExit):
        with quiet():
            cli.main(["load", "--help"])
    from test_safety import _STDOUT
    assert "--probe" in _STDOUT.getvalue()


# ------------------------------------------------------------- probe-diff


def _snap_file(tmp_path, name, regs, status="0xf855"):
    p = tmp_path / name
    p.write_text(json.dumps({"label": name, "status_word": status, "reg01": "0x22",
                             "magazine_present": True, "regs": regs}))
    return str(p)


def test_probe_diff_prints_register_differences_offline(tmp_path):
    a = _snap_file(tmp_path, "a.json", {"0x10": 1, "0x11": 2, "0x120": 5})
    b = _snap_file(tmp_path, "b.json", {"0x10": 1, "0x11": 3, "0x120": {"error": "x"}},
                   status="0xf455")
    with patched(safety.GuardedDevice, "ctrl_transfer",
                 lambda *a, **k: pytest.fail("probe-diff must not touch a device")):
        code, out, _ = _cli(["probe-diff", a, b])
    assert code == 0
    assert "0x11" in out and "0x02" in out and "0x03" in out
    assert "0x120" in out
    assert "0x10 " not in out and "0x10:" not in out          # unchanged: not listed
    assert "status_word" in out and "0xf455" in out


def test_probe_diff_ignore_written_hides_driver_written_registers(tmp_path):
    written = sorted(loadprobe.written_registers())
    assert 0x32 in written and 0x120 not in written
    a = _snap_file(tmp_path, "a.json", {"0x32": 1, "0x120": 5})
    b = _snap_file(tmp_path, "b.json", {"0x32": 2, "0x120": 6})
    _c, shown, _ = _cli(["probe-diff", a, b])
    _c, hidden, _ = _cli(["probe-diff", "--ignore-written", a, b])
    assert "0x32" in shown and "0x32" not in hidden
    assert "0x120" in hidden


def test_probe_diff_bad_input_exits_nonzero(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json")
    code, _o, err = _cli(["probe-diff", str(bad), str(bad)])
    assert code != 0 and err
    code, _o, err = _cli(["probe-diff", str(tmp_path / "missing.json"), str(bad)])
    assert code != 0 and err
