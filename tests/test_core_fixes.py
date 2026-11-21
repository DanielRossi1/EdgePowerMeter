"""Regression tests: benchmark gaps, running gap threshold, settings sanitising."""
from __future__ import annotations

import dataclasses
import math

import numpy as np
import pytest

from app.core import RunningStats, Samples, Statistics
from app.core.benchmark import BenchmarkInput, compute
from app.core.settings import AppSettings, _coerce


def _run(n, t0, rate=1.0, p=10.0):
    t = t0 + np.arange(n) / rate
    v = np.full(n, 10.0)
    i = np.full(n, p / 10.0)
    return Samples.from_columns(t, 1.79e9 + t, v, i, v * i)


# ---------------------------------------------------------- benchmark gaps

def test_benchmark_power_ignores_disconnection_gap():
    # 100 s at 10 W, 1000 s outage, 100 s at 10 W (1 Hz sampling).
    a = _run(101, 0.0)
    b = _run(101, 1100.0)
    st = Statistics.from_samples(a.concat(b))
    assert st.duration_seconds == pytest.approx(1200.0)
    assert st.connected_seconds == pytest.approx(200.0)
    assert st.energy_wh * 3600 == pytest.approx(2000.0)
    r = compute(st, BenchmarkInput(units=100))
    assert r.avg_power_w == pytest.approx(10.0)
    assert r.rate == pytest.approx(100 / 200.0)
    assert r.duration_s == pytest.approx(1200.0)
    assert r.connected_s == pytest.approx(200.0)
    assert r.energy_j == pytest.approx(2000.0)


def test_connected_equals_duration_without_gaps():
    st = Statistics.from_samples(_run(501, 0.0))
    assert st.connected_seconds == pytest.approx(st.duration_seconds)


def test_benchmark_all_gap_range_is_none():
    st = dataclasses.replace(Statistics.from_samples(_run(11, 0.0)), connected_seconds=0.0)
    assert compute(st, BenchmarkInput(units=5)) is None


# ------------------------------------------------------ running gap threshold

def test_small_batch_does_not_persist_gap_threshold():
    rs = RunningStats()
    # 4 intervals of 1 ms: a per-call threshold of 2 s (floor). Must not be stored.
    rs.update(_run(5, 0.0, rate=1000.0))
    assert rs._gap is None
    # Slow stream, 10 s intervals (join interval included). A persisted 2 s
    # threshold would wrongly skip all of them; the per-call threshold (100 s)
    # integrates them: 10 W over 0.004 s + 50 s.
    rs.update(_run(5, 10.004, rate=0.1))
    assert rs.energy_ws == pytest.approx(10.0 * 50.004, rel=1e-6)


# --------------------------------------------------------------- settings

def test_coerce_rejects_non_finite_numbers():
    with pytest.raises(ValueError):
        _coerce(float("inf"), 1)
    with pytest.raises(ValueError):
        _coerce("nan", 1.0)
    assert _coerce("3", 1) == 3


def test_validate_resets_nan_and_inf_to_default():
    s = AppSettings()
    s.line_width = float("nan")
    s.time_window_s = float("inf")
    s.grid_alpha = float("nan")
    s.validate()
    d = AppSettings()
    assert s.line_width == d.line_width
    assert s.time_window_s == d.time_window_s
    assert s.grid_alpha == d.grid_alpha
    assert all(not isinstance(getattr(s, f), float) or math.isfinite(getattr(s, f))
               for f in ("line_width", "time_window_s", "grid_alpha", "shunt_ohm"))


# ------------------------------------------------------ marker labels (CSV safety)

from app.core.markers import Marker, MarkerList, clean_label  # noqa: E402


def test_clean_label_replaces_lone_surrogates_and_stays_utf8():
    out = clean_label("A\ud800B\udfffC")
    assert out == "A�B�C"
    out.encode("utf-8")                          # must not raise


def test_clean_label_keeps_existing_cap_and_whitespace_rules():
    assert clean_label("x\ty  z") == "x y z"
    assert len(clean_label("a" * 300)) == 60


@pytest.mark.parametrize("label", ["=1+1", "+1", "-2", "@SUM(1)", "'quoted", "'=x"])
def test_marker_line_escapes_formula_leads(label):
    field = MarkerList.line(Marker(1, 0.5, label)).split("\t")[3]
    assert field == "'" + label


def test_marker_line_leaves_ordinary_labels_alone():
    assert MarkerList.line(Marker(1, 0.5, "peak load")).split("\t")[3] == "peak load"


