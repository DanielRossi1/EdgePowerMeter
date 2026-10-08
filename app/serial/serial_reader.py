"""Background serial reader thread.

Owns the port, performs the protocol handshake (firmware 2.x) or falls back
to the legacy CSV stream (firmware 1.x), decodes data in bulk and hands the
GUI one batch of samples per frame instead of one signal per sample.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from datetime import datetime
from typing import Optional

from PySide6 import QtCore

from ..core.samples import Samples
from .config import SerialConfig
from .handler import DeviceDisconnected, SerialPortHandler
from . import protocol as P

PROTO_UNKNOWN = "unknown"
PROTO_V2 = "v2"
PROTO_LEGACY = "legacy"


class SerialReader(QtCore.QThread):
    """Reads, decodes and batches measurements from the device.

    Signals:
        samples_ready(Samples): a batch of calibrated samples (≈ every frame).
        protocol_detected(str): PROTO_V2 or PROTO_LEGACY.
        device_info(DeviceInfo), device_config(DeviceConfig): from the handshake.
        command_reply(bool ok, str command, str message)
        device_log(str): informational lines printed by the firmware.
        clock_synced(str): the RTC was set to this local time.
        data_stale(float): no data for STALE_WARNING_S (non-fatal).
        data_resumed(): data is flowing again after a stale warning.
        error(str): fatal for this session (the thread is ending).
        disconnected(str): the device went away (fatal; eligible for reconnect).
    """

    samples_ready = QtCore.Signal(object)
    protocol_detected = QtCore.Signal(str)
    device_info = QtCore.Signal(object)
    device_config = QtCore.Signal(object)
    command_reply = QtCore.Signal(bool, str, str)
    device_log = QtCore.Signal(str)
    clock_synced = QtCore.Signal(str)
    data_stale = QtCore.Signal(float)
    data_resumed = QtCore.Signal()
    error = QtCore.Signal(str)
    disconnected = QtCore.Signal(str)

    BATCH_INTERVAL_S = 1 / 60
    READ_TIMEOUT_S = 0.02
    STALE_WARNING_S = 3.0
    # Booting firmware may be silent for a few seconds (display/RTC init,
    # SQW wait), so the first sample gets more slack.
    FIRST_SAMPLE_GRACE_S = 8.0
    HELLO_INTERVAL_S = 0.7
    # Firmware 2.x answers HELLO within milliseconds once it prints data, so
    # CSV lines with no reply this long after a HELLO mean firmware 1.x.
    LEGACY_REPLY_WAIT_S = 0.4
    # RAW lines without any HELLO reply this long (device left in RAW mode by
    # a crashed session): treat as firmware 2.x anyway.
    RAW_WITHOUT_REPLY_S = 1.5
    # A SYNC must leave right at the second boundary; if the loop is later
    # than this it waits for the next boundary instead.
    SYNC_MAX_LATENESS_S = 0.005

    def __init__(self, port: str, baud: int = SerialConfig.DEFAULT_BAUD,
                 calibration: Optional[P.Calibration] = None,
                 host_averaging: int = 1, no_data_timeout_s: float = 15.0,
                 sync_clock: bool = True, parent=None):
        super().__init__(parent)
        self._port = SerialPortHandler(port, baud)
        self._decoder = P.StreamDecoder(calibration, host_averaging)
        self._no_data_timeout = max(no_data_timeout_s, self.STALE_WARNING_S + 1)
        self._sync_clock = sync_clock
        self._commands: "queue.Queue[str]" = queue.Queue()
        self._pending_cal: Optional[P.Calibration] = None
        self._cal_lock = threading.Lock()
        # Set only by stop(); run() never clears it, so a stop issued while
        # the port is still opening is not lost.
        self._stop_requested = threading.Event()
        self.protocol = PROTO_UNKNOWN

    @property
    def port(self) -> str:
        return self._port.port

    @property
    def lost_samples(self) -> int:
        return self._decoder.total_lost

    # ------------------------------------------------------------ GUI thread

    def send_command(self, text: str) -> None:
        """Queue a protocol command (thread-safe)."""
        self._commands.put(text.strip())

    def set_calibration(self, cal: P.Calibration) -> None:
        """Apply new calibration to subsequent samples (thread-safe)."""
        with self._cal_lock:
            self._pending_cal = cal

    def request_clock_sync(self) -> None:
        self._commands.put("__SYNC__")

    @property
    def _running(self) -> bool:
        return not self._stop_requested.is_set()

    def stop(self, wait_ms: int = 3000) -> bool:
        """Ask the thread to end and wait for it. Returns True if it ended."""
        self._stop_requested.set()
        return self.wait(wait_ms)


    # ---------------------------------------------------------- reader thread

    def run(self) -> None:
        try:
            self._port.open()
        except ConnectionError as e:
            if self._running:
                self.error.emit(str(e))
            return
        try:
            if self._running:
                self._loop()
        except DeviceDisconnected as e:
            if self._running:
                # Deliver the last samples before the disconnect notification
                # (queued signals keep their order).
                self._flush_samples()
                self.disconnected.emit(str(e))
        except Exception:
            if self._running:
                self._flush_samples()
                self.error.emit(traceback.format_exc())
        finally:
            # The tail of the recording (also on a requested stop); the
            # controller still accepts it from a stopped reader.
            if self.protocol != PROTO_UNKNOWN:
                self._flush_samples()
            if self.protocol == PROTO_V2:
                # Leave the device in its human-readable default output.
                try:
                    self._send("MODE CSV")
                except Exception:
                    pass
            self._port.close()

    def _loop(self) -> None:
        dec = self._decoder
        # Samples are buffered from the first line but only handed out once
        # the format is known, so the start of the recording is not lost.
        dec.accept_data = True
        start = time.monotonic()
        last_hello = -1e9
        legacy_hello_at: Optional[float] = None
        raw_seen_at: Optional[float] = None
        last_flush = start
        last_data: Optional[float] = None
        stale = False
        sync_due: Optional[float] = None

        while self._running:
            now = time.monotonic()

            # --- handshake ------------------------------------------------
            if self.protocol == PROTO_UNKNOWN:
                if dec.legacy_lines and legacy_hello_at is None:
                    # The device is printing data, so it is past boot and any
                    # firmware 2.x would answer right away: ask once more.
                    self._send("HELLO")
                    last_hello = legacy_hello_at = now
                elif now - last_hello >= self.HELLO_INTERVAL_S:
                    self._send("HELLO")
                    last_hello = now
                if legacy_hello_at is not None and now - legacy_hello_at >= self.LEGACY_REPLY_WAIT_S:
                    self._set_protocol(PROTO_LEGACY)
                elif dec.raw_lines:
                    raw_seen_at = raw_seen_at or now
                    if now - raw_seen_at >= self.RAW_WITHOUT_REPLY_S:
                        self._start_v2_session()

            # --- outgoing commands ----------------------------------------
            while not self._commands.empty():
                cmd = self._commands.get_nowait()
                if cmd == "__SYNC__":
                    sync_due = self._next_second(now)
                else:
                    if cmd.upper().replace(" ", "") == "SETSTREAM1":
                        self._decoder.reset_sequence()
                    if not self._send(cmd):
                        self.command_reply.emit(False, cmd.split()[0].upper() if cmd else "?",
                                                "write_timeout")
            if sync_due is not None and self.protocol == PROTO_V2:
                remaining = sync_due - time.monotonic()
                if remaining < -self.SYNC_MAX_LATENESS_S:
                    # Missed the boundary (slow iteration): a late SYNC would
                    # set the RTC off by the lateness, so wait for the next one.
                    sync_due = self._next_second(time.monotonic())
                elif remaining <= self.READ_TIMEOUT_S:
                    if remaining > 0:
                        time.sleep(remaining)
                    when = datetime.fromtimestamp(round(time.time()))
                    if self._send(P.sync_command(when)):
                        self.clock_synced.emit(when.strftime("%Y-%m-%d %H:%M:%S"))
                    sync_due = None

            with self._cal_lock:
                cal, self._pending_cal = self._pending_cal, None
            if cal is not None:
                dec.calibration = cal

            # --- input ----------------------------------------------------
            data = self._port.read_chunk(self.READ_TIMEOUT_S)
            if data:
                dec.feed(data)
                for kind, payload in dec.take_events():
                    sync_due = self._on_event(kind, payload, sync_due)

            now = time.monotonic()
            if dec.has_pending:
                last_data = now
                if stale:
                    stale = False
                    self.data_resumed.emit()
            if now - last_flush >= self.BATCH_INTERVAL_S and self.protocol != PROTO_UNKNOWN:
                self._flush_samples()
                last_flush = now

            # --- watchdog -------------------------------------------------
            # Slow sensor settings (e.g. 1024 averages x 8.2 ms = 16.9 s per
            # sample) are legitimate: the thresholds scale with the period.
            period_s = dec.period_us / 1e6 if self.protocol == PROTO_V2 else 0.0
            if last_data is None:
                idle = now - start
                warn_at = self.FIRST_SAMPLE_GRACE_S + 2.5 * period_s
                fatal_at = warn_at + self._no_data_timeout
            else:
                idle = now - last_data
                warn_at = max(self.STALE_WARNING_S, 2.5 * period_s)
                fatal_at = self._no_data_timeout + 3.0 * period_s
            if idle >= fatal_at:
                self.error.emit(f"NO_DATA:{idle:.0f}")
                return
            if idle >= warn_at and not stale:
                stale = True
                self.data_stale.emit(idle)

    def _on_event(self, kind: str, payload, sync_due: Optional[float]) -> Optional[float]:
        if kind == P.EV_HELLO:
            self.device_info.emit(payload)
            if self.protocol == PROTO_UNKNOWN:
                self._start_v2_session()
                if self._sync_clock:
                    sync_due = self._next_second(time.monotonic())
            # A HELLO reply after legacy mode was declared is ignored: switching
            # now would restart the time axis in the middle of the recording.
        elif kind == P.EV_CONFIG:
            self.device_config.emit(payload)
        elif kind == P.EV_OK:
            self.command_reply.emit(True, str(payload), "")
            if str(payload).upper().startswith(("SET", "SAVE")):
                self._send("GET")
        elif kind == P.EV_ERR:
            cmd, reason = payload
            self.command_reply.emit(False, cmd, reason)
        elif kind == P.EV_LOG:
            text = str(payload)
            self.device_log.emit(text)
            if self.protocol == PROTO_V2 and "ready (protocol" in text:
                # The device rebooted without leaving the USB bus (panic,
                # watchdog): it is back in CSV mode, so restore RAW streaming.
                self._decoder.reset_sequence()
                self._start_v2_commands()
        return sync_due

    def _start_v2_session(self) -> None:
        self._set_protocol(PROTO_V2)
        self._start_v2_commands()

    def _start_v2_commands(self) -> None:
        self._send("GET")
        # A previous session (or the console) may have paused the stream.
        self._send("SET STREAM 1")
        self._send("MODE RAW")

    def _send(self, text: str) -> bool:
        """Write a command; a device that does not drain its input (e.g. old
        firmware that never reads) must not end the session."""
        try:
            self._port.write_line(text)
            return True
        except TimeoutError:
            return False

    def _set_protocol(self, proto: str) -> None:
        self.protocol = proto
        if proto == PROTO_V2:
            # Firmware 2.x keeps printing CSV until it processes MODE RAW; those
            # lines must not be mixed into the device-clock time base.
            self._decoder.raw_only = True
            self._decoder.discard_legacy_pending()
            self._decoder.reset_timebase()
        self.protocol_detected.emit(proto)

    @staticmethod
    def _next_second(now_mono: float) -> float:
        frac = time.time() % 1.0
        return now_mono + (1.0 - frac)

    def _flush_samples(self) -> None:
        batch: Samples = self._decoder.take_samples()
        if len(batch):
            self.samples_ready.emit(batch)
