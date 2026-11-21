"""The `analysis` extension: spectrum, power quality, distribution and peak detection.

The tools come from the real registry (app/api/ext/__init__.py), and run through
ApiService.call, so argument validation is the real one.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("PySide6.QtWidgets")

from app.api import ext, schema
from app.api.ext import analysis_schema
from app.api.service import ApiError, ApiService, DirectContext
from app.core import AppSettings, Samples

RATE = 100.0
ROOT = Path(__file__).resolve().parents[1]
TOOL_NAMES = ("analyze_spectrum", "power_quality", "distribution", "detect_peaks")


# ----------------------------------------------------------------- helpers

def samples_from(p, rate=RATE, v=5.0, i=None):
    """Samples with constant voltage v, current p/v (or i), power p (all arrays or scalars)."""
    p = np.asarray(p, dtype=np.float64)
    n = p.size
    t = np.arange(n) / rate
    volts = np.broadcast_to(np.asarray(v, dtype=np.float64), (n,)).copy()
    amps = p / volts if i is None else np.broadcast_to(np.asarray(i, dtype=np.float64), (n,))
    return Samples.from_columns(t, 1.79e9 + t, volts, amps, p)


def make_service(data=None):
    ctrl = SimpleNamespace(data=lambda: data if data is not None else Samples.empty(),
                           markers=None, state="idle")
    return ApiService(ctrl, AppSettings(), DirectContext())


@pytest.fixture
def registered():
    """The analysis tools are in the real registry (app/api/ext), so there is nothing to add."""
    assert "analysis" in ext.EXTENSIONS
    assert set(TOOL_NAMES) <= set(schema.TOOLS_BY_NAME)


def call(data, name, **args):
    """Run a tool through the service and check the result is strict JSON (no NaN)."""
    out = make_service(data).call(name, args)
    json.dumps(out, allow_nan=False)
    return out


# ------------------------------------------------------------ registration

def test_schema_module_is_stdlib_only_apart_from_the_api_package():
    tree = ast.parse((ROOT / "app/api/ext/analysis_schema.py").read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported <= {"__future__", "typing", "app"}, imported


def test_tool_names_and_timeouts():
    assert tuple(t.name for t in analysis_schema.TOOLS) == TOOL_NAMES
    assert analysis_schema.TIMEOUTS == {name: "long" for name in TOOL_NAMES}


def test_every_tool_takes_the_range_arguments():
    for t in analysis_schema.TOOLS:
        assert set(schema.RANGE_PROPERTIES) <= set(t.input_schema["properties"]), t.name
        assert t.input_schema["additionalProperties"] is False


def test_install_extensions_on_copies_adds_all_tools():
    # Copies without the analysis tools, which the real registry already holds.
    tools = [t for t in schema.TOOLS if t.name not in TOOL_NAMES]
    by_name = {n: t for n, t in schema.TOOLS_BY_NAME.items() if n not in TOOL_NAMES}
    timeouts = {}
    schema.install_extensions([analysis_schema], tools, by_name, timeouts)
    assert set(TOOL_NAMES) <= set(by_name)
    assert timeouts == {name: "long" for name in TOOL_NAMES}


def test_real_registry_lists_analysis():
    assert "analysis" in ext.EXTENSIONS
    assert set(TOOL_NAMES) <= set(schema.TOOLS_BY_NAME)


def test_handler_table_matches_the_declared_tools():
    from app.api.ext import analysis
    table = analysis.handlers(make_service())
    assert set(table) == set(TOOL_NAMES)


# ------------------------------------------------------------- spectrum

def sine_current(n=2000, freq=5.0, rate=RATE):
    t = np.arange(n) / rate
    return samples_from(np.full(n, 1.0), rate=rate, i=0.2 + 0.05 * np.sin(2 * np.pi * freq * t))


def test_spectrum_finds_the_sine_frequency(registered):
    out = call(sine_current(), "analyze_spectrum", signal="current")
    assert out["available"] is True
    assert out["peaks"][0]["frequency_hz"] == pytest.approx(5.0, abs=0.1)
    assert out["peaks"][0]["amplitude"] == pytest.approx(0.05, rel=0.05)
    assert out["sample_rate"] == pytest.approx(RATE, rel=1e-6)
    assert out["nyquist_hz"] == pytest.approx(RATE / 2, rel=1e-6)
    assert out["resolution_hz"] == pytest.approx(1 / 20.0, rel=1e-3)
    assert out["modulation_percent"] == pytest.approx(out["std"] / out["mean"] * 100)


def test_spectrum_output_is_reduced_to_points_out(registered):
    data = sine_current()
    default = call(data, "analyze_spectrum")
    assert len(default["spectrum"]["frequency_hz"]) == 256
    assert len(default["spectrum"]["amplitude"]) == 256
    small = call(data, "analyze_spectrum", points_out=16)
    assert len(small["spectrum"]["frequency_hz"]) == 16
    # Max-pooling keeps the strongest bin, so the peak survives the reduction.
    assert max(small["spectrum"]["amplitude"]) == pytest.approx(
        default["peaks"][0]["amplitude"], rel=1e-6)


def test_spectrum_n_peaks_limits_the_peak_list(registered):
    rng = np.random.default_rng(1)
    t = np.arange(2000) / RATE
    wave = sum(0.01 * k * np.sin(2 * np.pi * k * 3.0 * t) for k in range(1, 8))
    data = samples_from(np.ones(2000), i=0.2 + wave + 0.001 * rng.standard_normal(2000))
    out = call(data, "analyze_spectrum", n_peaks=3)
    assert len(out["peaks"]) == 3


@pytest.mark.parametrize("n", [63])
def test_spectrum_too_short_is_not_an_error(registered, n):
    data = samples_from(np.linspace(1, 2, n), v=5.0)
    out = call(data, "analyze_spectrum")
    assert out["available"] is False
    assert "need at least 64 samples" in out["reason"] and "got 63" in out["reason"]


def test_spectrum_with_64_samples_is_computed(registered):
    t = np.arange(64) / RATE
    data = samples_from(np.ones(64), i=0.2 + 0.01 * np.sin(2 * np.pi * 10 * t))
    out = call(data, "analyze_spectrum")
    assert out["available"] is True
    assert len(out["spectrum"]["frequency_hz"]) == 33   # 64 // 2 + 1 bins


def test_spectrum_two_samples_is_reported_not_raised(registered):
    out = call(samples_from([1.0, 2.0]), "analyze_spectrum")
    assert out["available"] is False


def test_spectrum_constant_signal_is_reported(registered):
    out = call(samples_from(np.full(500, 2.0), v=5.0), "analyze_spectrum", signal="power")
    assert out["available"] is False
    assert "no variation" in out["reason"]


def test_spectrum_zero_and_one_sample_are_errors(registered):
    with pytest.raises(ApiError):
        make_service().call("analyze_spectrum", {})
    with pytest.raises(ApiError, match="need at least 2"):
        make_service(samples_from([1.0])).call("analyze_spectrum", {})


def test_spectrum_huge_values_stay_finite(registered):
    t = np.arange(500) / RATE
    data = samples_from(np.ones(500), i=1e30 + 1e28 * np.sin(2 * np.pi * 4 * t))
    out = call(data, "analyze_spectrum")
    assert out["available"] is True
    assert out["mean"] == pytest.approx(1e30, rel=1e-6)
    assert all(np.isfinite(out["spectrum"]["amplitude"]))


def test_spectrum_rejects_nan(registered):
    data = samples_from(np.ones(100))
    data.i[10] = np.nan
    with pytest.raises(ApiError, match="NaN or infinite"):
        make_service(data).call("analyze_spectrum", {"signal": "current"})


def test_spectrum_range_argument_selects_samples(registered):
    out = call(sine_current(), "analyze_spectrum", last_s=5.0)
    assert out["range"]["label"] == "last 5 s"
    assert out["range"]["samples"] == pytest.approx(500, abs=2)


# ------------------------------------------------------- power quality

def ripple_supply(n=1000, nominal=5.0, ripple_v=0.01):
    t = np.arange(n) / RATE
    v = nominal + ripple_v * np.sin(2 * np.pi * 3 * t)   # 50 Hz would alias at 100 Hz
    return Samples.from_columns(t, 1.79e9 + t, v, np.full(n, 0.2), v * 0.2)


def test_power_quality_ripple_and_rating(registered):
    out = call(ripple_supply(), "power_quality", nominal_voltage=5.0)
    assert out["voltage_ripple_mv"] == pytest.approx(20.0, rel=2e-2)
    assert out["voltage_ripple_percent"] == pytest.approx(0.4, rel=2e-2)
    assert out["stability_rating"] == "fair"
    assert out["meets_1percent_spec"] is True
    assert out["meets_01percent_spec"] is False
    assert out["nominal_voltage"] == pytest.approx(5.0)
    assert out["nominal_voltage_source"] == "given"
    assert out["load_regulation_percent"] is None
    assert out["settling_time_ms"] is None


def test_power_quality_defaults_nominal_to_mean(registered):
    out = call(ripple_supply(), "power_quality")
    assert out["nominal_voltage_source"] == "mean"
    assert out["nominal_voltage"] == pytest.approx(5.0, abs=1e-3)


def test_power_quality_load_step_gives_regulation_and_settling(registered):
    n = 1000
    t = np.arange(n) / RATE
    i = np.where(t < 5.0, 0.2, 1.0)
    decay = 0.2 * np.exp(-np.clip(t - 5.0, 0, None) / 0.05)
    v = np.where(t < 5.0, 5.0, 4.9 + decay * (t >= 5.0))
    data = Samples.from_columns(t, 1.79e9 + t, v, i, v * i)
    out = call(data, "power_quality", nominal_voltage=5.0)
    assert out["load_regulation_percent"] is not None
    assert out["load_regulation_percent"] > 0
    assert out["settling_time_ms"] is not None and out["settling_time_ms"] > 0


def test_power_quality_needs_ten_samples(registered):
    with pytest.raises(ApiError, match="need at least 10 samples"):
        make_service(samples_from(np.ones(9))).call("power_quality", {})
    out = call(samples_from(np.ones(10)), "power_quality")
    assert out["range"]["samples"] == 10


@pytest.mark.parametrize("n", [0, 1, 2])
def test_power_quality_short_or_empty_is_an_error(registered, n):
    data = samples_from(np.ones(n)) if n else Samples.empty()
    with pytest.raises(ApiError):
        make_service(data).call("power_quality", {})


def test_power_quality_zero_nominal_gives_null_ripple(registered):
    out = call(ripple_supply(nominal=0.0, ripple_v=0.0), "power_quality", nominal_voltage=0.0)
    assert out["voltage_ripple_percent"] is None
    assert out["meets_1percent_spec"] is False
    assert out["stability_rating"] == "poor"


def test_power_quality_huge_voltage_is_finite(registered):
    data = ripple_supply(nominal=3.0e38, ripple_v=0.0)
    out = call(data, "power_quality")
    assert out["max_voltage"] == pytest.approx(3.0e38, rel=1e-3)


def test_power_quality_nominal_voltage_validation(registered):
    svc = make_service(ripple_supply())
    with pytest.raises(ApiError, match="nominal_voltage must be at most 1000"):
        svc.call("power_quality", {"nominal_voltage": 1001})
    with pytest.raises(ApiError, match="nominal_voltage must be at least 0"):
        svc.call("power_quality", {"nominal_voltage": -1})


# ----------------------------------------------------------- distribution

def test_distribution_summary_and_percentiles(registered):
    p = np.arange(1, 101, dtype=np.float64)              # 1..100 W
    out = call(samples_from(p), "distribution")
    assert out["count"] == 100
    assert out["min"] == pytest.approx(1.0) and out["max"] == pytest.approx(100.0)
    assert out["mean"] == pytest.approx(50.5, rel=1e-6)
    assert out["percentiles"]["p50"] == pytest.approx(np.percentile(p, 50), rel=1e-6)
    assert set(out["percentiles"]) == {"p1", "p5", "p25", "p50", "p75", "p95", "p99"}
    assert len(out["histogram"]["edges"]) == 41
    assert len(out["histogram"]["counts"]) == 40
    assert sum(out["histogram"]["counts"]) == 100


def test_distribution_custom_percentiles_and_keys(registered):
    out = call(samples_from(np.arange(1, 101.0)), "distribution",
               percentiles=[2.5, 0, 100], bins=2)
    assert set(out["percentiles"]) == {"p2.5", "p0", "p100"}
    assert out["percentiles"]["p100"] == pytest.approx(100.0)
    assert out["percentiles"]["p0"] == pytest.approx(1.0)
    assert len(out["histogram"]["counts"]) == 2


def test_distribution_quantity_selects_the_column(registered):
    data = samples_from(np.full(50, 4.0), v=2.0)         # current 2 A, voltage 2 V
    assert call(data, "distribution", quantity="current")["mean"] == pytest.approx(2.0)
    assert call(data, "distribution", quantity="voltage")["mean"] == pytest.approx(2.0)
    assert call(data, "distribution")["mean"] == pytest.approx(4.0)   # power default


def test_distribution_bins_limits(registered):
    data = samples_from(np.arange(1, 101.0))
    svc = make_service(data)
    with pytest.raises(ApiError, match="bins must be at least 2"):
        svc.call("distribution", {"bins": 1})
    with pytest.raises(ApiError, match="bins must be at most 200"):
        svc.call("distribution", {"bins": 201})
    assert len(svc.call("distribution", {"bins": 200})["histogram"]["counts"]) == 200


def test_distribution_percentile_validation(registered):
    svc = make_service(samples_from(np.arange(1, 101.0)))
    with pytest.raises(ApiError, match="at most 100"):
        svc.call("distribution", {"percentiles": [50, 101]})
    with pytest.raises(ApiError, match="at least 0"):
        svc.call("distribution", {"percentiles": [-1]})
    with pytest.raises(ApiError, match="at least 1 item"):
        svc.call("distribution", {"percentiles": []})
    with pytest.raises(ApiError, match="at most 20 items"):
        svc.call("distribution", {"percentiles": list(range(21))})
    assert len(svc.call("distribution", {"percentiles": list(range(20))})["percentiles"]) == 20


def test_distribution_percentile_items_must_be_numbers(registered):
    with pytest.raises(ApiError, match="must be a number"):
        make_service(samples_from(np.ones(5))).call("distribution", {"percentiles": ["50"]})


def test_distribution_constant_data(registered):
    out = call(samples_from(np.full(10, 3.0)), "distribution")
    assert out["std"] == pytest.approx(0.0)
    assert out["percentiles"]["p50"] == pytest.approx(3.0)
    assert sum(out["histogram"]["counts"]) == 10


@pytest.mark.parametrize("n", [2, 63, 64])
def test_distribution_small_counts(registered, n):
    out = call(samples_from(np.linspace(1, 2, n)), "distribution")
    assert out["count"] == n
    assert sum(out["histogram"]["counts"]) == n


@pytest.mark.parametrize("n", [0, 1])
def test_distribution_zero_or_one_sample_is_an_error(registered, n):
    data = samples_from(np.ones(n)) if n else Samples.empty()
    with pytest.raises(ApiError):
        make_service(data).call("distribution", {})


def test_distribution_huge_values(registered):
    p = np.array([3.0e38, -3.0e38, 1.0, 2.0])
    out = call(samples_from(p, v=1.0), "distribution")
    assert out["max"] == pytest.approx(3.0e38, rel=1e-6)
    assert out["min"] == pytest.approx(-3.0e38, rel=1e-6)
    assert all(np.isfinite(out["histogram"]["edges"]))


def test_distribution_range_arguments(registered):
    out = call(samples_from(np.ones(1001)), "distribution", t0=5.0, t1=6.0)
    assert out["count"] == 101
    assert out["range"]["t0"] == pytest.approx(5.0)


def test_distribution_rejects_nan(registered):
    data = samples_from(np.ones(20))
    data.p[3] = np.nan
    with pytest.raises(ApiError, match="NaN or infinite"):
        make_service(data).call("distribution", {})


# ----------------------------------------------------------- detect_peaks

def pulses(starts_s, lengths_s, base=1.0, high=10.0, n=1000, rate=RATE):
    p = np.full(n, base)
    for s, length in zip(starts_s, lengths_s):
        a = int(round(s * rate))
        b = a + int(round(length * rate))
        p[a:b] = high
    return samples_from(p, rate=rate)


def test_detect_peaks_threshold_events(registered):
    data = pulses([2.0, 5.0], [0.5, 0.5])                 # samples [200,250) and [500,550)
    out = call(data, "detect_peaks", threshold=5.0)
    assert out["count"] == 2 and out["truncated"] is False
    assert out["threshold_used"] == pytest.approx(5.0)
    first, second = out["events"]
    assert first["t_start"] == pytest.approx(2.0)
    assert first["t_end"] == pytest.approx(2.49)
    assert first["duration_s"] == pytest.approx(0.49)
    assert first["peak"] == pytest.approx(10.0)
    assert first["mean"] == pytest.approx(10.0)
    assert first["energy_j"] == pytest.approx(10.0 * 0.49, rel=1e-6)
    assert second["t_start"] == pytest.approx(5.0)
    assert out["mean_duration_s"] == pytest.approx(0.49)
    assert out["rate_per_s"] == pytest.approx(2 / 9.99, rel=1e-3)


def test_detect_peaks_sigma_threshold_default(registered):
    data = pulses([2.0], [0.2])
    out = call(data, "detect_peaks")
    p = data.p.astype(np.float64)
    expected = float(p.mean()) + 3.0 * float(p.std())
    assert out["threshold_used"] == pytest.approx(expected, rel=1e-6)
    assert out["count"] == 1
    assert "energy_j" in out["events"][0]


def test_detect_peaks_sigma_argument(registered):
    data = pulses([2.0], [0.2])
    p = data.p.astype(np.float64)
    out = call(data, "detect_peaks", threshold_sigma=0.5)
    assert out["threshold_used"] == pytest.approx(float(p.mean()) + 0.5 * float(p.std()),
                                                  rel=1e-6)


def test_detect_peaks_threshold_above_maximum_gives_no_events(registered):
    out = call(pulses([2.0], [0.5]), "detect_peaks", threshold=1000.0)
    assert out["count"] == 0
    assert out["events"] == []
    assert out["truncated"] is False
    assert out["mean_duration_s"] is None
    assert out["rate_per_s"] == pytest.approx(0.0)
    assert out["threshold_used"] == pytest.approx(1000.0)


def test_detect_peaks_max_events_truncates_but_counts_all(registered):
    data = pulses([1.0, 2.0, 3.0, 4.0, 5.0], [0.05] * 5)
    out = call(data, "detect_peaks", threshold=5.0, max_events=2)
    assert out["count"] == 5
    assert out["truncated"] is True
    assert len(out["events"]) == 2
    assert [e["t_start"] for e in out["events"]] == pytest.approx([1.0, 2.0])


def test_detect_peaks_max_events_exact_fit_is_not_truncated(registered):
    data = pulses([1.0, 2.0], [0.05, 0.05])
    out = call(data, "detect_peaks", threshold=5.0, max_events=2)
    assert out["count"] == 2 and out["truncated"] is False


def test_detect_peaks_min_duration_filters_short_events(registered):
    data = pulses([1.0, 3.0], [0.1, 0.5])
    out = call(data, "detect_peaks", threshold=5.0, min_duration_s=0.2)
    assert out["count"] == 1
    assert out["events"][0]["t_start"] == pytest.approx(3.0)


def test_detect_peaks_rate_uses_recording_span(registered):
    data = pulses([1.0, 3.0, 6.0], [0.05] * 3, n=1001)   # span 10 s
    out = call(data, "detect_peaks", threshold=5.0)
    assert out["rate_per_s"] == pytest.approx(3 / 10.0)


def test_detect_peaks_constant_signal(registered):
    flat = samples_from(np.full(100, 2.0))
    assert call(flat, "detect_peaks")["count"] == 0      # nothing strictly above the mean
    out = call(flat, "detect_peaks", threshold=1.0)
    assert out["count"] == 1
    assert out["events"][0]["duration_s"] == pytest.approx(0.99)
    assert out["events"][0]["energy_j"] == pytest.approx(2.0 * 0.99)


def test_detect_peaks_current_has_no_energy(registered):
    data = pulses([2.0], [0.2])
    out = call(data, "detect_peaks", quantity="current", threshold=0.01)
    assert out["count"] == 1
    assert "energy_j" not in out["events"][0]


def test_detect_peaks_run_at_the_end_of_the_data(registered):
    p = np.ones(100)
    p[-5:] = 10.0
    out = call(samples_from(p), "detect_peaks", threshold=5.0)
    assert out["count"] == 1
    assert out["events"][0]["t_end"] == pytest.approx(99 / RATE)


def test_detect_peaks_two_samples(registered):
    out = call(samples_from([10.0, 10.0]), "detect_peaks", threshold=5.0)
    assert out["count"] == 1
    assert out["events"][0]["duration_s"] == pytest.approx(0.01)


@pytest.mark.parametrize("n", [0, 1])
def test_detect_peaks_zero_or_one_sample_is_an_error(registered, n):
    data = samples_from(np.ones(n)) if n else Samples.empty()
    with pytest.raises(ApiError):
        make_service(data).call("detect_peaks", {})


@pytest.mark.parametrize("n", [63, 64])
def test_detect_peaks_around_64_samples(registered, n):
    p = np.ones(n)
    p[n // 2] = 10.0
    out = call(samples_from(p), "detect_peaks", threshold=5.0)
    assert out["count"] == 1 and out["events"][0]["peak"] == pytest.approx(10.0)


def test_detect_peaks_huge_values(registered):
    data = samples_from(np.full(50, 3.0e38), v=1.0)
    out = call(data, "detect_peaks", threshold=1.0)
    assert out["count"] == 1
    assert out["events"][0]["peak"] == pytest.approx(3.0e38, rel=1e-6)
    assert np.isfinite(out["events"][0]["energy_j"])


def test_detect_peaks_rejects_nan(registered):
    data = samples_from(np.ones(20))
    data.p[4] = np.inf
    with pytest.raises(ApiError, match="NaN or infinite"):
        make_service(data).call("detect_peaks", {})


def test_detect_peaks_argument_validation(registered):
    svc = make_service(pulses([1.0], [0.1]))
    with pytest.raises(ApiError, match="threshold_sigma must be at least 0.5"):
        svc.call("detect_peaks", {"threshold_sigma": 0.4})
    with pytest.raises(ApiError, match="threshold_sigma must be at most 20"):
        svc.call("detect_peaks", {"threshold_sigma": 21})
    with pytest.raises(ApiError, match="min_duration_s must be at most 60"):
        svc.call("detect_peaks", {"min_duration_s": 61})
    with pytest.raises(ApiError, match="max_events must be at least 1"):
        svc.call("detect_peaks", {"max_events": 0})
    with pytest.raises(ApiError, match="max_events must be at most 1000"):
        svc.call("detect_peaks", {"max_events": 1001})
    with pytest.raises(ApiError, match="quantity must be one of"):
        svc.call("detect_peaks", {"quantity": "energy"})
    with pytest.raises(ApiError, match="threshold must be a number"):
        svc.call("detect_peaks", {"threshold": "high"})


# ------------------------------------------------- schema validation, all tools

def test_unknown_argument_is_rejected(registered):
    svc = make_service(samples_from(np.ones(100)))
    for name in TOOL_NAMES:
        with pytest.raises(ApiError, match="unknown key 'bogus'"):
            svc.call(name, {"bogus": 1})


def test_enum_and_integer_limits(registered):
    svc = make_service(samples_from(np.arange(1, 201.0)))
    with pytest.raises(ApiError, match="signal must be one of"):
        svc.call("analyze_spectrum", {"signal": "energy"})
    with pytest.raises(ApiError, match="n_peaks must be at least 1"):
        svc.call("analyze_spectrum", {"n_peaks": 0})
    with pytest.raises(ApiError, match="n_peaks must be at most 20"):
        svc.call("analyze_spectrum", {"n_peaks": 21})
    with pytest.raises(ApiError, match="max_points must be at least 1024"):
        svc.call("analyze_spectrum", {"max_points": 1023})
    with pytest.raises(ApiError, match="points_out must be at most 2048"):
        svc.call("analyze_spectrum", {"points_out": 2049})
    with pytest.raises(ApiError, match="points_out must be an integer"):
        svc.call("analyze_spectrum", {"points_out": 16.5})


def test_range_argument_validation_is_shared(registered):
    svc = make_service(samples_from(np.ones(100)))
    with pytest.raises(ApiError, match="last_s must be greater than 0"):
        svc.call("distribution", {"last_s": 0})
    with pytest.raises(ApiError, match="t1 must be greater than t0"):
        svc.call("power_quality", {"t0": 2.0, "t1": 1.0})


# ------------------------------------------------- adversarial fixes: percentile keys

def test_percentiles_that_would_share_a_key_are_refused(registered):
    data = samples_from(np.arange(1, 101.0))
    with pytest.raises(ApiError, match="percentiles too close"):
        call(data, "distribution", percentiles=[99.9999, 99.99991])
    with pytest.raises(ApiError, match="percentiles too close"):
        call(data, "distribution", percentiles=[50, 50.0])


def test_readable_keys_are_kept_for_distinct_percentiles(registered):
    out = call(samples_from(np.arange(1, 1001.0)), "distribution", percentiles=[99.9, 99])
    assert set(out["percentiles"]) == {"p99.9", "p99"}
    assert out["percentiles"]["p99.9"] == pytest.approx(np.percentile(np.arange(1, 1001.0), 99.9))


# ------------------------------------------------- adversarial fixes: spectrum decimation

def test_spectrum_reports_no_decimation_for_a_short_range(registered):
    out = call(sine_current(), "analyze_spectrum")
    assert out["decimated"] is False
    assert out["effective_sample_rate_hz"] == pytest.approx(RATE, rel=1e-6)
    assert out["note"] is None


def test_spectrum_reports_decimation_with_the_effective_rate_and_the_filter_cutoff(registered):
    # 2000 samples with max_points 1024: decimated by 2 (100 Hz -> 50 Hz) before the FFT.
    out = call(sine_current(n=2000), "analyze_spectrum", max_points=1024)
    assert out["decimated"] is True
    assert out["effective_sample_rate_hz"] == pytest.approx(RATE / 2, rel=1e-3)
    assert out["sample_rate"] == pytest.approx(out["effective_sample_rate_hz"], rel=1e-9)
    cutoff = 0.8 * out["nyquist_hz"]                    # 20 Hz for the 25 Hz Nyquist
    assert cutoff == pytest.approx(20.0, rel=1e-3)
    assert out["note"] == (f"components above {cutoff:.4g} Hz were filtered out before the FFT")
