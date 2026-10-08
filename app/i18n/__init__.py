"""Lightweight runtime translation for EdgePowerMeter.

English source strings are the lookup keys::

    from app.i18n import tr
    label = tr("Start")
    msg = tr("No data received for {seconds}s", seconds=5)

Catalogs are flat JSON maps ``{english: translated}`` stored in
``app/i18n/locales/<code>.json``. They are generated offline by
``tools/i18n/translate.py`` (local machine translation + manual overrides),
so the runtime only reads JSON and has no heavy dependencies.

Strings that must be declared before a language is chosen (e.g. module-level
tables) are marked with ``N_("...")`` so the extractor finds them, and are
translated later with ``tr(value)``.
"""

from __future__ import annotations

import json
import locale
import os
from pathlib import Path
from typing import Dict, Optional

__all__ = [
    "LANGUAGES",
    "DEFAULT_LANGUAGE",
    "N_",
    "tr",
    "set_language",
    "get_language",
    "available_languages",
    "detect_system_language",
]

# Native language names, shown as-is in the language selector.
LANGUAGES: Dict[str, str] = {
    "en": "English",
    "it": "Italiano",
    "es": "Español",
    "fr": "Français",
    "de": "Deutsch",
}

DEFAULT_LANGUAGE = "en"

# Resolved relative to this file so it also works inside a PyInstaller bundle
# (as long as app/i18n/locales is shipped as data).
_LOCALES_DIR = Path(__file__).parent / "locales"

_current_language = DEFAULT_LANGUAGE
_catalog: Dict[str, str] = {}
_cache: Dict[str, Dict[str, str]] = {}


def N_(text: str) -> str:
    """Mark a string for extraction without translating it yet."""
    return text


def _normalize(code: Optional[str]) -> Optional[str]:
    """Map a locale name such as 'it_IT.UTF-8' or 'de-DE' to a supported code."""
    if not code:
        return None
    base = code.replace("-", "_").split(".")[0].split("_")[0].lower()
    return base if base in LANGUAGES else None


def detect_system_language() -> str:
    """Best-effort OS language detection; falls back to English."""
    candidates = []
    try:
        from PySide6.QtCore import QLocale  # Optional: only if Qt is installed
        candidates.append(QLocale.system().name())
    except Exception:
        pass
    try:
        candidates.append(locale.getlocale()[0])
    except Exception:
        pass
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        value = os.environ.get(var)
        if value:
            candidates.extend(value.split(":"))
    for candidate in candidates:
        code = _normalize(candidate)
        if code:
            return code
    return DEFAULT_LANGUAGE


def _load_catalog(code: str) -> Dict[str, str]:
    if code == DEFAULT_LANGUAGE:
        return {}
    if code not in _cache:
        path = _LOCALES_DIR / f"{code}.json"
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            _cache[code] = {str(k): str(v) for k, v in data.items() if v}
        except (OSError, ValueError):
            _cache[code] = {}
    return _cache[code]


def set_language(code: Optional[str]) -> str:
    """Activate a language ('auto' or None detects it). Returns the code in effect."""
    global _current_language, _catalog
    if not code or code == "auto":
        resolved = detect_system_language()
    else:
        resolved = _normalize(code) or DEFAULT_LANGUAGE
    _current_language = resolved
    _catalog = _load_catalog(resolved)
    return resolved


def get_language() -> str:
    """Code of the active language."""
    return _current_language


def available_languages() -> Dict[str, str]:
    """Supported languages as {code: native name}."""
    return dict(LANGUAGES)


def tr(text: str, **kwargs) -> str:
    """Translate an English source string, then apply str.format(**kwargs).

    Missing translations, or translations whose placeholders don't format,
    fall back to the English text.
    """
    translated = _catalog.get(text, text)
    if not kwargs:
        return translated
    try:
        return translated.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError):
            return text