def test_marker_line_escapes_tab_and_cr_leads_without_breaking_fields():
    line = MarkerList.line(Marker(1, 0.5, "\t=x\rY"))
    assert line.count("\t") == 3 and "\r" not in line
    assert line.split("\t")[3] == "' =x Y"       # leading space still counts as a formula lead


def test_marker_label_round_trips_through_line_and_apply_line():
    labels = ["=1+1", "+x", "-x", "@x", "'=x", "'plain", "plain", "a b c", "''=x"]
    src = MarkerList()
    for k, lab in enumerate(labels):
        src.add(0.1 * k, lab)
    dst = MarkerList()
    for ln in src.lines():
        assert dst.apply_line(ln + "\n")
    assert [m.label for m in dst.sorted()] == [m.label for m in src.sorted()]


def test_marker_line_with_surrogate_label_is_valid_utf8():
    line = MarkerList.line(Marker(3, 1.0, "BAD\ud800"))
    line.encode("utf-8")
    assert line.endswith("BAD�")


# ------------------------------------------------------------ settings clamps

import os  # noqa: E402
import stat as _stat  # noqa: E402
from pathlib import Path  # noqa: E402

from app.core.settings import BAUD_RATES, AppSettings as _AS  # noqa: E402


@pytest.mark.parametrize("baud", [9600, 115200, 2000000])
def test_known_baud_rates_are_kept(baud):
    s = _AS(baud_rate=baud)
    s.validate()
    assert s.baud_rate == baud


@pytest.mark.parametrize("baud", [12345, 0, -7, 99999999, 2000001])
def test_unknown_baud_rate_falls_back_to_115200(baud):
    s = _AS(baud_rate=baud)
    s.validate()
    assert s.baud_rate == 115200
    assert 115200 in BAUD_RATES


def test_calibration_offsets_are_limited():
    s = _AS(voltage_offset_v=500.0, current_offset_a=-100.5)
    s.validate()
    assert s.voltage_offset_v == 0.0
    assert s.current_offset_a == 0.0


def test_calibration_offsets_at_limit_are_kept():
    s = _AS(voltage_offset_v=-100.0, current_offset_a=100.0)
    s.validate()
    assert s.voltage_offset_v == -100.0
    assert s.current_offset_a == 100.0


def test_non_finite_offsets_and_gains_reset_to_defaults():
    s = _AS(voltage_offset_v=float("nan"), current_offset_a=float("inf"),
            voltage_gain=float("nan"), current_gain=9.0)
    s.validate()
    d = _AS()
    assert (s.voltage_offset_v, s.current_offset_a) == (d.voltage_offset_v, d.current_offset_a)
    assert s.voltage_gain == d.voltage_gain
    assert s.current_gain == d.current_gain


# ------------------------------------------------- settings file permissions

@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")
def test_saved_settings_file_is_owner_only(tmp_path: Path):
    # A child process so XDG_CONFIG_HOME decides where QSettings writes (the
    # test session itself redirects QSettings to a temporary directory).
    import subprocess
    import sys

    root = Path(__file__).resolve().parent.parent
    home = tmp_path / "home"
    cfg = tmp_path / "config"
    env = dict(os.environ, XDG_CONFIG_HOME=str(cfg), HOME=str(home), QT_QPA_PLATFORM="offscreen")
    script = (
        "import os, sys\n"
        "from PySide6.QtCore import QSettings\n"
        "from app.core.settings import AppSettings, _ORG, _APP\n"
        "AppSettings(api_token='secret-token').save()\n"
        "print(QSettings(_ORG, _APP).fileName())\n"
    )
    out = subprocess.run([sys.executable, "-c", script], cwd=str(root), env=env,
                         capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr
    conf = Path(out.stdout.strip().splitlines()[-1])
    assert str(cfg) in str(conf)                # written under the temporary config dir
    assert conf.exists() and "secret-token" in conf.read_text(encoding="utf-8", errors="replace")
    assert _stat.S_IMODE(conf.stat().st_mode) == 0o600

    os.chmod(conf, 0o644)                       # e.g. recreated with default umask
    child = script.replace("AppSettings(api_token='secret-token')",
                           "AppSettings(api_token='secret-token', baud_rate=921600)")
    subprocess.run([sys.executable, "-c", child], cwd=str(root), env=env, check=True,
                   capture_output=True, timeout=120)
    assert _stat.S_IMODE(conf.stat().st_mode) == 0o600


def test_restrict_permissions_ignores_missing_path(tmp_path: Path):
    from app.core.settings import _restrict_permissions
    _restrict_permissions(str(tmp_path / "does-not-exist.conf"))   # must not raise
    _restrict_permissions("")                                       # must not raise
