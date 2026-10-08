"""EdgePowerMeter serial protocol (see docs/PROTOCOL.md).

`StreamDecoder` turns the raw byte stream into calibrated sample batches and
protocol events. It understands both the legacy CSV output of firmware 1.x
and the RAW format of firmware 2.x, and is independent of Qt and of the
serial port so it can be unit-tested with plain byte strings.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Callable, List, Optional, Tuple

import numpy as np

from ..core.samples import Samples

PROTOCOL_VERSION = 2

BUS_LSB_V = 1.25e-3
SHUNT_LSB_V = 2.5e-6
LEGACY_FIRMWARE_SHUNT_OHM = 0.010   # hard-coded in firmware 1.x
MIN_VALID_YEAR = 2020               # older RTC dates mean "clock never set"

AVG_CHOICES = (1, 4, 16, 64, 128, 256, 512, 1024)
CT_CHOICES_US = (140, 204, 332, 588, 1100, 2116, 4156, 8244)

_EPOCH = datetime(1970, 1, 1)


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
    shunt_ohm: float = LEGACY_FIRMWARE_SHUNT_OHM
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
        """Parse `!CFG,key=value,...`; unknown keys are ignored."""
        cfg = cls()
        for part in line.split(",")[1:]:
            key, _, value = part.partition("=")
            key, value = key.strip().lower(), value.strip()
            try:
                if key == "avg":
                    cfg.avg = int(value)
                elif key == "vct":
                    cfg.vct_us = int(value)
                elif key == "ict":
                    cfg.ict_us = int(value)
                elif key == "shunt":
                    cfg.shunt_ohm = float(value)
                elif key == "oled":
                    cfg.oled = value == "1"
                elif key == "stream":
                    cfg.stream = value == "1"
                elif key == "mode":
                    cfg.mode = value.upper()
                elif key == "sqw":
                    cfg.sqw = value == "1"
                elif key == "period_us":
                    cfg.period_us = int(value)
            except ValueError:
                continue
        return cfg


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
        v = bus * (BUS_LSB_V * self.voltage_gain) + self.voltage_offset_v
        i = shunt * (SHUNT_LSB_V / self.shunt_ohm * self.current_gain) + self.current_offset_a
        return v, i, v * i

    def from_legacy(self, v: np.ndarray, i: np.ndarray) -> Tuple[np.ndarray, ...]:
        # Firmware 1.x computed current with a fixed 10 mOhm shunt; rescale to
        # the configured value. Power is recomputed from the calibrated V and I
        # (the INA226 power register has a coarse 2.5 mW LSB anyway).
        v = v * self.voltage_gain + self.voltage_offset_v
        i = i * (LEGACY_FIRMWARE_SHUNT_OHM / self.shunt_ohm * self.current_gain) + self.current_offset_a
        return v, i, v * i


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
                 clock: Callable[[], float] = time.time) -> None:
        self.calibration = calibration or Calibration()
        self.host_averaging = max(1, int(host_averaging))
        self._clock = clock
        self._buf = b""
        self.accept_data = True
        self.raw_only = False   # set once firmware 2.x is detected
        self.events: List[tuple] = []

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
        self._leg_t0: Optional[float] = None
        self._leg_last_t = 0.0
        self._ts_cache: Tuple[str, Tuple[float, float]] = ("", (0.0, 0.0))
        self._avg_carry: Optional[Samples] = None

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
        buf = self._buf + data
        lines = buf.split(b"\n")
        self._buf = lines.pop()
        if len(self._buf) > 4096:     # no newline for a long time: garbage
            self._buf = b""
        for raw in lines:
            line = raw.decode("ascii", errors="ignore").strip()
            if line:
                self._handle_line(line)

    def _handle_line(self, line: str) -> None:
        c = line[0]
        if c == "D" and line.startswith("D,"):
            self._handle_raw(line)
        elif c == "T" and line.startswith("T,"):
            self._handle_anchor(line)
        elif c == "!":
            self._handle_reply(line)
        elif c == "#" or c == "[" or c == "=":
            self.events.append((EV_LOG, line.lstrip("# ").rstrip()))
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
        self.raw_lines += 1
        if self._last_seq is not None:
            gap = (seq - self._last_seq - 1) & 0xFFFFFFFF
            if gap < 0x80000000:
                self.lost_samples += gap
        seq_gap = ((seq - self._last_seq - 1) & 0xFFFFFFFF) if self._last_seq is not None else 0
        if self._last_t_us is not None and self.period_us > 0 and seq_gap == 0:
            # Only without a seq gap: the INA226 clock has ±10 % tolerance, so
            # dividing a long gap by the nominal period would overcount.
            dt = t_us - self._last_t_us
            if dt > 1.5 * self.period_us:
                self.missed_samples += max(0, int(round(dt / self.period_us)) - 1)
        self._last_seq = seq
        self._last_t_us = t_us
        if not self.accept_data:
            return
        if self._raw_last_t_us is not None and t_us < self._raw_last_t_us:
            # Device clock went backwards: the device rebooted without leaving
            # the USB bus. Rebase so the recording's time axis continues.
            if self._t0_us is not None:
                elapsed = self._raw_last_t_us - self._t0_us
                self._t0_us = t_us - elapsed - max(self.period_us, 1000)
            self._host_ref = None
            self._anchor = None
        self._raw_last_t_us = t_us
        if self._host_ref is None:
            self._host_ref = (t_us, self._clock())
        self._raw_seq.append(seq)
        self._raw_t.append(t_us)
        self._raw_bus.append(bus)
        self._raw_shunt.append(shunt)

    def _handle_anchor(self, line: str) -> None:
        parts = line.split(",")
        try:
            t_us, rtc_s = int(parts[1]), int(parts[2])
        except (IndexError, ValueError):
            self.bad_lines += 1
            return
        src = parts[3].strip() if len(parts) > 3 else "S"
        try:
            # RTC holds naive local time; convert its calendar to a POSIX stamp.
            posix = (_EPOCH + timedelta(seconds=rtc_s)).timestamp()
        except (OverflowError, OSError, ValueError):
            return
        if (_EPOCH + timedelta(seconds=rtc_s)).year < MIN_VALID_YEAR:
            return
        self._anchor = (t_us, posix)
        self.anchor_source = src
        self.events.append((EV_ANCHOR, src))

    def _handle_reply(self, line: str) -> None:
        if line.startswith("!HELLO"):
            parts = line.split(",")
            try:
                info = DeviceInfo(parts[1], parts[2], int(parts[3]))
            except (IndexError, ValueError):
                info = DeviceInfo("EdgePowerMeter", "?", PROTOCOL_VERSION)
            self.events.append((EV_HELLO, info))
        elif line.startswith("!CFG"):
            cfg = DeviceConfig.parse(line)
            self.period_us = cfg.period_us or (cfg.vct_us + cfg.ict_us) * cfg.avg
            self.events.append((EV_CONFIG, cfg))
        elif line.startswith("!OK"):
            self.events.append((EV_OK, line[3:].strip()))
        elif line.startswith("!ERR"):
            rest = line[4:].strip()
            cmd, _, reason = rest.partition(" ")
            self.events.append((EV_ERR, (cmd, reason)))
        else:
            self.events.append((EV_LOG, line))

    def _handle_legacy(self, line: str) -> None:
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
        if not self.accept_data or self.raw_only:
            return
        self._leg_wall.append(wall)
        self._leg_naive.append(naive)
        self._leg_v.append(v)
        self._leg_i.append(i)
        self._leg_host.append(self._clock())

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
            if dt.year >= MIN_VALID_YEAR:
                base = (dt.timestamp(), (dt - _EPOCH).total_seconds())
            else:
                base = (float("nan"), float("nan"))
            self._ts_cache = (key, base)
        frac = 0.0
        if len(ts) > 20 and ts[19] == ".":
            digits = ts[20:]
            frac = int(digits) / (10 ** len(digits))
        wall, naive = self._ts_cache[1]
        return wall + frac, naive + frac

    # ----------------------------------------------------------------- output

    def take_events(self) -> List[tuple]:
        ev, self.events = self.events, []
        return ev

    def take_samples(self) -> Samples:
        """Convert everything decoded so far into one calibrated batch."""
        batches = []
        if self._raw_t:
            batches.append(self._take_raw())
        if self._leg_v:
            batches.append(self._take_legacy())
        if not batches:
            return Samples.empty()
        out = batches[0] if len(batches) == 1 else batches[0].concat(batches[1])
        return self._average(out)

    def _take_raw(self) -> Samples:
        t_us = np.asarray(self._raw_t, dtype=np.int64)
        bus = np.asarray(self._raw_bus, dtype=np.float64)
        shunt = np.asarray(self._raw_shunt, dtype=np.float64)
        for buf in (self._raw_seq, self._raw_t, self._raw_bus, self._raw_shunt):
            buf.clear()

        if self._t0_us is None:
            self._t0_us = int(t_us[0])
        t = (t_us - self._t0_us) * 1e-6
        ref = self._anchor or self._host_ref
        wall = ref[1] + (t_us - ref[0]) * 1e-6
        v, i, p = self.calibration.from_raw(bus, shunt)
        return Samples.from_columns(t, wall, v, i, p)

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
        return Samples.from_columns(t, wall, v, i, p)

    def _average(self, s: Samples) -> Samples:
        """Block-average N consecutive samples (host-side oversampling)."""
        n = self.host_averaging
        if n <= 1:
            return s
        if self._avg_carry is not None:
            s = self._avg_carry.concat(s)
            self._avg_carry = None
        full = (len(s) // n) * n
        if full < len(s):
            self._avg_carry = s.slice(full, len(s))
        if not full:
            return Samples.empty()
        cols = [getattr(s, c)[:full].reshape(-1, n).mean(axis=1)
                for c in ("t", "wall", "v", "i", "p")]
        return Samples.from_columns(*cols)

    # ------------------------------------------------------------- utilities

    @property
    def has_pending(self) -> bool:
        return bool(self._raw_t or self._leg_v)

    def discard_legacy_pending(self) -> None:
        """Drop buffered CSV samples (firmware 2.x prints CSV until MODE RAW)."""
        for buf in (self._leg_wall, self._leg_naive, self._leg_v, self._leg_i, self._leg_host):
            buf.clear()
        self._leg_t0 = None
        self._leg_last_t = 0.0

    def discard_raw_pending(self) -> None:
        """Drop buffered RAW samples and start counting losses afresh.

        RAW lines that arrive before the session starts are stale when the
        device was left streaming (the previous session ended without MODE
        CSV, e.g. a crash): its USB buffer still holds old lines, followed by
        a sequence gap that is no loss of this recording."""
        for buf in (self._raw_seq, self._raw_t, self._raw_bus, self._raw_shunt):
            buf.clear()
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
        """Start t=0 at the next sample (used after switching to RAW mode)."""
        self._t0_us = None
        self._leg_t0 = None
        self._leg_last_t = 0.0
        self._avg_carry = None


def sync_command(when: datetime) -> str:
    return f"SYNC {when.strftime('%Y-%m-%d %H:%M:%S')}"
