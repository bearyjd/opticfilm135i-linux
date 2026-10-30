"""Offline capture-contract tests; no USB device discovery or hardware access."""
import hashlib
import json
import itertools
import time
from pathlib import Path

import pytest
import usb.core

from of135i import cli, safety
from of135i.device import Scanner
from of135i.usbio import UsbIo
from test_safety import FakeUsbDevice


class StartupDevice(FakeUsbDevice):
    def __init__(self, *, status=b'\xf0\x55', **kwargs):
        super().__init__(**kwargs)
        self.entry_status = status
        self.motor_reads = {}
        self.motor_override = None
        self.reg32_reads = iter((0x1f,0x05,0x1f,0x1f,0x1f,0x1d))
        self.reg31_reads = 0

    def ctrl_transfer(self, bm, br, wv=0, wi=0, data_or_wLength=None, timeout=None):
        result = super().ctrl_transfer(bm, br, wv, wi, data_or_wLength, timeout)
        if bm == 0xc0 and wv == 0x018e and wi == 0x0122:
            if not self.pulses:
                return self.entry_status if not self.out_count else b'\xd0\x55'
            n = self.motor_reads.get(self.pulses, 0)
            self.motor_reads[self.pulses] = n + 1
            if self.motor_override:
                return self.motor_override(self.pulses, n)
            return (b'\xd1\x55' if self.pulses == 1 else b'\xd9\x55') if n == 0 else (b'\xd8\x55' if self.pulses == 1 else b'\xf8\x55')
        if bm == 0xc0 and br == 0x0c and wv == 0x008e:
            return b'\x02' if wi == 0x18 else b'\x55'
        if bm == 0xc0 and br == 4 and wv == 0x008e and wi == 0x3122:
            self.reg31_reads += 1
            return b'\xfc\x55' if self.reg31_reads == 1 else b'\x7c\x55'
        if bm == 0xc0 and br == 4 and wv == 0x008e and wi == 0x3222:
            return bytes((next(self.reg32_reads),0x55))
        if bm == 0xc0 and br == 4 and wv == 0x018e and wi == 0x22 and not self.pulses:
            return b'\x70\x55'
        return result


@pytest.fixture(autouse=True)
def no_wait(monkeypatch):
    ticks = itertools.count()
    monkeypatch.setattr(time,'sleep',lambda _: None)
    monkeypatch.setattr(time,'monotonic',lambda:next(ticks)*0.05)


def run(fake):
    scanner = Scanner(UsbIo(fake))
    scanner.vendor_startup(experimental=True)
    return scanner


def test_complete_ordered_capture_write_transcript():
    fake = StartupDevice()
    scanner = run(fake)
    expected = json.loads((Path(__file__).parent / 'fixtures/vendor_startup_writes.json').read_text())['writes']
    actual = []
    for event in fake.wire_log:
        if event['t'] == 'cw':
            actual.append(dict(kind='cw', bm=0x40, **{k:event[k] for k in ('br','wv','wi')}, data=event['data'].hex()))
        elif event['t'] == 'bo':
            actual.append(dict(kind='bo', length=len(event['data']), sha256=hashlib.sha256(event['data']).hexdigest()))
    assert actual == [{k:v for k,v in event.items() if k != 'packet'} for event in expected]
    assert fake.pulses == scanner.session.execute_pulses == 4
    assert sum(event['kind'] == 'bo' for event in actual) == 8
    assert not scanner._vendor_open
    # Selectors each immediately precede ACK then the 64-byte read window.
    selectors = [i for i,e in enumerate(fake.wire_log) if e['t']=='cw' and e['wv']==0x8b and e['wi']==0xb]
    assert [fake.wire_log[i]['data'].hex() for i in selectors] == ['00000100','40000100']
    for i in selectors:
        assert fake.wire_log[i+2] == dict(t='cr',br=4,wv=0x8a,wi=0xf,length=64)
    assert fake.events[0] == 'ctrl_in reg01'


@pytest.mark.parametrize('reg01', [0,0x23,b'',b'\x22\x00',RuntimeError('unreadable')])
def test_entry_reg01_refusal_writes_nothing(reg01):
    fake = StartupDevice(reg01=reg01)
    with pytest.raises(safety.SafetyError):
        run(fake)
    assert fake.touched == 0


