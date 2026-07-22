"""Shared pytest fixtures. Forces headless Qt so GUI tests run without a display."""
from __future__ import annotations

import os

# Must be set before any Qt import.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest


@pytest.fixture(scope="session")
def qapp():
    """A single QApplication for the whole test session."""
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    yield app
