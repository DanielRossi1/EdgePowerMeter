"""OpenGL capability probe tests for PlotWidget.

Regression coverage for a bug where the app trusted `import OpenGL`
(the Python bindings) as proof that GL rendering actually works. On some
sandboxed installs (e.g. snap on a hybrid Intel/NVIDIA laptop) PyOpenGL
imports fine while the system GL driver - hardware *and* the swrast
software fallback - fails to load, which broke Qt's whole backing-store
compositor instead of just falling back to non-GL rendering. The probe
must actually attempt to create a GL context, and must not crash the
caller (or leave pyqtgraph misconfigured) when that fails.
"""
from __future__ import annotations

import sys

import pytest

pytest.importorskip("PySide6.QtWidgets")

from app.ui.widgets import plot_widget as pw


@pytest.fixture(autouse=True)
def _reset_opengl_cache():
    """Each test controls its own cached probe result."""
    original = pw._opengl_checked
    pw._opengl_checked = None
    yield
    pw._opengl_checked = original


def test_opengl_probe_false_when_context_create_fails(qapp, monkeypatch):
    from PySide6.QtGui import QOpenGLContext

    monkeypatch.setattr(QOpenGLContext, "create", lambda self: False)
    assert pw._opengl_actually_works() is False


def test_opengl_probe_false_when_surface_invalid(qapp, monkeypatch):
    from PySide6.QtGui import QOffscreenSurface

    monkeypatch.setattr(QOffscreenSurface, "isValid", lambda self: False)
    assert pw._opengl_actually_works() is False


def test_opengl_probe_false_when_pyopengl_not_importable(qapp, monkeypatch):
    # Setting a module to None in sys.modules makes the import system raise
    # ImportError for it, simulating PyOpenGL not being installed.
    monkeypatch.setitem(sys.modules, "OpenGL", None)
    assert pw._opengl_actually_works() is False


def test_opengl_probe_never_raises_on_unexpected_failure(qapp, monkeypatch):
    from PySide6.QtGui import QOpenGLContext

    def _boom(self):
        raise RuntimeError("simulated driver crash")

    monkeypatch.setattr(QOpenGLContext, "create", _boom)
    assert pw._opengl_actually_works() is False


def test_opengl_available_disables_pyqtgraph_gl_config_on_failure(qapp, monkeypatch):
    from PySide6.QtGui import QOpenGLContext
    import pyqtgraph as pg

    monkeypatch.setattr(QOpenGLContext, "create", lambda self: False)

    captured = {}
    monkeypatch.setattr(
        pg, "setConfigOptions",
        lambda **kw: captured.update(kw),
    )

    assert pw._opengl_available() is False
    assert captured == {"useOpenGL": False, "enableExperimental": False}

    # Cached: a second call must not re-probe or re-apply config.
    captured.clear()
    assert pw._opengl_available() is False
    assert captured == {}
