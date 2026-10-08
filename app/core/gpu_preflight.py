"""GPU rendering preflight: detect a broken GL path before QApplication
exists, and steer around it instead of leaving a blank or crashed window.

Call ensure_gpu_ready() as the very first thing in app.main.main(),
before constructing QApplication. Three tiers, cheapest first:
  1. MODE_DEFAULT - the current environment as-is; works on most systems.
  2. MODE_NVIDIA_PRIME - render-offload env vars for hybrid Intel/NVIDIA
     laptops, where the default GLVND vendor selection can pick a driver
     that fails to load under sandboxing (seen under snap strict
     confinement).
  3. MODE_SOFTWARE - LIBGL_ALWAYS_SOFTWARE, a last resort for any GPU
     setup; the caller must show a persistent "software rendering"
     banner since 3D performance will be degraded.

Each tier is verified by actually creating a GL context in an isolated
subprocess (gpu_probe.py) so a segfaulting driver can't take the main
process down with it. The winning tier is cached (gpu_cache.py) so
ordinary startups don't pay the probe/re-exec cost every time, and the
cache auto-invalidates itself if the detected GPU hardware changes.
"""
from __future__ import annotations

import os
import subprocess
import sys
from typing import Dict

from . import gpu_cache
from .gpu_detect import detect_gpu_descriptions

_MODE_ENV_VAR = "EPM_GPU_MODE"
_PROBE_TIMEOUT_S = 8

MODE_DEFAULT = "default"
MODE_NVIDIA_PRIME = "nvidia_prime"
MODE_SOFTWARE = "software"

clear_cache = gpu_cache.clear_cache


def _env_for_mode(mode: str) -> Dict[str, str]:
    env = dict(os.environ)
    if mode == MODE_NVIDIA_PRIME:
        env["__NV_PRIME_RENDER_OFFLOAD"] = "1"
        env["__GLX_VENDOR_LIBRARY_NAME"] = "nvidia"
    elif mode == MODE_SOFTWARE:
        env["LIBGL_ALWAYS_SOFTWARE"] = "1"
    return env


# Set in the probe subprocess's environment. app.main checks it before doing
# anything else, so a probe can never start the full application.
PROBE_ENV_VAR = "EPM_GPU_PROBE"


def _frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def _probe_command() -> list:
    # In a PyInstaller build sys.executable is the application itself and
    # "-m" means nothing to it: without the env-var guard in app.main every
    # probe started another full app, which probed again (runaway processes,
    # and the probe always "failed" -> software rendering cached).
    if _frozen():
        return [sys.executable]
    return [sys.executable, "-m", "app.core.gpu_probe"]


def _run_probe(env: Dict[str, str], label: str) -> bool:
    env = dict(env)
    env[PROBE_ENV_VAR] = "1"
    try:
        result = subprocess.run(
            _probe_command(),
            env=env, timeout=_PROBE_TIMEOUT_S, capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"[WARNING] GPU probe failed ({label}): exit={result.returncode} "
                  f"stderr={result.stderr.strip()[:300]}")
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print(f"[WARNING] GPU probe timed out ({label}) after {_PROBE_TIMEOUT_S}s")
        return False
    except Exception as e:
        print(f"[WARNING] GPU probe could not be launched ({label}): {e}")
        return False


def _reexec(mode: str) -> None:
    env = _env_for_mode(mode)
    env[_MODE_ENV_VAR] = mode
    try:
        # Frozen builds: argv[0] is the executable itself, do not pass it twice.
        # Otherwise always restart as a module: with "-m app.main" sys.argv[0]
        # is the path of app/main.py, and running that file as a script puts
        # app/ on sys.path, where app/serial shadows pyserial's "serial".
        if _frozen():
            argv = [sys.executable] + sys.argv[1:]
        else:
            argv = [sys.executable, "-m", "app.main"] + sys.argv[1:]
        os.execve(sys.executable, argv, env)
    except OSError as e:
        # Can't replace the process image - apply what we can in-place and
        # keep going rather than crashing. The GL env vars may arrive too
        # late to help, but the caller-side software-rendering banner (for
        # MODE_SOFTWARE) still fires since the mode is returned regardless.
        print(f"[WARNING] GPU preflight re-exec into mode={mode} failed ({e}); continuing in-process")
        os.environ.update(env)


def ensure_gpu_ready() -> str:
    """Pick a working GL rendering mode and, if needed, re-exec into it.

    Must be called before QApplication is constructed. Returns the mode
    now in effect (MODE_DEFAULT / MODE_NVIDIA_PRIME / MODE_SOFTWARE);
    the caller should show a persistent banner when it's MODE_SOFTWARE.
    """
    already_decided = os.environ.get(_MODE_ENV_VAR)
    if already_decided:
        return already_decided

    gpus = detect_gpu_descriptions()
    fingerprint = "|".join(gpus)

    cached = gpu_cache.read_cached_mode(fingerprint)
    if cached:
        if cached != MODE_DEFAULT:
            _reexec(cached)
        return cached

    if _run_probe(os.environ.copy(), MODE_DEFAULT):
        gpu_cache.write_cache(fingerprint, MODE_DEFAULT)
        return MODE_DEFAULT

    is_hybrid_nvidia = len(gpus) > 1 and any("nvidia" in g.lower() for g in gpus)
    if is_hybrid_nvidia and _run_probe(_env_for_mode(MODE_NVIDIA_PRIME), MODE_NVIDIA_PRIME):
        gpu_cache.write_cache(fingerprint, MODE_NVIDIA_PRIME)
        _reexec(MODE_NVIDIA_PRIME)
        return MODE_NVIDIA_PRIME  # pragma: no cover - _reexec normally replaces the process

    gpu_cache.write_cache(fingerprint, MODE_SOFTWARE)
    _reexec(MODE_SOFTWARE)
    return MODE_SOFTWARE  # pragma: no cover - _reexec normally replaces the process
