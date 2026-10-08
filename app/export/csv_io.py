"""CSV export and import."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

import numpy as np

from ..core.markers import MARKER_PREFIX, MarkerList
from ..core.samples import Samples

ProgressFn = Optional[Callable[[float], None]]

HEADER = ["Timestamp", "RelativeTime[s]", "Voltage[V]", "Current[A]", "Power[W]"]
EPOCH_HEADER = "UnixTime[s]"


class CsvFormat:
    """Column layout shared by exports and live recordings."""

    def __init__(self, separator: str = ",", decimal: str = ".", time_format: str = "datetime"):
        # A decimal comma needs another separator to keep the file parseable.
        self.separator = ";" if decimal == "," and separator == "," else separator
        self.decimal = decimal
        self.time_format = time_format

    def header(self) -> str:
        cols = list(HEADER)
        if self.time_format == "epoch":
            cols[0] = EPOCH_HEADER
        return self.separator.join(cols) + "\n"

    def rows(self, part: Samples) -> Iterable[str]:
        rel = np.char.mod("%.6f", part.t)
        # %.9g keeps full INA226 resolution even with high-value shunts
        # (0.25 µA steps), where fixed 6 decimals would round to 1 µA.
        v = np.char.mod("%.9g", part.v.astype(np.float64))
        i = np.char.mod("%.9g", part.i.astype(np.float64))
        p = np.char.mod("%.9g", part.p.astype(np.float64))
        epoch = self.time_format == "epoch"
        ts = np.char.mod("%.6f", part.wall) if epoch else _format_datetimes(part.wall)
        if self.decimal == ",":
            rel, v, i, p = (np.char.replace(x, ".", ",") for x in (rel, v, i, p))
            if epoch:
                ts = np.char.replace(ts, ".", ",")
        sep = self.separator
        return (sep.join(r) + "\n" for r in zip(ts, rel, v, i, p))


def export_csv(path: Path, s: Samples, separator: str = ",", decimal: str = ".",
               time_format: str = "datetime", progress: ProgressFn = None,
               markers: Optional[MarkerList] = None) -> None:
    """Write samples (and markers, as trailing '#MARKER' lines) to CSV.

    `decimal=","` produces spreadsheet-friendly files for locales that use a
    decimal comma; in that case a comma separator is replaced by ';' so the
    file stays parseable.
    """
    fmt = CsvFormat(separator, decimal, time_format)
    n = len(s)
    chunk = 100_000   # format per chunk: whole-recording string arrays need ~13x the data
    with open(path, "w", newline="", encoding="utf-8") as f:
        f.write(fmt.header())
        for start in range(0, n, chunk):
            stop = min(n, start + chunk)
            f.writelines(fmt.rows(s.slice(start, stop)))
            if progress:
                progress(stop / n)
        if markers is not None and len(s):
            # Only markers inside the exported range, on its time axis.
            t0, t1 = float(s.t[0]), float(s.t[-1])
            for m in markers.sorted():
                if t0 <= m.t <= t1:
                    f.write(MarkerList.line(m) + "\n")


def _format_datetimes(wall: np.ndarray) -> np.ndarray:
    """POSIX seconds -> 'YYYY-MM-DD HH:MM:SS.mmm' (local time), vectorized
    over whole seconds so strftime runs once per second, not once per sample."""
    secs = np.floor(wall).astype(np.int64)
    ms = np.clip(np.round((wall - secs) * 1000).astype(np.int64), 0, 999)
    uniq, inverse = np.unique(secs, return_inverse=True)
    prefixes = np.array([datetime.fromtimestamp(int(x)).strftime("%Y-%m-%d %H:%M:%S")
                         for x in uniq])
    return np.char.add(np.char.add(prefixes[inverse], "."), np.char.zfill(ms.astype(str), 3))


# ----------------------------------------------------------------- import

_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
    "%d-%m-%Y %H:%M:%S.%f",
    "%d-%m-%Y %H:%M:%S",
    "%d/%m/%Y %H:%M:%S.%f",
    "%d/%m/%Y %H:%M:%S",
)


def detect_separator(lines: List[str]) -> str:
    best, best_score = None, 0
    for sep in (";", "\t", ",", " "):
        counts = [ln.count(sep) for ln in lines]
        if counts and min(counts) >= 3 and max(counts) == min(counts) and counts[0] > best_score:
            best, best_score = sep, counts[0]
    if best is None:
        raise ValueError("Could not detect the CSV separator (comma, semicolon, tab or space).")
    return best


def _parse_float(text: str) -> float:
    return float(text.strip().replace(",", "."))


_EPOCH = datetime(1970, 1, 1)


def _parse_time(text: str) -> tuple:
    """-> (POSIX wall time, monotonic calendar seconds without DST rules)."""
    text = text.strip()
    try:
        x = float(text.replace(",", "."))        # epoch seconds
        return x, x
    except ValueError:
        pass
    for fmt in _TS_FORMATS:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return dt.timestamp(), (dt - _EPOCH).total_seconds()
    raise ValueError(f"Unrecognized timestamp: {text}")


def import_csv(path: Path, progress: ProgressFn = None) -> Samples:
    """Samples only (see import_recording for markers too)."""
    return import_recording(path, progress)[0]


def import_recording(path: Path, progress: ProgressFn = None) -> Tuple[Samples, MarkerList]:
    """Read a CSV written by this app (any version) or by the firmware log.

    Accepted layouts: `Timestamp,RelativeTime,V,I,P` (app >= 1.6) and
    `Timestamp,V,I,P`. Separator and decimal comma are auto-detected.
    '#MARKER' lines restore the event markers.
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    markers = MarkerList()
    text_lines = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for ln in f:
            if ln.startswith(MARKER_PREFIX):
                markers.apply_line(ln)
            # Skip comments and serial-log chatter (firmware banners, "!" replies).
            elif ln.strip() and not ln.lstrip().startswith(("#", "[", "=", "!")):
                text_lines.append(ln.rstrip("\r\n"))
    if len(text_lines) < 2:
        raise ValueError("The file contains no data rows.")
    sep = detect_separator(text_lines[:5])
    rows = csv.reader(text_lines, delimiter=sep)
    header = next(rows)
    has_rel = len(header) >= 5 and "relative" in header[1].lower()

    wall: List[float] = []
    cal: List[float] = []
    rel: List[float] = []
    v: List[float] = []
    i: List[float] = []
    p: List[float] = []
    total = len(text_lines)
    for k, row in enumerate(rows):
        if len(row) < 4:
            continue
        try:
            w, c = _parse_time(row[0])
            if has_rel and len(row) >= 5:
                r = _parse_float(row[1])
                vals = row[2:5]
            else:
                r = float("nan")
                vals = row[1:4]
            vv, ii, pp = (_parse_float(x) for x in vals)
        except ValueError:
            continue
        wall.append(w)
        cal.append(c)
        rel.append(r)
        v.append(vv)
        i.append(ii)
        p.append(pp)
        if progress and k % 50_000 == 0:
            progress(k / total)

    if not v:
        raise ValueError("No valid data rows found.")
    wall_a = np.asarray(wall)
    cal_a = np.asarray(cal)
    rel_a = np.asarray(rel)
    v_a, i_a, p_a = np.asarray(v), np.asarray(i), np.asarray(p)
    # Rows with NaN/inf values (e.g. "nan" cells) would poison every statistic.
    ok = np.isfinite(v_a) & np.isfinite(i_a) & np.isfinite(p_a) & np.isfinite(wall_a)
    if not ok.all():
        wall_a, cal_a, rel_a, v_a, i_a, p_a = (a[ok] for a in (wall_a, cal_a, rel_a, v_a, i_a, p_a))
        if not len(v_a):
            raise ValueError("No valid data rows found.")
    # Prefer the precise relative-time column; derive it from timestamps otherwise.
    # (calendar seconds, not POSIX time: a DST change must not reorder rows)
    t = rel_a if not np.isnan(rel_a).any() else cal_a - cal_a[0]
    order = np.argsort(t, kind="stable")
    offset = float(t[order][0])
    t = t[order] - offset
    for m in markers.sorted():          # same rebase as the samples
        m.t -= offset
    return Samples.from_columns(t, wall_a[order], v_a[order], i_a[order], p_a[order]), markers
