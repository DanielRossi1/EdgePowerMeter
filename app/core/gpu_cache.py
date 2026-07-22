"""Persistent cache for the winning GPU rendering mode.

Split out of gpu_preflight.py to keep that module under its line budget.
Uses QSettings (the same store AppSettings already uses) so the config
path is discovered through Qt's standard per-platform API rather than
hardcoded, and stays consistent with the rest of the app.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSettings

_ORG, _APP = "EdgePowerMeter", "EdgePowerMeter"
_SETTINGS_GROUP = "gpu_preflight"


def _settings() -> QSettings:
    return QSettings(_ORG, _APP)


def read_cached_mode(fingerprint: str) -> Optional[str]:
    """Return the cached mode if it was cached for this exact GPU
    fingerprint; None otherwise (cache miss or hardware changed)."""
    try:
        s = _settings()
        s.beginGroup(_SETTINGS_GROUP)
        has_mode = s.contains("mode")
        cached_fp = s.value("fingerprint", "") if has_mode else ""
        cached_mode = s.value("mode", "") if has_mode else None
        s.endGroup()
        if has_mode and str(cached_fp) == fingerprint:
            return str(cached_mode)
    except Exception:
        pass
    return None


def write_cache(fingerprint: str, mode: str) -> None:
    try:
        s = _settings()
        s.beginGroup(_SETTINGS_GROUP)
        s.setValue("fingerprint", fingerprint)
        s.setValue("mode", mode)
        s.endGroup()
        s.sync()
    except Exception:
        pass


def clear_cache() -> None:
    """Force a fresh probe on next launch. Exposed for a Settings action."""
    try:
        s = _settings()
        s.beginGroup(_SETTINGS_GROUP)
        s.remove("")
        s.endGroup()
        s.sync()
    except Exception:
        pass