@pytest.mark.parametrize('live', [0x00,0x01,b'\x22',b'\x22\x00',usb.core.USBError('reg01 failed')])
def test_cached_armed_state_requires_fresh_live_reg01(live):
    fake = StartupDevice()
    scanner = Scanner(UsbIo(fake))
    assert scanner.check_start_state() is safety.StartState.IDLE
    if isinstance(live, int):
        fake.regs[0x01] = live
    else:
        fake.reg01_reply = live
    with pytest.raises((safety.SafetyError,usb.core.USBError)):
        scanner.vendor_startup(experimental=True)
    assert fake.touched == fake.out_count == fake.pulses == 0
    assert scanner.session.writes_attempted == scanner.session.execute_pulses == 0
    assert scanner.session.state is safety.SessionState.FAILED
    # The failed live check is terminal, even if the device later looks ready.
    fake.regs[0x01] = 0x22
    fake.reg01_reply = None
    before = (fake.touched,fake.in_count)
    with pytest.raises(safety.SessionFailedError): scanner.vendor_startup(experimental=True)
    with pytest.raises(safety.SessionFailedError): scanner.eject()
    assert (fake.touched,fake.in_count) == before


@pytest.mark.parametrize('failure', ['late','at_deadline','before_read','usb_timeout'])
def test_motor_deadline_stops_before_any_later_write(monkeypatch,failure):
    clock = [0.0]
    timeouts = []

    class TimedDevice(StartupDevice):
        def ctrl_transfer(self,bm,br,wv=0,wi=0,data_or_wLength=None,timeout=None):
            if bm == 0xc0 and wv == 0x018e and wi == 0x0122 and self.pulses:
                timeouts.append(timeout)
            return super().ctrl_transfer(bm,br,wv,wi,data_or_wLength,timeout)

    fake = TimedDevice()
    scanner = Scanner(UsbIo(fake))
    monkeypatch.setattr(time,'monotonic',lambda:clock[0])
    if failure == 'before_read':
        def sleep(seconds):
            if seconds == 0.004: clock[0] = 22.0
        monkeypatch.setattr(time,'sleep',sleep)

    def status(pulse,n):
        assert pulse == 1, 'a later move started after the deadline'
        if n == 0:
            clock[0] = 0.1
            return b'\xd1\x55'
        if failure == 'usb_timeout':
            raise usb.core.USBTimeoutError('motor status read timed out')
        clock[0] = 21.0 if failure == 'at_deadline' else 22.0
        return b'\xd8\x55'
    fake.motor_override = status
    with pytest.raises((safety.StrictPollTimeoutError,usb.core.USBTimeoutError)):
        scanner.vendor_startup(experimental=True)
    assert fake.pulses == 1
    assert fake.out_log[-1]['data'] == b'\x0f\x01'
    assert scanner.session.state is safety.SessionState.FAILED
    assert len(timeouts) == (1 if failure == 'before_read' else 2)
    assert all(isinstance(ms,int) and 0 < ms <= 21000 for ms in timeouts)
    if len(timeouts) == 2: assert timeouts[1] <= 20900
    before = fake.touched
    with pytest.raises(safety.SessionFailedError): scanner.vendor_startup(experimental=True)
    with pytest.raises(safety.SessionFailedError): scanner.eject()
    assert fake.touched == before


@pytest.mark.parametrize('status',[b'\xf8\x55',b'\xf0\x00',b'',b'\xf0',b'\xf0\x55\x00'])
def test_exact_prewrite_status_and_ack_required(status):
    fake = StartupDevice(status=status)
    with pytest.raises(safety.SafetyError):
        run(fake)
    assert fake.touched == 0


def test_opt_in_required_and_failed_session_terminal():
    fake = StartupDevice()
    scanner = Scanner(UsbIo(fake))
    with pytest.raises(safety.OperationNotAllowedError):
        scanner.vendor_startup()
    assert fake.touched == fake.in_count == 0
    scanner.session.fail(RuntimeError('earlier failure'))
    with pytest.raises(safety.SessionFailedError):
        scanner.vendor_startup(experimental=True)
    assert fake.touched == fake.in_count == 0


