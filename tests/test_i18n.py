"""Tests for the runtime translation module (app.i18n)."""
from __future__ import annotations

import json

import pytest

import app.i18n as i18n


@pytest.fixture
def catalog_dir(tmp_path, monkeypatch):
    """Point the runtime at a temporary locales dir with an Italian catalog."""
    (tmp_path / "it.json").write_text(json.dumps({
        "Start": "Avvia",
        "{n} samples": "{n} campioni",
        "Broken {n}": "Rotto {missing}",
    }), encoding="utf-8")
    monkeypatch.setattr(i18n, "_LOCALES_DIR", tmp_path)
    monkeypatch.setattr(i18n, "_cache", {})
    yield tmp_path
    i18n.set_language("en")


def test_lookup_and_english_fallback(catalog_dir):
    assert i18n.set_language("it") == "it"
    assert i18n.tr("Start") == "Avvia"
    assert i18n.tr("Not translated") == "Not translated"


def test_format_kwargs_applied_after_lookup(catalog_dir):
    i18n.set_language("it")
    assert i18n.tr("{n} samples", n=5) == "5 campioni"
    assert i18n.tr("{x} untranslated", x=1) == "1 untranslated"


def test_broken_translation_placeholder_falls_back_to_english(catalog_dir):
    i18n.set_language("it")
    assert i18n.tr("Broken {n}", n=3) == "Broken 3"


def test_english_is_identity(catalog_dir):
    i18n.set_language("en")
    assert i18n.tr("Start") == "Start"


def test_missing_catalog_file_is_harmless(catalog_dir):
    assert i18n.set_language("de") == "de"
    assert i18n.tr("Start") == "Start"


def test_unknown_code_and_region_normalization(catalog_dir):
    assert i18n.set_language("xx") == "en"
    assert i18n.set_language("it_IT.UTF-8") == "it"
    assert i18n.set_language("de-DE") == "de"


def test_auto_detection_uses_environment(catalog_dir, monkeypatch):
    monkeypatch.setattr(i18n, "detect_system_language", lambda: "it")
    assert i18n.set_language("auto") == "it"
    assert i18n.tr("Start") == "Avvia"


def test_detect_falls_back_to_english_for_unsupported_locale(monkeypatch):
    import locale
    import sys

    # Hide Qt so only locale/env are consulted.
    monkeypatch.setitem(sys.modules, "PySide6.QtCore", None)
    monkeypatch.setattr(locale, "getlocale", lambda: ("ja_JP", "UTF-8"))
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "ja_JP.UTF-8")
    assert i18n.detect_system_language() == "en"

    monkeypatch.setenv("LANGUAGE", "fr_FR:en")
    assert i18n.detect_system_language() == "fr"


def test_languages_and_marker():
    assert set(i18n.available_languages()) == {"en", "it", "es", "fr", "de"}
    assert i18n.N_("Voltage") == "Voltage"
