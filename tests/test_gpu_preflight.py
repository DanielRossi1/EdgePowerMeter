"""GPU preflight tests: cache persistence, hybrid-GPU detection, and the
tiered default -> nvidia_prime -> software decision logic.

Regression coverage for a bug where the app assumed the default GL
environment always works, which produced a blank/crashed window under
snap strict confinement on hybrid Intel/NVIDIA (Optimus) laptops. The
fix probes GL in an isolated subprocess before QApplication exists and,
on failure, tries NVIDIA PRIME render-offload env vars (if a hybrid
NVIDIA setup is actually detected) before finally falling back to
software rendering - never leaving a silently blank window.
"""
from __future__ import annotations

import subprocess

import pytest

pytest.importorskip("PySide6.QtCore")

from PySide6.QtCore import QSettings

from app.core import gpu_cache, gpu_preflight


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Redirect the GPU cache to a throwaway ini file for every test, so
    tests never read/write the user's real EdgePowerMeter config, and
    each test starts from a clean cache."""
    ini_path = str(tmp_path / "gpu_preflight_test.ini")
    monkeypatch.setattr(gpu_cache, "_settings", lambda: QSettings(ini_path, QSettings.IniFormat))


@pytest.fixture(autouse=True)
def _clear_mode_env(monkeypatch):
    monkeypatch.delenv(gpu_preflight._MODE_ENV_VAR, raising=False)


# --- cache -------------------------------------------------------------

def test_cache_round_trip():
    assert gpu_cache.read_cached_mode("fp-a") is None
    gpu_cache.write_cache("fp-a", gpu_preflight.MODE_NVIDIA_PRIME)
    assert gpu_cache.read_cached_mode("fp-a") == gpu_preflight.MODE_NVIDIA_PRIME


def test_cache_miss_on_fingerprint_change():
    gpu_cache.write_cache("fp-old", gpu_preflight.MODE_SOFTWARE)
    assert gpu_cache.read_cached_mode("fp-new") is None


def test_clear_cache_removes_entry():
    gpu_cache.write_cache("fp-a", gpu_preflight.MODE_DEFAULT)
    gpu_cache.clear_cache()
    assert gpu_cache.read_cached_mode("fp-a") is None


# --- ensure_gpu_ready decision tree --------------------------------------
# GPU inventory detection itself (lspci + sysfs vendor-ID fallback) is
# covered separately in tests/test_gpu_detect.py.

def test_ensure_gpu_ready_returns_immediately_if_mode_env_already_set(monkeypatch):
    monkeypatch.setenv(gpu_preflight._MODE_ENV_VAR, gpu_preflight.MODE_SOFTWARE)
    monkeypatch.setattr(gpu_preflight, "_run_probe", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not probe")))

    assert gpu_preflight.ensure_gpu_ready() == gpu_preflight.MODE_SOFTWARE


def test_single_gpu_default_probe_succeeds_no_reexec(monkeypatch):
    """Intel-only / AMD-only machine: default GL just works. Must conclude
    on the first probe, with zero re-execs and no NVIDIA-specific env vars
    ever touched."""
    monkeypatch.setattr(gpu_preflight, "detect_gpu_descriptions", lambda: ["Intel Corporation UHD Graphics"])
    monkeypatch.setattr(gpu_preflight, "_run_probe", lambda env, label: True)
    reexec_calls = []
    monkeypatch.setattr(gpu_preflight, "_reexec", lambda mode: reexec_calls.append(mode))

    mode = gpu_preflight.ensure_gpu_ready()

    assert mode == gpu_preflight.MODE_DEFAULT
    assert reexec_calls == []
    assert gpu_cache.read_cached_mode("Intel Corporation UHD Graphics") == gpu_preflight.MODE_DEFAULT


def test_single_gpu_default_probe_fails_skips_straight_to_software(monkeypatch):
    """A single-GPU machine where the default probe fails must never try
    NVIDIA PRIME env vars (nothing to offload to) - it should go straight
    to the software fallback."""
    monkeypatch.setattr(gpu_preflight, "detect_gpu_descriptions", lambda: ["Intel Corporation UHD Graphics"])

    probed_envs = []

    def _fake_probe(env, label):
        probed_envs.append(label)
        return False

    monkeypatch.setattr(gpu_preflight, "_run_probe", _fake_probe)
    reexec_calls = []
    monkeypatch.setattr(gpu_preflight, "_reexec", lambda mode: reexec_calls.append(mode))

    mode = gpu_preflight.ensure_gpu_ready()

    assert mode == gpu_preflight.MODE_SOFTWARE
    assert gpu_preflight.MODE_NVIDIA_PRIME not in probed_envs
    assert reexec_calls == [gpu_preflight.MODE_SOFTWARE]


def test_hybrid_nvidia_default_fails_prime_offload_succeeds(monkeypatch):
    """The reported bug's exact scenario: hybrid Intel/NVIDIA laptop,
    default GL path fails under confinement, NVIDIA PRIME render-offload
    fixes it. Must re-exec into nvidia_prime, not software."""
    gpus = [
        "Intel Corporation UHD Graphics",
        "NVIDIA Corporation TU104M [GeForce RTX 2070 Mobile]",
    ]
    monkeypatch.setattr(gpu_preflight, "detect_gpu_descriptions", lambda: gpus)

    def _fake_probe(env, label):
        return label == gpu_preflight.MODE_NVIDIA_PRIME

    monkeypatch.setattr(gpu_preflight, "_run_probe", _fake_probe)
    reexec_calls = []
    monkeypatch.setattr(gpu_preflight, "_reexec", lambda mode: reexec_calls.append(mode))

    mode = gpu_preflight.ensure_gpu_ready()

    assert mode == gpu_preflight.MODE_NVIDIA_PRIME
    assert reexec_calls == [gpu_preflight.MODE_NVIDIA_PRIME]
    assert gpu_cache.read_cached_mode("|".join(sorted(gpus))) == gpu_preflight.MODE_NVIDIA_PRIME


def test_hybrid_nvidia_both_tiers_fail_falls_back_to_software(monkeypatch):
    gpus = ["Intel Corporation UHD Graphics", "NVIDIA Corporation TU104M"]
    monkeypatch.setattr(gpu_preflight, "detect_gpu_descriptions", lambda: gpus)
    monkeypatch.setattr(gpu_preflight, "_run_probe", lambda env, label: False)
    reexec_calls = []
    monkeypatch.setattr(gpu_preflight, "_reexec", lambda mode: reexec_calls.append(mode))

    mode = gpu_preflight.ensure_gpu_ready()

    assert mode == gpu_preflight.MODE_SOFTWARE
    assert reexec_calls == [gpu_preflight.MODE_SOFTWARE]


def test_cached_mode_is_used_without_reprobing(monkeypatch):
    gpus = ["Intel Corporation UHD Graphics"]
    monkeypatch.setattr(gpu_preflight, "detect_gpu_descriptions", lambda: gpus)
    gpu_cache.write_cache("Intel Corporation UHD Graphics", gpu_preflight.MODE_DEFAULT)
    monkeypatch.setattr(gpu_preflight, "_run_probe", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not probe")))

    assert gpu_preflight.ensure_gpu_ready() == gpu_preflight.MODE_DEFAULT


def test_cached_non_default_mode_reexecs_without_reprobing(monkeypatch):
    gpus = ["Intel Corporation UHD Graphics", "NVIDIA Corporation TU104M"]
    fingerprint = "|".join(sorted(gpus))
    monkeypatch.setattr(gpu_preflight, "detect_gpu_descriptions", lambda: gpus)
    gpu_cache.write_cache(fingerprint, gpu_preflight.MODE_NVIDIA_PRIME)
    monkeypatch.setattr(gpu_preflight, "_run_probe", lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not probe")))
    reexec_calls = []
    monkeypatch.setattr(gpu_preflight, "_reexec", lambda mode: reexec_calls.append(mode))

    mode = gpu_preflight.ensure_gpu_ready()

    assert mode == gpu_preflight.MODE_NVIDIA_PRIME
    assert reexec_calls == [gpu_preflight.MODE_NVIDIA_PRIME]


# --- env building / re-exec -----------------------------------------------

def test_env_for_mode_nvidia_prime_sets_expected_vars():
    env = gpu_preflight._env_for_mode(gpu_preflight.MODE_NVIDIA_PRIME)
    assert env["__NV_PRIME_RENDER_OFFLOAD"] == "1"
    assert env["__GLX_VENDOR_LIBRARY_NAME"] == "nvidia"


def test_env_for_mode_software_sets_libgl_var():
    env = gpu_preflight._env_for_mode(gpu_preflight.MODE_SOFTWARE)
    assert env["LIBGL_ALWAYS_SOFTWARE"] == "1"


def test_env_for_mode_default_does_not_add_gpu_vars():
    env = gpu_preflight._env_for_mode(gpu_preflight.MODE_DEFAULT)
    assert "__NV_PRIME_RENDER_OFFLOAD" not in env
    assert "LIBGL_ALWAYS_SOFTWARE" not in env


def test_reexec_calls_execve_with_mode_marker(monkeypatch):
    calls = []
    monkeypatch.setattr(gpu_preflight.os, "execve", lambda path, argv, env: calls.append((path, argv, env)))

    gpu_preflight._reexec(gpu_preflight.MODE_SOFTWARE)

    assert len(calls) == 1
    _, _, env = calls[0]
    assert env[gpu_preflight._MODE_ENV_VAR] == gpu_preflight.MODE_SOFTWARE
    assert env["LIBGL_ALWAYS_SOFTWARE"] == "1"


def test_reexec_failure_does_not_raise(monkeypatch):
    def _raise(*a, **kw):
        raise OSError("execve not permitted")

    monkeypatch.setattr(gpu_preflight.os, "execve", _raise)

    # Must not propagate - a failed re-exec should degrade, not crash the app.
    gpu_preflight._reexec(gpu_preflight.MODE_SOFTWARE)


# --- probe subprocess plumbing ---------------------------------------------

def test_run_probe_false_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(
        gpu_preflight.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 1, stdout="", stderr="context creation failed"),
    )
    assert gpu_preflight._run_probe({}, "default") is False


def test_run_probe_false_on_timeout(monkeypatch):
    def _raise(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="probe", timeout=8)

    monkeypatch.setattr(gpu_preflight.subprocess, "run", _raise)
    assert gpu_preflight._run_probe({}, "default") is False


def test_run_probe_true_on_zero_exit(monkeypatch):
    monkeypatch.setattr(
        gpu_preflight.subprocess, "run",
        lambda *a, **kw: subprocess.CompletedProcess(a, 0, stdout="", stderr=""),
    )
    assert gpu_preflight._run_probe({}, "default") is True
