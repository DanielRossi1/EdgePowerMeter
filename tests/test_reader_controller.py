"""SerialReader handshake/watchdog and AcquisitionController rules, with a fake device."""
from __future__ import annotations

import time

import numpy as np
import pytest

pytest.importorskip("PySide6.QtWidgets")

from app.core import AppSettings
from app.serial import PROTO_LEGACY, PROTO_V2, SerialReader
from app.ui import controller as C
from tests.fakes import FakePort


def _pump(qapp, seconds: float, until=None) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        if until is not None and until():
            return
        time.sleep(0.005)


def _reader(port: FakePort, **kw) -> SerialReader:
    r = SerialReader("/dev/fake", sync_clock=kw.pop("sync_clock", False), **kw)
    r._port = port
    r.HELLO_INTERVAL_S = 0.1
    return r


def test_v2_handshake_switches_to_raw(qapp):
    port = FakePort("v2")
    r = _reader(port, sync_clock=True)
    seen = {"proto": None, "cfg": None, "n": 0, "sync": None}
    r.protocol_detected.connect(lambda p: seen.__setitem__("proto", p))
    r.device_config.connect(lambda c: seen.__setitem__("cfg", c))
    r.samples_ready.connect(lambda b: seen.__setitem__("n", seen["n"] + len(b)))
    r.clock_synced.connect(lambda w: seen.__setitem__("sync", w))
    r.start()
    _pump(qapp, 2.5, lambda: seen["sync"] and seen["n"] > 200)
    r.stop()
    _pump(qapp, 0.1)
    assert seen["proto"] == PROTO_V2
    assert seen["cfg"].period_us == 1000
    assert seen["n"] > 200
    assert "MODE RAW" in port.written and "GET" in port.written
    assert any(w.startswith("SYNC ") for w in port.written)
    assert seen["sync"]


def test_legacy_firmware_detected(qapp):
    port = FakePort("v1")
    r = _reader(port)
    seen = {"proto": None, "n": 0}
    r.protocol_detected.connect(lambda p: seen.__setitem__("proto", p))
    r.samples_ready.connect(lambda b: seen.__setitem__("n", seen["n"] + len(b)))
    r.start()
    _pump(qapp, 2.0, lambda: seen["n"] > 50)
    r.stop()
    assert seen["proto"] == PROTO_LEGACY
    assert seen["n"] > 50


def test_legacy_detected_fast_and_keeps_first_samples(qapp):
    port = FakePort("v1", rate_hz=500)
    r = _reader(port)
    r.HELLO_INTERVAL_S = 0.7
    got = []
    r.samples_ready.connect(lambda b: got.append(b))
    t0 = time.monotonic()
    r.start()
    _pump(qapp, 3.0, lambda: got)
    latency = time.monotonic() - t0
    r.stop()
    assert got and latency < 1.0
    assert got[0].t[0] == 0.0
    assert len(got[0]) > 100            # lines received while deciding are kept


def test_v2_drops_csv_lines_printed_before_raw_mode(qapp):
    port = FakePort("v2", rate_hz=500)
    r = _reader(port)
    got = []
    r.samples_ready.connect(lambda b: got.append(b))
    r.start()
    _pump(qapp, 1.5, lambda: sum(map(len, got)) > 200)
    r.stop()
    assert got
    # Every sample has the RAW calibration (4.0 V / 0.1 A); CSV lines were dropped
    # and the time base is the device clock starting at 0.
    assert all(abs(float(b.v.min()) - 4.0) < 1e-6 for b in got)
    assert got[0].t[0] == 0.0
    assert port.written[-1] == "MODE CSV"


