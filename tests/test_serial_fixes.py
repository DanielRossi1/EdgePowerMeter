"""Regression tests for serial-layer fixes: RTC/DST, reboot rebase, averaging
carry, corrupted lines, legacy->V2 upgrade, pause, and port handling."""
from __future__ import annotations

import os
import sys
import time
import types
from datetime import datetime, timezone

import numpy as np
import pytest
import serial

from app.serial import PROTO_V2
from app.serial import handler as H
from app.serial import protocol as P
from tests.fakes import FakePort

_EPOCH = datetime(1970, 1, 1)


def _rtc(*args) -> int:
    """Naive local RTC calendar reading as seconds since 1970."""
    return int((datetime(*args) - _EPOCH).total_seconds())


def _decoder(**kw) -> P.StreamDecoder:
    return P.StreamDecoder(clock=lambda: 1_800_000_000.0, **kw)


@pytest.fixture
def rome_tz():
    if sys.platform == "win32" or not hasattr(time, "tzset"):
        pytest.skip("needs POSIX tzset")
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/Rome"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


# ---------------------------------------------------------------- protocol

def test_anchor_offset_stable_across_dst_switch(rome_tz):
    """The RTC is naive local time without DST: a session crossing the spring
    switch (2026-03-29 02:00 CET -> 03:00 CEST) must keep a continuous wall
    clock. Converting each anchor with the host's DST rules shifted it 1 h."""
    d = _decoder()
    d.feed(b"T,0,%d,S\nD,1,0,3200,400\n" % _rtc(2026, 3, 29, 1, 0))
    first = d.take_samples()
    d.feed(b"T,7200000000,%d,S\nD,2,7200000000,3200,400\n" % _rtc(2026, 3, 29, 3, 0))
    second = d.take_samples()
    utc = lambda h: datetime(2026, 3, 29, h, tzinfo=timezone.utc).timestamp()
    assert first.wall[0] == pytest.approx(utc(0))
    assert second.wall[0] == pytest.approx(utc(2))


def test_legacy_timestamps_stable_across_dst_switch(rome_tz):
    d = _decoder()
    d.feed(b"2026-03-29 01:00:00.000,5.0,0.2,1.0\n2026-03-29 03:00:00.000,5.0,0.2,1.0\n")
    s = d.take_samples()
    assert s.wall[0] == pytest.approx(datetime(2026, 3, 29, 0, tzinfo=timezone.utc).timestamp())
    assert s.wall[1] - s.wall[0] == pytest.approx(7200.0)


def test_sync_reply_rederives_rtc_offset(rome_tz):
    """After !OK SYNC the RTC may hold a new calibration: the next anchor sets
    the offset again (here January CET -> July CEST)."""
    d = _decoder()
    d.feed(b"T,0,%d,S\n!OK SYNC\n" % _rtc(2026, 1, 15, 12, 0))
    d.take_events()
    d.feed(b"T,1000000,%d,S\nD,1,1000000,3200,400\n" % _rtc(2026, 7, 15, 12, 0))
    s = d.take_samples()
    assert s.wall[0] == pytest.approx(datetime(2026, 7, 15, 10, tzinfo=timezone.utc).timestamp())


def test_reboot_converts_pending_samples_with_old_reference():
    """Samples read before a device reboot must keep the old time base; the
    rebase only applies to what follows (the time axis stays monotonic)."""
    d = _decoder()
    d.feed(b"D,1,5000000,3200,400\nD,2,5001000,3200,400\n")
    d.feed(b"D,3,1000,3200,400\nD,4,2000,3200,400\n")     # device rebooted
    s = d.take_samples()
    assert len(s) == 4
    assert s.t.tolist() == pytest.approx([0.0, 0.001, 0.002, 0.003])
    d.feed(b"D,5,3000,3200,400\n")
    more = d.take_samples()
    assert np.all(np.diff(np.concatenate([s.t, more.t])) > 0)


def test_reboot_never_averages_across_the_rebase():
    """With host averaging, no block may mix pre- and post-reboot samples, and
    the pre-reboot carry is emitted as a shorter block instead of being lost.
    Pre-reboot samples read 4.0 V, post-reboot ones 2.0 V (bus 3200 / 1600)."""
    d = _decoder(host_averaging=4)
    d.feed(b"".join(b"D,%d,%d,3200,400\n" % (k, k * 1000) for k in range(1, 7)))
    d.feed(b"".join(b"D,%d,%d,1600,400\n" % (k, (k - 6) * 1000) for k in range(7, 14)))
    s = d.take_samples()
    # one full pre block, the pre carry (2 samples) as a partial block, one post block
    assert len(s) == 3
    assert s.v.tolist() == pytest.approx([4.0, 4.0, 2.0])
    assert np.all(np.diff(s.t) > 0)


def test_discard_raw_pending_drops_average_carry():
    d = _decoder(host_averaging=4)
    d.feed(b"D,1,0,3200,0\nD,2,1000,3200,0\n")
    d.take_samples()                                   # two samples carried
    d.discard_raw_pending()
    d.feed(b"D,10,9000,800,0\nD,11,10000,800,0\nD,12,11000,800,0\nD,13,12000,800,0\n")
    s = d.take_samples()
    assert len(s) == 1
    assert s.v[0] == pytest.approx(800 * 1.25e-3)      # only the new samples


def test_corrupted_non_ascii_line_is_rejected_not_repaired():
    d = _decoder()
    d.feed(b"D,1,0,3200,4\xff00\nD,2,1000,3200,400\n")
    s = d.take_samples()
    assert len(s) == 1
    assert s.v[0] == pytest.approx(4.0)
    assert d.bad_lines == 1


@pytest.mark.parametrize("base_us", [1_000_000, 5_000_000])
def test_anchor_from_new_boot_does_not_retime_pending_samples(base_us):
    """An anchor that arrives after the last pre-reboot line (and before the
    first post-reboot line) must not re-time the pre-reboot samples, and must
    itself survive the reboot for the samples that follow. The short base
    (about 1 s of device time before the reboot) is the reviewer's case."""
    d = _decoder()
    d.period_us = 1000
    d.feed(b"T,%d,%d,S\n" % (base_us, _rtc(2026, 5, 1, 12, 0, 0)))
    d.feed(b"".join(b"D,%d,%d,3200,400\n" % (k, base_us + k * 1000) for k in range(1, 4)))
    d.feed(b"T,100000,%d,S\n" % _rtc(2026, 5, 1, 13, 0, 0))   # new boot, one hour later
    d.feed(b"D,4,100000,3200,400\nD,5,101000,3200,400\n")
    s = d.take_samples()
    assert len(s) == 5
    assert np.all(np.diff(s.t) > 0)
    ref = _decoder()
    wall_a = ref._rtc_to_posix(_rtc(2026, 5, 1, 12, 0, 0))
    wall_b = ref._rtc_to_posix(_rtc(2026, 5, 1, 13, 0, 0))
    assert s.wall[:3] == pytest.approx(wall_a + np.array([0.001, 0.002, 0.003]), abs=1e-6)
    assert s.wall[3:] == pytest.approx(wall_b + np.array([0.0, 0.001]), abs=1e-6)


