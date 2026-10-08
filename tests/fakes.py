"""Test doubles: a scripted serial port that emulates firmware 1.x / 2.x."""
from __future__ import annotations

import time
from typing import List


class FakePort:
    """Stands in for SerialPortHandler. Emulates the device side of the protocol."""

    def __init__(self, firmware: str = "v2", rate_hz: float = 1000.0, silent: bool = False,
                 boot_delay_s: float = 0.0, disconnect_after_s: float = 0.0,
                 skip_seq_every: int = 0):
        self.firmware = firmware
        self.rate_hz = rate_hz
        self.silent = silent
        self.boot_delay_s = boot_delay_s
        self.disconnect_after_s = disconnect_after_s
        self.skip_seq_every = skip_seq_every
        self.port = "/dev/fake"
        self.written: List[str] = []
        self.mode = "CSV"
        self._out = b""
        self._t_start = 0.0
        self._last_emit = 0.0
        self._seq = 0
        self._t_us = 1_000_000
        self.closed = False

    def open(self):
        self._t_start = self._last_emit = time.monotonic()

    def close(self):
        self.closed = True

    def write_line(self, text: str) -> None:
        self.written.append(text)
        if self.firmware != "v2":
            return
        cmd = text.strip().upper()
        if cmd == "HELLO":
            self._out += b"!HELLO,EdgePowerMeter,2.0.0,2\n"
        elif cmd == "GET":
            self._out += b"!CFG,avg=4,vct=140,ict=140,shunt=0.01,oled=1,stream=1,mode=" + \
                self.mode.encode() + b",sqw=1,period_us=%d\n" % int(1e6 / self.rate_hz)
        elif cmd.startswith("MODE"):
            self.mode = cmd.split()[1]
            self._out += b"!OK MODE\n"
            if self.mode == "RAW":
                self._out += b"T,%d,%d,S\n" % (self._t_us, 1_790_000_000)
        elif cmd.startswith(("SET", "SYNC", "SAVE")):
            self._out += b"!OK " + cmd.split()[0].encode() + b"\n"
        else:
            self._out += b"!ERR " + cmd.split()[0].encode() + b" unknown_command\n"

    def read_chunk(self, timeout: float = 0.05, max_bytes: int = 65536) -> bytes:
        from app.serial.handler import DeviceDisconnected
        now = time.monotonic()
        if self.disconnect_after_s and now - self._t_start > self.disconnect_after_s:
            raise DeviceDisconnected("unplugged")
        if not self.silent and now - self._t_start >= self.boot_delay_s:
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
