"""Opt-in ``Scanner.cold_homing_rounds`` / ``--cold-homing-rounds N``.

cold_init() runs three loader-homing rounds of three moves each. The
vendor app, observed in vendor-app traffic with the holder already at the
sensor, runs one set of three jogs. The opt-in setting skips only the
EXTRA rounds (1 or 2 rounds instead of 3); every round that does run, the
pre-move prep, the resync and everything after cold_init are unchanged.
Default 3 is byte-identical to the previous behaviour (also pinned by
test_wire_baseline). EXPERIMENTAL: unverified on hardware.

Pure offline logic against fake devices: no USB, no scanner.
"""
from __future__ import annotations

import os
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from test_safety import (FakeUsbDevice, _cal_buffers, _cli, cli_over,  # noqa: E402
                         fast_time, make_scanner)

from of135i import cli, loadflow  # noqa: E402
from of135i.device import Scanner  # noqa: E402


@contextmanager
def patched_start_state():
    """Make watch's body stop right after the start-state check."""
    real = Scanner.check_start_state

    def stop(self):
        real(self)
        raise KeyboardInterrupt
    Scanner.check_start_state = stop
    try:
        yield
    finally:
        Scanner.check_start_state = real


def _record(fake):
    log = []
    real = fake.ctrl_transfer

    def ctrl_transfer(bm, br, wv=0, wi=0, data_or_wLength=None, timeout=None):
        got = real(bm, br, wv, wi, data_or_wLength, timeout)
        payload = data_or_wLength if isinstance(data_or_wLength, (bytes, bytearray)) else None
        log.append((bm, br, wv, wi, bytes(payload) if payload is not None else data_or_wLength,
                    bytes(got) if (bm & 0x80) else None))
        return got
    fake.ctrl_transfer = ctrl_transfer
    return log


def _cold_traffic(rounds=None):
    fake = FakeUsbDevice(reg01=0x00)
    log = _record(fake)
    scanner = make_scanner(fake)
    if rounds is not None:
        scanner.cold_homing_rounds = rounds
    with fast_time():
        scanner.cold_init()
    return fake, scanner, log


def _prefix_len(a, b):
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def test_default_is_three_and_identical_to_explicit_three():
    assert Scanner.__new__(Scanner).cold_homing_rounds == 3
    f0, _s, default = _cold_traffic()
    f3, _s, explicit = _cold_traffic(3)
    assert default == explicit
    assert f0.pulses == f3.pulses == 9


def test_fewer_rounds_are_a_strict_prefix_plus_the_shared_tail():
    _f, _s, full = _cold_traffic(3)
    f1, s1, one = _cold_traffic(1)
    f2, s2, two = _cold_traffic(2)
    assert f1.pulses == 3 and f2.pulses == 6
    assert s1.session.cold_init_done and s2.session.cold_init_done
    # N=1 is the default's traffic up to the end of round 1, then only the
    # shared post-homing tail; N=2 is the default's traffic up to the end
    # of round 2, then the same tail.
    for short, k in ((one, 1), (two, 2)):
        cut = _prefix_len(full, short)
        tail = len(short) - cut
        # No cold-table rewrite after the LAST round that runs: that
        # rewrite only prepares the next round, which is skipped.
        assert 0 < tail < 20, (k, cut, tail)
        assert short[cut:] == full[len(full) - tail:]
    assert _prefix_len(full, one) < _prefix_len(full, two)
    assert len(one) < len(two) < len(full)


@pytest.mark.parametrize("n", [1, 2, 3])
def test_cold_table_is_rewritten_only_between_rounds_that_run(monkeypatch, n):
    """The cold table is written once up front and again BETWEEN rounds:
    N rounds -> N writes, none after the last round that runs (it would
    only prepare a skipped round)."""
    calls = []
    real = Scanner._cold_write_table_and_afe
    monkeypatch.setattr(Scanner, "_cold_write_table_and_afe",
                        lambda self: (calls.append(self.session.phase), real(self))[1])
    _cold_traffic(n)
    assert len(calls) == n
    assert calls[-1] == f"cold_init:homing-{n - 1}" if n > 1 else calls[-1] == "cold_init"


def test_one_round_issues_exactly_the_first_rounds_moves():
    fake, _s, _log = _cold_traffic(1)
    assert fake.pulses == 3


@pytest.mark.parametrize("bad", [0, 4, -1, 2.0, "3", True, False, None, 3.5])
def test_invalid_value_is_rejected_before_any_transfer(bad):
    fake = FakeUsbDevice(reg01=0x00)
    log = _record(fake)
    s = make_scanner(fake)
    with pytest.raises(ValueError, match="cold_homing_rounds"):
        s.cold_homing_rounds = bad
    assert s.cold_homing_rounds == 3
    assert log == []


# ------------------------------------------------------------ CLI

@pytest.mark.parametrize("cmd", [["digitize", "-o", "x"], ["scan", "--frame", "1", "-o", "f.tiff"],
                                 ["eject"], ["load"], ["watch"]])
def test_parsers_default_three_and_accept_the_flag(cmd):
    parser = cli.build_parser()
    assert parser.parse_args(cmd).cold_homing_rounds == 3
    for n in (1, 2, 3):
        assert parser.parse_args(cmd + ["--cold-homing-rounds", str(n)]).cold_homing_rounds == n


