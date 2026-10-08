import os
import sys

from PySide6 import QtGui, QtWidgets

from app.core.gpu_preflight import MODE_SOFTWARE, PROBE_ENV_VAR, ensure_gpu_ready


def main():
    if os.environ.get(PROBE_ENV_VAR) == "1":
        # Started as the GPU probe subprocess (frozen builds re-run this same
        # executable): only test GL and exit, never start the application.
        from app.core.gpu_probe import probe
        sys.exit(0 if probe() else 1)

    # Must run before QApplication is constructed so a broken default GL
    # path can be routed around instead of leaving a blank/crashed window.
    # May re-exec this process with adjusted env vars, in which case this
    # call never returns.
    gpu_mode = ensure_gpu_ready()

    # Proper WM_CLASS on Linux for dock icon matching
    QtWidgets.QApplication.setDesktopFileName("edgepowermeter")
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("EdgePowerMeter")
    app.setOrganizationName("EdgePowerMeter")
    app.setStyle("Fusion")  # consistent base for the stylesheet on every platform
    QtGui.QIcon.setThemeName("")

    from app.ui.main_window import MainWindow  # heavy imports after the preflight
    win = MainWindow(gpu_mode)
    if gpu_mode == MODE_SOFTWARE:
        win.show_software_rendering_banner()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
