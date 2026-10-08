"""StreamDecoder: RAW / legacy parsing, calibration, loss accounting, time base."""
from __future__ import annotations

from datetime import datetime

import numpy as np
import pytest

from app.serial import protocol as P


def _decoder(**kw) -> P.StreamDecoder:
    return P.StreamDecoder(clock=lambda: 1_800_000_000.0, **kw)


def test_raw_lines_calibrated_with_device_time():
    d = _decoder()
    d.feed(b"D,1,5000000,3200,400\nD,2,5001000,3200,-400\n")
    s = d.take_samples()
    assert len(s) == 2
    assert s.t.tolist() == pytest.approx([0.0, 0.001])
    assert s.v[0] == pytest.approx(4.0)                  # 3200 * 1.25 mV
    assert s.i[0] == pytest.approx(0.1)                  # 400 * 2.5 uV / 0.01 Ohm
    assert s.i[1] == pytest.approx(-0.1)
    assert s.p[0] == pytest.approx(0.4)


def test_calibration_applied():
    cal = P.Calibration(shunt_ohm=0.02, voltage_gain=1.01, voltage_offset_v=0.01,
                        current_gain=0.5, current_offset_a=0.001)
    d = _decoder(calibration=cal)
    d.feed(b"D,1,0,3200,400\n")
    s = d.take_samples()
    assert s.v[0] == pytest.approx(4.0 * 1.01 + 0.01)
    assert s.i[0] == pytest.approx(0.05 * 0.5 + 0.001)


def test_lines_split_across_chunks():
    d = _decoder()
    d.feed(b"D,1,0,32")
    assert len(d.take_samples()) == 0
    d.feed(b"00,400\nD,2,1000,3200,4")
    d.feed(b"00\n")
    assert len(d.take_samples()) == 2


def test_seq_gap_counts_lost_samples_with_wraparound():
    d = _decoder()
    d.feed(b"D,4294967294,0,1,1\nD,1,3000,1,1\n")   # 4294967295 and 0 missing
    assert d.lost_samples == 2


def test_time_gap_counts_missed_conversions():
    d = _decoder()
    d.feed(b"!CFG,avg=1,vct=140,ict=140,period_us=1000\n")
    d.feed(b"D,1,0,1,1\nD,2,4000,1,1\n")   # 3 periods skipped, seq contiguous
    assert d.lost_samples == 0
    assert d.missed_samples == 3
    assert d.total_lost == 3


def test_reset_sequence_ignores_pause_gap():
    d = _decoder()
    d.feed(b"D,1,0,1,1\n")
    d.reset_sequence()
    d.feed(b"D,500,9000000,1,1\n")
    assert d.lost_samples == 0


def test_anchor_maps_device_time_to_wall_clock():
    d = _decoder()
    rtc = 1_790_000_000
    d.feed(b"T,2000000,%d,S\n" % rtc)
    d.feed(b"D,1,2500000,1,1\n")
    s = d.take_samples()
    expected = (datetime(1970, 1, 1) + __import__("datetime").timedelta(seconds=rtc)).timestamp() + 0.5
    assert s.wall[0] == pytest.approx(expected)
    assert d.anchor_source == "S"


def test_replies_and_logs_become_events():
    d = _decoder()
    d.feed(b"!HELLO,EdgePowerMeter,2.0.0,2\n!CFG,avg=16,vct=332,ict=588,sqw=1,period_us=14720\n"
           b"!OK SET\n!ERR SET invalid_value\n# ready\n[INFO] old style\n")
    kinds = [k for k, _ in d.take_events()]
    assert kinds == [P.EV_HELLO, P.EV_CONFIG, P.EV_OK, P.EV_ERR, P.EV_LOG, P.EV_LOG]


def test_config_parse_and_rate():
    cfg = P.DeviceConfig.parse("!CFG,avg=16,vct=332,ict=588,shunt=0.01,oled=0,stream=1,mode=RAW,sqw=1,period_us=14720")
    assert (cfg.avg, cfg.vct_us, cfg.ict_us, cfg.oled, cfg.sqw, cfg.mode) == (16, 332, 588, False, True, "RAW")
    assert cfg.sample_rate_hz == pytest.approx(1e6 / 14720)


def test_legacy_lines_use_firmware_timestamps():
    d = _decoder()
    d.feed(b"Timestamp,Voltage[V],Current[A],Power[W]\n"
           b"2026-10-08 12:00:00.000,5.0,0.2,1.0\n2026-10-08 12:00:00.004,5.0,0.2,1.0\n")
    s = d.take_samples()
    assert s.t.tolist() == pytest.approx([0.0, 0.004], abs=1e-6)   # µs: legacy is ms-resolution
    assert s.wall[0] == pytest.approx(datetime(2026, 10, 8, 12).timestamp())
    assert s.p[0] == pytest.approx(1.0)       # recomputed as V * I


def test_legacy_invalid_rtc_falls_back_to_host_clock():
    d = _decoder()
    d.feed(b"2000-01-01 00:00:01.000,5.0,0.2,1.0\n")
    s = d.take_samples()
    assert s.wall[0] == pytest.approx(1_800_000_000.0)


def test_legacy_time_is_monotonic():
    d = _decoder()
    d.feed(b"2026-10-08 12:00:00.999,1,1,1\n2026-10-08 12:00:00.998,1,1,1\n")
    s = d.take_samples()
    assert np.all(np.diff(s.t) >= 0)


def test_raw_only_ignores_legacy_lines():
    d = _decoder()
    d.raw_only = True
    d.feed(b"2026-10-08 12:00:00.000,5.0,0.2,1.0\nD,1,0,1,1\n")
    assert len(d.take_samples()) == 1


def test_accept_data_false_drops_samples_but_counts_lines():
    d = _decoder()
    d.accept_data = False
    d.feed(b"2026-10-08 12:00:00.000,5.0,0.2,1.0\n")
    assert len(d.take_samples()) == 0
    assert d.legacy_lines == 1


def test_host_averaging_blocks_with_carry():
    d = _decoder(host_averaging=4)
    d.feed(b"".join(b"D,%d,%d,%d,0\n" % (k, k * 1000, k) for k in range(6)))
    s = d.take_samples()
    assert len(s) == 1
    assert s.v[0] == pytest.approx(1.5 * 1.25e-3)
    d.feed(b"".join(b"D,%d,%d,%d,0\n" % (k, k * 1000, k) for k in range(6, 8)))
    s2 = d.take_samples()
    assert len(s2) == 1                       # carried 2 + new 2
    assert s2.v[0] == pytest.approx(5.5 * 1.25e-3)


def test_garbage_is_counted_not_fatal():
    d = _decoder()
    d.feed(b"\xff\xfe garbage\nD,x,1,2,3\n")
    assert len(d.take_samples()) == 0
    assert d.bad_lines == 2


def test_legacy_nan_values_rejected():
    d = _decoder()
    d.feed(b"2026-10-08 12:00:00.000,nan,0.2,1.0\n2026-10-08 12:00:00.001,5.0,inf,1.0\n"
           b"2026-10-08 12:00:00.002,5.0,0.2,1.0\n")
    s = d.take_samples()
    assert len(s) == 1 and d.bad_lines == 2
