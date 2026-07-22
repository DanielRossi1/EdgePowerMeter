import sys

from PySide6 import QtWidgets, QtGui

from app.core.gpu_preflight import MODE_SOFTWARE, ensure_gpu_ready
from app.ui.main_window import MainWindow


def main():
    # Must run before QApplication is constructed so a broken default GL
    # path can be routed around instead of leaving a blank/crashed window.
    # May re-exec this process with adjusted env vars, in which case this
    # call never returns.
    gpu_mode = ensure_gpu_ready()

    # Set application metadata before creating QApplication
    # This ensures proper WM_CLASS on Linux for dock icon matching
    QtWidgets.QApplication.setDesktopFileName("edgepowermeter")

    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("EdgePowerMeter")
    app.setOrganizationName("EdgePowerMeter")

    QtGui.QIcon.setThemeName('')
    win = MainWindow()
    if gpu_mode == MODE_SOFTWARE:
        win.show_software_rendering_banner()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
