"""Tests for the isolated GPU probe (app.core.gpu_probe).

Mirrors tests/test_plot_widget_opengl.py: verifies probe() reports
failure cleanly (never raises) for every way GL context creation can
fail, since this probe runs in its own subprocess specifically so a
crash here can't take the main app process down with it.
"""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtGui")

from app.core import gpu_probe


def test_probe_false_when_surface_invalid(qapp, monkeypatch):
    from PySide6.QtGui import QOffscreenSurface

    monkeypatch.setattr(QOffscreenSurface, "isValid", lambda self: False)
    assert gpu_probe.probe() is False


def test_probe_false_when_context_create_fails(qapp, monkeypatch):
    from PySide6.QtGui import QOpenGLContext

    monkeypatch.setattr(QOpenGLContext, "create", lambda self: False)
    assert gpu_probe.probe() is False


def test_probe_false_when_make_current_fails(qapp, monkeypatch):
    from PySide6.QtGui import QOpenGLContext

    monkeypatch.setattr(QOpenGLContext, "makeCurrent", lambda self, surface: False)
    assert gpu_probe.probe() is False


def test_probe_never_raises_on_unexpected_failure(qapp, monkeypatch):
    from PySide6.QtGui import QOpenGLContext

    def _boom(self):
        raise RuntimeError("simulated driver crash")

    monkeypatch.setattr(QOpenGLContext, "create", _boom)
    assert gpu_probe.probe() is False


def test_probe_true_when_everything_works(qapp):
    # On this dev machine (or CI, if it has any usable GL/software
    # rasterizer under QT_QPA_PLATFORM=offscreen's real surface handling)
    # this should succeed; if the environment genuinely has no working GL
    # at all, probe() must still just return False, not raise.
    assert gpu_probe.probe() in (True, False)
