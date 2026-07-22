"""Sample-rate controller and measurement parser tests."""
from __future__ import annotations

import time

from app.serial.sampler import SampleRateController
from app.serial.parser import MeasurementParser


# --- Sampler ---------------------------------------------------------------

def test_no_limit_accepts_all():
    s = SampleRateController(target_rate=0)
    assert not s.is_subsampling
    assert all(s.should_accept_sample() for _ in range(100))


def test_subsampling_reduces_rate():
    # Feed ~200 arrivals/s, target 50 Hz; expect roughly a quarter accepted.
    s = SampleRateController(target_rate=50)
    assert s.is_subsampling
    accepted = 0
    total = 400
    for _ in range(total):
        if s.should_accept_sample():
            accepted += 1
        time.sleep(1.0 / 200.0)  # 200 Hz arrival
    # Elapsed ~2 s at 50 Hz -> ~100 samples. Allow generous tolerance for jitter.
    assert 60 <= accepted <= 140


def test_update_target_resets():
    s = SampleRateController(target_rate=50)
    s.should_accept_sample()
    s.update_target(0)
    assert not s.is_subsampling
    assert s.should_accept_sample()


# --- Parser ----------------------------------------------------------------

def test_parse_csv_with_timestamp():
    m = MeasurementParser.parse_line("2025-11-30 12:34:56,12.345,1.234,15.234")
    assert m is not None
    assert abs(m.voltage - 12.345) < 1e-9
    assert abs(m.current - 1.234) < 1e-9
    assert abs(m.power - 15.234) < 1e-9


def test_parse_space_separated():
    m = MeasurementParser.parse_line("12.345 1.234 15.234")
    assert m is not None
    assert abs(m.voltage - 12.345) < 1e-9


def test_parse_invalid_returns_none():
    assert MeasurementParser.parse_line("") is None
    assert MeasurementParser.parse_line("not,a,number,here") is None
    assert MeasurementParser.parse_line("only two") is None
    assert MeasurementParser.parse_line("garbage header line") is None


def test_parse_old_rtc_uses_local_time():
    # Year < 2020 means RTC not set -> parser substitutes current time.
    m = MeasurementParser.parse_line("2000-01-01 00:00:00,5.0,0.1,0.5")
    assert m is not None
    assert m.timestamp.year >= 2020
