"""Test doubles: a scripted serial port that emulates firmware 1.x / 2.x."""
from __future__ import annotations

import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple


def _naive_local_rtc() -> int:
    """The device RTC reading now: naive local calendar seconds since 1970."""
    return int((datetime.now() - datetime(1970, 1, 1)).total_seconds())


class FakePort:
    """Stands in for SerialPortHandler. Emulates the device side of the protocol."""

    def __init__(self, firmware: str = "v2", rate_hz: float = 1000.0, silent: bool = False,
                 boot_delay_s: float = 0.0, disconnect_after_s: float = 0.0,
                 skip_seq_every: int = 0, stale_raw_lines: int = 0, hello_delay_s: float = 0.0,
                 reject_stream_off: bool = False,
                 cfg_reply: Optional[bytes] = None,
                 replies: Optional[Dict[str, bytes]] = None):
        self.firmware = firmware
        # Replaces the !CFG line the device sends for GET (e.g. an invalid one).
        self.cfg_reply = cfg_reply
        # Command (upper case, e.g. "GET") -> exact reply bytes, overriding the default.
        self.replies: Dict[str, bytes] = dict(replies or {})
        # Lines queued by inject() from the test thread, moved to the output
        # by read_chunk() on the reader thread (list.append is atomic).
        self._inject: List[bytes] = []
        # True: SET STREAM 0 is answered with !ERR and the stream keeps running.
        self.reject_stream_off = reject_stream_off
        # >0: HELLO replies arrive this long after the command (a busy device).
        self.hello_delay_s = hello_delay_s
        self.rate_hz = rate_hz
        self.silent = silent
        self.boot_delay_s = boot_delay_s
        self.disconnect_after_s = disconnect_after_s
        self.skip_seq_every = skip_seq_every
        # >0: the device is still streaming RAW from a session that ended
        # without MODE CSV; its USB buffer holds that many old lines. Those
        # are preceded by the previous session's last T anchor (an hour old)
        # and its "!OK MODE" (reply to its final MODE CSV), as in the real
        # buffer, so both arrive before the new session's !HELLO.
        self.stale_raw_lines = stale_raw_lines
        self.port = "/dev/fake"
        self.written: List[str] = []
        self.mode = "CSV"
        self._out = b""
        self._t_start = 0.0
        self._last_emit = 0.0
        self._seq = 0
        self._t_us = 1_000_000
        self.closed = False
        self.streaming = True       # False after SET STREAM 0
        self._delayed: List[Tuple[float, bytes]] = []

    def open(self):
        self._t_start = self._last_emit = time.monotonic()
        if self.stale_raw_lines:
            self.mode = "RAW"
            stale_rtc = _naive_local_rtc() - 3600
            self._out = (b"T,%d,%d,S\n" % (1_000_000, stale_rtc)) + b"!OK MODE\n" + \
                self._lines(self.stale_raw_lines)
            # Samples taken while nobody read the port and the buffer was full.
            self._seq += 5000
            self._t_us += 5000 * int(1e6 / self.rate_hz)

    def close(self):
        self.closed = True

    def write_line(self, text: str) -> None:
        self.written.append(text)
        if self.firmware != "v2":
            return
        cmd = text.strip().upper()
        if cmd in self.replies:
            self._out += self.replies[cmd]
            return
        if cmd == "HELLO":
            reply = b"!HELLO,EdgePowerMeter,2.0.0,2\n"
            if self.hello_delay_s:
                self._delayed.append((time.monotonic() + self.hello_delay_s, reply))
            else:
                self._out += reply
        elif cmd == "GET":
            if self.cfg_reply is not None:
                self._out += self.cfg_reply
            else:
                self._out += b"!CFG,avg=4,vct=140,ict=140,shunt=0.01,oled=1,stream=1,mode=" + \
                    self.mode.encode() + b",sqw=1,period_us=%d\n" % int(1e6 / self.rate_hz)
        elif cmd.startswith("MODE"):
            self.mode = cmd.split()[1]
            self._out += b"!OK MODE\n"
            if self.mode == "RAW":
                self._out += b"T,%d,%d,S\n" % (self._t_us, _naive_local_rtc())
        elif cmd.startswith(("SET", "SYNC", "SAVE")):
            if self.reject_stream_off and cmd.replace(" ", "") == "SETSTREAM0":
                self._out += b"!ERR SET invalid_value\n"
                return
            if cmd.replace(" ", "") in ("SETSTREAM0", "SETSTREAM1"):
                self.streaming = cmd.endswith("1")
            self._out += b"!OK " + cmd.split()[0].encode() + b"\n"
        else:
            self._out += b"!ERR " + cmd.split()[0].encode() + b" unknown_command\n"

    def inject(self, data: bytes) -> None:
        """Make the device send `data` next (callable from any thread)."""
        self._inject.append(data)

    def read_chunk(self, timeout: float = 0.05, max_bytes: int = 65536) -> bytes:
        from app.serial.handler import DeviceDisconnected
        while self._inject:
            self._out += self._inject.pop(0)
        now = time.monotonic()
        if self.disconnect_after_s and now - self._t_start > self.disconnect_after_s:
            raise DeviceDisconnected("unplugged")
        due = [r for t, r in self._delayed if t <= now]
        self._delayed = [(t, r) for t, r in self._delayed if t > now]
        for reply in due:
            self._out += reply
        if not self.streaming:
            self._last_emit = now       # nothing is produced while paused
        if not self.silent and self.streaming and now - self._t_start >= self.boot_delay_s:
            n = int((now - self._last_emit) * self.rate_hz)
            if n:
                self._last_emit += n / self.rate_hz
                self._out += self._lines(n)
        out, self._out = self._out, b""
        if not out:
            time.sleep(min(timeout, 0.005))
        return out

    def _lines(self, n: int) -> bytes:
        step_us = int(1e6 / self.rate_hz)
        parts = []
        for _ in range(n):
            self._seq += 1
            self._t_us += step_us
            if self.skip_seq_every and self._seq % self.skip_seq_every == 0:
                continue  # dropped on the device
            if self.mode == "RAW":
                # 4.0 V bus (3200 * 1.25 mV), 100 mA on 10 mOhm (400 * 2.5 uV)
                parts.append(b"D,%d,%d,3200,400\n" % (self._seq, self._t_us))
            else:
                ms = (self._t_us // 1000) % 1000
                s = 10 + (self._t_us // 1_000_000) % 50
                parts.append(b"2026-10-08 12:00:%02d.%03d,4.0,0.1,0.4\n" % (s, ms))
        return b"".join(parts)
