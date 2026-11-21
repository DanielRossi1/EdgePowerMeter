"""EdgePowerMeter serial protocol (see docs/PROTOCOL.md).

`StreamDecoder` turns the raw byte stream into calibrated sample batches and
protocol events. It understands both the legacy CSV output of firmware 1.x
and the RAW format of firmware 2.x, and is independent of Qt and of the
serial port so it can be unit-tested with plain byte strings.

The device is not trusted: every value it sends is checked against the range
the hardware can produce (see the limits below). A line outside those limits
is counted in `bad_lines` and dropped; it never reaches the time base or the
samples.
"""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Sequence, Tuple

import numpy as np

from ..core.samples import Samples

PROTOCOL_VERSION = 2
UNKNOWN_PROTOCOL = 0                # !HELLO protocol field missing or out of range

BUS_LSB_V = 1.25e-3
SHUNT_LSB_V = 2.5e-6
LEGACY_FIRMWARE_SHUNT_OHM = 0.010   # hard-coded in firmware 1.x
# A host command token is a bare word ("SET", "SYNC"); anything else in a
# !ERR line is the device echoing its own output back.
_COMMAND_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
# Device clock differences from this computer's clock up to this size are
# taken as normal. The difference is measured against the device time of a
# fresh D line (not the anchor's second boundary), so it is precise to the USB
# latency and the RTC's own second-level error is what remains.
CLOCK_TOLERANCE_S = 0.25
MIN_VALID_YEAR = 2020               # older RTC dates mean "clock never set"

AVG_CHOICES = (1, 4, 16, 64, 128, 256, 512, 1024)
CT_CHOICES_US = (140, 204, 332, 588, 1100, 2116, 4156, 8244)

# --- limits for device-supplied values --------------------------------------
MAX_SEQ = (1 << 32) - 1             # sequence number: 32-bit counter
MAX_T_US = 1 << 53                  # device µs clock: exact in float64 (~285 years)
BUS_RAW_RANGE = (0, 65535)          # INA226 bus voltage register: unsigned 16-bit
SHUNT_RAW_RANGE = (-32768, 32767)   # INA226 shunt voltage register: signed 16-bit
MAX_LINE_BYTES = 4096               # longer lines (complete or still partial) are garbage
MAX_EVENTS_PER_FEED = 10_000        # events beyond this in one feed() are dropped
ANCHOR_TOLERANCE_S = 5.0            # a new T anchor may differ from the current mapping by this
MAX_ANCHOR_YEAR = 2100
NAME_MAX_CHARS = 64                 # !HELLO name and firmware strings
PROTOCOL_MAX = 99                   # !HELLO protocol field: 1..99 is plausible
PERIOD_US_MIN = 100
# Largest conversion period the firmware can report: (vct + ict) * avg at the
# slowest settings (2 * 8244 µs * 1024, about 16.9 s).
PERIOD_US_MAX = 2 * CT_CHOICES_US[-1] * AVG_CHOICES[-1]
# A sequence jump is a stall of this device's stream, not lost samples, when it
# exceeds RESYNC_SEQ_GAP, or RESYNC_TIME_S worth of samples (never fewer than
# RESYNC_MIN_GAP samples, so slow settings still count their losses).
RESYNC_SEQ_GAP = 100_000
RESYNC_TIME_S = 10.0
RESYNC_MIN_GAP = 100
MAX_MISSED_PER_GAP = 100_000        # cap on conversions counted as missed from one time gap
# Physically impossible readings (also the float32 storage limit is far above):
# voltage and current beyond this are sensor garbage, not measurements.
PLAUSIBLE_ABS_VI = 1e4

_EPOCH = datetime(1970, 1, 1)


class InvalidConfig(Exception):
    """A !CFG value the firmware cannot have set. The whole line is ignored."""


@dataclass
class DeviceInfo:
    name: str
    firmware: str
    protocol: int


