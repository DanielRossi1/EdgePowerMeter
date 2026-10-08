"""Continuous recording to disk.

One visible CSV file per recording, in the same format as a manual export,
so it can be opened in a spreadsheet or imported back. Data is appended in
blocks (about once per second), therefore the file is always a valid CSV even
if the application or the PC stops abruptly. Markers are appended as
'#MARKER' lines; later lines for the same marker replace earlier ones, so the
file never has to be rewritten. No hidden or temporary files are created.
"""

from __future__ import annotations

import os
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from ..core.markers import Marker, MarkerList
from ..core.samples import Samples
from .csv_io import CsvFormat

# Recordings shorter than this (and without markers) are removed on close:
# accidental Start/Stop clicks must not leave clutter behind.
MIN_KEEP_SECONDS = 1.0
FSYNC_INTERVAL_S = 10.0


def user_home() -> Path:
    """The user's real home directory.

    Inside a strictly confined snap $HOME points to ~/snap/<name>/<revision>,
    a hidden folder that changes with every update; recordings must not end
    up there. SNAP_REAL_HOME is the actual home (writable through the
    `home` plug, which excludes hidden files only).
    """
    real = os.environ.get("SNAP_REAL_HOME")
    return Path(real) if real else Path.home()


_DOCUMENTS_NAMES = ("Documents", "Documenti", "Dokumente", "Documentos", "Documenten")


def _snap_documents_dir(home: Path) -> Optional[Path]:
    """XDG_DOCUMENTS_DIR from the copy of user-dirs.dirs the snap's desktop
    launcher keeps in $XDG_CONFIG_HOME, with the real paths already expanded
    (the original in the real ~/.config is a hidden file, not readable)."""
    config = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    try:
        lines = (Path(config) / "user-dirs.dirs").read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return None
    for line in lines:
        key, _, value = line.strip().partition("=")
        if key != "XDG_DOCUMENTS_DIR":
            continue
        value = value.strip().strip('"').replace("$HOME", str(home))
        path = Path(value)
        # Only a real folder inside the real home, and not the home itself
        # (that is what user-dirs uses for "no documents folder").
        if path.is_absolute() and path != home and home in path.parents and path.is_dir():
            return path
    return None


def default_folder() -> Path:
    """~/Documents/EdgePowerMeter (or ~/EdgePowerMeter without a Documents dir)."""
    if os.environ.get("SNAP_REAL_HOME"):
        home = user_home()
        docs = _snap_documents_dir(home)
        if docs is not None:
            return docs / "EdgePowerMeter"
        for name in _DOCUMENTS_NAMES:
            if (home / name).is_dir():
                return home / name / "EdgePowerMeter"
        return home / "EdgePowerMeter"
    try:
        from PySide6.QtCore import QStandardPaths
        docs = QStandardPaths.writableLocation(QStandardPaths.DocumentsLocation)
    except Exception:
        docs = ""
    base = Path(docs) if docs else Path.home()
    return base / "EdgePowerMeter"


def unique_path(folder: Path, stem: str, suffix: str = ".csv") -> Path:
    path = folder / f"{stem}{suffix}"
    k = 2
    while path.exists():
        path = folder / f"{stem}_{k}{suffix}"
        k += 1
    return path


class RecordingWriter:
    def __init__(self, folder: Path, fmt: CsvFormat, started: Optional[datetime] = None):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = (started or datetime.now()).strftime("%Y%m%d_%H%M%S")
        self.path = unique_path(folder, f"recording_{stamp}")
        self.fmt = fmt
        self._pending: List[Samples] = []
        self._pending_lines: List[str] = []
        self._f = open(self.path, "w", newline="", encoding="utf-8")
        self._f.write(fmt.header())
        self._f.flush()
        self._last_fsync = time.monotonic()
        self.samples_written = 0
        self.first_t: Optional[float] = None
        self.last_t: Optional[float] = None
        self.has_markers = False
        self.error: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self._f is not None

    def append(self, batch: Samples) -> None:
        if len(batch):
            self._pending.append(batch)

    def marker_changed(self, m: Marker) -> None:
        self.has_markers = True
        self._pending_lines.append(MarkerList.line(m))

    def marker_deleted(self, marker_id: int) -> None:
        self._pending_lines.append(MarkerList.delete_line(marker_id))

    def flush(self) -> None:
        """Write buffered samples and marker events. Errors (disk full,
        folder removed) are recorded in `error` instead of being raised."""
        if not (self._pending or self._pending_lines) or self.error:
            return
        try:
            f = self._f if self._f is not None else open(self.path, "a", newline="", encoding="utf-8")
            for b in self._pending:
                f.writelines(self.fmt.rows(b))
                self.samples_written += len(b)
                if self.first_t is None:
                    self.first_t = float(b.t[0])
                self.last_t = float(b.t[-1])
            for line in self._pending_lines:
                f.write(line + "\n")
            f.flush()
            if self._f is None:
                f.close()             # marker edited after the recording ended
            elif time.monotonic() - self._last_fsync > FSYNC_INTERVAL_S:
                os.fsync(f.fileno())
                self._last_fsync = time.monotonic()
        except OSError as e:
            self.error = str(e)
        finally:
            self._pending.clear()
            self._pending_lines.clear()

    def close(self) -> bool:
        """Finish the file. Returns False if it was removed as too short."""
        self.flush()
        if self._f is not None:
            try:
                os.fsync(self._f.fileno())
            except OSError:
                pass
            self._f.close()
            self._f = None
        duration = (self.last_t - self.first_t) if self.first_t is not None else 0.0
        if duration < MIN_KEEP_SECONDS and not self.has_markers:
            self.discard()
            return False
        return True

    def discard(self) -> None:
        if self._f is not None:
            self._f.close()
            self._f = None
        self._pending.clear()
        self._pending_lines.clear()
        try:
            self.path.unlink()
        except OSError:
            pass

    @property
    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0
