#!/usr/bin/env python3
"""Recorded-unit replay of the GL126 magazine load (offline).

Two halves:

  1. tests/pcap_to_replay.py, the converter from a usbmon pcap (or a
     vendor trace.json.gz) to a reply script. Tested on small synthetic
     captures built here, so these tests need nothing but python3.
  2. The replay itself: the BUILT backend's load-film flow run in
     genesys's test mode through tests/gl126_magazine_probe.cpp
     ("magprobe replay <script>"), its control-IN transfers answered from
     a recording of the owner's unit (tests/golden/replay_r1_backend.txt,
     tests/golden/replay_r3_python.txt) by matching. Needs what
     tests/test_sane_magazine.py needs (g++, SANE_BACKENDS_DIR, a built
     libsane-genesys.so) and skips cleanly without it.
  3. The same for the other sessions of the 2026-11-06 backend capture
     (s1-scanner.pcap, four processes): the load, the 600 and 3600 dpi
     scans (calibration, positioning, the scan pass's chunk reads, the
     park) and the eject. The scans' image data is not in the scripts;
     see ScanReplayTests.

Run with:
    python3 tests/test_sane_replay.py
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(REPO / "tests")]

import pcap_to_replay as conv  # noqa: E402
import test_sane_magazine as mag  # noqa: E402  (probe build, status codes)

GOLDEN = REPO / "tests" / "golden"
R1 = GOLDEN / "replay_r1_backend.txt"
R3 = GOLDEN / "replay_r3_python.txt"
S1_LOAD = GOLDEN / "replay_s1_load.txt"
S1_EJECT = GOLDEN / "replay_s1_eject.txt"
# (script, fills, polls, dpi, session): the two scans of s1-scanner.pcap
S1_SCANS = {
    600: (GOLDEN / "replay_s1_scan600.txt", GOLDEN / "replay_s1_scan600_fills.txt",
          GOLDEN / "replay_s1_scan600_polls.txt"),
    3600: (GOLDEN / "replay_s1_scan3600.txt", GOLDEN / "replay_s1_scan3600_fills.txt",
           GOLDEN / "replay_s1_scan3600_polls.txt"),
}


# --- synthetic usbmon captures -------------------------------------------

def _usbmon_packet(urb, kind, xfer, ep, dev, ts, setup=b"", data=b"",
                   status=0, length=None, bus=3):
    """One usbmon (linktype 220, 64-byte header) packet."""
    flag_setup = b"\0" if setup else b"-"
    flag_data = b"\0" if data else b"<"
    hdr = struct.pack("<QcBBBHccqiiII8siiII", urb, kind, xfer, ep, dev, bus,
                      flag_setup, flag_data, int(ts), int(round((ts % 1) * 1e6)),
                      status, len(data) if length is None else length, len(data),
                      setup.ljust(8, b"\0"), 0, 0, 0, 0)
    return hdr + data


def _pcap(packets, linktype=220):
    out = io.BytesIO()
    out.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 262144, linktype))
    for ts, pkt in packets:
        out.write(struct.pack("<IIII", int(ts), int(round((ts % 1) * 1e6)),
                              len(pkt), len(pkt)))
        out.write(pkt)
    return out.getvalue()


def _ctrl_in(urb, ts, dur, bm, req, wv, wi, reply, dev=22):
    setup = struct.pack("<BBHHH", bm, req, wv, wi, len(reply))
    return [(ts, _usbmon_packet(urb, b"S", 2, 0x80, dev, ts, setup=setup,
                                length=len(reply))),
            (ts + dur, _usbmon_packet(urb, b"C", 2, 0x80, dev, ts + dur, data=reply))]


def _ctrl_out(urb, ts, dur, bm, req, wv, wi, data, dev=22):
    setup = struct.pack("<BBHHH", bm, req, wv, wi, len(data))
    return [(ts, _usbmon_packet(urb, b"S", 2, 0x00, dev, ts, setup=setup, data=data)),
            (ts + dur, _usbmon_packet(urb, b"C", 2, 0x00, dev, ts + dur,
                                      length=len(data)))]


def _bulk_out(urb, ts, dur, data, dev=22):
    return [(ts, _usbmon_packet(urb, b"S", 3, 0x02, dev, ts, data=data)),
            (ts + dur, _usbmon_packet(urb, b"C", 3, 0x02, dev, ts + dur,
                                      length=len(data)))]


def _convert_bytes(blob, *args):
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "in.pcap"
        src.write_bytes(blob)
        return conv.convert_pcap(src, *args)


def _bulk_in(urb, ts, dur, data, ep=0x81, dev=22):
    return [(ts, _usbmon_packet(urb, b"S", 3, ep, dev, ts, length=len(data))),
            (ts + dur, _usbmon_packet(urb, b"C", 3, ep, dev, ts + dur, data=data))]


class ConverterTests(unittest.TestCase):

    def test_a_bulk_in_never_keeps_its_data_only_its_length(self):
        secret = bytes(range(1, 65))
        pkts = _bulk_in(5, 10.000, 0.001, secret)
        pkts += _ctrl_in(6, 10.002, 0.0004, 0xC0, 0x04, 0x018E, 0x0122, bytes([0xF8, 0x55]))
        script = _convert_bytes(_pcap(pkts))
        self.assertEqual(script.body(), ["0 BI 81 64 -", "1 CI c0 04 018e 0122 2 f855"])
        self.assertNotIn(secret.hex(), script.text())
        self.assertNotIn(secret[:8].hex(), script.text())

    def test_a_bulk_out_never_keeps_its_data_only_its_length(self):
        table = bytes(range(1, 65))
        script = _convert_bytes(_pcap(_bulk_out(5, 10.000, 0.001, table)))
        self.assertEqual(script.body(), ["0 BO 02 64 ~"])
        self.assertNotIn(table.hex(), script.text())
        self.assertNotIn(table[:8].hex(), script.text())

    def test_sessions_are_cut_at_long_idle_gaps_and_keep_the_capture_indices(self):
        pkts = []
        for k in range(2):      # the kernel's enumeration: too short to be a session
            pkts += _ctrl_in(k, 1.0 + k * 0.01, 0.001, 0x80, 0x06, 0x0100, 0, bytes(18))
        for run, t0 in ((0, 40.0), (1, 80.0)):    # two processes, 40 s apart
            for k in range(5):
                pkts += _ctrl_in(100 + run * 10 + k, t0 + k * 0.01, 0.001, 0xC0, 0x04,
                                 0x008E, 0x0122, bytes([k, 0x55]))
        runs = _convert_bytes(_pcap(pkts)).sessions()
        self.assertEqual([len(r.transfers) for r in runs], [7, 5])
        self.assertEqual([r.first_index for r in runs], [0, 7])
        self.assertTrue(runs[1].body()[0].startswith("7 CI c0 04 008e 0122 2 0055"))
        self.assertIn("from #7", runs[1].text())
        # One run alone is the whole capture.
        self.assertEqual(len(_convert_bytes(_pcap(pkts[:14])).sessions()), 1)

    def test_pairs_submit_and_completion_and_takes_each_direction_s_data(self):
        pkts = []
        pkts += _ctrl_out(7, 10.000, 0.0005, 0x40, 0x04, 0x0083, 0x0000, bytes([0x32, 0x1d]))
        pkts += _ctrl_in(7, 10.001, 0.0004, 0xC0, 0x0C, 0x008E, 0x0020, bytes([0x55]))
        pkts += _bulk_out(9, 10.002, 0.0001, bytes(range(8)))
        lines = _convert_bytes(_pcap(pkts)).body()
        self.assertEqual(lines, [
            "0 CO 40 04 0083 0000 2 321d",
            "1 CI c0 0c 008e 0020 1 55",
            "2 BO 02 8 ~",
        ])

    def test_interleaved_urbs_are_paired_by_id_and_ordered_by_submit(self):
        # Two URBs in flight: B submitted after A but completing first.
        a = _ctrl_in(1, 1.000, 0.010, 0xC0, 0x04, 0x018E, 0x0122, bytes([0xF8, 0x55]))
        b = _ctrl_in(2, 1.001, 0.001, 0xC0, 0x04, 0x008E, 0x3222, bytes([0x1F, 0x55]))
        pkts = sorted(a + b, key=lambda p: p[0])
        lines = _convert_bytes(_pcap(pkts)).body()
        self.assertEqual(lines, [
            "0 CI c0 04 018e 0122 2 f855",
            "1 CI c0 04 008e 3222 2 1f55",
        ])

    def test_identical_consecutive_transfers_collapse_to_one_run(self):
        pkts = []
        t = 1.0
        for i, b0 in enumerate([0xF8, 0xF8, 0xF8, 0xF0, 0xF0]):
            pkts += _ctrl_in(i, t, 0.0003, 0xC0, 0x04, 0x018E, 0x0122, bytes([b0, 0x55]))
            t += 0.01
        lines = _convert_bytes(_pcap(pkts)).body()
        self.assertEqual(lines, [
            "0 CI c0 04 018e 0122 2 f855 x3",
            "3 CI c0 04 018e 0122 2 f055 x2",
        ])

    def test_only_long_idle_gaps_are_kept(self):
        pkts = []
        pkts += _ctrl_out(1, 5.0, 0.001, 0x40, 0x04, 0x0083, 0x0000, bytes([0x0F, 0x01]))
        # 1.6 s idle (the feed's sleep), then a 10 ms one (not kept).
        pkts += _ctrl_in(2, 6.601, 0.001, 0xC0, 0x04, 0x018E, 0x0122, bytes([0xF4, 0x55]))
        pkts += _ctrl_in(3, 6.612, 0.001, 0xC0, 0x04, 0x018E, 0x0122, bytes([0xDC, 0x55]))
        lines = _convert_bytes(_pcap(pkts)).body()
        self.assertEqual(lines, [
            "0 CO 40 04 0083 0000 2 0f01",
            "GAP 1600",
            "1 CI c0 04 018e 0122 2 f455",
            "2 CI c0 04 018e 0122 2 dc55",
        ])

    def test_a_paced_poll_is_one_run_until_its_pace_changes(self):
        # The edge wait's 100 ms poll: three reads ~100 ms apart (100, 101), then one
        # after a 600 ms settle, which is a new entry with its own GAP.
        pkts = []
        for i, ts in enumerate([1.0, 1.101, 1.203, 1.804]):
            pkts += _ctrl_in(i, ts, 0.001, 0xC0, 0x04, 0x018E, 0x0122, bytes([0xF8, 0x55]))
        lines = _convert_bytes(_pcap(pkts)).body()
        self.assertEqual(lines, [
            "0 CI c0 04 018e 0122 2 f855 x3 every=100",
            "GAP 600",
            "3 CI c0 04 018e 0122 2 f855",
        ])

    def test_filters_one_device_and_defaults_to_the_busiest(self):
        pkts = []
        pkts += _ctrl_in(1, 1.0, 0.001, 0x80, 0x06, 0x0100, 0x0000, bytes(4), dev=1)
        pkts += _ctrl_in(2, 1.1, 0.001, 0xC0, 0x04, 0x018E, 0x0122, bytes([0xF8, 0x55]))
        pkts += _ctrl_in(3, 1.11, 0.001, 0xC0, 0x04, 0x018E, 0x0122, bytes([0xF0, 0x55]))
        script = _convert_bytes(_pcap(pkts))
        self.assertEqual(script.device, 22)
        self.assertEqual(len(script.body()), 2)
        self.assertEqual(_convert_bytes(_pcap(pkts), 1).body(),
                         ["0 CI 80 06 0100 0000 4 00000000"])

    def test_status_and_empty_payloads_are_spelled_out(self):
        urb = 4
        pkts = [(1.0, _usbmon_packet(urb, b"S", 1, 0x83, 22, 1.0, length=8)),
                (1.05, _usbmon_packet(urb, b"C", 1, 0x83, 22, 1.05, status=-2, length=0))]
        lines = _convert_bytes(_pcap(pkts)).body()
        self.assertEqual(lines, ["0 II 83 8 - st=-2"])

    def test_the_header_names_the_source_and_device(self):
        pkts = _ctrl_in(1, 1.0, 0.001, 0xC0, 0x04, 0x018E, 0x0122, bytes([0xF8, 0x55]))
        text = _convert_bytes(_pcap(pkts)).text()
        self.assertTrue(text.startswith("# gl126 reply script v1\n"))
        self.assertIn("# source: in.pcap (usbmon linktype 220), bus 3 device 22, 1 transfers\n",
                      text)

    def test_rejects_an_unknown_linktype(self):
        with self.assertRaises(ValueError):
            _convert_bytes(_pcap([], linktype=1))

    def test_parse_reads_back_what_format_wrote(self):
        text = "\n".join([
            "# comment",
            "0 CO 40 04 0083 0000 2 321d",
            "GAP 1600",
            "1 CI c0 04 018e 0122 2 f855 x3 every=100",
            "4 II 83 8 - st=-2",
        ]) + "\n"
        entries = conv.parse_script(text)
        self.assertEqual([(e.index, e.kind, e.data, e.count, e.gap_ms, e.every_ms, e.status)
                          for e in entries], [
            (0, "CO", bytes([0x32, 0x1d]), 1, 0, 0, 0),
            (1, "CI", bytes([0xF8, 0x55]), 3, 1600, 100, 0),
            (4, "II", b"", 1, 0, 0, -2),
        ])
        self.assertEqual(entries[1].fields, ["c0", "04", "018e", "0122", "2"])

    def test_vendor_trace_polls_are_marked_lossy(self):
        trace = [
            {"bm": 64, "br": 4, "wv": 131, "wi": 0, "t": "cw", "data": "0f01", "dt": 0.0},
            {"bm": 192, "br": 4, "wv": 398, "wi": 290, "t": "poll", "len": 2, "n": 4,
             "resps": ["d955", "f855"], "dur": 0.01, "dt": 1.6},
            {"t": "bo", "data": "0001", "dt": 0.0},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "t.trace.json.gz"
            with gzip.open(src, "wt") as f:
                json.dump(trace, f)
            script = conv.convert_vendor_trace(src)
        self.assertEqual(script.body(), [
            "0 CO 40 04 0083 0000 2 0f01",
            "GAP 1600",
            "# lossy poll: 4 reads, distinct replies d955 f855 (per-read order not recorded)",
            "1 CI c0 04 018e 0122 2 d955",
            "2 CI c0 04 018e 0122 2 f855 x3",
            "5 BO -- 2 ~",
        ])


class GoldenTests(unittest.TestCase):
    """The committed scripts are what the converter makes of the
    recordings (when the recordings are on this machine)."""

    CAPTURES = Path(os.environ.get(
        "GL126_CAPTURE_DIR",
        str(REPO.parent.parent.parent / ".omc" / "hw" / "capture-2026-11-06")))

    def _check(self, golden, pcap, device, session=None):
        src = self.CAPTURES / pcap
        if not src.exists():
            self.skipTest(f"{src} not on this machine")
        script = conv.convert_pcap(src, device)
        if session is not None:
            script = script.sessions()[session]
        self.assertEqual(script.text(), golden.read_text())

    def test_r1_golden_is_current(self):
        self._check(R1, "r1-backend-scanner.pcap", 22)

    def test_r3_golden_is_current(self):
        self._check(R3, "r3-scanner.pcap", 20)

    def test_s1_goldens_are_current(self):
        s1 = "s1/s1-scanner.pcap"
        self._check(S1_LOAD, s1, 25, 0)
        self._check(S1_SCANS[600][0], s1, 25, 1)
        self._check(S1_SCANS[3600][0], s1, 25, 2)
        self._check(S1_EJECT, s1, 25, 3)

    def test_goldens_carry_no_image_data(self):
        for g in (R1, R3, S1_LOAD, S1_EJECT, S1_SCANS[600][0], S1_SCANS[3600][0]):
            entries = conv.parse_script(g.read_text())
            self.assertFalse([e for e in entries if e.kind == "BI" and e.data], g.name)
            # Nor does a bulk OUT: the tables written to the scanner (the
            # shading tables, computed from the calibration frames,
            # among them) keep their length and position, not their bytes.
            self.assertFalse([e for e in entries if e.kind == "BO" and e.data], g.name)
            self.assertNotRegex(g.read_text(), r"(?m)^\d+ BO \S+ \d+ (?!~)", g.name)

    def test_the_stand_in_fills_are_a_few_lines_of_statistics(self):
        for _, fills, _ in S1_SCANS.values():
            rows = [l.split() for l in fills.read_text().splitlines()
                    if l.strip() and not l.startswith("#")]
            self.assertLessEqual(len(rows), 8, fills.name)
            self.assertTrue(all(len(r) in (4, 5) and all(re.fullmatch(r"[0-9]+(-[0-9]+)?", t)
                                                         for t in r) for r in rows), fills.name)


# --- the replay -------------------------------------------------------------

# Enough edge-wait polls for R1's 302 (the cap is also the edge wait's
# budget in test mode); it does not shorten any op program's own budget.
POLL_CAP_MS = 40000
STATUS_READ = " 018e 0122 2 "     # reg 0x101, the status word, in a script line


def _replay(script_text, *extra, debug_level=4):
    """Run "magprobe replay" on a script; the parsed output."""
    probe = mag._build_probe()
    if probe is None:
        raise unittest.SkipTest(mag._skip_reason)
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "script.txt"
        script.write_text(script_text)
        env = dict(os.environ, HOME=tmp, SANE_GL126_MAGAZINE_MARK=str(Path(tmp) / "mark"),
                   SANE_GL126_TEST_POLL_CAP_MS=str(POLL_CAP_MS),
                   SANE_DEBUG_GENESYS=str(debug_level))
        r = subprocess.run([probe, "replay", str(script), *extra], capture_output=True,
                           text=True, env=env, timeout=180)
    assert r.returncode == 0, f"probe exit {r.returncode}: {r.stdout[-2000:]}\n{r.stderr[-2000:]}"
    out = {"statuses": [], "xfers": [], "diverge": [], "before": [], "skipped": [],
           "log": r.stderr}
    for line in r.stdout.splitlines():
        tag, _, rest = line.partition(" ")
        if tag == "STATUS":
            code, _, msg = rest.partition(" ")
            out["statuses"].append((int(code), msg.strip()))
        elif tag == "XFER":
            out["xfers"].append(rest)
        elif tag == "REPLAY":
            out["replay"] = {k: int(v) for k, v in (f.split("=") for f in rest.split())}
        elif tag == "DIVERGE":
            out["diverge"].append(rest)
        elif tag == "BEFORE":
            out["before"].append(rest)
        elif tag == "SKIPPED":
            out["skipped"].append(rest)
        elif tag == "STARTSTATUS":
            code, _, msg = rest.partition(" ")
            out["start"] = (int(code), msg.strip())
        elif tag == "CHUNKS":
            n, done, want, *msg = rest.split(" ", 3)
            out["chunks"] = (int(n), int(done), int(want), " ".join(msg))
        elif tag in ("TEXT", "MARK"):
            out[tag.lower()] = rest
    return out


def _expand(xfers):
    """The transfer log with its "REPEAT 2 xN" lines written out."""
    out = []
    for line in xfers:
        if line.startswith("REPEAT 2 x"):
            out += out[-2:] * int(line[len("REPEAT 2 x"):])
        else:
            out.append(line)
    return out


def _polls(log, pattern):
    m = re.search(pattern + r", (\d+) polls", log)
    assert m, pattern
    return int(m.group(1))


# cold_init op 3: the best-effort ready poll R1 shows 330 times (it ran
# out of its 1.5 s on the unit); here it spins the same 1.5 s against a
# no-op sleep and every read past the 330th is held.
OP3 = r"cold_init op 3 poll: first 0x48 last 0x48"
OP3_RECORDED = 330


def _op3_held(log):
    return _polls(log, OP3) - OP3_RECORDED


def _r1_lines():
    return R1.read_text().splitlines()


def _with(lines, index, new):
    return lines[:index] + [new] + lines[index + 1:]


def _op24_status_line(lines):
    """LOAD's engaging-feed completion read: the first status read after
    the feed's trigger (reg 0x0f <- 0x01), the first trigger after LOAD's
    write reg 0x32 <- 0x1d (the last such write; the cold start makes the
    same write earlier). The status read before the trigger is op 4's."""
    load = max(i for i, l in enumerate(lines) if l.endswith(" 0083 0000 2 321d"))
    trigger = next(i for i in range(load, len(lines)) if lines[i].endswith(" 0083 0000 2 0f01"))
    return next(i for i in range(trigger, len(lines)) if STATUS_READ in lines[i])