@pytest.mark.parametrize('prior',['readonly','refused','operation','write','active'])
def test_requires_fresh_writing_session(prior):
    fake = StartupDevice()
    scanner = Scanner(UsbIo(fake,readonly=prior=='readonly'))
    if prior == 'refused': scanner.session.refuse('earlier refusal')
    if prior in ('operation','write','active'):
        scanner.check_start_state()
        if prior == 'active': scanner.session.operations.append('other')
        else:
            with scanner._operation('previous'):
                if prior == 'write': scanner.io.dev.ctrl_transfer(0x40,4,0x83,0,b'\x09\x00')
    before = fake.touched
    with pytest.raises(safety.SafetyError): scanner.vendor_startup(experimental=True)
    assert fake.touched == before


@pytest.mark.parametrize('phase',[1,2,3,4])
@pytest.mark.parametrize('failure',['busy','stale','wrongdone','badack','interrupt','usb','short'])
def test_phase_failure_stops_all_later_writes(phase,failure):
    fake = StartupDevice()
    scanner = Scanner(UsbIo(fake))
    if failure == 'short':
        fake.fault = lambda e: 1 if e['kind']=='ctrl_out' and e['pulse'] and e['pulses_so_far']==phase-1 else None
    else:
        def status(pulse,n):
            if pulse != phase:
                return (b'\xd1\x55' if pulse==1 else b'\xd9\x55') if n==0 else (b'\xd8\x55' if pulse==1 else b'\xf8\x55')
            if failure == 'interrupt': raise KeyboardInterrupt()
            if failure == 'usb': raise usb.core.USBError('USB read failure')
            if failure == 'badack': return b'\xd9\x00'
            if failure == 'busy': return b'\xd9\x55'
            if failure == 'wrongdone': return b'\xd9\x55' if n==0 else b'\xf0\x55'
            return b'\xd8\x55' if pulse==1 else b'\xf8\x55'
        fake.motor_override = status
    with pytest.raises((safety.SafetyError,usb.core.USBError,KeyboardInterrupt)):
        scanner.vendor_startup(experimental=True)
    assert scanner.session.state is safety.SessionState.FAILED
    assert fake.pulses == phase-(failure=='short')
    # No disable, recovery or later-phase write can follow the failed motor wait.
    if failure != 'short':
        assert fake.out_log[-1]['data'] == b'\x0f\x01'
    before = fake.touched
    with pytest.raises(safety.SessionFailedError): scanner.vendor_startup(experimental=True)
    with pytest.raises(safety.SessionFailedError): scanner.eject()
    assert fake.touched == before


def test_missing_cli_optin_never_opens(monkeypatch,capsys):
    def forbidden(): raise AssertionError('device must not open')
    monkeypatch.setattr(Scanner,'open',forbidden)
    assert cli.main(['vendor-startup']) == 2
    assert '--experimental' in capsys.readouterr().err


def test_cli_optin_completes_only_startup(monkeypatch,capsys):
    fake = StartupDevice()
    scanner = Scanner(UsbIo(fake))
    monkeypatch.setattr(Scanner,'open',lambda:scanner)
    assert cli.main(['vendor-startup','--experimental']) == 0
    assert fake.pulses == 4
    assert 'mechanical position unverified' in capsys.readouterr().out
    assert scanner.session.operations_started == 1


def test_stale_done_then_captured_busy_then_done_cannot_skip_wait():
    fake = StartupDevice()
    def status(pulse,n):
        done = b'\xd8\x55' if pulse==1 else b'\xf8\x55'
        if n == 1: return b'\xd1\x55' if pulse==1 else b'\xf9\x55'
        return done
    fake.motor_override = status
    run(fake)
    assert all(fake.motor_reads[pulse] >= 3 for pulse in range(1,5))


def test_wrong_traverse_done_in_same_class_never_starts_jog():
    fake = StartupDevice()
    fake.motor_override = lambda pulse,n: b'\xd1\x55' if n==0 else b'\xd9\x55'
    scanner = Scanner(UsbIo(fake))
    with pytest.raises(safety.StrictPollTimeoutError): scanner.vendor_startup(experimental=True)
    assert fake.pulses == 1
    assert fake.out_log[-1]['data'] == b'\x0f\x01'