@dataclass
class DeviceConfig:
    avg: int = 4
    vct_us: int = 1100
    ict_us: int = 1100
    # None: the device reported a shunt value that is not finite and positive.
    shunt_ohm: Optional[float] = LEGACY_FIRMWARE_SHUNT_OHM
    oled: bool = True
    stream: bool = True
    mode: str = "CSV"
    sqw: bool = False
    period_us: int = 0

    @property
    def sample_rate_hz(self) -> float:
        period = self.period_us or (self.vct_us + self.ict_us) * self.avg
        return 1e6 / period if period > 0 else 0.0

    @classmethod
    def parse(cls, line: str) -> "DeviceConfig":
        """Parse `!CFG,key=value,...`.

        Unknown keys and unreadable values are ignored. A value that is readable
        but not one the INA226 or the firmware can use (avg, vct, ict, period_us)
        raises InvalidConfig: a config like that was not set by this firmware.
        """
        cfg = cls()
        for part in line.split(",")[1:]:
            key, _, value = part.partition("=")
            key, value = key.strip().lower(), value.strip()
            try:
                if key == "avg":
                    cfg.avg = _choice("avg", int(value), AVG_CHOICES)
                elif key == "vct":
                    cfg.vct_us = _choice("vct", int(value), CT_CHOICES_US)
                elif key == "ict":
                    cfg.ict_us = _choice("ict", int(value), CT_CHOICES_US)
                elif key == "shunt":
                    shunt = float(value)
                    cfg.shunt_ohm = shunt if math.isfinite(shunt) and shunt > 0 else None
                elif key == "oled":
                    cfg.oled = value == "1"
                elif key == "stream":
                    cfg.stream = value == "1"
                elif key == "mode":
                    cfg.mode = value.upper()
                elif key == "sqw":
                    cfg.sqw = value == "1"
                elif key == "period_us":
                    period = int(value)
                    if not PERIOD_US_MIN <= period <= PERIOD_US_MAX:
                        raise InvalidConfig(f"period_us={period} out of range")
                    cfg.period_us = period
            except ValueError:
                continue
        return cfg


def _choice(key: str, value: int, allowed: Tuple[int, ...]) -> int:
    if value not in allowed:
        raise InvalidConfig(f"{key}={value} is not a valid setting")
    return value


def nominal_rate_hz(avg: int, vct_us: int, ict_us: int) -> float:
    return 1e6 / ((vct_us + ict_us) * avg)


@dataclass
class Calibration:
    """Host-side calibration applied to every sample."""

    shunt_ohm: float = 0.010
    voltage_gain: float = 1.0
    voltage_offset_v: float = 0.0
    current_gain: float = 1.0
    current_offset_a: float = 0.0

    @classmethod
    def from_settings(cls, s) -> "Calibration":
        return cls(s.shunt_ohm, s.voltage_gain, s.voltage_offset_v,
                   s.current_gain, s.current_offset_a)

    def from_raw(self, bus: np.ndarray, shunt: np.ndarray) -> Tuple[np.ndarray, ...]:
        with np.errstate(over="ignore", invalid="ignore"):
            v = bus * (BUS_LSB_V * self.voltage_gain) + self.voltage_offset_v
            i = shunt * (SHUNT_LSB_V / self.shunt_ohm * self.current_gain) + self.current_offset_a
            return v, i, v * i

    def from_legacy(self, v: np.ndarray, i: np.ndarray) -> Tuple[np.ndarray, ...]:
        # Firmware 1.x computed current with a fixed 10 mOhm shunt; rescale to
        # the configured value. Power is recomputed from the calibrated V and I
        # (the INA226 power register has a coarse 2.5 mW LSB anyway).
        with np.errstate(over="ignore", invalid="ignore"):
            v = v * self.voltage_gain + self.voltage_offset_v
            i = i * (LEGACY_FIRMWARE_SHUNT_OHM / self.shunt_ohm * self.current_gain) + self.current_offset_a
            return v, i, v * i


def _plausible(v: np.ndarray, i: np.ndarray, p: np.ndarray) -> np.ndarray:
    """Mask of samples that are finite and physically possible. Values that
    overflow float32 storage become inf, so they are caught here too."""
    with np.errstate(invalid="ignore"):
        return (np.isfinite(v) & np.isfinite(i) & np.isfinite(p)
                & (np.abs(v) <= PLAUSIBLE_ABS_VI) & (np.abs(i) <= PLAUSIBLE_ABS_VI))


_COLUMNS = ("t", "wall", "v", "i", "p")


def _concat(batches: Sequence[Samples]) -> Samples:
    """Join batches with one concatenation per column (not one per batch)."""
    if not batches:
        return Samples.empty()
    if len(batches) == 1:
        return batches[0]
    return Samples.from_columns(*[np.concatenate([getattr(b, c) for b in batches])
                                  for c in _COLUMNS])


# Event kinds produced by the decoder (besides samples).
EV_HELLO = "hello"      # payload: DeviceInfo
EV_CONFIG = "config"    # payload: DeviceConfig
EV_OK = "ok"            # payload: command text
EV_ERR = "err"          # payload: (command, reason)
EV_LOG = "log"          # payload: text line
EV_ANCHOR = "anchor"    # payload: 'S' (SQW) or 'P' (polling)


