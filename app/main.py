import sys
from PySide6 import QtWidgets, QtGui

from app.ui.main_window import MainWindow


def main():
    app = QtWidgets.QApplication(sys.argv)
    QtGui.QIcon.setThemeName('')
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