class CorruptedRead(StartupDevice):
    def __init__(self,wv,wi,occurrence,reply):
        super().__init__()
        self.target = (wv,wi,occurrence)
        self.seen = 0
        self.reply = reply

    def ctrl_transfer(self,bm,br,wv=0,wi=0,data_or_wLength=None,timeout=None):
        got = super().ctrl_transfer(bm,br,wv,wi,data_or_wLength,timeout)
        if bm & 0x80 and (wv,wi) == self.target[:2]:
            self.seen += 1
            if self.seen == self.target[2]:
                if isinstance(self.reply, BaseException):
                    raise self.reply
                return self.reply
        return got


# A single malformed reply must fail, even if a retry would appear valid.
@pytest.mark.parametrize('wv,wi,n,reply,pulses',[
    (0x8a,0x26fe,1,b'',0),       # chip-ID length, value itself varies by unit
    (0x8e,0x3722,1,b'\x34',0),  # informational register read is still ACK-bearing
    (0x8e,0x3722,1,b'\x34\x00',0),
    (0x8e,0x20,1,b'\x00',0),    # one bad ACK cannot be retried into success
    (0x8a,0xf,1,bytes(63),0),    # EEPROM window length
    (0x18e,0x122,6,b'\xd0\x00',0), # pre-traverse status poll after entry/config reads
    (0x8e,0x18,1,b'',0),        # slope upload completion length
])
def test_malformed_control_read_terminates_without_later_motor(wv,wi,n,reply,pulses):
    fake = CorruptedRead(wv,wi,n,reply)
    scanner = Scanner(UsbIo(fake))
    with pytest.raises(safety.SafetyError): scanner.vendor_startup(experimental=True)
    assert scanner.session.state is safety.SessionState.FAILED
    assert fake.pulses == pulses


@pytest.mark.parametrize('n',[1,2])
@pytest.mark.parametrize('reply',[b'\x72\x55',b'\x70',b'\x70\x00'])
def test_both_live_engine_eligibility_reads_fail_closed(n,reply):
    fake = CorruptedRead(0x18e,0x22,n,reply)
    with pytest.raises(safety.SafetyError): run(fake)
    assert fake.pulses == 0


@pytest.mark.parametrize('wv,wi,n,reply,pulses',[
    (0x8e,0x3122,1,b'\xfe\x55',0),
    (0x8e,0x3122,2,b'\x7e\x55',0),
    (0x8e,0x3222,1,b'\x1d\x55',0),
    (0x8e,0x3222,2,b'\x07\x55',1),
    (0x8e,0x3222,3,b'\x1d\x55',2),
    (0x8e,0x3522,1,b'\xbb\x55',2),
    (0x8e,0x3222,5,b'\x1d\x55',2),
    (0x8e,0x3222,6,b'\x1f\x55',3),
])
def test_captured_read_modify_write_values_are_gated(wv,wi,n,reply,pulses):
    fake = CorruptedRead(wv,wi,n,reply)
    with pytest.raises(safety.SafetyError): run(fake)
    assert fake.pulses == pulses


@pytest.mark.parametrize('wv,wi,n,reply',[
    (0x8a,0x26fe,1,b'\x12'),
    (0x8e,0x3722,1,b'\x12\x55'),
    (0x18e,0x22,1,b'\x74\x55'),
    (0x18e,0x22,2,b'\x74\x55'),
])
def test_unrelated_register_bits_may_differ(wv,wi,n,reply):
    fake = CorruptedRead(wv,wi,n,reply)
    run(fake)
    assert fake.pulses == 4


@pytest.mark.parametrize('reply',[b'\xd8\x55',b'\xf8\x55',b'\x08\x55'])
def test_vendor_presence_preflight_bit08_set_stops_before_base_write(reply):
    # Occurrence 1 is the native entry gate; occurrence 2 is packet 6813,
    # the vendor software's presence-preflight read.
    fake = CorruptedRead(0x18e,0x122,2,reply)
    scanner = Scanner(UsbIo(fake))
    with pytest.raises(safety.SafetyError): scanner.vendor_startup(experimental=True)
    assert scanner.session.state is safety.SessionState.FAILED
    assert fake.pulses == 0
    # Only the captured chip-ID acknowledgement precedes the refusal.
    assert [(e['wv'],e['wi'],e['data']) for e in fake.out_log] == [(0x8b,0x26fe,b'\x00')]
    assert not any(e['wv'] == 0x83 for e in fake.out_log)
    before = fake.touched
    with pytest.raises(safety.SessionFailedError): scanner.vendor_startup(experimental=True)
    assert fake.touched == before


