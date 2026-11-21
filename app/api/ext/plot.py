"""render_plot: a PNG picture of the waveform, rasterized with numpy only.

No Qt and no matplotlib: the headless app has no GUI application for QPainter, so the
image is drawn into a uint8 array, with a 5x7 bitmap font for the labels, and encoded with
zlib and struct. The work runs on copies taken by `service.range_samples`, so the GUI
thread is never held.
"""

from __future__ import annotations

import base64
import math
import struct
import zlib
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ..service import ApiError

MIME_TYPE = "image/png"
MAX_BASE64_CHARS = 4 * 1024 * 1024
MIN_WIDTH = 200
MIN_HEIGHT = 150

WHITE = (255, 255, 255)
GRID = (222, 222, 222)
FRAME = (60, 60, 60)
TEXT = (30, 30, 30)
MARKER = (0, 0, 0)

# quantity -> (sample field, panel title)
QUANTITY_FIELDS = {
    "voltage": ("v", "Voltage [V]"),
    "current": ("i", "Current [A]"),
    "power": ("p", "Power [W]"),
}
COLORS = {
    "voltage": (214, 39, 40),
    "current": (44, 160, 44),
    "power": (31, 119, 180),
}

# 5x7 bitmap font: '#' is an ink pixel. Characters not listed draw as '?'.
_FONT: Dict[str, Tuple[str, ...]] = {
    " ": ("     ",) * 7,
    "0": (" ### ", "#   #", "#  ##", "# # #", "##  #", "#   #", " ### "),
    "1": ("  #  ", " ##  ", "  #  ", "  #  ", "  #  ", "  #  ", " ### "),
    "2": (" ### ", "#   #", "    #", "   # ", "  #  ", " #   ", "#####"),
    "3": ("#####", "   # ", "  #  ", "   # ", "    #", "#   #", " ### "),
    "4": ("   # ", "  ## ", " # # ", "#  # ", "#####", "   # ", "   # "),
    "5": ("#####", "#    ", "#### ", "    #", "    #", "#   #", " ### "),
    "6": ("  ## ", " #   ", "#    ", "#### ", "#   #", "#   #", " ### "),
    "7": ("#####", "    #", "   # ", "  #  ", " #   ", " #   ", " #   "),
    "8": (" ### ", "#   #", "#   #", " ### ", "#   #", "#   #", " ### "),
    "9": (" ### ", "#   #", "#   #", " ####", "    #", "   # ", " ##  "),
    ".": ("     ", "     ", "     ", "     ", "     ", "  ## ", "  ## "),
    "-": ("     ", "     ", "     ", " ### ", "     ", "     ", "     "),
    "+": ("     ", "  #  ", "  #  ", "#####", "  #  ", "  #  ", "     "),
    ":": ("     ", "  ## ", "  ## ", "     ", "  ## ", "  ## ", "     "),
    "[": (" ##  ", " #   ", " #   ", " #   ", " #   ", " #   ", " ##  "),
    "]": ("  ## ", "   # ", "   # ", "   # ", "   # ", "   # ", "  ## "),
    "(": ("   # ", "  #  ", "#    ", "#    ", "#    ", "  #  ", "   # "),
    ")": (" #   ", "   # ", "    #", "    #", "    #", "   # ", " #   "),
    "/": ("    #", "    #", "   # ", "  #  ", " #   ", "#    ", "#    "),
    "%": ("##   ", "##  #", "   # ", "  #  ", " #   ", "#  ##", "   ##"),
    "?": (" ### ", "#   #", "    #", "   # ", "  #  ", "     ", "  #  "),
    "A": (" ### ", "#   #", "#   #", "#####", "#   #", "#   #", "#   #"),
    "C": (" ### ", "#   #", "#    ", "#    ", "#    ", "#   #", " ### "),
    "D": ("#### ", "#   #", "#   #", "#   #", "#   #", "#   #", "#### "),
    "E": ("#####", "#    ", "#    ", "#### ", "#    ", "#    ", "#####"),
    "H": ("#   #", "#   #", "#   #", "#####", "#   #", "#   #", "#   #"),
    "I": (" ### ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", " ### "),
    "K": ("#   #", "#  # ", "# #  ", "##   ", "# #  ", "#  # ", "#   #"),
    "L": ("#    ", "#    ", "#    ", "#    ", "#    ", "#    ", "#####"),
    "M": ("#   #", "## ##", "# # #", "# # #", "#   #", "#   #", "#   #"),
    "N": ("#   #", "##  #", "# # #", "#  ##", "#   #", "#   #", "#   #"),
    "O": (" ### ", "#   #", "#   #", "#   #", "#   #", "#   #", " ### "),
    "P": ("#### ", "#   #", "#   #", "#### ", "#    ", "#    ", "#    "),
    "R": ("#### ", "#   #", "#   #", "#### ", "# #  ", "#  # ", "#   #"),
    "S": (" ####", "#    ", "#    ", " ### ", "    #", "    #", "#### "),
    "T": ("#####", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  "),
    "U": ("#   #", "#   #", "#   #", "#   #", "#   #", "#   #", " ### "),
    "V": ("#   #", "#   #", "#   #", "#   #", "#   #", " # # ", "  #  "),
    "W": ("#   #", "#   #", "#   #", "# # #", "# # #", "## ##", "#   #"),
    "a": ("     ", "     ", " ### ", "    #", " ####", "#   #", " ####"),
    "b": ("#    ", "#    ", "#### ", "#   #", "#   #", "#   #", "#### "),
    "c": ("     ", "     ", " ### ", "#    ", "#    ", "#   #", " ### "),
    "d": ("    #", "    #", " ####", "#   #", "#   #", "#   #", " ####"),
    "e": ("     ", "     ", " ### ", "#   #", "#####", "#    ", " ### "),
    "f": ("  ## ", " #  #", " #   ", "#### ", " #   ", " #   ", " #   "),
    "g": ("     ", " ####", "#   #", "#   #", " ####", "    #", " ### "),
    "h": ("#    ", "#    ", "#### ", "#   #", "#   #", "#   #", "#   #"),
    "i": ("  #  ", "     ", " ##  ", "  #  ", "  #  ", "  #  ", " ### "),
    "k": ("#    ", "#    ", "#  # ", "# #  ", "##   ", "# #  ", "#  # "),
    "l": (" ##  ", "  #  ", "  #  ", "  #  ", "  #  ", "  #  ", " ### "),
    "m": ("     ", "     ", "## # ", "# # #", "# # #", "# # #", "# # #"),
    "n": ("     ", "     ", "#### ", "#   #", "#   #", "#   #", "#   #"),
    "o": ("     ", "     ", " ### ", "#   #", "#   #", "#   #", " ### "),
    "p": ("     ", "     ", "#### ", "#   #", "#### ", "#    ", "#    "),
    "r": ("     ", "     ", "# ## ", "##  #", "#    ", "#    ", "#    "),
    "s": ("     ", "     ", " ####", "#    ", " ### ", "    #", "#### "),
    "t": (" #   ", " #   ", "#####", " #   ", " #   ", " #  ", "  ## "),
    "u": ("     ", "     ", "#   #", "#   #", "#   #", "#  ##", " ## #"),
    "v": ("     ", "     ", "#   #", "#   #", "#   #", " # # ", "  #  "),
    "w": ("     ", "     ", "#   #", "#   #", "# # #", "# # #", " # # "),
    "x": ("     ", "     ", "#   #", " # # ", "  #  ", " # # ", "#   #"),
    "y": ("     ", "     ", "#   #", "#   #", " ####", "    #", " ### "),
    "z": ("     ", "     ", "#####", "   # ", "  #  ", " #   ", "#####"),
}
_GLYPHS = {
    ch: np.array([[c == "#" for c in row.ljust(5)[:5]] for row in rows], dtype=bool)
    for ch, rows in _FONT.items()
}
GLYPH_H = 7
GLYPH_ADVANCE = 6          # 5 columns plus one space


# ------------------------------------------------------------------ text

def _text_width(text: str, scale: int) -> int:
    return max(0, len(text) * GLYPH_ADVANCE - 1) * scale


def _text_mask(text: str, scale: int) -> np.ndarray:
    parts: List[np.ndarray] = []
    for ch in text:
        if parts:
            parts.append(np.zeros((GLYPH_H, 1), dtype=bool))
        parts.append(_GLYPHS.get(ch, _GLYPHS["?"]))
    if not parts:
        return np.zeros((GLYPH_H * scale, 0), dtype=bool)
    mask = np.hstack(parts)
    if scale > 1:
        mask = np.repeat(np.repeat(mask, scale, axis=0), scale, axis=1)
    return mask


def _draw_text(canvas: np.ndarray, x: int, y: int, text: str, color, scale: int,
               background=None) -> None:
    mask = _text_mask(text, scale)
    h, w = mask.shape
    if w == 0:
        return
    H, W = canvas.shape[:2]
    if background is not None:
        canvas[max(y - 1, 0):min(y + h + 1, H), max(x - 1, 0):min(x + w + 1, W)] = background
    x0, y0 = max(x, 0), max(y, 0)
    x1, y1 = min(x + w, W), min(y + h, H)
    if x0 >= x1 or y0 >= y1:
        return
    sub = mask[y0 - y:y1 - y, x0 - x:x1 - x]
    canvas[y0:y1, x0:x1][sub] = color


# ------------------------------------------------------------------ axes

def _decimals(step: float) -> int:
    dec = 0
    while dec < 12 and abs(step * 10 ** dec - round(step * 10 ** dec)) > 1e-6:
        dec += 1
    return dec


def _nice_ticks(lo: float, hi: float, count: int) -> Tuple[List[float], float]:
    """Tick values (multiples of a 1-2-2.5-5 step) inside [lo, hi], and the step."""
    raw = (hi - lo) / max(1, count)
    base = 10.0 ** math.floor(math.log10(raw))
    step = base * 10
    for mult in (1, 2, 2.5, 5, 10):
        step = mult * base
        if step >= raw * (1 - 1e-12):
            break
    first = math.ceil(lo / step - 1e-9)
    last = math.floor(hi / step + 1e-9)
    return [i * step for i in range(first, last + 1)], step


def _fmt(value: float, step: float) -> str:
    if abs(value) < abs(step) * 1e-9:
        return "0"
    if 1e-3 <= abs(value) < 1e6:
        return "{:.{}f}".format(value, _decimals(step))
    return "{:.2e}".format(value)


def _tint(color) -> Tuple[int, int, int]:
    """Light version of a quantity colour, for the min..max envelope."""
    return tuple(int(round(c * 0.4 + 255 * 0.6)) for c in color)


# ---------------------------------------------------------------- panels

@dataclass(frozen=True)
class _PanelLayout:
    """Vertical geometry and y ticks of one panel. The left margin is shared by all panels."""
    frame_top: int
    frame_bottom: int
    ph: int                                  # plot rows
    lo_v: float
    hi_v: float
    yticks: List[float]
    ylabels: List[str]
    label_w: int                             # widest y tick label, in pixels


def _panel_layout(top: int, width: int, height: int, y: np.ndarray, last: bool,
                  scale: int) -> _PanelLayout:
    ch = GLYPH_H * scale
    title_h = ch + 8
    bottom_h = 2 * ch + 13 if last else ch + 9
    frame_top = top + title_h
    frame_bottom = top + height - bottom_h
    ph = frame_bottom - frame_top - 1
    if ph < 8:
        raise ApiError("height is too small for the plot")

    lo, hi = float(y.min()), float(y.max())
    if hi - lo <= 0.0:                       # constant signal: give the axis some room
        d = abs(lo) * 0.1 if lo != 0.0 else 1.0
        lo, hi = lo - d, hi + d
    pad = 0.04 * (hi - lo)
    lo_v, hi_v = lo - pad, hi + pad

    yticks, ystep = _nice_ticks(lo_v, hi_v, max(2, min(6, ph // (2 * ch + 6))))
    ylabels = [_fmt(v, ystep) for v in yticks]
    label_w = max([_text_width(s, scale) for s in ylabels] + [0])
    return _PanelLayout(frame_top, frame_bottom, ph, lo_v, hi_v, yticks, ylabels, label_w)


def _draw_panel(canvas: np.ndarray, top: int, width: int, title: str,
                color, t: np.ndarray, y: np.ndarray, t0: float, t1: float,
                markers: Sequence[Tuple[int, float]], last: bool, scale: int,
                lay: _PanelLayout, pl: int) -> None:
    ch = GLYPH_H * scale
    frame_top, frame_bottom, ph = lay.frame_top, lay.frame_bottom, lay.ph
    lo_v, hi_v = lay.lo_v, lay.hi_v
    ylabels = lay.ylabels

    def row_of(values) -> np.ndarray:
        f = (hi_v - np.asarray(values, dtype=np.float64)) / (hi_v - lo_v) * (ph - 1)
        return frame_top + 1 + np.clip(np.rint(f), 0, ph - 1).astype(np.int64)

    fr = width - 9                           # right frame column
    pw = fr - pl - 1                         # plot columns

    # grid, frame
    yrows = [int(row_of([v])[0]) for v in lay.yticks]
    for r in yrows:
        canvas[r, pl + 1:fr] = GRID
    canvas[frame_top, pl:fr + 1] = FRAME
    canvas[frame_bottom, pl:fr + 1] = FRAME
    canvas[frame_top:frame_bottom + 1, pl] = FRAME
    canvas[frame_top:frame_bottom + 1, fr] = FRAME

    # the envelope and mean, one column of pixels per bin of samples
    span = t1 - t0
    edges = np.linspace(t0, t1, pw + 1)
    idx = np.searchsorted(t, edges, side="left")
    idx[-1] = np.searchsorted(t, t1, side="right")
    lo_i, hi_i = int(idx[0]), int(idx[-1])
    if hi_i > lo_i:
        yy = y[lo_i:hi_i]
        starts = idx[:-1] - lo_i
        ends = idx[1:] - lo_i
        cols = np.nonzero(ends > starts)[0]
        if cols.size:
            # reduceat over the starts of the non-empty bins: each segment ends where the
            # next non-empty bin starts, which is exact because bins are contiguous.
            st = starts[cols]
            mins = np.minimum.reduceat(yy, st)
            maxs = np.maximum.reduceat(yy, st)
            means = np.add.reduceat(yy, st) / (ends[cols] - starts[cols])
            xs = pl + 1 + cols
            r_top = row_of(maxs)
            r_bot = row_of(mins)
            r_mean = row_of(means)
            tint = np.array(_tint(color), dtype=np.uint8)
            for k in range(cols.size):
                canvas[r_top[k]:r_bot[k] + 1, xs[k]] = tint
            line = np.array(color, dtype=np.uint8)
            for k in range(cols.size):
                canvas[r_mean[k], xs[k]] = line
                if k + 1 < cols.size and cols[k + 1] == cols[k] + 1:
                    a, b = sorted((int(r_mean[k]), int(r_mean[k + 1])))
                    canvas[a:b + 1, xs[k]] = line

    # markers: dashed vertical lines with their ids
    for mid, mt in markers:
        if not (t0 <= mt <= t1):
            continue
        x = pl + 1 + int(round((mt - t0) / span * (pw - 1)))
        rows = np.arange(frame_top + 1, frame_bottom)
        rows = rows[((rows - frame_top) // 4) % 2 == 0]
        canvas[rows, x] = MARKER
        label = str(mid)
        lx = min(x + 3, fr - _text_width(label, scale) - 2)
        _draw_text(canvas, lx, frame_top + 3, label, MARKER, scale, background=WHITE)

    # axis labels
    for r, s in zip(yrows, ylabels):
        canvas[r, pl - 4:pl] = FRAME
        _draw_text(canvas, pl - 7 - _text_width(s, scale), r - ch // 2, s, TEXT, scale)
    xticks, xstep = _nice_ticks(t0, t1, max(2, min(8, pw // (GLYPH_ADVANCE * 9 * scale))))
    label_end = -10 ** 9
    for v in xticks:
        if v < t0 or v > t1:
            continue
        x = pl + 1 + int(round((v - t0) / span * (pw - 1)))
        canvas[frame_bottom + 1:frame_bottom + 4, x] = FRAME
        s = _fmt(v, xstep)
        start = x - _text_width(s, scale) // 2
        if start < label_end + GLYPH_ADVANCE * scale:
            continue                         # would touch the previous label
        _draw_text(canvas, start, frame_bottom + 5, s, TEXT, scale)
        label_end = start + _text_width(s, scale)
    _draw_text(canvas, pl, top + 4, title, TEXT, scale)
    if last:
        time_label = "Time [s]"
        _draw_text(canvas, pl + (pw - _text_width(time_label, scale)) // 2,
                   frame_bottom + 5 + ch + 4, time_label, TEXT, scale)


def render_rgb(series: Sequence[Tuple[np.ndarray, np.ndarray, str, str]], t0: float,
               t1: float, markers: Sequence[Tuple[int, float]], width: int,
               height: int) -> np.ndarray:
    """RGB pixels (uint8, rows x columns x 3) of the stacked panels.

    `series` holds (t, y, title, quantity name) with finite, sorted t and finite y. All
    panels share one left margin (the widest y tick label of any panel), so a time
    maps to the same column in every panel.
    """
    scale = 2 if width >= 600 and height >= 200 else 1
    last = len(series) - 1
    layouts = [_panel_layout(k * height, width, height, y, k == last, scale)
               for k, (_, y, _, _) in enumerate(series)]
    pl = max((lay.label_w for lay in layouts), default=0) + 9
    if (width - 9) - pl - 1 < 8:
        raise ApiError("width is too small for the plot")
    canvas = np.full((height * len(series), width, 3), 255, dtype=np.uint8)
    for k, (t, y, title, name) in enumerate(series):
        _draw_panel(canvas, k * height, width, title, COLORS[name], t, y, t0, t1,
                    markers, k == last, scale, layouts[k], pl)
    return canvas


# ------------------------------------------------------------------- PNG

def encode_png(rgb: np.ndarray) -> bytes:
    h, w, _ = rgb.shape
    raw = np.empty((h, 1 + w * 3), dtype=np.uint8)
    raw[:, 0] = 0                            # filter type 0 (none) for every row
    raw[:, 1:] = rgb.reshape(h, w * 3)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)     # 8-bit RGB
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw.tobytes(), 6)) + chunk(b"IEND", b""))


# ---------------------------------------------------------------- handler

def _render_tool(service, quantities, width: int, height: int, show_markers: bool,
                 range_args: dict) -> dict:
    names = list(quantities) if quantities is not None else ["power"]
    if not 1 <= len(names) <= 3:
        raise ApiError("quantities must have 1 to 3 items")
    if len(set(names)) != len(names):
        raise ApiError("quantities must not repeat")

    t0, t1, label, data = service.range_samples(dict(range_args))
    n = len(data)
    if n < 2:
        raise ApiError("not enough samples in range (need at least 2)")
    if not t1 > t0:
        t1 = t0 + 1.0
    t_all = np.asarray(data.t, dtype=np.float64)

    series = []
    per_quantity: Dict[str, dict] = {}
    for name in names:
        field, title = QUANTITY_FIELDS[name]
        y = np.asarray(getattr(data, field), dtype=np.float64)
        ok = np.isfinite(y) & np.isfinite(t_all)
        t, y = (t_all, y) if ok.all() else (t_all[ok], y[ok])
        if y.size == 0:
            raise ApiError("no finite {} values in range".format(name))
        per_quantity[name] = {"min": float(y.min()), "max": float(y.max()),
                              "mean": float(y.mean())}
        series.append((t, y, title, name))

    markers: List[Tuple[int, float]] = []
    if show_markers:
        listed = service.call("list_markers", {}).get("markers", [])
        markers = [(int(m["id"]), float(m["t"])) for m in listed]

    w, h = width, height
    while True:
        rgb = render_rgb(series, float(t0), float(t1), markers, w, h)
        png = encode_png(rgb)
        data_b64 = base64.b64encode(png).decode("ascii")
        if len(data_b64) <= MAX_BASE64_CHARS:
            break
        if w <= MIN_WIDTH and h <= MIN_HEIGHT:
            raise ApiError("the plot is too large to send: use a shorter range or fewer "
                           "quantities")
        w = max(MIN_WIDTH, int(w * 0.75))
        h = max(MIN_HEIGHT, int(h * 0.75))

    return {
        "image": {"mime_type": MIME_TYPE, "data_base64": data_b64},
        "width": int(rgb.shape[1]),
        "height": int(rgb.shape[0]),
        "range": {"t0": float(t0), "t1": float(t1), "label": label},
        "samples": n,
        "per_quantity": per_quantity,
    }


def handlers(service) -> Dict[str, Callable[..., dict]]:
    def render_plot(*, quantities: Optional[list] = None, width: int = 900, height: int = 360,
                    show_markers: bool = True, **range_args) -> dict:
        return _render_tool(service, quantities, width, height, show_markers, range_args)

    return {"render_plot": render_plot}
