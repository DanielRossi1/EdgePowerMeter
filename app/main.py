import sys
from PySide6 import QtWidgets, QtGui

from app.ui.main_window import MainWindow


def main():
    # Set application metadata before creating QApplication
    # This ensures proper WM_CLASS on Linux for dock icon matching
    QtWidgets.QApplication.setDesktopFileName("edgepowermeter")
    
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName("EdgePowerMeter")
    app.setOrganizationName("EdgePowerMeter")
    
    QtGui.QIcon.setThemeName('')
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
