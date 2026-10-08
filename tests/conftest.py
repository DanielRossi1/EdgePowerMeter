"""Shared pytest fixtures. Forces headless Qt so GUI tests run without a display."""
from __future__ import annotations

import os
import tempfile

# Must be set before any Qt import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(scope="session", autouse=True)
def _isolated_qsettings():
    """Keep tests away from the user's real settings (and vice versa)."""
    try:
        from PySide6.QtCore import QSettings
    except ImportError:
        yield
        return
    with tempfile.TemporaryDirectory() as tmp:
        QSettings.setDefaultFormat(QSettings.IniFormat)
        QSettings.setPath(QSettings.IniFormat, QSettings.UserScope, tmp)
        QSettings.setPath(QSettings.NativeFormat, QSettings.UserScope, tmp)
        yield


@pytest.fixture(autouse=True)
def _isolated_recordings(tmp_path, monkeypatch):
    """Live sessions autosave recordings: never into the user's Documents."""
    from app.export import recorder
    folder = tmp_path / "recordings"
    monkeypatch.setattr(recorder, "default_folder", lambda: folder)
    yield folder


@pytest.fixture(scope="session")
def qapp():
    """A single QApplication for the whole test session."""
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app
