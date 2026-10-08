"""Inline SVG icons (stroke style), tinted at runtime to match the theme."""

from __future__ import annotations

from functools import lru_cache

from PySide6 import QtCore, QtGui

_PATHS = {
    # live signal / oscilloscope
    "live": '<path d="M3 12h4l2-6 4 12 2-6h6"/>',
    # bar chart with selection
    "analysis": '<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',
    # microchip
    "device": ('<rect x="7" y="7" width="10" height="10" rx="1.5"/>'
               '<path d="M10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M3 14h4M17 10h4M17 14h4"/>'),
    # sliders
    "settings": ('<path d="M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0"/>'
                 '<circle cx="16" cy="6" r="2"/><circle cx="10" cy="12" r="2"/>'
                 '<circle cx="18" cy="18" r="2"/>'),
    "info": '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7.5v.5"/>',
    "play": '<path d="M7 5l12 7-12 7z"/>',
    "stop": '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    "refresh": '<path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/>',
    "trash": '<path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/>',
    "import": '<path d="M12 3v12M7 10l5 5 5-5M4 20h16"/>',
    "export": '<path d="M12 15V3M7 8l5-5 5 5M4 20h16"/>',
    "pdf": ('<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4M9 13h6M9 17h6"/>'),
    "follow": '<path d="M5 12h12M13 6l6 6-6 6"/>',
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/>',
    "zero": '<circle cx="12" cy="12" r="8"/><path d="M6 18L18 6"/>',
    "flag": '<path d="M5 21V4M5 4h11l-2 4 2 4H5"/>',
    "bolt": '<path d="M13 2L4 14h7l-1 8 9-12h-7z"/>',
    "close": '<path d="M6 6l12 12M18 6L6 18"/>',
}


@lru_cache(maxsize=256)
def icon(name: str, color: str, size: int = 22) -> QtGui.QIcon:
    """Render the named icon in `color`."""
    from PySide6 import QtSvg

    body = _PATHS.get(name, _PATHS["info"])
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" '
           f'stroke="{color}" stroke-width="1.8" stroke-linecap="round" '
           f'stroke-linejoin="round">{body}</svg>')
    renderer = QtSvg.QSvgRenderer(QtCore.QByteArray(svg.encode()))
    result = QtGui.QIcon()
    for scale in (1, 2):
        pm = QtGui.QPixmap(size * scale, size * scale)
        pm.fill(QtCore.Qt.transparent)
        painter = QtGui.QPainter(pm)
        renderer.render(painter)
        painter.end()
        pm.setDevicePixelRatio(scale)
        result.addPixmap(pm)
    return result