def test_late_upgrade_to_raw_continues_the_time_axis():
    """Legacy samples already emitted, then the late HELLO reply upgrades the
    stream: the RAW axis continues one period after the last legacy t."""
    d = _decoder()
    d.feed(b"2026-11-20 10:00:00,5.0,1.0,1\n2026-11-20 10:00:01,5.0,1.0,1\n"
           b"2026-11-20 10:00:02,5.0,1.0,1\n")
    legacy = d.take_samples()
    # the sequence SerialReader._set_protocol runs for PROTO_V2
    d.raw_only = True
    d.discard_legacy_pending()
    d.discard_raw_pending()
    d.reset_timebase()
    d.feed(b"!CFG,avg=1,vct=140,ict=140,shunt=0.01,oled=1,stream=1,mode=RAW,sqw=1,period_us=1000\n")
    d.feed(b"T,1000000,%d,S\n" % _rtc(2026, 11, 20, 10, 0, 3))
    d.feed(b"".join(b"D,%d,%d,3200,400\n" % (k, 1_000_000 + k * 1000) for k in range(1, 4)))
    raw = d.take_samples()
    t = np.concatenate([legacy.t, raw.t])
    assert np.all(np.diff(t) > 0)
    assert raw.t[0] == pytest.approx(legacy.t[-1] + 0.001)


def test_first_raw_session_still_starts_at_zero():
    """Without emitted samples the time axis starts at 0 as before."""
    d = _decoder()
    d.reset_timebase()
    d.feed(b"T,1000000,%d,S\nD,1,1000000,3200,400\n" % _rtc(2026, 5, 1, 12, 0, 0))
    assert d.take_samples().t[0] == pytest.approx(0.0)


# ----------------------------------------------------------- serial reader

def _reader(port, **kw):
    from app.serial import SerialReader
    r = SerialReader("/dev/fake", sync_clock=False, **kw)
    r._port = port
    r.HELLO_INTERVAL_S = 0.1
    return r


def _pump(qapp, seconds: float, until=None) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        if until is not None and until():
            return
        time.sleep(0.005)


def test_late_hello_reply_upgrades_legacy_declaration(qapp):
    from app.serial import PROTO_LEGACY, PROTO_V2
    port = FakePort("v2", rate_hz=500, hello_delay_s=0.7)   # reply after legacy was declared
    r = _reader(port)
    protos = []
    r.protocol_detected.connect(protos.append)
    got = []
    r.samples_ready.connect(got.append)
    r.start()
    _pump(qapp, 3.0, lambda: r.protocol == PROTO_V2 and "MODE RAW" in port.written)
    _pump(qapp, 0.3, lambda: sum(len(b) for b in got) > 0)
    r.stop()
    _pump(qapp, 0.1)
    assert protos == [PROTO_LEGACY, PROTO_V2]
    assert "MODE RAW" in port.written
    # Legacy batches flushed before the upgrade, RAW batches after it: the
    # recording's time axis must not restart.
    t = np.concatenate([b.t for b in got])
    assert len(t) > 100
    assert np.all(np.diff(t) > 0)


def test_rejected_stream_off_does_not_pause_the_watchdog(qapp):
    """!ERR SET for SET STREAM 0 means the stream still runs: no pause, and a
    device that then goes silent is still reported as NO_DATA."""
    from app.serial import PROTO_V2
    port = FakePort("v2", rate_hz=200, reject_stream_off=True)
    r = _reader(port)
    r.STALE_WARNING_S = 0.2
    r._no_data_timeout = 0.4
    replies = []
    r.command_reply.connect(lambda ok, cmd, msg: replies.append((ok, cmd)))
    errors = []
    r.error.connect(errors.append)
    count = []
    r.samples_ready.connect(lambda b: count.append(len(b)))
    r.start()
    _pump(qapp, 2.0, lambda: r.protocol == PROTO_V2 and sum(count) > 50)
    r.send_command("SET AVG 4")                       # answered !OK SET
    r.send_command("SET STREAM 0")                    # answered !ERR SET
    _pump(qapp, 1.0, lambda: (False, "SET") in replies)
    assert (True, "SET") in replies and (False, "SET") in replies
    assert r._paused is False
    port.silent = True
    _pump(qapp, 3.0, lambda: errors)
    r.stop()
    assert errors and errors[0].startswith("NO_DATA")


def test_paused_stream_is_not_reported_as_dead_device(qapp):
    port = FakePort("v2", rate_hz=200)
    r = _reader(port)
    r.STALE_WARNING_S = 0.2
    r._no_data_timeout = 0.4
    events = []
    r.error.connect(events.append)
    r.data_stale.connect(lambda s: events.append("stale"))
    got = []
    r.samples_ready.connect(got.append)
    count = lambda: sum(len(b) for b in got)
    r.start()
    _pump(qapp, 2.0, lambda: count() > 50)
    r.send_command("SET STREAM 0")
    _pump(qapp, 1.5)                                    # well past the no-data timeout
    assert not events
    paused_at = count()
    r.send_command("SET STREAM 1")
    _pump(qapp, 2.0, lambda: count() > paused_at + 50)
    r.stop()
    _pump(qapp, 0.1)
    assert not events
    assert count() > paused_at + 50


def test_run_closes_port_when_loop_fails(qapp):
    port = FakePort("v2")

    def broken_read(timeout=0.05, max_bytes=65536):
        raise RuntimeError("boom")

    port.read_chunk = broken_read
    r = _reader(port)
    errors = []
    r.error.connect(errors.append)
    r.run()                                              # same thread, as the reader would
    assert port.closed
    assert errors and "boom" in errors[0]


# ------------------------------------------------------------------ handler

def test_pyserial_write_timeout_is_not_a_disconnect():
    h = H.SerialPortHandler("COM3")

    class Ser:
        is_open = True

        def write(self, data):
            raise serial.SerialTimeoutException("Write timeout")

    h._ser = Ser()
    # TimeoutError (tolerated by the reader), not DeviceDisconnected.
    with pytest.raises(TimeoutError):
        h.write_line("GET")


def test_pyserial_write_timeout_ends_the_possibly_partial_line():
    """pyserial does not report a partial write: after a timeout a bare newline
    is sent so a fragment cannot glue onto the next command."""
    h = H.SerialPortHandler("COM3")
    writes = []

    class Ser:
        is_open = True

        def write(self, data):
            writes.append(bytes(data))
            if len(writes) == 1:
                raise serial.SerialTimeoutException("Write timeout")

    h._ser = Ser()
    with pytest.raises(TimeoutError):
        h.write_line("SET AVG 16")
    assert writes == [b"SET AVG 16\n", b"\n"]


def test_failed_post_open_step_releases_the_port(monkeypatch):
    closed = []

    class Ser:
        is_open = True

        def __init__(self, *a, **kw):
            pass

        def reset_input_buffer(self):
            raise serial.SerialException("reset failed")

        def close(self):
            closed.append(True)
            self.is_open = False

    monkeypatch.setattr(H.serial, "Serial", Ser)
    h = H.SerialPortHandler("/dev/fake")
    with pytest.raises(ConnectionError):
        h._open()                                        # direct open fails: pyserial path
    assert closed == [True]
    assert h._ser is None and not h.is_open


def _partial_write_handler(monkeypatch, first_write_bytes: int):
    writes = []

    def fake_write(fd, data):
        writes.append(bytes(data))
        if len(writes) == 1 and first_write_bytes:
            return first_write_bytes
        raise BlockingIOError()

    monkeypatch.setattr(H, "os", types.SimpleNamespace(write=fake_write))
    monkeypatch.setattr(H, "select", types.SimpleNamespace(
        select=lambda *a: (time.sleep(0.01), ([], [], []))[1]))
    h = H.SerialPortHandler("/dev/fake")
    h._fd = 99
    return h, writes


def test_partial_write_timeout_ends_the_fragment(monkeypatch):
    h, writes = _partial_write_handler(monkeypatch, first_write_bytes=5)
    with pytest.raises(TimeoutError):
        h.write_line("SET AVG 16")
    assert writes[0] == b"SET AVG 16\n"
    assert writes[-1] == b"\n"           # the device's line buffer is terminated


