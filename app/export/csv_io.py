"""CSV export and import."""

from __future__ import annotations

import csv
import io
import logging
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, List, Optional, Tuple

import numpy as np

from ..core.markers import MARKER_PREFIX, MarkerList
from ..core.samples import Samples

ProgressFn = Optional[Callable[[float], None]]
log = logging.getLogger(__name__)

HEADER = ["Timestamp", "RelativeTime[s]", "Voltage[V]", "Current[A]", "Power[W]"]
EPOCH_HEADER = "UnixTime[s]"
# Larger files are refused before reading (a whole recording is held in memory).
IMPORT_MAX_BYTES = 256 * 1024 ** 2


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
    path = Path(path)
    fmt = CsvFormat(separator, decimal, time_format)
    n = len(s)
    chunk = 100_000   # format per chunk: whole-recording string arrays need ~13x the data
    # Write beside the target and replace it only when complete, so a failure
    # part way never destroys the previous file. mkstemp creates a fresh
    # random name with O_EXCL, so a file planted at a predictable name is never followed.
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.name + ".",
                                    suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
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
        os.replace(tmp, path)
    except BaseException:
        # Cleanup must not hide the original error.
        try:
            tmp.unlink()
        except (OSError, ValueError):
            pass
        raise


def _format_datetimes(wall: np.ndarray) -> np.ndarray:
    """POSIX seconds -> 'YYYY-MM-DD HH:MM:SS.mmm' (local time), vectorized
    over whole seconds so strftime runs once per second, not once per sample."""
    secs = np.floor(wall).astype(np.int64)
    ms = np.clip(np.round((wall - secs) * 1000).astype(np.int64), 0, 999)
    uniq, inverse = np.unique(secs, return_inverse=True)
    prefixes = []
    out_of_range = np.zeros(len(uniq), dtype=bool)
    for k, x in enumerate(uniq):
        try:
            prefixes.append(datetime.fromtimestamp(int(x)).strftime("%Y-%m-%d %H:%M:%S"))
        except (OverflowError, OSError, ValueError):
            prefixes.append("")
            out_of_range[k] = True
    ts = np.char.add(np.char.add(np.array(prefixes)[inverse], "."), np.char.zfill(ms.astype(str), 3))
    if out_of_range.any():
        # Wall time the platform cannot format: keep the row as epoch seconds
        # rather than failing the whole export.
        rows = out_of_range[inverse]
        cells = ts.tolist()
        for idx in np.flatnonzero(rows):
            cells[idx] = "%.6f" % wall[idx]
        ts = np.array(cells)
    return ts


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
        if not counts:
            continue
        expected = counts[0]
        # A crash can leave a shorter last line; it must not reject the file.
        body = counts[:-1] if len(counts) > 1 and counts[-1] < expected else counts
        if expected >= 3 and all(c == expected for c in body) and expected > best_score:
            best, best_score = sep, expected
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


_DST_HOUR_S = 3600.0


def _repair_fall_back(wall: np.ndarray, rel: np.ndarray) -> np.ndarray:
    """Rebuild the wall clock from relative time for a DST fall-back import.

    The clock correction between consecutive rows is the wall step minus the
    relative-time step. A fall-back shows up as a correction of about -3600 s.
    The rebuild is applied only when there is exactly one such step and no
    other correction (e.g. a clock sync that set the device clock back), since
    the rebuild would flatten that correction.
    """
    with np.errstate(over="ignore", invalid="ignore"):     # absurd timestamps overflow
        jump = np.diff(wall) - np.diff(rel)
        dst = np.abs(jump + _DST_HOUR_S) <= 2.0
        other = (np.abs(jump) > 1.0) & ~dst
    if int(dst.sum()) == 1 and not other.any():
        return wall[0] + (rel - rel[0])
    return wall


def _csv_rows(lines: List[str], sep: str):
    """csv.reader rows; malformed input (e.g. a field over the csv size limit, or NUL
    bytes before Python 3.11) becomes a ValueError with the line number."""
    reader = csv.reader(lines, delimiter=sep)
    try:
        for row in reader:
            yield row
    except csv.Error as exc:
        raise ValueError(f"malformed CSV at line {reader.line_num}: {exc}") from exc


def import_csv(path: Path, progress: ProgressFn = None) -> Samples:
    """Samples only (see import_recording for markers too)."""
    return import_recording(path, progress)[0]


def _open_regular(path: Path):
    """Open `path` for binary reading after checking it is a regular file of
    at most IMPORT_MAX_BYTES. FIFOs, device nodes and directories are refused
    before any read could block or run forever. Symlinks to regular files are followed."""
    st = os.stat(path)          # FileNotFoundError propagates
    if not stat.S_ISREG(st.st_mode):
        raise ValueError(f"not a regular file: {path}")
    if st.st_size > IMPORT_MAX_BYTES:
        raise ValueError("file too large to import (max 256 MiB)")
    # O_NONBLOCK: a FIFO swapped in after the check cannot block the open.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    try:
        fst = os.fstat(fd)
        if not stat.S_ISREG(fst.st_mode) or (fst.st_dev, fst.st_ino) != (st.st_dev, st.st_ino):
            raise ValueError(f"not a regular file: {path}")
        if fst.st_size > IMPORT_MAX_BYTES:
            raise ValueError("file too large to import (max 256 MiB)")
        return os.fdopen(fd, "rb")
    except BaseException:
        os.close(fd)
        raise