def test_slow_sensor_settings_do_not_trip_watchdog(qapp):
    """Regression: a period longer than the stale/no-data thresholds (e.g.
    1024 averages x 8.2 ms = 16.9 s) must not be reported as a dead device."""
    r = _reader(FakePort("v2", rate_hz=2.0), no_data_timeout_s=0.4)   # 0.5 s period
    r.STALE_WARNING_S = 0.2
    r._no_data_timeout = 0.4
    events = []
    r.data_stale.connect(lambda s: events.append("stale"))
    r.error.connect(lambda e: events.append(e))
    r.samples_ready.connect(lambda b: events.append("samples"))
    r.start()
    _pump(qapp, 3.0)
    r.stop()
    assert "samples" in events
    assert "stale" not in events and not any(str(e).startswith("NO_DATA") for e in events)


def test_stop_while_opening_does_not_leave_thread(qapp):
    """Regression: stop() during open() used to be overwritten by run()."""
    port = FakePort("v2")
    original_open = port.open

    def slow_open():
        time.sleep(0.3)
        original_open()

    port.open = slow_open
    r = _reader(port)
    r.start()
    time.sleep(0.05)
    assert r.stop(2000)
    assert not r.isRunning()


def test_seq_gaps_reported_as_lost(qapp):
    port = FakePort("v2", skip_seq_every=10)
    r = _reader(port)
    r.start()
    _pump(qapp, 1.0)
    r.stop()
    assert r.lost_samples > 10


def test_device_left_streaming_raw_drops_stale_buffer(qapp):
    port = FakePort("v2", stale_raw_lines=150)
    r = _reader(port)
    batches = []
    r.samples_ready.connect(batches.append)
    r.start()
    _pump(qapp, 2.0, lambda: sum(len(b) for b in batches) > 300)
    r.stop()
    _pump(qapp, 0.1)
    assert r.protocol == PROTO_V2
    assert r.lost_samples == 0
    t = np.concatenate([b.t for b in batches])
    assert t[0] == 0.0
    assert np.max(np.diff(t)) < 0.05        # no multi-second hole at the start


def test_silence_warns_then_fails(qapp):
    r = _reader(FakePort("v2", silent=True), no_data_timeout_s=0.3)
    r.FIRST_SAMPLE_GRACE_S = 0.1
    r.STALE_WARNING_S = 0.05
    r._no_data_timeout = 0.2
    events = []
    r.data_stale.connect(lambda s: events.append("stale"))
    r.error.connect(lambda e: events.append(e))
    r.start()
    _pump(qapp, 2.0, lambda: len(events) >= 2)
    r.stop()
    assert events[0] == "stale"
    assert events[1].startswith("NO_DATA:")


def test_slow_boot_is_not_reported_as_stale(qapp):
    r = _reader(FakePort("v2", boot_delay_s=0.4))
    r.FIRST_SAMPLE_GRACE_S = 1.0
    events = []
    r.data_stale.connect(lambda s: events.append("stale"))
    r.start()
    _pump(qapp, 1.0)
    r.stop()
    assert events == []


def test_unplug_emits_disconnected(qapp):
    r = _reader(FakePort("v2", disconnect_after_s=0.3))
    events = []
    r.disconnected.connect(lambda e: events.append(e))
    r.start()
    _pump(qapp, 1.5, lambda: events)
    r.stop()
    assert events


# ----------------------------------------------------------------- controller


@pytest.fixture
def ctrl(qapp, monkeypatch):
    ports = {"list": ["/dev/fake"], "fakes": []}

    class PatchedReader(SerialReader):
        def __init__(self, port, **kw):
            super().__init__(port, **kw)
            fake = ports["fakes"].pop(0) if ports["fakes"] else FakePort("v2")
            self._port = fake
            self.HELLO_INTERVAL_S = 0.1

    monkeypatch.setattr(C, "SerialReader", PatchedReader)
    monkeypatch.setattr(C.PortDiscovery, "list_ports", staticmethod(lambda: list(ports["list"])))
    s = AppSettings()
    s.sync_clock_on_connect = False
    c = C.AcquisitionController(s)
    c.RECONNECT_POLL_MS = 50
    c._reconnect_timer.setInterval(50)
    c._ports = ports
    yield c
    c.shutdown()