class StreamDecoder:
    """Incremental decoder for the device output.

    Time base:
      * RAW: `t` comes from the device µs clock, so sample spacing reflects
        the real conversion timing regardless of USB/GUI latency. Wall-clock
        time comes from the RTC anchors (`T` lines).
      * Legacy CSV: `t` comes from the firmware's millisecond timestamps when
        the RTC is valid, otherwise from the host clock at reception.
    In both cases `t` starts at 0 for the first sample of the session.
    """

    def __init__(self, calibration: Optional[Calibration] = None,
                 host_averaging: int = 1,
                 clock: Callable[[], float] = time.time,
                 host_wall_clock: Optional[Callable[[], float]] = None,
                 require_mode_ack: bool = False) -> None:
        self.calibration = calibration or Calibration()
        self.host_averaging = max(1, int(host_averaging))
        self._clock = clock
        # This computer's wall clock, used to detect a device RTC that is set
        # wrong (e.g. a DST offset). None: trust the RTC as is.
        self._host_wall_clock = host_wall_clock
        self._buf = b""
        # True while the rest of an oversize partial line is being skipped.
        self._discarding = False
        self.accept_data = True
        self.raw_only = False   # set once firmware 2.x is detected
        # With require_mode_ack (a SerialReader session), RAW lines and T anchors
        # are ignored until the "!OK MODE" that answers this session's MODE RAW
        # (see expect_mode_ack): before that the device may still be sending
        # lines left in its USB buffer by a previous session. Those lines must
        # not set the wall-clock reference.
        self._awaiting_ack = require_mode_ack
        # True once MODE RAW was sent by this session. A "!OK MODE" before that
        # is a stale reply from the previous session and must not open the gate.
        self._mode_requested = False
        self.events: List[tuple] = []
        self._feed_events = 0           # events kept during the current feed()
        self.dropped_events = 0         # events beyond MAX_EVENTS_PER_FEED

        # RAW accumulators
        self._raw_seq: List[int] = []
        self._raw_t: List[int] = []
        self._raw_bus: List[int] = []
        self._raw_shunt: List[int] = []
        # Legacy accumulators
        self._leg_wall: List[float] = []
        self._leg_naive: List[float] = []   # RTC calendar seconds (no DST)
        self._leg_v: List[float] = []
        self._leg_i: List[float] = []
        self._leg_host: List[float] = []

        self._t0_us: Optional[int] = None
        self._raw_last_t_us: Optional[int] = None
        self._last_seq: Optional[int] = None
        self._last_t_us: Optional[int] = None
        self.period_us = 0          # nominal conversion period, from !CFG
        self._anchor: Optional[Tuple[int, float]] = None      # (t_us, posix)
        self._host_ref: Optional[Tuple[int, float]] = None    # (t_us, posix) fallback
        # Set by a SYNC reply and by a device reboot: the next anchor may move
        # the time base, so it is accepted without the consistency check.
        self._trust_next_anchor = False
        self._leg_t0: Optional[float] = None
        self._leg_last_t = 0.0
        self._ts_cache: Tuple[str, float] = ("", 0.0)
        self._avg_carry: Optional[Samples] = None
        # RTC calendar -> POSIX offset, fixed per clock setting (see _rtc_to_posix).
        self._rtc_offset: Optional[float] = None
        # Fixed correction (seconds) applied on top of _rtc_offset when the RTC
        # disagrees with this computer's clock by more than CLOCK_TOLERANCE_S.
        self._rtc_correction = 0.0
        # True once the host-clock check ran for the current offset. Only a
        # fresh T anchor runs it (see _accept_anchor); reset with the offset.
        self._rtc_checked = False
        # Device time (µs) of the latest D line received since the gate opened:
        # the device's "now" for the host-clock check. None until one arrives.
        self._fresh_t_us: Optional[int] = None
        # A fresh anchor waiting for _fresh_t_us (no D line yet): (t_us, rtc_s, source).
        self._pending_anchor: Optional[Tuple[int, int, str]] = None
        # Finished batches waiting for the next take_samples(): RAW samples
        # converted before a device reboot (or a new anchor) rebased the time
        # axis. They are already averaged, so they are never mixed with the
        # samples that follow.
        self._ready: List[Samples] = []
        # Last t handed out by take_samples(); a later RAW time base (after the
        # legacy -> RAW upgrade) continues from it instead of restarting at 0.
        self._emitted_t: Optional[float] = None
        self._t_resume: Optional[float] = None

        # Samples the device read but could not send (seq gaps)...
        self.lost_samples = 0
        # ...and conversions it never read (time gaps > 1.5 periods), which
        # happens with very fast INA226 settings.
        self.missed_samples = 0
        self.legacy_lines = 0
        self.raw_lines = 0
        self.bad_lines = 0
        self.anchor_source: Optional[str] = None

    # ------------------------------------------------------------------ input

    def feed(self, data: bytes) -> None:
        if not data:
            return
        self._feed_events = 0
        if self._discarding:
            end = data.find(b"\n")
            if end < 0:
                return
            data = data[end + 1:]
            self._discarding = False
        buf = self._buf + data
        lines = buf.split(b"\n")
        self._buf = lines.pop()
        if len(self._buf) > MAX_LINE_BYTES:
            # No newline for a long time: garbage. Drop it, and the rest of
            # that line when it ends, so no fragment is read as a line.
            self._buf = b""
            self._discarding = True
            self.bad_lines += 1
        for raw in lines:
            if len(raw) > MAX_LINE_BYTES:
                self.bad_lines += 1
                continue
            try:
                line = raw.decode("ascii").strip()
            except UnicodeDecodeError:
                # Corrupted bytes: dropping them would leave a plausible but
                # wrong line, so the whole line is rejected.
                self.bad_lines += 1
                continue
            if line:
                self._handle_line(line)

    def _emit(self, kind: str, payload) -> None:
        """Queue an event for take_events(), up to MAX_EVENTS_PER_FEED per feed.
        Data (samples and the time base) is never dropped with them."""
        if self._feed_events >= MAX_EVENTS_PER_FEED:
            self.dropped_events += 1
            return
        self._feed_events += 1
        self.events.append((kind, payload))

    def _handle_line(self, line: str) -> None:
        c = line[0]
        if c == "D" and line.startswith("D,"):
            self._handle_raw(line)
        elif c == "T" and line.startswith("T,"):
            self._handle_anchor(line)
        elif c == "!":
            self._handle_reply(line)
        elif c == "#" or c == "[" or c == "=":
            self._emit(EV_LOG, line.lstrip("# ").rstrip())
        elif c.isdigit():
            self._handle_legacy(line)
        elif line.startswith("Timestamp"):
            pass  # legacy CSV header
        else:
            self.bad_lines += 1

    def _handle_raw(self, line: str) -> None:
        parts = line.split(",")
        if len(parts) < 5:
            self.bad_lines += 1
            return
        try:
            seq, t_us, bus, shunt = int(parts[1]), int(parts[2]), int(parts[3]), int(parts[4])
        except ValueError:
            self.bad_lines += 1
            return
        if not (0 <= seq <= MAX_SEQ and 0 <= t_us <= MAX_T_US
                and BUS_RAW_RANGE[0] <= bus <= BUS_RAW_RANGE[1]
                and SHUNT_RAW_RANGE[0] <= shunt <= SHUNT_RAW_RANGE[1]):
            # Outside the counter or register ranges of the hardware: not a sample.
            self.bad_lines += 1
            return
        self.raw_lines += 1
        if self._awaiting_ack:
            # Not yet known to be this session's stream: neither a sample nor
            # a sequence/time reference (the first line after the ack is fresh).
            return
        if self._pending_anchor is not None:
            # The first fresh line: its device time is the "now" for the
            # deferred host-clock check. Done before this line is stored, so
            # the samples are timed from the corrected anchor.
            t_p, rtc_p, src_p = self._pending_anchor
            self._pending_anchor = None
            self._accept_anchor(t_p, rtc_p, src_p, t_us)
        self._fresh_t_us = t_us
        seq_gap = 0
        if self._last_seq is not None:
            seq_gap = (seq - self._last_seq - 1) & MAX_SEQ
            if seq_gap > self._seq_resync_limit():
                # A stall or restart of the device's stream, not losses.
                self._emit(EV_LOG, f"sequence jumped by {seq_gap} samples: "
                                   "device restart or stale data, not counted as lost")
            else:
                self.lost_samples += seq_gap
        if self._last_t_us is not None and self.period_us > 0 and seq_gap == 0:
            # Only without a seq gap: the INA226 clock has ±10 % tolerance, so
            # dividing a long gap by the nominal period would overcount.
            dt = t_us - self._last_t_us
            if dt > 1.5 * self.period_us:
                # round(dt / period) - 1, in integers
                skipped = (2 * dt + self.period_us) // (2 * self.period_us) - 1
                self.missed_samples += min(MAX_MISSED_PER_GAP, max(0, skipped))
        self._last_seq = seq
        self._last_t_us = t_us
        if not self.accept_data:
            return
        if self._raw_last_t_us is not None and t_us < self._raw_last_t_us:
            # Device clock went backwards: the device rebooted without leaving
            # the USB bus. Convert the samples still pending with the old
            # reference, then rebase so the recording's time axis continues.
            self._flush_pre_boot()
            if self._t0_us is not None:
                elapsed = self._raw_last_t_us - self._t0_us
                self._t0_us = t_us - elapsed - max(self.period_us, 1000)
            self._host_ref = None
            # An anchor that arrived after the last pre-reboot line belongs to
            # the new boot (its t_us precedes this line): keep it.
            if self._anchor is not None and self._anchor[0] > t_us:
                self._anchor = None
            # The next anchor is from the new boot: its time may be far off the
            # old mapping, so it is accepted without the consistency check.
            self._trust_next_anchor = True
        self._raw_last_t_us = t_us
        if self._host_ref is None:
            self._host_ref = (t_us, self._clock())
        self._raw_seq.append(seq)
        self._raw_t.append(t_us)
        self._raw_bus.append(bus)
        self._raw_shunt.append(shunt)

    def _seq_resync_limit(self) -> int:
        """Largest sequence jump that is counted as lost samples."""
        limit = RESYNC_SEQ_GAP
        if self.period_us > 0:
            by_time = max(int(RESYNC_TIME_S * 1e6) // self.period_us, RESYNC_MIN_GAP)
            limit = min(limit, by_time)
        return limit

    def _handle_anchor(self, line: str) -> None:
        parts = line.split(",")
        try:
            t_us, rtc_s = int(parts[1]), int(parts[2])
        except (IndexError, ValueError):
            self.bad_lines += 1
            return
        if not 0 <= t_us <= MAX_T_US:
            self.bad_lines += 1
            return
        src = parts[3].strip() if len(parts) > 3 else "S"
        if self._awaiting_ack:
            # Possibly a stale anchor from the previous session. Ignored before
            # it can be told apart (and the RTC offset is not touched); the
            # firmware sends a fresh anchor right after "!OK MODE".
            return
        try:
            year = (_EPOCH + timedelta(seconds=rtc_s)).year
        except (OverflowError, OSError, ValueError):
            self.bad_lines += 1
            return
        if year < MIN_VALID_YEAR:
            return                  # RTC never set: normal before the first SYNC
        if year > MAX_ANCHOR_YEAR:
            self.bad_lines += 1
            return
        if (self._host_wall_clock is not None and not self._rtc_checked
                and self._fresh_t_us is None):
            # The host-clock check needs the device's "now": wait for the first
            # fresh D line (see _handle_raw), before any sample is stored.
            self._pending_anchor = (t_us, rtc_s, src)
            return
        self._accept_anchor(t_us, rtc_s, src, self._fresh_t_us)

    def _accept_anchor(self, t_us: int, rtc_s: int, src: str,
                       now_t_us: Optional[int]) -> None:
        """Use a fresh T anchor: host-clock check (first anchor per offset),
        consistency check, then it becomes the time base. `now_t_us` is the
        device time at which the check is made (a fresh D line)."""
        try:
            posix = self._rtc_to_posix(rtc_s)
        except (OverflowError, OSError, ValueError):
            self.bad_lines += 1
            return
        if (self._host_wall_clock is not None and not self._rtc_checked
                and now_t_us is not None):
            # The correction is 0 until checked, so posix is still uncorrected.
            self._check_host_clock(posix, t_us, now_t_us)
            posix += self._rtc_correction
        new_boot = (self._raw_last_t_us is not None
                    and t_us < self._raw_last_t_us - self._reboot_margin_us())
        if self._anchor is not None and not (new_boot or self._trust_next_anchor):
            # A single bogus anchor must not move the time base: it has to agree
            # with the current mapping (SYNC and reboots are the exceptions).
            expected = self._anchor[1] + (t_us - self._anchor[0]) * 1e-6
            if abs(posix - expected) > ANCHOR_TOLERANCE_S:
                self.bad_lines += 1
                return
        self._trust_next_anchor = False
        if self._raw_t and new_boot:
            # The anchor is from a new boot: the samples still pending belong
            # to the old one and must keep the old reference.
            self._flush_pre_boot()
        self._anchor = (t_us, posix)
        self.anchor_source = src
        self._emit(EV_ANCHOR, src)

    def _check_host_clock(self, anchor_posix: float, anchor_t_us: int,
                          now_t_us: int) -> None:
        """Compare this computer's clock with the device's, at the same instant.

        The anchor is a device second boundary, possibly up to a second old, so
        the device's current time is the anchor advanced by the device µs ticks
        since it (to the fresh D line at now_t_us)."""
        self._rtc_checked = True
        self._rtc_correction = 0.0
        device_now = anchor_posix + (now_t_us - anchor_t_us) * 1e-6
        diff = self._host_wall_clock() - device_now
        if abs(diff) > CLOCK_TOLERANCE_S:
            self._rtc_correction = diff
            self._emit(EV_LOG, (
                "device clock differs from this computer by {:+.2f} s; "
                "using this computer's time until the clock is synced"
            ).format(diff))

    def _handle_reply(self, line: str) -> None:
        if line.startswith("!HELLO"):
            parts = line.split(",")
            try:
                name = parts[1][:NAME_MAX_CHARS]
                firmware = parts[2][:NAME_MAX_CHARS]
                protocol = int(parts[3])
            except (IndexError, ValueError):
                info = DeviceInfo("EdgePowerMeter", "?", PROTOCOL_VERSION)
            else:
                if not 1 <= protocol <= PROTOCOL_MAX:
                    protocol = UNKNOWN_PROTOCOL
                info = DeviceInfo(name, firmware, protocol)
            self._emit(EV_HELLO, info)
        elif line.startswith("!CFG"):
            try:
                cfg = DeviceConfig.parse(line)
            except InvalidConfig as e:
                self.bad_lines += 1
                self._emit(EV_LOG, f"invalid device config ignored: {e}")
                return
            self.period_us = cfg.period_us or (cfg.vct_us + cfg.ict_us) * cfg.avg
            self._emit(EV_CONFIG, cfg)
        elif line.startswith("!OK"):
            cmd = line[3:].strip()
            if cmd.upper() == "MODE" and self._mode_requested:
                # The reply to this session's MODE RAW: the stream may be used.
                self._awaiting_ack = False
            if cmd.upper() == "SYNC":
                # Samples received so far keep the mapping they were received
                # under (the RTC is about to change), so convert them first.
                self._flush_pre_boot()
                # The RTC was set: the next anchor may carry a new offset.
                self._rtc_offset = None
                self._rtc_correction = 0.0
                self._rtc_checked = False
                self._pending_anchor = None
                self._trust_next_anchor = True
            self._emit(EV_OK, cmd)
        elif line.startswith("!ERR"):
            # Firmware format: "!ERR <command> <reason>", with an empty
            # command leaving two spaces in a row.
            fields = line[4:].split(" ", 2)
            cmd = fields[1] if len(fields) > 1 else ""
            reason = fields[2].strip() if len(fields) > 2 else ""
            if _COMMAND_WORD.fullmatch(cmd):
                self._emit(EV_ERR, (cmd, reason))
            else:
                # Not a reply to a host command: the device's own output (e.g.
                # "# ..." boot text) echoed back on Linux before the tty was
                # set raw. Reporting it as a rejected command is misleading.
                self._emit(EV_LOG, line)
        else:
            self._emit(EV_LOG, line)

    def _handle_legacy(self, line: str) -> None:
        if not self.accept_data or self.raw_only:
            # Dropped before anything is parsed: a CSV line left in the device's
            # USB buffer (old RTC time) must not touch the RTC offset or the
            # host-clock correction. Still counted, since the reader uses
            # legacy_lines to tell that the device is printing CSV.
            self.legacy_lines += 1
            return
        parts = line.split(",")
        if len(parts) < 4:
            self.bad_lines += 1
            return
        try:
            v = float(parts[1])
            i = float(parts[2])
            wall, naive = self._parse_legacy_ts(parts[0].strip())
        except (ValueError, OSError, OverflowError):
            self.bad_lines += 1
            return
        if not (math.isfinite(v) and math.isfinite(i)):
            # Firmware 1.x prints "nan"/"inf" when a sensor read fails; one
            # such sample would poison every running statistic.
            self.bad_lines += 1
            return
        self.legacy_lines += 1
        self._leg_wall.append(wall)
        self._leg_naive.append(naive)
        self._leg_v.append(v)
        self._leg_i.append(i)
        self._leg_host.append(self._clock())

    def _rtc_to_posix(self, rtc_s: float) -> float:
        """RTC calendar seconds (naive local, no DST) -> POSIX time, with the
        host-clock correction in force.

        The offset is taken once, from the first valid reading, and reused:
        applying the host's DST rules to each reading would shift the wall
        time by an hour across a DST switch. It is re-derived after a SYNC.

        The correction (set by _check_host_clock when the RTC is off by more
        than CLOCK_TOLERANCE_S, e.g. set during summer time) is fixed, not per
        reading, so the sample times stay monotonic; it is re-derived after a
        SYNC. Only a fresh T anchor derives it (see _accept_anchor). Legacy CSV
        lines use the offset and the current correction but never set them:
        CSV lines left in the USB buffer by a previous session carry old RTC
        times, and comparing those with now says nothing about the clock.
        Firmware 1.x has no anchors, so its legacy-only sessions get no
        correction and keep the RTC as read.
        """
        if self._rtc_offset is None:
            self._rtc_offset = (_EPOCH + timedelta(seconds=rtc_s)).timestamp() - rtc_s
        return rtc_s + self._rtc_offset + self._rtc_correction

    def _parse_legacy_ts(self, ts: str) -> Tuple[float, float]:
        """'YYYY-MM-DD HH:MM:SS[.mmm]' -> (POSIX wall time, calendar seconds).

        Calendar seconds are the naive RTC reading as seconds since 1970 with
        no time-zone rules applied: the time axis is built from them, so a DST
        change during the recording does not freeze or jump the axis. Both are
        NaN if the RTC was never set.
        """
        key = ts[:19]
        if key != self._ts_cache[0]:
            dt = datetime.strptime(key, "%Y-%m-%d %H:%M:%S")
            naive = (dt - _EPOCH).total_seconds() if dt.year >= MIN_VALID_YEAR else float("nan")
            self._ts_cache = (key, naive)
        frac = 0.0
        if len(ts) > 20 and ts[19] == ".":
            digits = ts[20:]
            frac = int(digits) / (10 ** len(digits))
        naive = self._ts_cache[1]
        if math.isnan(naive):
            return naive, naive
        return self._rtc_to_posix(naive) + frac, naive + frac

    # ----------------------------------------------------------------- output

    def take_events(self) -> List[tuple]:
        ev, self.events = self.events, []
        return ev

    def take_samples(self) -> Samples:
        """Convert everything decoded so far into one calibrated batch."""
        ready, self._ready = self._ready, []
        batches = []
        if self._raw_t:
            batches.append(self._take_raw())
        if self._leg_v:
            batches.append(self._take_legacy())
        current = Samples.empty()
        if batches:
            current = self._average(_concat(batches))
        parts = [b for b in ready if len(b)]
        if len(current):
            parts.append(current)
        if not parts:
            return Samples.empty()
        out = _concat(parts)
        self._emitted_t = float(out.t[-1])
        return out

    @property
    def awaiting_mode_ack(self) -> bool:
        """True while RAW lines are ignored for lack of this session's MODE ack."""
        return self._awaiting_ack

    def _reboot_margin_us(self) -> int:
        """How far an anchor may precede the last received line without being a
        reboot: a conversion finishing after the SQW tick and the USB latency
        (about one period, plus a little)."""
        return 2 * self.period_us + 50_000

    def _flush_pre_boot(self) -> None:
        """Move everything pending from before a device reboot into _ready as
        averaged blocks of its own, so no block spans the rebase. The averaging
        carry is kept (as a shorter block) rather than dropped."""
        pre = self._avg_carry
        self._avg_carry = None
        if self._raw_t:
            batch = self._take_raw()
            pre = batch if pre is None else _concat([pre, batch])
        if pre is not None and len(pre):
            self._ready.append(self._average(pre, final=True))

    def _take_raw(self) -> Samples:
        t_us = np.asarray(self._raw_t, dtype=np.int64)
        bus = np.asarray(self._raw_bus, dtype=np.float64)
        shunt = np.asarray(self._raw_shunt, dtype=np.float64)
        for buf in (self._raw_seq, self._raw_t, self._raw_bus, self._raw_shunt):
            buf.clear()

        if self._t0_us is None:
            self._t0_us = int(t_us[0])
            if self._t_resume is not None:
                # Legacy samples were already emitted: continue after them, one
                # nominal period later (the period may not be known yet).
                step = self.period_us * 1e-6 if self.period_us > 0 else 1e-3
                self._t0_us -= round((self._t_resume + step) * 1e6)
                self._t_resume = None
        # Float64 difference: exact for device times (at most 2**53 µs), and it
        # cannot wrap around even after many reboot rebases push _t0_us far back.
        t = (t_us.astype(np.float64) - float(self._t0_us)) * 1e-6
        ref = self._anchor or self._host_ref
        wall = ref[1] + (t_us - ref[0]) * 1e-6
        v, i, p = self.calibration.from_raw(bus, shunt)
        return self._checked(t, wall, v, i, p)

    def _take_legacy(self) -> Samples:
        wall = np.asarray(self._leg_wall, dtype=np.float64)
        naive = np.asarray(self._leg_naive, dtype=np.float64)
        host = np.asarray(self._leg_host, dtype=np.float64)
        v_dev = np.asarray(self._leg_v, dtype=np.float64)
        i_dev = np.asarray(self._leg_i, dtype=np.float64)
        for buf in (self._leg_wall, self._leg_naive, self._leg_v, self._leg_i, self._leg_host):
            buf.clear()

        invalid = np.isnan(wall)
        wall = np.where(invalid, host, wall)
        clock = np.where(invalid, host, naive)
        if self._leg_t0 is None:
            self._leg_t0 = float(clock[0])
        t = clock - self._leg_t0
        # Firmware 1.x timestamps can step back by a millisecond at the
        # once-per-second RTC resync; keep the time axis monotonic.
        t = np.maximum.accumulate(np.maximum(t, self._leg_last_t))
        self._leg_last_t = float(t[-1])
        v, i, p = self.calibration.from_legacy(v_dev, i_dev)
        return self._checked(t, wall, v, i, p)

    def _checked(self, t, wall, v, i, p) -> Samples:
        """Drop the samples that are not finite or not physically possible
        (counted as bad lines) and store the rest."""
        ok = _plausible(v, i, p)
        dropped = int(ok.size - np.count_nonzero(ok))
        if dropped:
            self.bad_lines += dropped
            t, wall, v, i, p = t[ok], wall[ok], v[ok], i[ok], p[ok]
        return Samples.from_columns(t, wall, v, i, p)

    def _average(self, s: Samples, final: bool = False) -> Samples:
        """Block-average N consecutive samples (host-side oversampling).

        Samples that do not fill a block wait in the carry for the next call;
        with `final` they are averaged as a shorter block instead."""
        n = self.host_averaging
        if n <= 1:
            return s
        if self._avg_carry is not None:
            s = self._avg_carry.concat(s)
            self._avg_carry = None
        full = (len(s) // n) * n
        parts = []
        if full:
            parts.append(self._block_means(s.slice(0, full), n))
        if full < len(s):
            if final:
                parts.append(self._block_means(s.slice(full, len(s)), len(s) - full))
            else:
                self._avg_carry = s.slice(full, len(s))
        if not parts:
            return Samples.empty()
        return _concat(parts)

    @staticmethod
    def _block_means(s: Samples, k: int) -> Samples:
        cols = [getattr(s, c).reshape(-1, k).mean(axis=1)
                for c in ("t", "wall", "v", "i", "p")]
        return Samples.from_columns(*cols)

    # ------------------------------------------------------------- utilities

    @property
    def has_pending(self) -> bool:
        return bool(self._raw_t or self._leg_v or self._ready)

    def discard_legacy_pending(self) -> None:
        """Drop buffered CSV samples (firmware 2.x prints CSV until MODE RAW)."""
        for buf in (self._leg_wall, self._leg_naive, self._leg_v, self._leg_i, self._leg_host):
            buf.clear()
        self._leg_t0 = None
        self._leg_last_t = 0.0
        self._avg_carry = None

    def expect_mode_ack(self) -> None:
        """Call whenever this session sends MODE RAW.

        Arms the gate: RAW lines and T anchors are ignored until the "!OK MODE"
        reply, which is the first thing the device says after the command. Any
        anchor or host reference taken before this point may come from the
        previous session or the previous boot, so it is forgotten. Samples
        already received are converted now, with the old reference, so that a
        device reboot does not lose them.
        """
        self._mode_requested = True
        self._awaiting_ack = True
        self._flush_pre_boot()
        self._anchor = None
        self._host_ref = None
        # The RTC offset and host-clock correction may have come from stale
        # CSV lines of the previous session: the first fresh anchor re-derives them.
        self._rtc_offset = None
        self._rtc_correction = 0.0
        self._rtc_checked = False
        self._fresh_t_us = None
        self._pending_anchor = None

    def discard_raw_pending(self) -> None:
        """Drop buffered RAW samples and start counting losses afresh.

        RAW lines that arrive before the session starts are stale when the
        device was left streaming (the previous session ended without MODE
        CSV, e.g. a crash): its USB buffer still holds old lines, followed by
        a sequence gap that is no loss of this recording."""
        for buf in (self._raw_seq, self._raw_t, self._raw_bus, self._raw_shunt):
            buf.clear()
        self._ready = []
        self._avg_carry = None
        self._raw_last_t_us = None
        self._host_ref = None       # may come from a stale line: wrong wall time
        self.reset_sequence()
        self.lost_samples = 0
        self.missed_samples = 0

    def reset_sequence(self) -> None:
        """Forget the last seq/time (stream paused: the next gap is not a loss)."""
        self._last_seq = None
        self._last_t_us = None

    @property
    def total_lost(self) -> int:
        return self.lost_samples + self.missed_samples

    def reset_timebase(self) -> None:
        """Start the time axis at the next sample (used after switching to RAW
        mode). If samples were already emitted (legacy CSV before a late HELLO
        reply), the new axis continues after the last emitted t instead of
        restarting at 0, so the recording's t stays monotonic."""
        self._t_resume = self._emitted_t
        self._t0_us = None
        self._leg_t0 = None
        self._leg_last_t = 0.0
        self._avg_carry = None


def sync_command(when: datetime) -> str:
    return f"SYNC {when.strftime('%Y-%m-%d %H:%M:%S')}"
