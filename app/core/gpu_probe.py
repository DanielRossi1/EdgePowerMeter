"""Minimal GPU rendering probe, run as an isolated subprocess.

Invoked as `python -m app.core.gpu_probe` by app.core.gpu_preflight,
before the main application creates its QApplication. Actually attempts
to create a GL context through the real platform plugin (not the
"offscreen" QPA platform, which would trivially succeed and prove
nothing about the real windowing GL path) so a broken driver stack -
including a broken swrast software fallback, as seen under snap strict
confinement on hybrid Intel/NVIDIA laptops - is caught here instead of
inside the real application window.

Exits 0 on a working GL context, 1 on any failure. Never lets an
exception (or a driver crash) escape into the caller's process: the
caller treats any non-zero exit code, or the subprocess dying/timing
out, as "this environment doesn't work".
"""
from __future__ import annotations

import sys


def probe() -> bool:
    try:
        from PySide6.QtGui import QGuiApplication, QOffscreenSurface, QOpenGLContext

        app = QGuiApplication.instance() or QGuiApplication([sys.argv[0]])
        surface = QOffscreenSurface()
        surface.create()
        if not surface.isValid():
            return False
        ctx = QOpenGLContext()
        if not ctx.create():
            return False
        return bool(ctx.makeCurrent(surface))
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(0 if probe() else 1)
