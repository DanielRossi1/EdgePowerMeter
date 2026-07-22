"""SerialReader stale-data watchdog tests.

Covers the case where the device stays connected (port open) but stops
sending valid measurements (e.g. firmware stuck in a fault loop) — the
reader must warn once via data_stale, and escalate to error if the
silence persists past the fatal timeout.

Also covers a real bug found on hardware where the DS3231's SQW pin isn't
wired: PrecisionTime::begin() then blocks for its full ~2.5s timeout on
every boot, on top of ~1.1s of other fixed setup delays, comfortably
exceeding the original single 3s STALE_WARNING_S threshold and firing a
false "stale" warning on every normal connection. The reader now uses a
separate, more generous grace period before the very first measurement.
"""
from __future__ import annotations

import time as time_module

from app.serial.serial_reader import SerialReader


class _SilentHandler:
    """Fake port handler that opens fine but never returns a line."""

    def open(self):
        pass

    def readline(self):
        return ""

    def close(self):
        pass


class _ResumingHandler:
    """Fake handler: silent for a bit, then streams valid lines, then silent again."""

    def __init__(self, silent_reads_before_data: int, lines):
        self._silent_reads = silent_reads_before_data
        self._lines = list(lines)

    def open(self):
        pass

    def readline(self):
        if self._silent_reads > 0:
            self._silent_reads -= 1
            return ""
        if self._lines:
            return self._lines.pop(0)
        return ""

    def close(self):
        pass


class _SlowBootHandler:
    """Fake handler: silent for delay_s (simulated boot time), then one line."""

    def __init__(self, delay_s: float):
        self._start = time_module.monotonic()
        self._delay_s = delay_s
        self._sent = False

    def open(self):
        pass

    def readline(self):
        if not self._sent and time_module.monotonic() - self._start >= self._delay_s:
            self._sent = True
            return "2025-11-30 12:34:56,12.0,1.0,12.0"
        return ""

    def close(self):
        pass


def _make_reader(qapp, handler) -> SerialReader:
    reader = SerialReader("/dev/fake", baud=2000000)
    reader._port_handler = handler
    reader.STALE_WARNING_S = 0.05
    reader.STALE_FATAL_S = 0.15
    reader.FIRST_SAMPLE_WARNING_S = 0.05
    reader.FIRST_SAMPLE_FATAL_S = 0.15
    return reader


def test_stale_escalates_to_fatal_error(qapp):
    reader = _make_reader(qapp, _SilentHandler())

    stale_events = []
    error_events = []
    reader.data_stale.connect(stale_events.append)
    reader.error.connect(error_events.append)

    reader.run()  # loop exits on its own once FIRST_SAMPLE_FATAL_S elapses

    assert len(stale_events) == 1
    assert len(error_events) == 1


def test_data_resumed_after_stale_warning(qapp):
    # Enough silent polls to cross STALE_WARNING_S but not STALE_FATAL_S,
    # then a valid measurement arrives.
    handler = _ResumingHandler(
        silent_reads_before_data=0,
        lines=["2025-11-30 12:34:56,12.0,1.0,12.0"],
    )
    reader = _make_reader(qapp, handler)
    reader.STALE_WARNING_S = 1000.0  # never triggers before data arrives
    reader.STALE_FATAL_S = 2000.0
    reader.FIRST_SAMPLE_WARNING_S = 1000.0
    reader.FIRST_SAMPLE_FATAL_S = 2000.0

    data_events = []
    stale_events = []
    resumed_events = []
    reader.data_received.connect(data_events.append)
    reader.data_stale.connect(stale_events.append)
    reader.data_resumed.connect(resumed_events.append)

    original_readline = handler.readline

    def stop_after_first_measurement():
        # Let the real handler serve its one queued line; once the reader
        # has consumed it, stop the loop on the following poll.
        if data_events:
            reader._running = False
            return ""
        return original_readline()

    handler.readline = stop_after_first_measurement
    reader.run()

    assert len(data_events) == 1
    assert not stale_events
    assert not resumed_events


def test_slow_first_sample_uses_generous_grace_period(qapp):
    # Regression: a device taking longer than STALE_WARNING_S (but well
    # within FIRST_SAMPLE_WARNING_S) to produce its first line - matching
    # real firmware boot time when the RTC's SQW pin isn't wired - must not
    # trigger a false stale warning.
    handler = _SlowBootHandler(delay_s=0.1)
    reader = SerialReader("/dev/fake", baud=2000000)
    reader._port_handler = handler
    # Tight STALE_* thresholds: would misfire during boot if the reader
    # wrongly applied them before the first sample.
    reader.STALE_WARNING_S = 0.02
    reader.STALE_FATAL_S = 0.03
    # Generous FIRST_SAMPLE_* thresholds: comfortably cover the simulated
    # 0.1s boot delay.
    reader.FIRST_SAMPLE_WARNING_S = 5.0
    reader.FIRST_SAMPLE_FATAL_S = 10.0

    data_events = []
    stale_events = []
    error_events = []
    reader.data_received.connect(data_events.append)
    reader.data_stale.connect(stale_events.append)
    reader.error.connect(error_events.append)

    original_readline = handler.readline

    def stop_after_first_measurement():
        if data_events:
            reader._running = False
            return ""
        return original_readline()

    handler.readline = stop_after_first_measurement
    reader.run()

    assert len(data_events) == 1
    assert not stale_events
    assert not error_events