def test_manual_stop_never_reconnects_and_keeps_data(qapp, ctrl):
    """Regression: auto-reconnect used to restart after Stop and wipe the data."""
    ctrl.start("/dev/fake")
    _pump(qapp, 2.0, lambda: len(ctrl.store) > 300)
    ctrl.stop()
    _pump(qapp, 0.2)                  # the tail of the recording is delivered
    n = len(ctrl.store)
    assert n > 300 and ctrl.state == C.STATE_IDLE
    _pump(qapp, 0.5)
    assert ctrl.state == C.STATE_IDLE
    assert len(ctrl.store) == n


def test_unplug_reconnects_and_appends(qapp, ctrl):
    ctrl._ports["fakes"] = [FakePort("v2", disconnect_after_s=0.6), FakePort("v2")]
    ctrl.start("/dev/fake")
    _pump(qapp, 3.0, lambda: ctrl.state == C.STATE_RECONNECTING)
    assert ctrl.state == C.STATE_RECONNECTING
    n_before = len(ctrl.store)
    t_before = ctrl.store.last_t
    _pump(qapp, 3.0, lambda: len(ctrl.store) > n_before + 200)
    assert ctrl.state == C.STATE_STREAMING
    data = ctrl.data()
    assert len(data) > n_before                       # appended, not wiped
    assert data.t[n_before] > t_before                # time axis continues
    assert (data.t[1:] >= data.t[:-1]).all()
    assert ctrl.gaps == 1


def test_unplug_without_auto_reconnect_goes_idle(qapp, ctrl):
    ctrl.settings.auto_reconnect = False
    ctrl._ports["fakes"] = [FakePort("v2", disconnect_after_s=0.4)]
    ctrl.start("/dev/fake")
    _pump(qapp, 2.0, lambda: ctrl.state == C.STATE_IDLE)
    assert ctrl.state == C.STATE_IDLE
    assert len(ctrl.store) > 0


def test_reconnect_before_first_batch_starts_at_zero(qapp, ctrl):
    """Regression: the time axis was offset by ~1.79e9 s when the device dropped
    before sending anything and then came back."""
    ctrl._ports["fakes"] = [FakePort("v2", silent=True, disconnect_after_s=0.2), FakePort("v2")]
    ctrl.start("/dev/fake")
    _pump(qapp, 4.0, lambda: len(ctrl.store) > 100)
    assert len(ctrl.store) > 100
    assert ctrl.data().t[0] < 1.0
    assert ctrl.gaps == 0


def test_stop_keeps_last_batch(qapp, ctrl):
    ctrl.start("/dev/fake")
    _pump(qapp, 1.5, lambda: len(ctrl.store) > 300)
    reader = ctrl.reader
    ctrl.stop()
    _pump(qapp, 0.3)
    # Everything the reader decoded reached the store (no tail lost on Stop).
    assert len(ctrl.store) >= reader._decoder.raw_lines - 5


def test_clear_while_running_restarts_time_axis(qapp, ctrl):
    ctrl.start("/dev/fake")
    _pump(qapp, 1.5, lambda: len(ctrl.store) > 300 and ctrl.store.last_t > 0.3)
    ctrl.clear()
    _pump(qapp, 1.0, lambda: len(ctrl.store) > 100)
    assert ctrl.data().t[0] == pytest.approx(0.0, abs=0.05)


def test_load_and_clear(qapp, ctrl):
    from app.core import Samples
    s = Samples.from_columns([0, 1, 2], [0, 1, 2], [1, 1, 1], [1, 1, 1], [1, 1, 1])
    ctrl.load(s, "file.csv")
    assert len(ctrl.store) == 3 and ctrl.source == C.SOURCE_IMPORT
    assert not ctrl.has_unsaved_data
    ctrl.clear()
    assert ctrl.store.is_empty