@pytest.mark.parametrize("cmd", [["digitize", "-o", "x"], ["scan", "-o", "f.tiff"],
                                 ["eject"], ["load"], ["watch"]])
@pytest.mark.parametrize("bad", ["0", "4", "-1", "x", "1.5"])
def test_parser_rejects_invalid_values(cmd, bad, capsys):
    with pytest.raises(SystemExit) as e:
        cli.build_parser().parse_args(cmd + ["--cold-homing-rounds", bad])
    assert e.value.code == 2


def test_help_says_experimental_and_unverified():
    for cmd in ("scan", "digitize", "eject", "load", "watch"):
        sub = cli.build_parser()._subparsers._group_actions[0].choices[cmd]
        text = " ".join(sub.format_help().split())
        assert "--cold-homing-rounds" in text
        assert "experimental" in text.lower() and "unverified on hardware" in text.lower()


def test_invalid_cli_value_opens_no_device():
    fake = FakeUsbDevice(reg01=0x00)
    with cli_over(fake) as opened:
        with pytest.raises(SystemExit):
            cli.main(["eject", "--cold-homing-rounds", "7"])
    assert opened == [] and fake.out_count == 0


@pytest.mark.parametrize("n", [1, 2, 3])
def test_scan_cli_flag_reaches_the_scanner(tmp_path, n):
    fake = FakeUsbDevice(reg01=0x22, cal_buffers=_cal_buffers())
    argv = ["scan", "--frame", "1", "-o", str(tmp_path / "f.tiff"), "--no-diag",
            "--cold-homing-rounds", str(n)]
    with cli_over(fake) as opened:
        _cli(argv)
    scanners = [o for o in opened if isinstance(o, Scanner)]
    assert scanners and scanners[0].cold_homing_rounds == n


@pytest.mark.parametrize("n", [1, 2, 3])
def test_eject_cli_flag_reaches_the_scanner(n):
    fake = FakeUsbDevice(reg01=0x22)
    with cli_over(fake) as opened, fast_time():
        _cli(["eject", "--cold-homing-rounds", str(n)])
    scanners = [o for o in opened if isinstance(o, Scanner)]
    assert scanners and scanners[0].cold_homing_rounds == n


def test_cold_eject_via_cli_runs_the_requested_rounds():
    fake = FakeUsbDevice(reg01=0x00)
    with cli_over(fake), fast_time():
        _cli(["eject", "--cold-homing-rounds", "1"])
    # one homing round (3 moves) then the eject move
    assert fake.pulses == 4


def test_digitize_sets_the_setting_on_its_scanner(tmp_path):
    fake = FakeUsbDevice(reg01=0x23)
    with cli_over(fake) as opened:
        _cli(["digitize", "-o", str(tmp_path), "--assume-loaded", "--cold-homing-rounds", "2"])
    scanners = [o for o in opened if isinstance(o, Scanner)]
    assert scanners and scanners[0].cold_homing_rounds == 2


@pytest.mark.parametrize("args,expect", [([], 3), (["--cold-homing-rounds", "1"], 1)])
def test_load_cli_passes_it_to_the_load_flow(monkeypatch, args, expect):
    seen = {}

    def fake_run(ask=input, **kw):
        seen.update(kw)
        return 0
    monkeypatch.setattr(loadflow, "run", fake_run)
    code, _o, _e = _cli(["load"] + args)
    assert code == 0 and seen["cold_homing_rounds"] == expect


@pytest.mark.parametrize("n", [1, 2, 3])
def test_load_flow_runs_the_requested_cold_rounds(monkeypatch, n):
    """Cold scanner, release-only load: n rounds (3 moves each) in the
    cold start, then the 3-move app-start jog."""
    fake = FakeUsbDevice(reg01=0x00)
    monkeypatch.setattr(Scanner, "open", staticmethod(lambda: make_scanner(fake)))
    with fast_time():
        rc = loadflow.run(ask=lambda p: "", release_only=True, cold_homing_rounds=n)
    assert rc == 0
    assert fake.pulses == 3 * n + 3


@pytest.mark.parametrize("bad", [0, 4, True, None, "2"])
def test_load_flow_rejects_invalid_before_opening_the_device(monkeypatch, bad):
    opened = []
    monkeypatch.setattr(Scanner, "open", staticmethod(lambda: opened.append(1)))
    with pytest.raises(ValueError, match="cold_homing_rounds"):
        loadflow.run(ask=lambda p: "", release_only=True, cold_homing_rounds=bad)
    assert opened == []


@pytest.mark.parametrize("n", [1, 2, 3])
def test_watch_cli_flag_reaches_the_scanner(n):
    fake = FakeUsbDevice(reg01=0x22)
    with cli_over(fake) as opened, patched_start_state():
        _cli(["watch", "--cold-homing-rounds", str(n)])
    scanners = [o for o in opened if isinstance(o, Scanner)]
    assert scanners and scanners[0].cold_homing_rounds == n


def test_digitize_load_step_passes_it_to_the_load_flow(monkeypatch, tmp_path):
    seen = {}

    def fake_run(ask=input, **kw):
        seen.update(kw)
        return 130
    monkeypatch.setattr(loadflow, "run", fake_run)
    _cli(["digitize", "-o", str(tmp_path), "--cold-homing-rounds", "2"])
    assert seen.get("cold_homing_rounds") == 2