@pytest.mark.parametrize('failure', [usb.core.USBError('preflight failed'), KeyboardInterrupt()])
def test_vendor_presence_preflight_exception_stops_before_base_write(failure):
    fake = CorruptedRead(0x18e, 0x122, 2, failure)
    scanner = Scanner(UsbIo(fake))
    with pytest.raises(type(failure)):
        scanner.vendor_startup(experimental=True)
    assert scanner.session.state is safety.SessionState.FAILED
    assert fake.pulses == 0
    assert [(e['wv'], e['wi'], e['data']) for e in fake.out_log] == [(0x8b, 0x26fe, b'\x00')]
    before = (fake.touched, fake.in_count)
    with pytest.raises(safety.SessionFailedError):
        scanner.vendor_startup(experimental=True)
    assert (fake.touched, fake.in_count) == before


@pytest.mark.parametrize('reply',[b'\xd0\x55',b'\xf4\x55',b'\x70\x55'])
def test_vendor_presence_preflight_other_bits_informational(reply):
    fake = CorruptedRead(0x18e,0x122,2,reply)
    run(fake)
    assert fake.pulses == 4


@pytest.mark.parametrize('reply',[b'\xf0',b'\xf0\x00',b''])
def test_vendor_presence_preflight_malformed_fails_closed(reply):
    fake = CorruptedRead(0x18e,0x122,2,reply)
    with pytest.raises(safety.SafetyError): run(fake)
    assert fake.pulses == 0
    assert not any(e['wv'] == 0x83 for e in fake.out_log)


def test_presence_preflight_table_invariant_checked_before_any_write(monkeypatch):
    from of135i import tables_startup
    monkeypatch.setattr(tables_startup,'PRESENCE_PREFLIGHT_INDEX',4)  # base write 6825
    fake = StartupDevice()
    with pytest.raises(safety.SafetyError): run(fake)
    assert fake.touched == 0 and fake.pulses == 0


def test_startup_tables_keep_vendor_predicate_order():
    from of135i import tables_startup as ts
    ops = ts.CONFIGURE.ops
    pre = ops[ts.PRESENCE_PREFLIGHT_INDEX]
    assert (pre.kind,pre.wv,pre.wi,pre.length,pre.resp) == ('cr',0x18e,0x122,2,b'\xf0\x55')
    assert [(o.kind,o.wv,o.wi) for o in ops[:3]] == [('cr',0x8a,0x26fe),('cw',0x8b,0x26fe),('poll',0x8e,0x20)]
    assert ops[4].kind == 'cw' and ops[4].wv == 0x83 and ops[4].data[:2] == b'\x01\x22'
    tr = ts.TRAVERSE.ops
    home = next(i for i,o in enumerate(tr) if o.kind=='cw' and o.data == b'\x32\x1d')
    feed = next(i for i,o in enumerate(tr) if o.kind=='cw' and o.data == ts.TRAVERSE_FEED)
    between = [(o.kind,o.wv,o.wi,o.resp) for o in tr[home+1:feed]]
    # ACK, exact D055 (presence clear), then exactly two live reg100 reads; no write between.
    assert between == [('poll',0x8e,0x20,b'\x55'),('poll',0x18e,0x122,b'\xd0\x55'),
                       ('cr',0x18e,0x22,b'\x70\x55'),('cr',0x18e,0x22,b'\x70\x55')]
    go = [i for i,o in enumerate(tr) if o.kind=='cw' and o.wv==0x83 and o.data==b'\x0f\x01']
    assert len(go) == 1 and go[0] > feed
    assert sum(o.kind=='cr' and o.wv==0x18e and o.wi==0x22 for o in tr) == 2


@pytest.mark.parametrize('n,pulses',[(32,1),(40,2),(48,3),(54,4)])
def test_single_bad_ack_after_each_go_stops_before_next_write(n,pulses):
    fake = CorruptedRead(0x8e,0x20,n,b'\x00')
    scanner = Scanner(UsbIo(fake))
    with pytest.raises(safety.SafetyError): scanner.vendor_startup(experimental=True)
    assert fake.pulses == pulses
    assert fake.out_log[-1]['data'] == b'\x0f\x01'
