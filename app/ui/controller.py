"""Acquisition controller: owns the device session and the recorded data.

Kept free of widgets so the GUI pages only observe it, and so the
connection / reconnection / data-retention rules can be tested headless.

Rules:
  * Start creates a new recording; existing unsaved data is only discarded
    after the UI confirmed it (see `has_unsaved_data`).
  * Stop is final: nothing restarts the acquisition behind the user's back.
  * An *unexpected* disconnect (USB unplugged, port hung up) keeps the data
    and, with auto-reconnect enabled, polls for the port to come back and
    then appends to the same recording, bridging the gap in the time axis.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6 import QtCore

from ..core import AppSettings, RunningStats, Samples, SampleStore
from ..core.markers import Marker, MarkerList
from ..export.csv_io import CsvFormat
from ..export import recorder
from ..export.recorder import RecordingWriter
from ..i18n import tr
from ..serial import PROTO_V2, Calibration, DeviceConfig, DeviceInfo, SerialReader
from ..serial.handler import ERR_PORT_BUSY, ERR_PORT_MISSING, ERR_PORT_PERMISSION
from .widgets.port_discovery import PortDiscovery

STATE_IDLE = "idle"
STATE_CONNECTING = "connecting"
STATE_STREAMING = "streaming"
STATE_STALE = "stale"
STATE_RECONNECTING = "reconnecting"

SOURCE_NONE = ""
SOURCE_LIVE = "live"
SOURCE_IMPORT = "import"


class AcquisitionController(QtCore.QObject):
    state_changed = QtCore.Signal(str)
    data_appended = QtCore.Signal()
    data_reset = QtCore.Signal()
    protocol_changed = QtCore.Signal(str)
    device_changed = QtCore.Signal()
    device_log = QtCore.Signal(str)
    command_reply = QtCore.Signal(bool, str, str)
    clock_synced = QtCore.Signal(str)
    # level: 'info' | 'warning' | 'error'
    message = QtCore.Signal(str, str)
    markers_changed = QtCore.Signal()
    recording_changed = QtCore.Signal()

    RECONNECT_POLL_MS = 1000
    RECORDING_FLUSH_MS = 1000
    # Batches a stopped reader still has queued arrive shortly after Stop;
    # the recording file is finished after them.
    RECORDING_FINISH_DELAY_MS = 400

    def __init__(self, settings: AppSettings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.store = SampleStore()
        self.stats = RunningStats()
        self.reader: Optional[SerialReader] = None
        self.state = STATE_IDLE
        self.port: Optional[str] = None
        self.protocol: str = ""
        self.device_info: Optional[DeviceInfo] = None
        self.device_config: Optional[DeviceConfig] = None
        self.source = SOURCE_NONE
        self.source_name = ""
        self.has_unsaved_data = False
        self.lost_samples = 0
        self._lost_base = 0
        self.gaps = 0
        self.last_clock_sync = ""
        # Recording time of the last calibration change (samples before it
        # were converted with the previous calibration).
        self.calibration_changed_t = float("-inf")
        # (BenchmarkInput, (t0, t1), range label) set by the Analysis page
        # for the PDF report, or None.
        self.benchmark = None

        self._t_offset = 0.0
        self._resume_host_time: Optional[float] = None
        self._last_host_time = 0.0
        self._rebase_next = False
        # Readers stopped during this recording: batches they emitted just
        # before stopping are still queued and belong to the recording.
        self._retired: list = []

        self.markers = MarkerList()
        # Continuous recording to disk (live sessions with autosave enabled).
        # The writer is kept after Stop so marker edits can still be appended.
        self.writer: Optional[RecordingWriter] = None
        self._writer_warned = False
        self._flush_timer = QtCore.QTimer(self)
        self._flush_timer.setInterval(self.RECORDING_FLUSH_MS)
        self._flush_timer.timeout.connect(self._flush_recording)

        self._reconnect_timer = QtCore.QTimer(self)
        self._reconnect_timer.setInterval(self.RECONNECT_POLL_MS)
        self._reconnect_timer.timeout.connect(self._poll_reconnect)

    # ----------------------------------------------------------- properties

    @property
    def is_running(self) -> bool:
        return self.state in (STATE_CONNECTING, STATE_STREAMING, STATE_STALE, STATE_RECONNECTING)

    @property
    def is_v2(self) -> bool:
        return self.protocol == PROTO_V2

    def data(self) -> Samples:
        return self.store.view()

    def recent_rate(self, window_s: float = 1.0) -> float:
        s = self.store.view()
        if len(s) < 3:
            return 0.0
        k = int(np.searchsorted(s.t, s.t[-1] - window_s))
        span = float(s.t[-1] - s.t[k])
        return (len(s) - 1 - k) / span if span > 0 else 0.0

    def window_avg_power(self, window_s: float) -> float:
        s = self.store.view()
        if not len(s):
            return 0.0
        k = int(np.searchsorted(s.t, s.t[-1] - window_s))
        return float(np.mean(s.p[k:], dtype=np.float64))

    # -------------------------------------------------------------- control

    @property
    def recording_path(self) -> Optional[Path]:
        w = self.writer
        return w.path if w is not None and w.path.exists() else None

    @property
    def is_recording_to_disk(self) -> bool:
        return self.writer is not None and self.writer.is_open and not self.writer.error

    def recordings_folder(self) -> Path:
        d = self.settings.recordings_dir.strip()
        return Path(d).expanduser() if d else recorder.default_folder()

    def start(self, port: str) -> None:
        """Begin a new recording on `port` (caller confirmed discarding data)."""
        self._stop_reader()
        self._reconnect_timer.stop()
        self._finish_recording()
        self.clear()
        self.port = port
        self.source = SOURCE_LIVE
        self.source_name = port
        self._t_offset = 0.0
        self._resume_host_time = None
        self._last_host_time = 0.0
        self._rebase_next = False
        self._open_recording()
        self._open_reader()

    def stop(self) -> None:
        self._reconnect_timer.stop()
        was_running = self.reader is not None
        self._stop_reader()
        self._set_state(STATE_IDLE)
        if was_running and self.writer is not None and self.writer.is_open:
            QtCore.QTimer.singleShot(self.RECORDING_FINISH_DELAY_MS, self._finish_recording)

    def _release_retired(self) -> None:
        for r in self._retired:
            self._release(r)
        self._retired.clear()

    def clear(self) -> None:
        self._release_retired()      # late batches of the old data are not wanted
        if self.is_running:
            # Clearing during a live run starts a new time axis at 0.
            self._rebase_next = True
            self._resume_host_time = None
        self.store.clear()
        self.stats.reset()
        self.has_unsaved_data = False
        self.lost_samples = self._lost_base = 0
        self.gaps = 0
        self.markers.clear()
        if self.is_running:
            # The data on disk is discarded too and a fresh file started.
            if self.writer is not None:
                self.writer.discard()
                self.writer = None
            self._open_recording()
        else:
            self._finish_recording()
            self.writer = None           # the file stays on disk, the view is empty
            self.source = SOURCE_NONE
            self.source_name = ""
        self.markers_changed.emit()
        self.recording_changed.emit()
        self.data_reset.emit()

    def load(self, samples: Samples, name: str, markers: Optional[MarkerList] = None) -> None:
        """Replace the data with an imported recording."""
        self.stop()
        self._finish_recording()
        self.writer = None
        self._release_retired()
        self.markers = markers if markers is not None else MarkerList()
        self.store.replace(samples)
        self.stats.rebuild(samples)
        self.source = SOURCE_IMPORT
        self.source_name = name
        self.has_unsaved_data = False
        self.lost_samples = self._lost_base = 0
        self.gaps = 0
        self.markers_changed.emit()
        self.recording_changed.emit()
        self.data_reset.emit()

    def mark_saved(self) -> None:
        self.has_unsaved_data = False

    # -------------------------------------------------------------- markers

    def live_time(self) -> Optional[float]:
        """Best estimate of the recording time 'now' (for markers added live):
        the newest sample time plus the time elapsed since it arrived."""
        if not len(self.store):
            return None
        if self.state != STATE_STREAMING:
            return self.store.last_t
        return self.store.last_t + min(0.5, max(0.0, time.time() - self._last_host_time))

    def add_marker(self, t: Optional[float] = None, label: str = "") -> Optional[Marker]:
        if t is None:
            t = self.live_time()
        if t is None:
            return None
        m = self.markers.add(t, label)
        self._marker_saved(m)
        self.markers_changed.emit()
        return m

    def rename_marker(self, marker_id: int, label: str) -> None:
        m = self.markers.rename(marker_id, label)
        if m is not None:
            self._marker_saved(m)
            self.markers_changed.emit()

    def move_marker(self, marker_id: int, t: float) -> None:
        m = self.markers.move(marker_id, t)
        if m is not None:
            self._marker_saved(m)
            self.markers_changed.emit()

    def remove_marker(self, marker_id: int) -> None:
        if self.markers.remove(marker_id):
            if self.writer is not None:
                self.writer.marker_deleted(marker_id)
                self._flush_if_closed()
            else:
                self.has_unsaved_data = True
            self.markers_changed.emit()

    def clear_markers(self) -> None:
        for m in self.markers.sorted():
            self.remove_marker(m.id)

    def _marker_saved(self, m: Marker) -> None:
        if self.writer is not None:
            self.writer.marker_changed(m)
            self._flush_if_closed()
        else:
            self.has_unsaved_data = True   # imported data: needs an export

    def _flush_if_closed(self) -> None:
        # While recording the periodic flush writes it; afterwards write now.
        if self.writer is not None and not self.writer.is_open:
            self.writer.flush()

    # ------------------------------------------------------------- recording

    def open_recordings_folder(self) -> None:
        from PySide6 import QtGui
        folder = self.recording_path.parent if self.recording_path else self.recordings_folder()
        folder.mkdir(parents=True, exist_ok=True)
        QtGui.QDesktopServices.openUrl(QtCore.QUrl.fromLocalFile(str(folder)))

    def _open_recording(self) -> None:
        self.writer = None
        if not self.settings.autosave:
            self.recording_changed.emit()
            return
        s = self.settings
        try:
            self.writer = RecordingWriter(self.recordings_folder(),
                                          CsvFormat(s.csv_separator, s.csv_decimal, s.csv_time_format))
            self._flush_timer.start()
        except OSError as e:
            self.message.emit("warning", tr(
                "Cannot save the recording in {folder}: {error}. The data is kept in memory only.",
                folder=str(self.recordings_folder()), error=str(e)))
        self._writer_warned = False
        self.recording_changed.emit()

    def _flush_recording(self) -> None:
        w = self.writer
        if w is None:
            self._flush_timer.stop()
            return
        w.flush()
        if w.error and not self._writer_warned:
            self._writer_warned = True
            self.has_unsaved_data = True
            self.message.emit("error", tr(
                "Writing the recording file failed ({error}). Export the data before closing.",
                error=w.error))
            self.recording_changed.emit()

    def _finish_recording(self) -> None:
        self._flush_timer.stop()
        w = self.writer
        if w is None or not w.is_open:
            return
        kept = w.close()
        if not kept:
            self.writer = None
        elif not w.error:
            self.has_unsaved_data = False
        self.recording_changed.emit()

    def send_command(self, cmd: str) -> bool:
        if self.reader is None or not self.reader.isRunning():
            return False
        self.reader.send_command(cmd)
        return True

    def request_clock_sync(self) -> bool:
        if self.reader is None or not self.is_v2:
            return False
        self.reader.request_clock_sync()
        return True

    def apply_calibration(self) -> None:
        self.calibration_changed_t = self.store.last_t if len(self.store) else float("-inf")
        if self.reader is not None:
            self.reader.set_calibration(Calibration.from_settings(self.settings))

    def shutdown(self) -> None:
        self._reconnect_timer.stop()
        self._stop_reader()
        self._release_retired()
        self._finish_recording()

    # ------------------------------------------------------------ internals

    def _open_reader(self) -> None:
        s = self.settings
        r = SerialReader(self.port, baud=s.baud_rate,
                         calibration=Calibration.from_settings(s),
                         host_averaging=s.host_averaging,
                         no_data_timeout_s=s.no_data_timeout_s,
                         sync_clock=s.sync_clock_on_connect)
        q = QtCore.Qt.QueuedConnection
        r.samples_ready.connect(self._on_samples, q)
        r.protocol_detected.connect(self._on_protocol, q)
        r.device_info.connect(self._on_device_info, q)
        r.device_config.connect(self._on_device_config, q)
        r.command_reply.connect(self._on_command_reply, q)
        r.device_log.connect(self._on_device_log, q)
        r.clock_synced.connect(self._on_clock_synced, q)
        r.data_stale.connect(self._on_stale, q)
        r.data_resumed.connect(self._on_resumed, q)
        r.error.connect(self._on_error, q)
        r.disconnected.connect(self._on_disconnected, q)
        r.finished.connect(self._on_reader_finished, q)
        self.reader = r
        self.protocol = ""
        self.device_info = None
        self.device_config = None
        self.device_changed.emit()
        self._set_state(STATE_CONNECTING)
        r.start()

    def _stop_reader(self) -> None:
        r, self.reader = self.reader, None
        if r is None:
            return
        r.stop()
        # samples_ready stays connected: the batches the thread emitted before
        # ending (including its final flush) are still queued, and PySide6
        # discards queued calls of a disconnected signal. They are accepted as
        # the tail of the recording while the reader is "retired".
        for sig in (r.protocol_detected, r.device_info, r.device_config,
                    r.command_reply, r.device_log, r.clock_synced, r.data_stale,
                    r.data_resumed, r.error, r.disconnected, r.finished):
            try:
                sig.disconnect()
            except (RuntimeError, TypeError):
                pass
        self._lost_base += r.lost_samples
        self._retired.append(r)
        if len(self._retired) > 8:
            self._release(self._retired.pop(0))

    @staticmethod
    def _release(r) -> None:
        try:
            r.samples_ready.disconnect()
        except (RuntimeError, TypeError):
            pass
        r.deleteLater()

    def _from_current_reader(self) -> bool:
        # Queued signals from a reader that was already replaced/stopped can
        # still be delivered after it was stopped; ignore them. Once that
        # reader object is deleted Qt reports sender() as None, so "no active
        # reader" must also reject the call.
        if self.reader is None:
            return False
        sender = self.sender()
        return sender is None or sender is self.reader

    def _set_state(self, state: str) -> None:
        if state != self.state:
            self.state = state
            self.state_changed.emit(state)

    def _on_samples(self, batch: Samples) -> None:
        if not len(batch):
            return
        sender = self.sender()
        if sender is not None and sender is not self.reader:
            if any(sender is r for r in self._retired):
                self._ingest(batch)       # tail of a stopped session, in order
            return
        if not self._from_current_reader():
            return
        self._ingest(batch)
        if self.reader is not None:
            self.lost_samples = self._lost_base + self.reader.lost_samples
        if self.state != STATE_STREAMING:
            self._set_state(STATE_STREAMING)

    def _ingest(self, batch: Samples) -> None:
        """Append a batch to the recording (time-axis bridging after reconnects)."""
        now = time.time()
        if self._rebase_next:
            self._t_offset = -float(batch.t[0])
            self._rebase_next = False
        elif self._resume_host_time is not None:
            # First batch after a reconnect: the device clock restarted, so
            # continue the time axis from where it stopped plus the real
            # (host-measured) outage duration. Nothing to bridge if the device
            # dropped before sending anything.
            if len(self.store):
                self._t_offset = self.store.last_t + max(0.0, now - self._last_host_time)
                self.gaps += 1
            self._resume_host_time = None
        if self._t_offset:
            batch = Samples(batch.t + self._t_offset, batch.wall, batch.v, batch.i, batch.p)
        self._last_host_time = now
        self.store.append(batch)
        self.stats.update(batch)
        if self.writer is not None and self.writer.is_open and not self.writer.error:
            self.writer.append(batch)     # saved within a second: nothing to lose
        else:
            self.has_unsaved_data = True
        self.data_appended.emit()

    def _on_protocol(self, proto: str) -> None:
        if not self._from_current_reader():
            return
        self.protocol = proto
        self.protocol_changed.emit(proto)
        self.device_changed.emit()
        if proto != PROTO_V2:
            self.message.emit("info", tr(
                "Firmware 1.x detected: device time, lost-sample detection and remote "
                "configuration need firmware 2.0."))

    def _on_device_info(self, info: DeviceInfo) -> None:
        if not self._from_current_reader():
            return
        self.device_info = info
        self.device_changed.emit()

    def _on_device_config(self, cfg: DeviceConfig) -> None:
        if not self._from_current_reader():
            return
        self.device_config = cfg
        self.device_changed.emit()

    def _on_command_reply(self, ok: bool, cmd: str, msg: str) -> None:
        if self._from_current_reader():
            self.command_reply.emit(ok, cmd, msg)

    def _on_device_log(self, text: str) -> None:
        if not self._from_current_reader():
            return
        self.device_log.emit(text)
        for prefix in ("ERROR", "WARN"):
            if text.startswith(prefix):
                self.message.emit("warning", tr("Device reports: {text}",
                                                text=text[len(prefix):].strip()))
                break

    def _on_clock_synced(self, when: str) -> None:
        if not self._from_current_reader():
            return
        self.last_clock_sync = when
        self.clock_synced.emit(when)

    def _on_stale(self, seconds: float) -> None:
        if not self._from_current_reader():
            return
        self._set_state(STATE_STALE)
        self.message.emit("warning", tr("No data received for {seconds}s", seconds=f"{seconds:.0f}"))

    def _on_resumed(self) -> None:
        if not self._from_current_reader():
            return
        self._set_state(STATE_STREAMING)

    def _on_error(self, msg: str) -> None:
        if not self._from_current_reader():
            return
        if self._resume_host_time is not None and not msg.startswith("NO_DATA:"):
            # The port reappeared but could not be opened yet (udev still
            # setting permissions, port busy): keep waiting for it.
            self._stop_reader()
            self._set_state(STATE_RECONNECTING)
            self._reconnect_timer.start()
            return
        if msg.startswith("NO_DATA:"):
            text = tr("No data received for {seconds}s: the device may be stuck or "
                      "running incompatible firmware.", seconds=msg.split(":", 1)[1])
        elif msg.startswith(ERR_PORT_MISSING):
            text = tr("The port {port} no longer exists: the device was disconnected or is "
                      "resetting. Check the USB cable, reconnect the meter and press Start again.",
                      port=self.port)
        elif msg.startswith(ERR_PORT_PERMISSION):
            text = tr("Permission denied on {port}. On Linux add your user to the 'dialout' "
                      "group (sudo usermod -a -G dialout $USER) and log in again.", port=self.port)
        elif msg.startswith(ERR_PORT_BUSY):
            text = tr("The port {port} is in use by another program (serial monitor, "
                      "another instance of this app). Close it and try again.", port=self.port)
        else:
            text = tr("Serial error on {port}: {error}", port=self.port, error=msg)
        self.stop()
        self.message.emit("error", text)

    def _on_disconnected(self, reason: str) -> None:
        if not self._from_current_reader():
            return
        self._stop_reader()
        if self.settings.auto_reconnect:
            self._resume_host_time = time.time()
            self._set_state(STATE_RECONNECTING)
            self.message.emit("warning", tr("Device disconnected. Reconnecting..."))
            self._reconnect_timer.start()
        else:
            self._set_state(STATE_IDLE)
            self.message.emit("error", tr("Device disconnected."))

    def _on_reader_finished(self) -> None:
        # Thread ended on its own (e.g. port open failed after emitting error).
        if self._from_current_reader() and self.state == STATE_CONNECTING \
                and self._resume_host_time is None:
            self._stop_reader()
            self._set_state(STATE_IDLE)

    def _poll_reconnect(self) -> None:
        if self.state != STATE_RECONNECTING or not self.port:
            self._reconnect_timer.stop()
            return
        if self.port in PortDiscovery.list_ports():
            self._reconnect_timer.stop()
            self._open_reader()
            # _open_reader moved us to CONNECTING; samples will flip it to
            # STREAMING and bridge the time axis.
