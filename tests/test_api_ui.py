"""UI side of the automation API: pages must follow settings changed through the API,
and the API port spinbox must not restart the server on every step."""
from __future__ import annotations

import pytest

pytest.importorskip("PySide6.QtWidgets")

from app.i18n import set_language


@pytest.fixture
def window(qapp, monkeypatch):
    import app.ui.main_window as mw
    from app.core import AppSettings

    def load():
        s = AppSettings()
        s.use_opengl = False      # offscreen platform has no QOpenGLWidget
        s.confirm_discard = False
        return s

    monkeypatch.setattr(mw.AppSettings, "load", staticmethod(load))
    monkeypatch.setattr(mw.AppSettings, "save", lambda self: None)   # keep the real config untouched
    win = mw.MainWindow()
    win.resize(1400, 900)
    win.show()
    qapp.processEvents()
    yield win
    win.ctrl.has_unsaved_data = False
    win.close()
    set_language("en")


def test_api_change_refreshes_device_calibration(window, qapp):
    s = window.settings
    s.shunt_ohm = 0.050                       # as update_settings({"shunt_ohm": ...}) does
    s.current_gain = 1.02
    window._on_api_settings_changed()
    qapp.processEvents()
    assert window.device.shunt.value() == pytest.approx(0.050)
    assert window.device.i_gain.value() == pytest.approx(1.02)

    # A later edit of another calibration spin must not write the old shunt back.
    window.device.v_gain.setValue(1.01)
    assert s.voltage_gain == pytest.approx(1.01)
    assert s.shunt_ohm == pytest.approx(0.050)
    assert s.current_gain == pytest.approx(1.02)


def test_api_change_refreshes_spectrum_signal(window, qapp):
    window.settings.spectrum_signal = "power"
    window._on_api_settings_changed()
    qapp.processEvents()
    assert window.analysis.spec_signal.currentData() == "power"


def test_api_port_applies_on_finish_not_per_step(window, qapp, monkeypatch):
    calls = []
    monkeypatch.setattr(window, "_apply_api", lambda: calls.append(window.settings.api_port))
    port_spin = window.settings_page._bound["api_port"]
    start = window.settings.api_port

    port_spin.setValue(start + 1)
    port_spin.stepUp()
    qapp.processEvents()
    assert window.settings.api_port == start      # steps do not change the setting yet
    assert calls == []                            # and do not restart the server

    port_spin.editingFinished.emit()              # Enter or focus-out
    assert window.settings.api_port == start + 2
    assert len(calls) == 1