def _text_encoding(raw) -> str:
    """UTF-16 when the file starts with a byte order mark (Windows/Excel exports),
    otherwise UTF-8 (a UTF-8 BOM is dropped by 'utf-8-sig')."""
    head = raw.read(2)
    raw.seek(0)
    return "utf-16" if head in (b"\xff\xfe", b"\xfe\xff") else "utf-8-sig"


def import_recording(path: Path, progress: ProgressFn = None) -> Tuple[Samples, MarkerList]:
    """Read a CSV written by this app (any version) or by the firmware log.

    Accepted layouts: `Timestamp,RelativeTime,V,I,P` (app >= 1.6) and
    `Timestamp,V,I,P`. Separator and decimal comma are auto-detected.
    '#MARKER' lines restore the event markers. Rows that cannot be parsed are
    skipped and logged; use import_recording_ex to get their number.
    """
    samples, markers, skipped = import_recording_ex(path, progress)
    if skipped:
        log.warning("%s: %d unreadable row(s) skipped", path, skipped)
    return samples, markers


def import_recording_ex(path: Path, progress: ProgressFn = None) -> Tuple[Samples, MarkerList, int]:
    """Like import_recording, plus the number of data rows skipped as unparseable."""
    path = Path(path)
    markers = MarkerList()
    text_lines = []
    skipped = 0
    with _open_regular(path) as raw:
        # BOM-detected encoding: a byte order mark must not hide a '#' or '#MARKER' line.
        with io.TextIOWrapper(raw, encoding=_text_encoding(raw), errors="replace") as f:
            for ln in f:
                if ln.startswith(MARKER_PREFIX):
                    markers.apply_line(ln)
                # Skip comments and serial-log chatter (firmware banners, "!" replies).
                elif ln.strip() and not ln.lstrip().startswith(("#", "[", "=", "!")):
                    text_lines.append(ln.rstrip("\r\n"))
    if len(text_lines) < 2:
        raise ValueError("The file contains no data rows.")
    sep = detect_separator(text_lines[:5])
    rows = _csv_rows(text_lines, sep)
    header = next(rows)
    has_rel = len(header) >= 5 and "relative" in header[1].lower()

    wall: List[float] = []
    cal: List[float] = []
    rel: List[float] = []
    v: List[float] = []
    i: List[float] = []
    p: List[float] = []
    total = len(text_lines)
    min_len = 5 if has_rel else 4     # a truncated row must not shift the columns
    for k, row in enumerate(rows):
        if len(row) < min_len:
            skipped += 1
            continue
        try:
            w, c = _parse_time(row[0])
            if has_rel:
                r = _parse_float(row[1])
                vals = row[2:5]
            else:
                r = float("nan")
                vals = row[1:4]
            vv, ii, pp = (_parse_float(x) for x in vals)
        except ValueError:
            skipped += 1
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
    # rel_a is all NaN when the file has no relative-time column, so the time
    # check must use the column that is actually in use.
    ok = np.isfinite(v_a) & np.isfinite(i_a) & np.isfinite(p_a) & np.isfinite(wall_a)
    ok &= np.isfinite(rel_a) if has_rel else np.isfinite(cal_a)
    if not ok.all():
        skipped += int((~ok).sum())
        wall_a, cal_a, rel_a, v_a, i_a, p_a = (a[ok] for a in (wall_a, cal_a, rel_a, v_a, i_a, p_a))
    if not len(v_a):
        raise ValueError("No valid data rows found.")
    # Local-time strings are ambiguous in the repeated hour of a DST fall-back;
    # with a monotonic relative-time column the wall clock follows from it.
    with np.errstate(over="ignore", invalid="ignore"):
        monotonic = len(wall_a) > 1 and np.isfinite(rel_a).all() and bool(np.all(np.diff(rel_a) >= 0))
    if monotonic:
        wall_a = _repair_fall_back(wall_a, rel_a)
    # Prefer the precise relative-time column; derive it from timestamps otherwise.
    # (calendar seconds, not POSIX time: a DST change must not reorder rows)
    with np.errstate(over="ignore", invalid="ignore"):   # the subtraction can overflow
        t = rel_a if has_rel else cal_a - cal_a[0]
    fin = np.isfinite(t)
    if not fin.all():
        skipped += int((~fin).sum())
        wall_a, cal_a, v_a, i_a, p_a, t = (a[fin] for a in (wall_a, cal_a, v_a, i_a, p_a, t))
        if not len(t):
            raise ValueError("No valid data rows found.")
    order = np.argsort(t, kind="stable")
    offset = float(t[order][0])
    with np.errstate(over="ignore", invalid="ignore"):
        t = t[order] - offset
    # Times spanning more than the float64 range overflow the rebase.
    fin = np.isfinite(t)
    if not fin.all():
        skipped += int((~fin).sum())
        order, t = order[fin], t[fin]
        if not len(t):
            raise ValueError("No valid data rows found.")
    for m in markers.sorted():          # same rebase as the samples
        m.t -= offset
    samples = Samples.from_columns(t, wall_a[order], v_a[order], i_a[order], p_a[order])
    # Values finite as text but beyond the float32 range are dropped by from_columns.
    skipped += len(t) - len(samples)
    if not len(samples):
        raise ValueError("No valid data rows found.")
    return samples, markers, skipped
