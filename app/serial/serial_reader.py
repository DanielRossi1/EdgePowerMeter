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
from collections import deque
from datetime import datetime
from typing import Optional, Tuple

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
        port_opened(): the port opened; emitted before any samples_ready.
    """

    port_opened = QtCore.Signal()
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
    # A late HELLO reply still upgrades a legacy declaration: keep asking for
    # a while after it (firmware 2.x may be busy or slow to answer).
    LEGACY_RECHECK_S = 10.0
    LEGACY_HELLO_INTERVAL_S = 2.0
    # RAW lines without any HELLO reply this long (device left in RAW mode by
    # a crashed session): treat as firmware 2.x anyway.
    RAW_WITHOUT_REPLY_S = 1.5
    # A SYNC must leave right at the second boundary; if the loop is later
    # than this it waits for the next boundary instead.
    SYNC_MAX_LATENESS_S = 0.005
    # MODE RAW is resent while its "!OK MODE" is outstanding (a reply can be
    # dropped by the device when its TX buffer is full).
    MODE_RETRY_S = 1.0
    # Watchdog thresholds scale with the device's conversion period, but never
    # beyond this (a bad or hostile !CFG must not silence the watchdog).
    WATCHDOG_MAX_S = 60.0
    # A GET that follows a !OK SET/SAVE is sent at most this often.
    FOLLOWUP_GET_MIN_GAP_S = 0.2

    def __init__(self, port: str, baud: int = SerialConfig.DEFAULT_BAUD,
                 calibration: Optional[P.Calibration] = None,
                 host_averaging: int = 1, no_data_timeout_s: float = 15.0,
                 sync_clock: bool = True, parent=None):
        super().__init__(parent)
        self._port = SerialPortHandler(port, baud)
        self._decoder = P.StreamDecoder(calibration, host_averaging,
                                       host_wall_clock=time.time,
                                       require_mode_ack=True)
        # When MODE RAW was last sent in this session (resent if not acknowledged).
        self._mode_sent_at = 0.0
        self._no_data_timeout = max(no_data_timeout_s, self.STALE_WARNING_S + 1)
        self._sync_clock = sync_clock
        self._commands: "queue.Queue[str]" = queue.Queue()
        self._pending_cal: Optional[P.Calibration] = None
        self._cal_lock = threading.Lock()
        # Set only by stop(); run() never clears it, so a stop issued while
        # the port is still opening is not lost.
        self._stop_requested = threading.Event()
        self.protocol = PROTO_UNKNOWN
        # True after SET STREAM 0 was acknowledged (!OK SET), until STREAM 1.
        self._paused = False
        # One entry per SET written and not yet answered, in order: True/False
        # for SET STREAM 1/0, None for other parameters. The firmware answers
        # every SET with exactly one !OK SET or !ERR SET, so replies match requests.
        self._set_queue: "deque[Optional[bool]]" = deque()
        # SET/SAVE commands written and not yet answered. A !OK SET/SAVE only
        # triggers a GET refresh while this is positive, so a device that
        # answers GET with "!OK SET" cannot make the host loop.
        self._ok_replies_owed = 0
        self._last_followup_get = -1e9

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
        if self._running:
            self.port_opened.emit()
        try:
            if self._running:
                self._loop()
        except DeviceDisconnected as e:
            if self._running:
                # Deliver the last samples before the disconnect notification
                # (queued signals keep their order).
                self._flush_samples_safely()
                self.disconnected.emit(str(e))
        except Exception:
            if self._running:
                # Must not stop the error signal below: a failing flush here
                # would otherwise end the thread without any notification.
                self._flush_samples_safely()
                self.error.emit(traceback.format_exc())
        finally:
            try:
                # The tail of the recording (also on a requested stop); the
                # controller still accepts it from a stopped reader.
                if self.protocol != PROTO_UNKNOWN:
                    self._flush_samples_safely()
                if self.protocol == PROTO_V2:
                    # Leave the device in its human-readable default output.
                    try:
                        self._send("MODE CSV")
                    except Exception:
                        pass
            finally:
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
        resume_pending = False
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
            elif (self.protocol == PROTO_LEGACY and now - start < self.LEGACY_RECHECK_S
                  and now - last_hello >= self.LEGACY_HELLO_INTERVAL_S):
                self._send("HELLO")
                last_hello = now
            if (self.protocol == PROTO_V2 and dec.awaiting_mode_ack
                    and now - self._mode_sent_at >= self.MODE_RETRY_S):
                self._mode_sent_at = now
                self._decoder.expect_mode_ack()
                self._send("MODE RAW")

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
            if self._paused:
                # Output switched off on purpose (SET STREAM 0 acknowledged):
                # no data is expected, so the watchdog is skipped.
                resume_pending = True
                if stale:
                    stale = False
                    self.data_resumed.emit()
            else:
                if resume_pending:
                    # Streaming is back: the no-data timer starts now.
                    resume_pending = False
                    last_data = now
                if dec.has_pending:
                    last_data = now
                    if stale:
                        stale = False
                        self.data_resumed.emit()
            if now - last_flush >= self.BATCH_INTERVAL_S and self.protocol != PROTO_UNKNOWN:
                self._flush_samples()
                last_flush = now

            # --- watchdog -------------------------------------------------
            if not self._paused:
                # Slow sensor settings (e.g. 1024 averages x 8.2 ms = 16.9 s per
                # sample) are legitimate: the thresholds scale with the period.
                warn_at, fatal_at = self._watchdog_thresholds(dec.period_us, last_data is None)
                if last_data is None:
                    idle = now - start
                else:
                    idle = now - last_data
                if idle >= fatal_at:
                    self.error.emit(f"NO_DATA:{idle:.0f}")
                    return
                if idle >= warn_at and not stale:
                    stale = True
                    self.data_stale.emit(idle)

    def _on_event(self, kind: str, payload, sync_due: Optional[float]) -> Optional[float]:
        if kind == P.EV_HELLO:
            self.device_info.emit(payload)
            if self.protocol in (PROTO_UNKNOWN, PROTO_LEGACY):
                # A late reply upgrades a legacy declaration. The RAW time
                # axis continues after the last legacy sample (see _set_protocol).
                self._start_v2_session()
                if self._sync_clock:
                    sync_due = self._next_second(time.monotonic())
        elif kind == P.EV_CONFIG:
            self.device_config.emit(payload)
        elif kind == P.EV_OK:
            self.command_reply.emit(True, str(payload), "")
            verb = str(payload).upper()
            if verb == "SET":
                self._settle_set(True)
            if verb.startswith(("SET", "SAVE")) and self._ok_replies_owed > 0:
                # Refresh the configuration after a SET/SAVE this host sent.
                # Replies it did not ask for never trigger a command.
                self._ok_replies_owed -= 1
                now = time.monotonic()
                if now - self._last_followup_get >= self.FOLLOWUP_GET_MIN_GAP_S:
                    self._last_followup_get = now
                    self._send("GET")
        elif kind == P.EV_ERR:
            cmd, reason = payload
            if cmd.upper() == "SET":
                self._settle_set(False)
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
        self._mode_sent_at = time.monotonic()
        self._decoder.expect_mode_ack()
        self._send("MODE RAW")

    def _send(self, text: str) -> bool:
        """Write a command; a device that does not drain its input (e.g. old
        firmware that never reads) must not end the session."""
        try:
            self._port.write_line(text)
        except TimeoutError:
            # A partly written line may still get a reply that cannot be
            # matched to a request any more: forget the outstanding SETs.
            self._set_queue.clear()
            self._ok_replies_owed = 0
            return False
        words = text.split()
        verb = words[0].upper() if words else ""
        if verb == "SET":
            key = text.upper().replace(" ", "")
            self._set_queue.append({"SETSTREAM0": False, "SETSTREAM1": True}.get(key))
        if verb in ("SET", "SAVE"):
            self._ok_replies_owed += 1
        return True

    def _watchdog_thresholds(self, period_us: int, first_sample: bool) -> Tuple[float, float]:
        """(stale warning, no-data error) in seconds of silence.

        Slow sensor settings legitimately go quiet for a while, so the thresholds
        scale with the conversion period. The period comes from the device, so
        it is clamped: the error threshold stays between the base timeout and
        WATCHDOG_MAX_S, whatever the device reports."""
        period_s = 0.0
        if self.protocol == PROTO_V2:
            period_s = min(max(period_us, 0) / 1e6, self.WATCHDOG_MAX_S)
        cap = max(self._no_data_timeout, self.WATCHDOG_MAX_S)
        if first_sample:
            warn_at = self.FIRST_SAMPLE_GRACE_S + 2.5 * period_s
            fatal_at = warn_at + self._no_data_timeout
        else:
            warn_at = max(self.STALE_WARNING_S, 2.5 * period_s)
            fatal_at = self._no_data_timeout + 3.0 * period_s
        return min(warn_at, cap), min(fatal_at, cap)

    def _settle_set(self, ok: bool) -> None:
        """Match a !OK SET / !ERR SET reply with the oldest outstanding SET.
        The stream state changes only when the device acknowledged it."""
        if not self._set_queue:
            return
        want_stream_on = self._set_queue.popleft()
        if ok and want_stream_on is not None:
            self._paused = not want_stream_on

    def _set_protocol(self, proto: str) -> None:
        self.protocol = proto
        if proto == PROTO_V2:
            # Firmware 2.x keeps printing CSV until it processes MODE RAW; those
            # lines must not be mixed into the device-clock time base. RAW lines
            # still gated by the MODE ack (see StreamDecoder) are dropped too.
            self._decoder.raw_only = True
            self._decoder.discard_legacy_pending()
            self._decoder.discard_raw_pending()
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

    def _flush_samples_safely(self) -> None:
        """_flush_samples for the shutdown paths: an error here must not stop
        the error or disconnected signal that follows it."""
        try:
            self._flush_samples()
        except Exception:
            pass