def _set_run(line, count):
    """A run line with its xN replaced (or dropped, for 1)."""
    tok = [t for t in line.split() if not t.startswith(("x", "every="))]
    return " ".join(tok + ([f"x{count}", "every=100"] if count > 1 else []))


SKIPPED_ENUMERATION = [  # the kernel's enumeration, before the backend ran
    "0 CI 80 06 0100 0000 18", "1 CI 80 06 0200 0000 9", "2 CI 80 06 0200 0000 39"]


class ReplayTests(unittest.TestCase):

    def assert_loaded(self, r):
        self.assertEqual(r["statuses"], [(mag.SANE_STATUS_GOOD, "")])
        self.assertTrue(r["mark"].startswith("present loaded "), r["mark"])
        self.assertIn("gl126: magazine loaded (reg 0x101 = 0xdc)", r["log"])

    def test_r1_replays_to_loaded_with_no_divergence(self):
        """(a) The backend's own successful load on the owner's unit,
        replayed through the backend: every transfer it sends is in the
        recording, in the recorded order, and the recording is used up
        but for the kernel's enumeration."""
        r = _replay(R1.read_text())
        self.assert_loaded(r)
        self.assertEqual(r["diverge"], [])
        self.assertEqual(r["replay"]["divergences"], 0)
        self.assertEqual(r["replay"]["tail"], 0)
        self.assertEqual([s[:len(p)] for s, p in zip(r["skipped"], SKIPPED_ENUMERATION)],
                         SKIPPED_ENUMERATION)
        self.assertEqual(r["replay"]["skipped"], 3)
        # Its poll counts are the unit's own (r1-backend.log), but for
        # cold_init op 3, the best-effort ready poll that ran out of time
        # on the unit (330 polls in 1.5 s) and here spins the same 1.5 s
        # against a no-op sleep.
        for want in ("magazine cold_init op 43 poll: first 0xd9 last 0xf8, 423 polls",
                     "magazine jog op 83 poll: first 0xf9 last 0xf8, 2 polls",
                     "magazine edge seen (302 polls, ~30200 ms, last reg 0x101 0xf8)",
                     "magazine after the settle: reg 0x101 = 0xf8, reg 0x01 = 0x22, "
                     "regs 0x3b/0x3c = 0x00/0x00",
                     "magazine load op 24 poll: first 0xf4 last 0xf4, 1 polls",
                     "magazine load op 38 poll: first 0xdc last 0xdc, 1 polls",
                     "magazine load op 0 read reg 0x32: 0x1f (captured 0x1f)"):
            self.assertIn(want, r["log"])
        # And nothing else was held: no read the unit did not see either.
        self.assertEqual(r["replay"]["held"], _op3_held(r["log"]))

    def test_r3_alone_never_shows_the_edge(self):
        """(b, part 1) The Python driver's load is not comparable as
        recorded: the driver does not poll while the operator reseats the
        magazine (6.2 s of silence after the jog, the status word f855
        before and after), so the backend's edge wait, held at the last
        recorded f855, sees no clear read and times out. Nothing is
        loaded; the LOAD part of the recording is never reached."""
        r = _replay(R3.read_text())
        self.assertEqual(r["statuses"], [(mag.SANE_STATUS_GOOD, "")])
        self.assertIn("edge wait timed out (400 polls, ~40000 ms, first 0xf8 last 0xf8, "
                      "saw a clear read: no)", r["log"])
        self.assertTrue(r["mark"].startswith("present released "), r["mark"])
        self.assertNotIn("magazine load op", r["log"])
        self.assertGreater(r["replay"]["tail"], 60)    # R3's whole LOAD, never reached

    def test_r3_replies_load_once_the_edge_is_spliced_in(self):
        """(b, part 2) R3 with the edge the driver never polled for
        spliced in after its jog (two clear reads, five present, the
        settle, and the reg 0x101/0x01/0x3b/0x3c reads the backend makes
        after it, with R1's values): the backend loads on the Python
        driver's replies. What remains is the two flows' known order
        differences, each a reply of the same request taken out of the
        recorded order, never a request R3 lacks:
          - the driver's status read before its first EEPROM read (#5);
          - reg 0x32: the driver reads it three times before LOAD's
            first write, the backend once (r1-vs-r3-analysis.md Q1), so
            the backend's later reg 0x32 reads take the spare earlier
            replies."""
        lines = R3.read_text().splitlines()
        jog_end = next(i for i, l in enumerate(lines) if l.startswith("1156 CI"))
        splice = ["# splice: the reseat the driver did not poll for, R1's values",
                  "1156 CI c0 04 018e 0122 2 f055 x2 every=100",
                  "1156 CI c0 04 018e 0122 2 f855 x5 every=100",
                  "GAP 600",
                  "1156 CI c0 04 018e 0122 2 f855",
                  "1156 CI c0 04 008e 0122 2 2255",
                  "1156 CI c0 04 008e 3b22 2 0055",
                  "1156 CI c0 04 008e 3c22 2 0055"]
        r = _replay("\n".join(lines[:jog_end + 1] + splice + lines[jog_end + 1:]) + "\n")
        self.assert_loaded(r)
        self.assertIn("magazine load op 24 poll: first 0xf4 last 0xf4, 1 polls", r["log"])
        self.assertTrue(r["diverge"])
        for d in r["diverge"]:
            self.assertTrue(d.startswith("out of the recorded order"), d)
            self.assertTrue(d.endswith(("CI c0 04 008e 3222 2", "CI c0 04 018e 0122 2")), d)
        self.assertTrue(r["diverge"][0].startswith("out of the recorded order (recorded #5,"))
        # Left over: the enumeration, the driver's SET_CONFIGURATION and
        # interrupt listens, and its last reg 0x32/status reads.
        self.assertIn("4 CO 00 09 0001 0000 0 -", r["skipped"])
        self.assertTrue([s for s in r["skipped"] if " II 83 " in s])

    def test_op24_stuck_status_reproduces_the_owners_failure(self):
        """(c) R1 with one reply changed: LOAD's engaging-feed completion
        reads 0xfc55 (bit 0x08 still set) instead of 0xf455, as on the
        owner's unit 2026-11-05. The backend fails exactly as there: op
        24, mask 0xfb want 0xf0, first and last 0xfc, after its own
        ~4.8 s budget, nothing further sent; the session is then blocked.
        The second press's reg 0x01 read is appended (the unit would
        answer 0x22): R1 has no second press."""
        lines = _r1_lines()
        k = _op24_status_line(lines)
        self.assertTrue(lines[k].endswith(" f455"), lines[k])
        lines = _with(lines, k, lines[k][:-4] + "fc55")
        lines.append("9999 CI c0 04 008e 0122 2 2255")
        r = _replay("\n".join(lines) + "\n", "twice")

        self.assertEqual(len(r["statuses"]), 2, r["statuses"])
        (st1, msg1), (st2, msg2) = r["statuses"]
        self.assertEqual(st1, mag.SANE_STATUS_DEVICE_BUSY)
        for want in ("the magazine load did not reach the state it must reach "
                     "(the engaging feed's completion)",
                     "PollMasked at op 24 (mask 0xfb want 0xf0)",
                     "first 0xfc, last 0xfc, nothing further sent"):
            self.assertIn(want, msg1)
        self.assertEqual(st2, mag.SANE_STATUS_INVAL)
        self.assertIn("failed earlier in this session", msg2)
        self.assertIn("Nothing was written", msg2)
        self.assertTrue(r["mark"].startswith("present failed "), r["mark"])

        # The new diagnostic lines (logging commit 1f33bcf06).
        for want in ("magazine edge seen (302 polls, ~30200 ms, last reg 0x101 0xf8)",
                     "magazine after the settle: reg 0x101 = 0xf8, reg 0x01 = 0x22, "
                     "regs 0x3b/0x3c = 0x00/0x00",
                     "magazine load: no poll completed before op 24",
                     "magazine load op 24 poll timed out: first 0xfc last 0xfc",
                     "magazine load: last status words read by Read ops (not polls) "
                     "before op 24: reg 0x100 = 0xf0 (op 3), reg 0x101 = 0xf8 (op 4)",
                     "magazine load op 0 read reg 0x32: 0x1f (captured 0x1f)"):
            self.assertIn(want, r["log"])

        # On the wire: the feed's trigger, the 1.6 s sleep, then only the
        # stuck status read (polled), then the second press's one read.
        xfers = _expand(r["xfers"])
        trigger = max(i for i, x in enumerate(xfers) if x.endswith("len=2: 0f 01"))
        tail = xfers[trigger + 1:]
        self.assertEqual(tail[:2], ["CTRL IN req=0x0c val=0x008e idx=0x0020 len=1: 55",
                                    "SLEEP 1613000 us"])
        polled = set(tail[2:-1])
        self.assertEqual(polled, {"CTRL IN req=0x04 val=0x018e idx=0x0122 len=2: fc 55",
                                  "SLEEP 4000 us"})
        self.assertEqual(tail[-1], "CTRL IN req=0x04 val=0x008e idx=0x0122 len=2: 22 55")
        self.assertEqual(r["diverge"], [])
        op24 = _polls(r["log"], r"load op 24 poll timed out: first 0xfc last 0xfc")
        self.assertEqual(r["replay"]["held"], _op3_held(r["log"]) + op24 - 1)
        # The rest of LOAD was never sent: skipped (not tail), since the
        # appended reg 0x01 reply at the very end was consumed.
        self.assertGreater(r["replay"]["skipped"], 40)
        self.assertEqual(r["replay"]["tail"], 0)

    def test_load_starts_one_second_after_the_first_present_read(self):
        """(d) The sensor edge's timing, as designed (no behaviour change
        claimed): whatever the reseat looked like -- how long the sensor
        read present after the jog, how many clear reads -- LOAD's first
        write comes after exactly five present reads 100 ms apart and the
        600 ms settle, 1.0 s from the first present read after the clear
        ones (R1 measured 1.0058 s on the unit). Varied on R1's edge run:
        present reads before the clear ones (281 recorded) and clear reads
        (16 recorded)."""
        lines = _r1_lines()
        lead = next(i for i, l in enumerate(lines) if l.startswith("3902 CI"))
        clear = next(i for i, l in enumerate(lines) if l.startswith("4183 CI"))
        self.assertTrue(lines[lead].endswith("f855 x281 every=100"), lines[lead])
        self.assertTrue(lines[clear].endswith("f055 x16 every=100"), lines[clear])
        for present_before, clears in ((281, 16), (1, 16), (30, 2), (281, 2)):
            with self.subTest(present_before=present_before, clears=clears):
                v = _with(_with(lines, lead, _set_run(lines[lead], present_before)),
                          clear, _set_run(lines[clear], clears))
                r = _replay("\n".join(v) + "\n")
                self.assert_loaded(r)
                self.assertEqual(r["replay"]["divergences"], 0)
                self.assertEqual(r["replay"]["held"], _op3_held(r["log"]))
                polls = present_before + clears + 5
                self.assertIn(f"magazine edge seen ({polls} polls, ~{polls * 100} ms", r["log"])
                xfers = _expand(r["xfers"])
                load = max(i for i, x in enumerate(xfers) if x.endswith("len=2: 32 1d"))
                last_clear = max(i for i, x in enumerate(xfers[:load])
                                 if x.endswith("idx=0x0122 len=2: f0 55"))
                window = xfers[last_clear + 1:load]
                sleeps = [int(x.split()[1]) for x in window if x.startswith("SLEEP")]
                self.assertEqual(sleeps, [100000] * 5 + [600000])
                present = [x for x in window if x.endswith("idx=0x0122 len=2: f8 55")]
                self.assertEqual(len(present), 6)   # five for the edge, one after the settle
                first_present = window.index(present[0])
                self.assertEqual(sum(int(x.split()[1]) for x in window[first_present:]
                                     if x.startswith("SLEEP")), 1000000)


