"""About page: version, system information and help."""

from __future__ import annotations

import platform

from PySide6 import QtCore, QtWidgets

from ...i18n import get_language, tr
from ...version import APP_NAME, LICENSE, URL, __version__
from ..icons import icon
from ..widgets.common import Card, KeyValueList, button, label, scroll_page


class AboutPage(QtWidgets.QWidget):
    def __init__(self, gpu_mode: str, theme, parent=None):
        super().__init__(parent)
        body = QtWidgets.QWidget()
        col = QtWidgets.QVBoxLayout(body)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(14)

        hero = Card()
        head = QtWidgets.QHBoxLayout()
        head.setSpacing(10)
        logo = QtWidgets.QLabel()
        logo.setPixmap(icon("bolt", theme.accent, 32).pixmap(32, 32))
        head.addWidget(logo)
        title = label(APP_NAME, "PageTitle")
        title.setStyleSheet(f"font-size: 24px; color: {theme.accent};")
        head.addWidget(title)
        head.addStretch()
        hero.body.addLayout(head)
        hero.body.addWidget(label(tr("Version {version}", version=__version__), "Muted"))
        hero.body.addWidget(label(tr(
            "Precision power monitor for embedded and edge-AI workloads: INA226 sensor, "
            "DS3231 real-time clock, ESP32-C3 firmware and this desktop application."), wrap=True))
        links = label(f'<a href="{URL}">{URL}</a>  ·  {tr("License")}: {LICENSE}')
        links.setOpenExternalLinks(True)
        links.setTextInteractionFlags(QtCore.Qt.TextBrowserInteraction)
        hero.body.addWidget(links)
        col.addWidget(hero)

        help_card = Card(tr("Quick guide"))
        steps = [
            tr("Connect the meter over USB, choose its port in the top bar and press Start."),
            tr("Live: drag the plots to look back, use the mouse wheel to change the time window, "
               "double-click to return to live."),
            tr("Analysis: select a range to get statistics, energy, supply quality and spectrum; "
               "export it as CSV or PDF."),
            tr("Device: configure averaging and conversion times, synchronize the clock and "
               "calibrate offset and gain."),
            tr("Keyboard: Ctrl+R start/stop, Ctrl+O import, Ctrl+E export CSV, Ctrl+P export PDF, "
               "Ctrl+1...5 switch page."),
        ]
        help_card.body.addWidget(label("<br>".join(f"{i}. {s}" for i, s in enumerate(steps, 1)), wrap=True))
        col.addWidget(help_card)

        sysc = Card(tr("System"))
        import numpy
        import pyqtgraph
        import PySide6
        info = KeyValueList([
            ("python", "Python"), ("qt", "Qt / PySide6"), ("pg", "pyqtgraph"), ("np", "numpy"),
            ("os", tr("Operating system")), ("render", tr("Rendering")), ("lang", tr("Language")),
        ])
        info.set("python", platform.python_version())
        info.set("qt", f"{QtCore.qVersion()} / {PySide6.__version__}")
        info.set("pg", pyqtgraph.__version__)
        info.set("np", numpy.__version__)
        info.set("os", f"{platform.system()} {platform.release()} ({platform.machine()})")
        info.set("render", {"default": tr("GPU (default)"), "nvidia_prime": tr("GPU (NVIDIA offload)"),
                            "software": tr("Software")}.get(gpu_mode, gpu_mode or "-"))
        info.set("lang", get_language())
        sysc.body.addWidget(info)
        row = QtWidgets.QHBoxLayout()
        redetect = button(tr("Re-detect graphics on next launch"), "flat")
        status = label("", "Hint")

        def do_redetect():
            from ...core.gpu_preflight import clear_cache
            clear_cache()
            status.setText(tr("Done: graphics will be probed again at the next start."))

        redetect.clicked.connect(do_redetect)
        row.addWidget(redetect)
        row.addWidget(status, 1)
        sysc.body.addLayout(row)
        col.addWidget(sysc)
        col.addStretch()

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(scroll_page(body, 820))
