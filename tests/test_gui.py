"""Headless GUI smoke tests: pages build, data flows into plots, rebuilds keep data."""
from __future__ import annotations

import time

import numpy as np
import pytest

pytest.importorskip("PySide6.QtWidgets")

from app.core import AppSettings, Samples
from app.i18n import set_language


@pytest.fixture
def window(qapp, monkeypatch):
    import app.ui.main_window as mw

    def load():
        s = AppSettings()
        s.use_opengl = False      # offscreen platform has no QOpenGLWidget
        s.confirm_discard = False
        return s

    monkeypatch.setattr(mw.AppSettings, "load", staticmethod(load))
    win = mw.MainWindow()
    win.resize(1400, 900)
    win.show()
    qapp.processEvents()
    yield win
    win.ctrl.has_unsaved_data = False
    win.close()
    set_language("en")


def _feed(win, n=3000):
    t = np.arange(n) / 500.0
    i = 0.3 + 0.1 * np.sin(t * 6)
    win.ctrl._ingest(Samples.from_columns(t, time.time() + t, np.full(n, 5.0), i, 5.0 * i))


def _distinct_colors(widget) -> int:
    img = widget.grab().toImage()
    return len({img.pixelColor(x, y).rgb() for x in range(0, img.width(), 9)
                for y in range(0, img.height(), 9)})


def test_all_pages_build(window, qapp):
    for idx in range(5):
        window._show_page(idx)
        qapp.processEvents()
        assert window.stack.currentIndex() == idx


def test_data_reaches_live_plot_and_metrics(window, qapp):
    _feed(window)
    window._on_frame()
    window._on_metrics()
    qapp.processEvents()
    assert window.live.card_p.value.text() != "-"
    assert _distinct_colors(window.live.plot) > 20      # not a blank scene


def test_plot_clear_data_keeps_scene(window, qapp):
    """Regression: a method named clear() was shadowed by pyqtgraph and wiped the scene."""
    window.live.plot.clear_data()
    qapp.processEvents()
    assert len(window.live.plot._plots) == 3
    assert _distinct_colors(window.live.plot) > 5


def test_analysis_selection_statistics(window, qapp):
    _feed(window)
    window.ctrl.stop()
    window._show_page(1)
    window.analysis.on_data_reset()
    window.analysis.plot.set_region(1.0, 3.0)
    window.analysis._update_selection()
    assert len(window.analysis._selection) == pytest.approx(1001, abs=2)
    assert window.analysis.table.item(2, 2).text().endswith("W")


def test_language_switch_rebuilds_and_keeps_data(window, qapp):
    _feed(window)
    n = len(window.ctrl.store)
    window.settings.language = "it"
    window._on_setting_changed("language")
    qapp.processEvents()
    assert len(window.ctrl.store) == n
    assert window.start_btn.text() == "Avvia"


def test_theme_switch(window, qapp):
    window.settings.theme = "light"
    window._on_setting_changed("theme")
    qapp.processEvents()
    assert window.theme.name == "light"


def test_series_toggle_keeps_one_visible(window, qapp):
    page = window.live
    page.series_buttons["v"].setChecked(False)
    page.series_buttons["i"].setChecked(False)
    page.series_buttons["p"].setChecked(False)      # refused: last one
    assert page.series_buttons["p"].isChecked()
    assert list(page.plot._plots) == ["p"]


def test_dense_curves_use_thin_pens_and_stay_fast(window, qapp):
    """Regression: 1.5 px antialiased pens on dense curves took ~1.6 s per
    repaint and froze the window during acquisition."""
    n = 200_000
    t = np.arange(n) / 893.0
    i = 0.3 + 0.2 * (np.sin(t) > 0) + np.random.default_rng(0).normal(0, 0.003, n)
    window.ctrl._ingest(Samples.from_columns(t, time.time() + t, np.full(n, 5.0), i, 5.0 * i))
    window._show_page(1)
    plot = window.analysis.plot
    plot.set_data(window.ctrl.data())
    plot.show_all()
    assert plot._dense
    assert all(c.opts["pen"].widthF() == 1.0 and not c.opts["antialias"]
               for c in plot._curves.values())
    started = time.perf_counter()
    for _ in range(3):
        plot.refresh()
        plot.repaint()
        qapp.processEvents()
    assert (time.perf_counter() - started) / 3 < 0.25
