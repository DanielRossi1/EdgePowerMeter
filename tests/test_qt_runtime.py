"""Qt binding sanity checks that the rest of the suite cannot see."""
from __future__ import annotations

import sys

import pytest

QtWidgets = pytest.importorskip("PySide6.QtWidgets")


@pytest.mark.skipif(sys.version_info >= (3, 12),
                    reason="None is immortal from Python 3.12: a leak cannot crash")
def test_qt_calls_do_not_drop_references_to_none(qapp):
    """PySide6 6.12.0 released a reference to None on every call returning
    nothing; on Python 3.10/3.11 the app died within seconds of acquisition
    with 'Fatal Python error: none_dealloc'."""
    label = QtWidgets.QLabel()
    label.setText("x")
    before = sys.getrefcount(None)
    for _ in range(200):
        label.setText("x")
        label.update()
    assert sys.getrefcount(None) >= before - 10