def test_untouched_write_timeout_sends_nothing_extra(monkeypatch):
    h, writes = _partial_write_handler(monkeypatch, first_write_bytes=0)
    with pytest.raises(TimeoutError):
        h.write_line("SET AVG 16")
    assert writes and all(w == b"SET AVG 16\n" for w in writes)


def _utc(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


def _logs(events):
    return [p for kind, p in events if kind == P.EV_LOG]


def test_rtc_ahead_by_dst_hour_is_corrected_to_host_time(rome_tz):
    """RTC set in summer time, read in winter: 14:00 naive on the device while
    the host is at 12:00 UTC (13:00 CET). Samples must follow the host clock,
    and exactly one log event reports the difference."""
    host = _utc(2026, 1, 15, 12, 0)
    d = P.StreamDecoder(clock=lambda: host, host_wall_clock=lambda: host)
    d.feed(b"T,1000000,%d,S\nD,1,1000000,3200,400\nD,2,2000000,3200,400\n"
           % _rtc(2026, 1, 15, 14, 0))
    s = d.take_samples()
    assert s.wall[0] == pytest.approx(host)
    assert s.wall[1] - s.wall[0] == pytest.approx(1.0)
    logs = [p for p in _logs(d.take_events()) if "differs" in p]
    assert len(logs) == 1
    assert "-3600.00 s" in logs[0]


def test_rtc_correction_is_dropped_after_sync(rome_tz):
    """After !OK SYNC a correctly set RTC is trusted again: no correction and
    no new log event."""
    host = _utc(2026, 1, 15, 12, 0)
    d = P.StreamDecoder(clock=lambda: host, host_wall_clock=lambda: host)
    d.feed(b"T,0,%d,S\n" % _rtc(2026, 1, 15, 14, 0))
    d.take_events()
    d.feed(b"!OK SYNC\nT,1000000,%d,S\nD,1,1000000,3200,400\n"
           % _rtc(2026, 1, 15, 13, 0))
    s = d.take_samples()
    assert s.wall[0] == pytest.approx(host)
    assert not [p for p in _logs(d.take_events()) if "differs" in p]


def test_rtc_correction_recomputed_after_sync_if_still_off(rome_tz):
    host = _utc(2026, 1, 15, 12, 0)
    d = P.StreamDecoder(clock=lambda: host, host_wall_clock=lambda: host)
    d.feed(b"T,0,%d,S\nD,1,0,3200,400\n" % _rtc(2026, 1, 15, 14, 0))
    d.feed(b"!OK SYNC\nT,0,%d,S\nD,2,0,3200,400\n" % _rtc(2026, 1, 15, 14, 0))
    logs = [p for p in _logs(d.take_events()) if "differs" in p]
    assert len(logs) == 2
    assert d._rtc_correction == pytest.approx(-3600.0)


def test_rtc_difference_under_tolerance_is_not_corrected(rome_tz):
    """RTC 0.1 s behind the host: below CLOCK_TOLERANCE_S, so the RTC is used
    as read and no event is logged."""
    base = _utc(2026, 1, 15, 12, 0)
    d = P.StreamDecoder(clock=lambda: base + 1.1, host_wall_clock=lambda: base + 1.1)
    # the anchor is 12:00:00 (device time 0), the D line 1 s later: device now 12:00:01
    d.feed(b"T,0,%d,S\nD,1,1000000,3200,400\n" % _rtc(2026, 1, 15, 13, 0))
    s = d.take_samples()
    assert d._rtc_correction == 0.0
    assert s.wall[0] == pytest.approx(base + 1.0)
    assert not [p for p in _logs(d.take_events()) if "differs" in p]


def test_rtc_lag_under_a_second_is_corrected_and_the_wall_clock_stays_continuous(rome_tz):
    """Device RTC 0.79 s behind the host (seen on hardware). The anchor is a
    second boundary 0.6 s before the first D line, so comparing it with the
    host's clock is off by up to a second: the check uses the device time of
    the D line instead. Samples before !OK SYNC follow the host within 20 ms,
    and so do the ones after SYNC, with no backward step at the transition."""
    base = _utc(2026, 11, 21, 12, 0, 0)
    now = [base + 0.79]
    d = P.StreamDecoder(clock=lambda: now[0], host_wall_clock=lambda: now[0],
                        require_mode_ack=True)
    d.expect_mode_ack()
    # RTC reads 12:00:00 (local UTC+1); that second boundary is at host 12:00:00.79
    d.feed(b"!OK MODE\nT,5000000,%d,S\n" % _rtc(2026, 11, 21, 13, 0, 0))
    now[0] = base + 1.39
    d.feed(b"D,1,5600000,3200,400\n")                  # 0.6 s after the boundary
    first = d.take_samples()
    assert first.wall[0] == pytest.approx(base + 1.39, abs=0.02)
    assert d._rtc_correction == pytest.approx(0.79, abs=1e-6)

    # !OK SYNC: the RTC is set now, so the device's boundaries fall on host
    # seconds. Device t 6.21 s is host 12:00:02.00; the sample before the SYNC
    # line is converted with the old mapping (not the new one).
    now[0] = base + 2.0
    d.feed(b"D,2,6210000,3200,400\n!OK SYNC\nT,6210000,%d,S\n"
           % _rtc(2026, 11, 21, 13, 0, 2))
    now[0] = base + 2.1
    d.feed(b"D,3,6310000,3200,400\n")
    s = d.take_samples()
    wall = np.concatenate([first.wall, s.wall])
    assert wall == pytest.approx(base + np.array([1.39, 2.0, 2.1]), abs=0.02)
    assert np.all(np.diff(wall) > -0.02)               # no backward step
    assert d._rtc_correction == 0.0
    logs = [p for p in _logs(d.take_events()) if "differs" in p]
    assert len(logs) == 1
    assert "+0.79 s" in logs[0]


def test_anchor_before_any_d_line_defers_the_check_to_the_first_d_line(rome_tz):
    """No D line yet when the anchor arrives: nothing is stamped from it, and
    the host-clock check runs on the first D line, before its samples."""
    base = _utc(2026, 11, 21, 12, 0, 0)
    now = [base + 0.79]
    d = P.StreamDecoder(clock=lambda: now[0], host_wall_clock=lambda: now[0],
                        require_mode_ack=True)
    d.expect_mode_ack()
    d.feed(b"!OK MODE\nT,5000000,%d,S\n" % _rtc(2026, 11, 21, 13, 0, 0))
    assert d.take_samples().t.size == 0
    assert d._anchor is None and d._rtc_correction == 0.0
    assert not [p for p in _logs(d.take_events()) if "differs" in p]
    now[0] = base + 1.39
    d.feed(b"D,1,5600000,3200,400\nD,2,5700000,3200,400\n")
    s = d.take_samples()
    assert len(s) == 2
    assert s.wall == pytest.approx(base + np.array([1.39, 1.49]), abs=0.02)
    logs = [p for p in _logs(d.take_events()) if "differs" in p]
    assert len(logs) == 1


def test_without_host_wall_clock_rtc_is_trusted(rome_tz):
    """Default behaviour is unchanged: the RTC offset is used as read."""
    d = P.StreamDecoder(clock=lambda: 1_800_000_000.0)
    d.feed(b"T,1000000,%d,S\nD,1,1000000,3200,400\n" % _rtc(2026, 1, 15, 14, 0))
    s = d.take_samples()
    assert s.wall[0] == pytest.approx(_utc(2026, 1, 15, 13, 0))
    assert not [p for p in _logs(d.take_events()) if "differs" in p]


def test_echoed_device_output_in_err_is_a_log_not_a_rejection():
    """On Linux the device's boot text is echoed back before the tty is raw;
    the firmware answers each echo with '!ERR <token> unknown_command'. Those
    must not be reported as rejected host commands."""
    d = _decoder()
    d.feed(b"!ERR # unknown_command\n"
           b"!ERR D,1,2,3,4 unknown_command\n"
           b"!ERR T,1000,2,S unknown_command\n"
           b"!ERR ! unknown_command\n"
           b"!ERR  unknown_command\n")
    events = d.take_events()
    assert not [e for e in events if e[0] == P.EV_ERR]
    assert len(_logs(events)) == 5


def test_real_command_error_is_still_reported():
    d = _decoder()
    d.feed(b"!ERR SET bad value\n")
    assert d.take_events() == [(P.EV_ERR, ("SET", "bad value"))]


# ------------------------------------------- stale RAW lines at session start

def _session_feed(d: P.StreamDecoder, chunks) -> None:
    """Feed chunks the way SerialReader does: events of a chunk are handled
    after the whole chunk is decoded, and the first HELLO reply starts the
    session (discard_raw_pending + reset_timebase)."""
    started = False
    for chunk in chunks:
        d.feed(chunk)
        for kind, _ in d.take_events():
            if kind == P.EV_HELLO and not started:
                started = True
                d.discard_raw_pending()
                d.reset_timebase()
                d.expect_mode_ack()     # the reader sends MODE RAW here


def _stale_lines(n: int, t0_us: int) -> bytes:
    return b"".join(b"D,%d,%d,3200,400\n" % (k, t0_us + k * 1000) for k in range(1, n + 1))


def test_stale_raw_lines_never_set_the_wall_reference():
    """Lines left in the device's USB buffer by a previous session arrive
    before !HELLO: its last T anchor, its "!OK MODE" (reply to MODE CSV) and
    its RAW lines (old t_us). None of them may pair an old device time with
    'now' or open the gate. The fresh samples follow the fresh anchor."""
    now = 1_800_000_000.0
    d = P.StreamDecoder(clock=lambda: now, require_mode_ack=True)
    stale_anchor_rtc = _rtc(2026, 11, 20, 12, 0, 0)
    anchor_t, anchor_rtc = 9_000_000, _rtc(2026, 11, 21, 12, 0, 0)
    anchor_wall = _decoder()._rtc_to_posix(anchor_rtc)
    chunks = [
        b"T,1000000,%d,S\n!OK MODE\n" % stale_anchor_rtc,                 # stale
        _stale_lines(40, 1_000_000),                                        # 8 s old
        b"!HELLO,EdgePowerMeter,2.0.0,2\n!CFG,avg=4,vct=140,ict=140,shunt=0.01,"
        b"oled=1,stream=1,mode=RAW,sqw=1,period_us=1000\n",
        b"!OK SET\n!OK MODE\n",
        b"T,%d,%d,S\n" % (anchor_t, anchor_rtc),
        b"".join(b"D,%d,%d,3200,400\n" % (k, anchor_t + k * 1000) for k in range(1, 201)),
        b"!OK SYNC\n",
    ]
    _session_feed(d, chunks)
    s = d.take_samples()
    assert len(s) == 200                                     # only the fresh lines
    assert np.all(s.wall <= now + 0.1)                       # none in the future
    # Sample k (k>=1) has device time anchor_t + k*1000 us: wall = anchor_wall + k ms.
    assert s.wall == pytest.approx(anchor_wall + np.arange(1, 201) * 1e-3, abs=0.1)


def test_stale_anchor_and_mode_ack_before_hello_do_not_open_the_gate():
    """Stale T anchors and a stale "!OK MODE" that arrive before !HELLO are
    harmless: the gate is armed only by expect_mode_ack (MODE RAW sent)."""
    d = P.StreamDecoder(clock=lambda: 1.0, require_mode_ack=True)
    d.feed(b"T,1000,%d,S\n!OK MODE\n" % _rtc(2026, 11, 20, 12, 0, 0))
    assert d.awaiting_mode_ack
    d.feed(_stale_lines(5, 1000))
    assert d.awaiting_mode_ack and d.raw_lines == 5
    assert d.take_samples().t.size == 0
    assert d._anchor is None and d._rtc_offset is None


def test_fresh_anchor_after_mode_ack_is_used_and_stale_lines_are_not():
    """Full sequence with the fresh anchor sent after the fresh "!OK MODE":
    every fresh sample is timed from that anchor, none from the stale lines."""
    now = 1_800_000_000.0
    d = P.StreamDecoder(clock=lambda: now, require_mode_ack=True)
    d.feed(b"T,1000,%d,S\n" % _rtc(2026, 11, 20, 12, 0, 0))   # stale anchor
    d.feed(_stale_lines(20, 1_000_000))
    d.feed(b"!HELLO,EdgePowerMeter,2.0.0,2\n")
    d.expect_mode_ack()                                        # MODE RAW sent
    d.feed(_stale_lines(3, 1_000_000))                         # still in the buffer
    d.feed(b"!OK MODE\nT,9000000,%d,S\n" % _rtc(2026, 11, 21, 12, 0, 0))
    assert not d.awaiting_mode_ack
    d.feed(b"".join(b"D,%d,%d,3200,400\n" % (k, 9_000_000 + k * 1000) for k in range(1, 51)))
    s = d.take_samples()
    assert len(s) == 50
    assert np.all(s.wall <= now + 0.1)
    anchor_wall = _decoder()._rtc_to_posix(_rtc(2026, 11, 21, 12, 0, 0))
    assert s.wall == pytest.approx(anchor_wall + np.arange(1, 51) * 1e-3, abs=0.1)


def test_t_anchor_while_awaiting_is_ignored():
    d = P.StreamDecoder(clock=lambda: 1.0, require_mode_ack=True)
    d.expect_mode_ack()
    d.feed(b"T,2000,%d,S\n" % _rtc(2026, 5, 1, 12, 0, 0))
    assert d.awaiting_mode_ack
    assert d._anchor is None
    assert d._rtc_offset is None           # not even the RTC offset is taken
    assert not [e for e in d.take_events() if e[0] == P.EV_ANCHOR]
    d.feed(b"!OK MODE\n")
    assert not d.awaiting_mode_ack


def test_mode_ack_is_ignored_until_mode_raw_was_sent():
    d = P.StreamDecoder(clock=lambda: 1.0, require_mode_ack=True)
    d.feed(b"!OK MODE\n")
    assert d.awaiting_mode_ack
    d.expect_mode_ack()
    d.feed(b"!OK MODE\n")
    assert not d.awaiting_mode_ack


def test_expect_mode_ack_forgets_the_reference_but_keeps_pending_samples():
    now = 1_800_000_000.0
    d = P.StreamDecoder(clock=lambda: now, require_mode_ack=True)
    d.expect_mode_ack()
    d.feed(b"!OK MODE\nT,1000,%d,S\nD,1,2000,3200,400\nD,2,3000,3200,400\n"
           % _rtc(2026, 11, 21, 12, 0, 0))
    assert d.has_pending and d._anchor is not None
    # a second MODE RAW (retry or reboot) while samples are pending
    d.feed(b"D,3,4000,3200,400\n")
    d.expect_mode_ack()
    assert d._anchor is None and d._host_ref is None
    assert d.awaiting_mode_ack
    s = d.take_samples()                    # converted with the old reference
    assert len(s) == 3
    anchor_wall = _decoder()._rtc_to_posix(_rtc(2026, 11, 21, 12, 0, 0))
    assert s.wall == pytest.approx(anchor_wall + np.array([0.001, 0.002, 0.003]), abs=1e-6)


def test_fresh_samples_after_stale_lines_use_host_clock_only_from_a_fresh_line():
    """No anchor in this session: the host-time fallback comes from the first
    line after the MODE ack, never from a stale one."""
    now = 1_800_000_000.0
    d = P.StreamDecoder(clock=lambda: now, require_mode_ack=True)
    chunks = [
        _stale_lines(30, 1_000_000),
        b"!HELLO,EdgePowerMeter,2.0.0,2\n",
        b"!OK MODE\nD,31,5000000,3200,400\nD,32,5001000,3200,400\n",
    ]
    _session_feed(d, chunks)
    s = d.take_samples()
    assert len(s) == 2
    assert s.wall[0] == pytest.approx(now)
    assert s.wall[1] == pytest.approx(now + 0.001)
    assert d.lost_samples == 0


def test_lines_between_hello_and_mode_ack_are_not_samples_nor_losses():
    d = P.StreamDecoder(require_mode_ack=True)
    d.feed(b"D,1,1000,3200,400\n")                  # before the ack: ignored
    assert d.awaiting_mode_ack and d.raw_lines == 1
    d.expect_mode_ack()
    d.feed(b"!OK MODE\nD,500,9000000,3200,400\nD,501,9001000,3200,400\n")
    assert not d.awaiting_mode_ack
    assert d.lost_samples == 0                       # no gap counted from stale seq 1
    assert len(d.take_samples()) == 2


def test_valid_anchor_alone_does_not_open_the_stream():
    """A valid T anchor no longer opens the gate: only this session's
    "!OK MODE" does (the firmware sends the fresh anchor right after it)."""
    d = P.StreamDecoder(clock=lambda: 1.0, require_mode_ack=True)
    d.expect_mode_ack()
    d.feed(b"D,1,1000,3200,400\nT,2000,%d,S\nD,2,2000,3200,400\n" % _rtc(2026, 5, 1, 12, 0, 0))
    assert d.awaiting_mode_ack and len(d.take_samples()) == 0
    d.feed(b"!OK MODE\nT,3000,%d,S\nD,3,3000,3200,400\n" % _rtc(2026, 5, 1, 12, 0, 1))
    s = d.take_samples()
    assert len(s) == 1 and not d.awaiting_mode_ack


def test_decoder_without_require_mode_ack_is_unchanged():
    d = _decoder()
    d.feed(b"D,1,1000,3200,400\n")
    assert not d.awaiting_mode_ack and len(d.take_samples()) == 1


def test_reader_session_with_stale_lines_has_no_future_samples(qapp):
    """End to end through SerialReader: the fake device has an old T anchor,
    an old "!OK MODE" and old RAW lines in its buffer before the handshake
    (FakePort.stale_raw_lines)."""
    from app.serial import PROTO_V2
    port = FakePort("v2", rate_hz=1000, stale_raw_lines=400)
    r = _reader(port)
    got = []
    r.samples_ready.connect(got.append)
    r.start()
    _pump(qapp, 3.0, lambda: r.protocol == PROTO_V2 and sum(len(b) for b in got) > 300)
    r.stop()
    _pump(qapp, 0.1)
    samples = [b for b in got if len(b)]
    assert samples
    wall = np.concatenate([b.wall for b in samples])
    t = np.concatenate([b.t for b in samples])
    assert np.all(wall <= time.time() + 0.1)
    # the fake's fresh anchor is "now": the first sample is not an hour off
    assert abs(wall[0] - time.time()) < 2.0
    assert np.all(np.diff(wall) > 0)
    # wall spacing follows the device clock (1 kHz fake)
    assert np.allclose(np.diff(wall), np.diff(t), atol=0.01)


# ----------------------------------------------------- hostile device: decoder
# The device is not trusted. Values outside the hardware's ranges, anchors that
# disagree with the time base, out-of-range configs and runaway output are
# counted and dropped; they never raise, poison the time base or reach samples.

MAX_T = 1 << 53


@pytest.mark.parametrize("line", [
    b"D,2,%d,3200,400" % (MAX_T + 1),        # device time beyond 2**53 µs
    b"D,2,-1,3200,400",                       # negative device time
    b"D,2,99999999999999999999,3200,400",     # beyond int64
    b"D,2,1000,65536,400",                    # bus beyond the 16-bit register
    b"D,2,1000,-1,400",
    b"D,2,1000,3200,32768",                   # shunt beyond the signed 16-bit register
    b"D,2,1000,3200,-32769",
    b"D,4294967296,1000,3200,400",            # seq beyond 32 bits
    b"D,-1,1000,3200,400",
    b"D,2,1000,3200," + b"1" + b"0" * 400,    # a 401-digit value
])
def test_out_of_range_raw_line_is_a_bad_line(line):
    d = _decoder()
    d.feed(b"D,1,1000,3200,400\n" + line + b"\nD,3,2000,3200,400\n")
    s = d.take_samples()
    assert len(s) == 2
    assert d.bad_lines == 1
    assert d.raw_lines == 2


def test_out_of_range_raw_lines_are_counted_exactly():
    d = _decoder()
    d.feed(b"D,1,1000,3200,400\nD,2,%d,3200,400\nD,3,2000,65536,400\n"
           b"D,4,3000,3200,400\n" % (MAX_T + 1))
    s = d.take_samples()
    assert len(s) == 2
    assert d.bad_lines == 2
    assert d.raw_lines == 2


def test_extreme_valid_raw_values_are_accepted():
    d = _decoder()
    d.feed(b"D,4294967295,0,0,-32768\nD,0,%d,65535,32767\n" % MAX_T)
    s = d.take_samples()
    assert len(s) == 2 and d.bad_lines == 0
    assert np.all(np.isfinite(s.v)) and np.all(np.isfinite(s.i))


def test_extreme_device_times_do_not_wrap():
    """A time difference near 2**53 µs must not wrap int64 into a negative
    time. The range check rules it out; this pins the arithmetic too."""
    d = _decoder()
    d.feed(b"D,1,0,3200,400\nD,2,%d,3200,400\n" % MAX_T)
    s = d.take_samples()
    assert s.t.tolist() == pytest.approx([0.0, MAX_T * 1e-6])
    assert np.all(np.diff(s.t) > 0)


def test_repeated_reboots_to_extreme_times_stay_monotonic():
    """Each step back is a reboot, and each rebase moves the origin further back.
    The time axis must stay finite and increasing, with no int64 wrap."""
    d = _decoder()
    lines = [b"D,%d,%d,3200,400" % (k + 1, t) for k, t in enumerate((MAX_T, 0, MAX_T, 0, MAX_T))]
    d.feed(b"\n".join(lines) + b"\n")
    s = d.take_samples()
    assert len(s) == 5
    assert np.all(np.isfinite(s.t)) and np.all(np.diff(s.t) > 0)


@pytest.mark.parametrize("rtc", [_rtc(2101, 1, 1), 10**19, 2**63 - 1])
def test_anchor_with_implausible_year_is_rejected(rtc):
    d = _decoder()
    d.feed(b"T,1000,%d,S\n" % rtc)
    assert d._anchor is None
    assert d.bad_lines == 1
    assert not [e for e in d.take_events() if e[0] == P.EV_ANCHOR]


def test_anchor_with_out_of_range_device_time_is_rejected():
    d = _decoder()
    d.feed(b"T,%d,%d,S\n" % (MAX_T + 1, _rtc(2026, 5, 1, 12, 0, 0)))
    assert d._anchor is None and d.bad_lines == 1


def test_inconsistent_anchor_is_ignored_and_does_not_move_the_time_base():
    """One bogus anchor 30 s off the current mapping is counted and ignored:
    the samples keep following the first anchor."""
    d = _decoder()
    d.feed(b"T,1000000,%d,S\n" % _rtc(2026, 5, 1, 12, 0, 0))
    d.feed(b"T,2000000,%d,S\nD,1,3000000,3200,400\n" % _rtc(2026, 5, 1, 12, 0, 31))
    assert d.bad_lines == 1
    s = d.take_samples()
    base = _decoder()._rtc_to_posix(_rtc(2026, 5, 1, 12, 0, 0))
    assert s.wall[0] == pytest.approx(base + 2.0, abs=1e-6)


def test_anchor_within_tolerance_is_accepted():
    d = _decoder()
    d.feed(b"T,1000000,%d,S\nT,2000000,%d,S\n"
           % (_rtc(2026, 5, 1, 12, 0, 0), _rtc(2026, 5, 1, 12, 0, 4)))   # 3 s off the mapping
    assert d.bad_lines == 0
    assert d._anchor[1] == pytest.approx(_decoder()._rtc_to_posix(_rtc(2026, 5, 1, 12, 0, 4)))


def test_far_anchor_is_accepted_after_sync():
    rtc_a, rtc_b = _rtc(2026, 5, 1, 12, 0, 0), _rtc(2026, 5, 1, 13, 0, 0)
    d = _decoder()
    d.feed(b"T,1000000,%d,S\nT,2000000,%d,S\n" % (rtc_a, rtc_b))      # 1 h off: ignored
    assert d.bad_lines == 1
    d.feed(b"!OK SYNC\nT,2000000,%d,S\nD,1,2000000,3200,400\n" % rtc_b)
    assert d.bad_lines == 1
    s = d.take_samples()
    assert s.wall[0] == pytest.approx(_decoder()._rtc_to_posix(rtc_b))


def test_far_anchor_is_accepted_after_reboot_and_follows_the_new_boot():
    rtc_a, rtc_b = _rtc(2026, 5, 1, 12, 0, 0), _rtc(2026, 5, 1, 13, 0, 0)
    d = _decoder()
    d.feed(b"T,100,%d,S\n" % rtc_a)
    d.feed(b"D,1,5000,3200,400\nD,2,6000,3200,400\n")
    d.feed(b"D,3,1000,3200,400\n")                  # reboot: the device clock went back
    d.feed(b"T,2000,%d,S\n" % rtc_b)                # the new boot, an hour later
    d.feed(b"D,4,3000,3200,400\n")
    assert d.bad_lines == 0
    s = d.take_samples()
    assert s.wall[-1] == pytest.approx(_decoder()._rtc_to_posix(rtc_b) + 0.001, abs=1e-6)
    assert np.all(np.diff(s.t) > 0)


def test_first_valid_anchor_sets_the_base_after_an_unset_clock():
    """An anchor from a never-set RTC is skipped silently; the first anchor
    that passes the year check is the one the time base is derived from."""
    d = _decoder()
    d.feed(b"T,1000,%d,S\n" % _rtc(2019, 1, 1))
    d.feed(b"T,2000000,%d,S\nD,1,2000000,3200,400\n" % _rtc(2026, 5, 1, 12, 0, 0))
    assert d.bad_lines == 0
    s = d.take_samples()
    assert s.wall[0] == pytest.approx(_decoder()._rtc_to_posix(_rtc(2026, 5, 1, 12, 0, 0)))


def test_host_correction_comes_from_first_anchor_that_passes_year_check(rome_tz):
    host = _utc(2026, 1, 15, 12, 0)
    d = P.StreamDecoder(clock=lambda: host, host_wall_clock=lambda: host)
    d.feed(b"T,1000,%d,S\n" % _rtc(2019, 1, 1))              # never set: not used
    d.feed(b"T,1000000,%d,S\nD,1,1000000,3200,400\n" % _rtc(2026, 1, 15, 14, 0))
    s = d.take_samples()
    assert s.wall[0] == pytest.approx(host)
    assert d._rtc_correction == pytest.approx(-3600.0)


def _bad_float_decoder(**kw):
    return P.StreamDecoder(clock=lambda: 1_800_000_000.0, **kw)


def test_raw_samples_that_overflow_float32_are_dropped():
    d = _bad_float_decoder(calibration=P.Calibration(voltage_gain=1e40))
    d.feed(b"D,1,1000,3200,400\nD,2,2000,3200,400\n")
    s = d.take_samples()
    assert len(s) == 0
    assert d.bad_lines == 2


def test_legacy_values_beyond_plausible_range_are_dropped():
    d = _decoder()
    d.feed(b"2026-10-08 12:00:00.000,1e30,1e30,0\n"
           b"2026-10-08 12:00:00.001,9e3,9e3,0\n"
           b"2026-10-08 12:00:00.002,4.0,0.1,0.4\n")
    s = d.take_samples()
    assert len(s) == 2 and d.bad_lines == 1
    assert np.all(np.isfinite(s.p)) and np.all(np.abs(s.v) <= 1e4)


def test_legacy_gain_overflow_is_dropped_not_stored_as_inf():
    d = P.StreamDecoder(calibration=P.Calibration(voltage_gain=1e6),
                        clock=lambda: 1_800_000_000.0)
    d.feed(b"2026-10-08 12:00:00.000,5.0,0.2,1.0\n")
    s = d.take_samples()
    assert len(s) == 0 and d.bad_lines == 1


@pytest.mark.parametrize("cfg", [
    b"!CFG,avg=3,vct=140,ict=140",
    b"!CFG,avg=-4,vct=-1,ict=-1",
    b"!CFG,avg=4,vct=1000,ict=140",
    b"!CFG,avg=4,vct=140,ict=0",
    b"!CFG,period_us=-10000000",
    b"!CFG,period_us=1",
    b"!CFG,period_us=0",
    b"!CFG,period_us=1000000000000",
    b"!CFG,period_us=9223372036854775807",
])
def test_invalid_device_config_is_ignored(cfg):
    d = _decoder()
    d.feed(b"!CFG,avg=4,vct=140,ict=140,period_us=1000\n")
    d.take_events()
    d.feed(cfg + b"\n")
    events = d.take_events()
    assert not [e for e in events if e[0] == P.EV_CONFIG]
    assert any("invalid device config ignored" in text for text in _logs(events))
    assert d.period_us == 1000
    assert d.bad_lines == 1


def test_slowest_firmware_config_is_accepted():
    """1024 averages of 8244 µs on both channels is about 16.9 s per sample: a
    setting the firmware allows, so it must not be rejected."""
    assert P.PERIOD_US_MAX == 16_883_712
    d = _decoder()
    d.feed(b"!CFG,avg=1024,vct=8244,ict=8244,shunt=0.01,period_us=16883712\n")
    events = d.take_events()
    assert [e for e in events if e[0] == P.EV_CONFIG]
    assert d.period_us == 16_883_712 and d.bad_lines == 0


@pytest.mark.parametrize("value,expected", [
    ("nan", None), ("inf", None), ("0", None), ("-1", None), ("0.02", 0.02),
])
def test_shunt_in_config_is_finite_positive_or_none(value, expected):
    assert P.DeviceConfig.parse("!CFG,shunt=" + value).shunt_ohm == expected


def test_config_parse_raises_for_out_of_range_values():
    with pytest.raises(P.InvalidConfig):
        P.DeviceConfig.parse("!CFG,avg=5")
    with pytest.raises(P.InvalidConfig):
        P.DeviceConfig.parse("!CFG,period_us=10")


def test_hello_name_and_firmware_are_capped():
    d = _decoder()
    d.feed(b"!HELLO," + b"N" * 1000 + b"," + b"F" * 1000 + b",2\n")
    info = [p for k, p in d.take_events() if k == P.EV_HELLO][0]
    assert len(info.name) == 64 and len(info.firmware) == 64
    assert info.protocol == 2


@pytest.mark.parametrize("field,expected", [
    ("2", 2), ("99", 99), ("100", P.UNKNOWN_PROTOCOL), ("0", P.UNKNOWN_PROTOCOL),
    ("-1", P.UNKNOWN_PROTOCOL), ("999999999999999999999", P.UNKNOWN_PROTOCOL),
])
def test_hello_protocol_out_of_range_is_unknown(field, expected):
    d = _decoder()
    d.feed(b"!HELLO,EdgePowerMeter,2.0.0," + field.encode() + b"\n")
    info = [p for k, p in d.take_events() if k == P.EV_HELLO][0]
    assert info.protocol == expected


def test_large_seq_jump_is_a_resync_not_losses():
    d = _decoder()
    d.feed(b"D,1,0,3200,400\nD,200001,1000,3200,400\n")       # 200000 skipped
    assert d.lost_samples == 0
    assert [p for p in _logs(d.take_events()) if "sequence jumped" in p]


def test_moderate_seq_gap_is_still_counted_as_lost():
    d = _decoder()
    d.feed(b"D,1,0,3200,400\nD,50001,1000,3200,400\n")   # seq 2..50000 missing
    assert d.lost_samples == 49999


def test_seq_gap_is_a_resync_after_ten_seconds_at_the_known_rate():
    d = _decoder()
    d.feed(b"!CFG,avg=1,vct=140,ict=140,period_us=1000\n")
    d.feed(b"D,1,0,3200,400\nD,20002,1000,3200,400\n")       # 20000 at 1 kHz = 20 s
    assert d.lost_samples == 0
    d2 = _decoder()
    d2.feed(b"!CFG,avg=1,vct=140,ict=140,period_us=1000\n")
    d2.feed(b"D,1,0,3200,400\nD,5002,1000,3200,400\n")       # 5 s: a real loss
    assert d2.lost_samples == 5000


def test_backwards_seq_is_not_counted_as_loss():
    d = _decoder()
    d.feed(b"D,5,1000,3200,400\nD,4,2000,3200,400\n")
    assert d.lost_samples == 0


def test_huge_time_gap_counts_missed_conversions_with_a_cap():
    d = _decoder()
    d.feed(b"!CFG,avg=1,vct=140,ict=140,period_us=1000\n")
    d.feed(b"D,1,0,1,1\nD,2,%d,1,1\n" % 10**15)
    assert d.missed_samples == P.MAX_MISSED_PER_GAP
    assert d.lost_samples == 0


def test_oversize_partial_line_is_counted_and_skipped_to_the_next_newline():
    d = _decoder()
    d.feed(b"A" * 5000)
    assert d.bad_lines == 1 and d._buf == b""
    d.feed(b"D,1,1,1,1,DDDD" * 1000)              # more of the same line: still skipped
    d.feed(b"\nD,1,1000,3200,400\n")
    assert d.bad_lines == 1
    assert len(d.take_samples()) == 1


def test_oversize_complete_line_is_a_bad_line():
    d = _decoder()
    d.feed(b"#" + b"x" * 5000 + b"\nD,1,1000,3200,400\n")
    assert d.bad_lines == 1
    assert not [e for e in d.take_events() if e[0] == P.EV_LOG]
    assert len(d.take_samples()) == 1


def test_events_per_feed_are_capped_but_samples_are_kept():
    d = _decoder()
    flood = b"!OK X\n" * (P.MAX_EVENTS_PER_FEED + 5000)
    d.feed(flood + b"D,1,1000,3200,400\n")
    assert len(d.take_events()) == P.MAX_EVENTS_PER_FEED
    assert d.dropped_events == 5000
    assert len(d.take_samples()) == 1
    d.feed(b"!OK X\n" * 3)                         # the cap applies per feed
    assert len(d.take_events()) == 3


def test_take_samples_concatenates_once_however_many_batches(monkeypatch):
    """Each backward step is a reboot and leaves one pending batch. The batches
    are joined with one concatenation per column, not one per batch."""
    d = _decoder()
    lines = []
    for k in range(400):
        t = 10_000_000 + k * 1000 if k % 2 == 0 else 10_000_000 + k * 1000 - 5000
        lines.append(b"D,%d,%d,3200,400" % (k + 1, t))
    d.feed(b"\n".join(lines) + b"\n")
    calls = []
    real = np.concatenate

    def counting(arrays, *a, **kw):
        calls.append(len(arrays))
        return real(arrays, *a, **kw)

    monkeypatch.setattr(P.np, "concatenate", counting)
    s = d.take_samples()
    monkeypatch.undo()
    assert len(s) == 400
    assert len(calls) == 5                         # one per column
    assert calls[0] > 100 and len(set(calls)) == 1
    assert np.all(np.diff(s.t) > 0)


# ------------------------------------------------- hostile device: reader

def test_reader_survives_out_of_range_raw_line(qapp):
    """Before the fix one out-of-range D line ended the thread without any
    error or disconnect signal. Now the line is dropped and streaming goes on."""
    port = FakePort("v2", rate_hz=500)
    r = _reader(port)
    errors, disc, got = [], [], []
    r.error.connect(errors.append)
    r.disconnected.connect(disc.append)
    r.samples_ready.connect(got.append)
    count = lambda: sum(len(b) for b in got)
    r.start()
    _pump(qapp, 4.0, lambda: r.protocol == PROTO_V2 and count() > 50)
    port.inject(b"D,999999,99999999999999999999,3200,400\n")
    before = count()
    _pump(qapp, 2.0, lambda: count() > before + 100)
    r.stop()
    _pump(qapp, 0.1)
    assert not errors and not disc
    assert count() > before + 100
    assert r._decoder.bad_lines >= 1


@pytest.mark.parametrize("failure", ["error", "disconnect"])
def test_shutdown_signal_is_emitted_even_if_the_final_flush_fails(failure):
    from app.serial.handler import DeviceDisconnected
    port = FakePort("v2")

    def broken_read(timeout=0.05, max_bytes=65536):
        if failure == "error":
            raise RuntimeError("boom")
        raise DeviceDisconnected("unplugged")

    port.read_chunk = broken_read
    r = _reader(port)

    def poisoned_flush():
        raise OverflowError("poisoned batch")

    r._flush_samples = poisoned_flush
    errors, disc = [], []
    r.error.connect(errors.append)
    r.disconnected.connect(disc.append)
    r.run()                                        # same thread, as the reader would
    assert port.closed
    if failure == "error":
        assert errors and "boom" in errors[0]
    else:
        assert disc and "unplugged" in disc[0]


def test_reader_ignores_invalid_period_config_and_keeps_streaming(qapp):
    """A negative period_us used to end the session with NO_DATA right after the
    first sample. The invalid !CFG is now ignored and the stream keeps running."""
    cfg = b"!CFG,avg=4,vct=140,ict=140,shunt=0.01,oled=1,stream=1,mode=RAW,sqw=1,period_us=-10000000\n"
    port = FakePort("v2", rate_hz=500, cfg_reply=cfg)
    r = _reader(port)
    errors, configs, logs, got = [], [], [], []
    r.error.connect(errors.append)
    r.device_config.connect(configs.append)
    r.device_log.connect(logs.append)
    r.samples_ready.connect(got.append)
    count = lambda: sum(len(b) for b in got)
    r.start()
    _pump(qapp, 3.0, lambda: count() > 100)
    r.stop()
    _pump(qapp, 0.1)
    assert not errors
    assert count() > 100
    assert configs == []
    assert any("invalid device config ignored" in text for text in logs)


def test_reader_keeps_silence_detection_for_an_absurd_period(qapp):
    """A huge period_us used to scale the no-data threshold to days, so a
    silent device was never reported. The invalid config is ignored, and the
    watchdog still reports NO_DATA."""
    cfg = b"!CFG,avg=4,vct=140,ict=140,shunt=0.01,oled=1,stream=1,mode=RAW,sqw=1,period_us=100000000000\n"
    port = FakePort("v2", rate_hz=500, cfg_reply=cfg)
    r = _reader(port)
    r.STALE_WARNING_S = 0.2
    r._no_data_timeout = 0.4
    errors, got = [], []
    r.error.connect(errors.append)
    r.samples_ready.connect(got.append)
    count = lambda: sum(len(b) for b in got)
    r.start()
    _pump(qapp, 3.0, lambda: r.protocol == PROTO_V2 and count() > 50)
    port.silent = True
    _pump(qapp, 3.0, lambda: errors)
    r.stop()
    assert errors and errors[0].startswith("NO_DATA")


def test_watchdog_thresholds_are_clamped_and_unchanged_for_normal_rates(qapp):
    r = _reader(FakePort("v2"))
    r.protocol = PROTO_V2
    warn, fatal = r._watchdog_thresholds(1000, False)           # 1 kHz
    assert warn == pytest.approx(r.STALE_WARNING_S)
    assert fatal == pytest.approx(r._no_data_timeout + 0.003)
    for period_us in (0, 1000, P.PERIOD_US_MAX, 10**12, -10**9):
        for first in (True, False):
            warn, fatal = r._watchdog_thresholds(period_us, first)
            assert 0 < warn <= r.WATCHDOG_MAX_S
            assert r._no_data_timeout <= fatal <= r.WATCHDOG_MAX_S


def test_unsolicited_ok_set_does_not_trigger_a_get(qapp):
    port = FakePort("v2", rate_hz=200)
    r = _reader(port)
    got = []
    r.samples_ready.connect(got.append)
    count = lambda: sum(len(b) for b in got)
    r.start()
    _pump(qapp, 3.0, lambda: r.protocol == PROTO_V2 and count() > 20)
    _pump(qapp, 0.3)
    gets = port.written.count("GET")
    port.inject(b"!OK SET\n!OK SAVE\n")
    _pump(qapp, 0.5)
    r.stop()
    _pump(qapp, 0.1)
    assert port.written.count("GET") == gets


def test_device_answering_get_with_ok_set_cannot_loop(qapp):
    """The device answers every GET with "!OK SET". The host must not follow
    each reply with another GET (it used to send ~17000 in 3 s)."""
    port = FakePort("v2", rate_hz=200, replies={"GET": b"!OK SET\n"})
    r = _reader(port)
    got = []
    r.samples_ready.connect(got.append)
    count = lambda: sum(len(b) for b in got)
    r.start()
    _pump(qapp, 3.0, lambda: r.protocol == PROTO_V2 and count() > 20)
    _pump(qapp, 1.0)
    r.stop()
    _pump(qapp, 0.1)
    assert port.written.count("GET") <= 3


def test_set_replies_trigger_a_refresh_at_most_once_per_interval(qapp):
    port = FakePort("v2", rate_hz=200)
    r = _reader(port)
    got = []
    r.samples_ready.connect(got.append)
    count = lambda: sum(len(b) for b in got)
    r.start()
    _pump(qapp, 3.0, lambda: r.protocol == PROTO_V2 and count() > 20)
    _pump(qapp, 0.3)
    gets = port.written.count("GET")
    for _ in range(5):
        r.send_command("SET AVG 4")                 # five answered in one burst
    _pump(qapp, 0.5)
    r.stop()
    _pump(qapp, 0.1)
    refreshes = port.written.count("GET") - gets
    assert 1 <= refreshes <= 2


# ------------------------------------ RTC correction only from fresh T anchors

def _v2_feed(d: P.StreamDecoder, chunks):
    """Feed chunks the way SerialReader does for firmware 2.x: the first HELLO
    with protocol 2 switches to RAW (see SerialReader._set_protocol), then
    MODE RAW is sent. Returns every event, in order."""
    events = []
    started = False
    for chunk in chunks:
        d.feed(chunk)
        for kind, payload in d.take_events():
            events.append((kind, payload))
            if kind == P.EV_HELLO and not started and payload.protocol == 2:
                started = True
                d.raw_only = True
                d.discard_legacy_pending()
                d.discard_raw_pending()
                d.reset_timebase()
                d.expect_mode_ack()
    return events


def test_stale_csv_line_does_not_set_host_clock_correction(rome_tz):
    """A CSV line left in the USB buffer by the previous session is 127 s older
    than the host clock. It must not set the correction: the fresh anchor
    (RTC matches the host) gives wall times within 1 s of the host and no
    "device clock differs" event."""
    host = _utc(2026, 11, 21, 12, 0, 0)
    d = P.StreamDecoder(clock=lambda: host, host_wall_clock=lambda: host,
                        require_mode_ack=True)
    stale = b"".join(b"2026-11-21 12:57:53.%03d,5.0,0.2,1.0\n" % k for k in (0, 4, 8))
    events = _v2_feed(d, [
        stale,
        b"!HELLO,EdgePowerMeter,2.0.0,2\n",
        b"!OK MODE\nT,9000000,%d,S\n" % _rtc(2026, 11, 21, 13, 0, 0),
        b"".join(b"D,%d,%d,3200,400\n" % (k, 9_000_000 + k * 1000) for k in range(1, 51)),
    ])
    s = d.take_samples()
    assert len(s) == 50
    assert abs(s.wall[0] - host) < 1.0
    assert d._rtc_correction == 0.0
    assert not [p for p in _logs(events) if "differs" in p]


def test_fresh_anchor_off_by_an_hour_is_corrected_once(rome_tz):
    """Same session, but the fresh anchor really is 3600 s behind the host:
    the correction is taken from it and reported exactly once."""
    host = _utc(2026, 11, 21, 12, 0, 0)
    d = P.StreamDecoder(clock=lambda: host, host_wall_clock=lambda: host,
                        require_mode_ack=True)
    stale = b"".join(b"2026-11-21 12:57:53.%03d,5.0,0.2,1.0\n" % k for k in (0, 4, 8))
    events = _v2_feed(d, [
        stale,
        b"!HELLO,EdgePowerMeter,2.0.0,2\n",
        # device clock says 12:00 local (= 11:00 UTC), the host is at 12:00 UTC
        b"!OK MODE\nT,9000000,%d,S\n" % _rtc(2026, 11, 21, 12, 0, 0),
        b"".join(b"D,%d,%d,3200,400\n" % (k, 9_000_000 + k * 1000) for k in range(1, 51)),
    ])
    s = d.take_samples()
    assert len(s) == 50
    assert abs(s.wall[0] - host) < 1.0
    assert d._rtc_correction == pytest.approx(3600.0)
    logs = [p for p in _logs(events) if "differs" in p]
    assert len(logs) == 1
    assert "+3600.00 s" in logs[0]


def test_firmware_1x_legacy_session_uses_rtc_wall_times(rome_tz):
    """Firmware 1.x has no T anchors, so its legacy-only session gets no host
    correction and keeps the RTC-based wall times. The host clock here is
    read at reception (1.0) to show the times do not come from it."""
    d = P.StreamDecoder(clock=lambda: 1.0,
                        host_wall_clock=lambda: _utc(2026, 11, 21, 12, 0, 0))
    d.feed(b"Timestamp,Voltage[V],Current[A],Power[W]\n"
           b"2026-11-21 13:00:00.000,5.0,0.2,1.0\n"
           b"2026-11-21 13:00:00.004,5.0,0.2,1.0\n")
    s = d.take_samples()
    assert len(s) == 2
    assert s.wall[0] == pytest.approx(_utc(2026, 11, 21, 12, 0, 0))
    assert s.wall[1] - s.wall[0] == pytest.approx(0.004, abs=1e-6)
    assert d._rtc_correction == 0.0
    assert not [p for p in _logs(d.take_events()) if "differs" in p]