def _poll_lines(log):
    """The backend's "op N poll: first X last Y, N polls" lines, in order,
    its host-side ms dropped, as tests/golden/replay_s1_*_polls.txt has them."""
    return [re.sub(r"^\[[^\]]*\] \[genesys\] ", "", re.sub(r", \d+ ms$", "", l))
            for l in log.splitlines() if "poll:" in l]


class ScanReplayTests(unittest.TestCase):
    """The scan of the 2026-11-06 backend capture (s1-scanner.pcap), run
    through the backend's own calibration and scan flow in test mode:
    sane_start (offset, gain and shading calibration, positioning, the
    scan pass's setup), the scan pass's chunk reads, the park.

    What the recording cannot give is the image data (bulk IN payloads are
    stripped from the scripts and never kept). Genesys's test mode answers
    a bulk IN with zeroes, and calibration cannot get through zeroes (the
    offset and gain codes are computed from the frames, the dark frame is
    refused as a residual). So the calibration frames are stood in for by
    constants with the means the backend logged for them (the fills file),
    and what is proved is the CONTROL PATH: every control transfer the
    backend sends is in the recording, in its order, with the bytes it
    holds, and every bulk write by its length and position. The bulk
    OUTs' bytes are not in the recording (the shading tables in them are
    computed from the calibration frames' per-pixel data, which a
    constant cannot reproduce)."""

    def run_scan(self, dpi, frame=4, fills=True):
        script, fills_path, _ = S1_SCANS[dpi]
        extra = ["scan", str(dpi), str(frame)] + ([str(fills_path)] if fills else [])
        return _replay(script.read_text(), *extra)

    def check_scan(self, dpi, chunks, chunk_bytes, gain_codes, feedl):
        r = self.run_scan(dpi)
        log = r["log"]
        self.assertEqual(r["start"], (mag.SANE_STATUS_GOOD, ""))
        # Every raw chunk of the scan pass was read, through the backend's
        # read_image_chunk_usb(), then the park ran.
        self.assertEqual(r["chunks"], (chunks, chunks * chunk_bytes, chunks * chunk_bytes, ""))
        self.assertIn(f"scan pass complete, {chunks * chunk_bytes} raw bytes read", log)
        # The chunk size is the backend's own figure, not the script's.
        self.assertIn(f"{chunk_bytes} B per chunk", log)
        self.assertIn("gl126: parked (2 waits recorded)", log)
        # The calibration values computed from the stand-in frames are the
        # unit's, to the code.
        for ch, code in (("R", "0x010a"), ("G", "0x0109"), ("B", "0x010a")):
            self.assertTrue(re.search(rf"gl126: offset {ch}: .*, code {code}$", log, re.M),
                            f"offset {ch} code {code}")
        self.assertIn(gain_codes, log)
        self.assertIn(f"positioning to frame 4 (FEEDL {feedl})", log)
        # The control path, whole: nothing held, nothing sent that the
        # recording lacks, nothing recorded left unsent. The bulk OUTs, the
        # shading tables among them (8 a scan), are matched by length and
        # position alone: their bytes are not in the recording.
        self.assertEqual(r["replay"]["held"], 0)
        self.assertEqual(r["replay"]["divergences"], 0)
        self.assertEqual(r["diverge"], [])
        self.assertEqual(r["replay"]["skipped"], 0)
        self.assertEqual(r["skipped"], [])
        # The recording's last two entries are the two status reads of
        # sanei_genesys_wait_for_home() after the park (s600.log: "already
        # at home"); genesys's test mode returns from it before any read,
        # so the replayed backend never sends them (sane_close sends
        # nothing either).
        self.assertEqual(r["replay"]["tail"], 2)
        # Every poll the backend logs is the unit's: same op, same first
        # and last reply, same count (positioning 1783 and the park 2316
        # included).
        want = [l for l in S1_SCANS[dpi][2].read_text().splitlines() if not l.startswith("#")]
        self.assertEqual(_poll_lines(log), want)
        return r

    def test_600_dpi_frame_4_scan_replays_end_to_end(self):
        r = self.check_scan(
            600, 19, 515088,
            "gl126: gain codes R 0x2a G 0x21 B 0x26 after 1 measurement(s)", 38809)
        self.assertEqual(r["replay"]["consumed"], 5577)

    def test_3600_dpi_frame_4_scan_replays_end_to_end(self):
        r = self.check_scan(
            3600, 231, 519156,
            "gl126: gain codes R 0x2b G 0x20 B 0x27 after 1 measurement(s)", 38806)
        self.assertEqual(r["replay"]["consumed"], 6414)

    def test_without_the_stand_ins_the_calibration_does_not_get_through(self):
        """The fills are needed, not decoration: on zeroes the dark frame
        is refused (or the white line never reads as data), nothing
        further is sent, and the recording is mostly never reached."""
        r = self.run_scan(600, fills=False)
        self.assertNotEqual(r["start"][0], mag.SANE_STATUS_GOOD)
        self.assertEqual(r["chunks"][0], 0)
        self.assertGreater(r["replay"]["tail"], 1000)

    def test_a_changed_dark_frame_changes_the_offset_codes_and_is_caught(self):
        """Mutation: dark_b's means 1000 counts lower, so the slope the offset
        code comes from changes (a shift of both would not). The backend
        computes another offset code, writes another AFE register value,
        and the matching flags the write the recording does not have."""
        with tempfile.TemporaryDirectory() as tmp:
            fills = Path(tmp) / "fills.txt"
            lines = S1_SCANS[600][1].read_text().splitlines()
            lines = [re.sub(r"^(4386) (\d+) (\d+) (\d+) ",
                            lambda m: f"{m[1]} {int(m[2]) - 1000} {int(m[3]) - 1000} "
                                      f"{int(m[4]) - 1000} ", l) for l in lines]
            fills.write_text("\n".join(lines) + "\n")
            r = _replay(S1_SCANS[600][0].read_text(), "scan", "600", "4", str(fills))
        self.assertTrue([d for d in r["diverge"] if "CO 40 04 0083 0000 6 51" in d], r["diverge"])
        self.assertGreater(r["replay"]["divergences"], 0)

    def test_the_load_of_the_same_capture_loads_with_no_divergence(self):
        r = _replay(S1_LOAD.read_text())
        self.assert_loaded_s1(r)
        self.assertEqual(r["replay"]["divergences"], 0)
        self.assertEqual(r["replay"]["tail"], 0)
        self.assertEqual(r["replay"]["skipped"], 3)      # the kernel's enumeration
        # cold_init op 3's best-effort poll, as in R1 (see OP3), but here
        # the recording holds 344 of its reads.
        self.assertEqual(r["replay"]["held"], _polls(r["log"], OP3) - 344)

    def assert_loaded_s1(self, r):
        self.assertEqual(r["statuses"], [(mag.SANE_STATUS_GOOD, "")])
        self.assertTrue(r["mark"].startswith("present loaded "), r["mark"])
        self.assertIn("magazine load op 24 poll: first 0xf4 last 0xf4, 1 polls", r["log"])
        self.assertIn("magazine load op 38 poll: first 0xd5 last 0xdc, 34 polls", r["log"])

    def test_the_eject_of_the_same_capture_replays_with_no_divergence(self):
        r = _replay(S1_EJECT.read_text(), "eject")
        self.assertEqual(r["statuses"], [(mag.SANE_STATUS_GOOD, "")])
        self.assertTrue(r["mark"].startswith("present ejected "), r["mark"])
        self.assertEqual((r["replay"]["divergences"], r["replay"]["skipped"],
                          r["replay"]["tail"], r["replay"]["held"]), (0, 0, 0, 0))
        self.assertIn("magazine eject op 9 poll: first 0xc9 last 0xe8, 213 polls", r["log"])


if __name__ == "__main__":
    suites = [unittest.defaultTestLoader.loadTestsFromTestCase(c)
              for c in (ConverterTests, GoldenTests, ReplayTests, ScanReplayTests)]
    result = unittest.TextTestRunner(stream=sys.stdout, verbosity=1).run(
        unittest.TestSuite(suites))
    print(f"{result.testsRun - len(result.failures) - len(result.errors)} tests passed")
    sys.exit(0 if result.wasSuccessful() else 1)
