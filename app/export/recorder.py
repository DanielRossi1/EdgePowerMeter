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
import stat
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


def create_unique_file(folder: Path, stem: str, suffix: str = ".csv"):
    """Create a new file (never overwriting one) -> (path, open file).

    Creating with mode "x" makes the check and the creation atomic, so two
    instances starting in the same second get different names."""
    k = 1
    while True:
        name = f"{stem}{suffix}" if k == 1 else f"{stem}_{k}{suffix}"
        path = folder / name
        try:
            return path, open(path, "x", newline="", encoding="utf-8")
        except FileExistsError:
            k += 1


def _identity(st: os.stat_result):
    return (st.st_dev, st.st_ino)


class RecordingWriter:
    def __init__(self, folder: Path, fmt: CsvFormat, started: Optional[datetime] = None):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        stamp = (started or datetime.now()).strftime("%Y%m%d_%H%M%S")
        self.path, self._f = create_unique_file(folder, f"recording_{stamp}")
        self.fmt = fmt
        self._pending: List[Samples] = []
        self._pending_lines: List[str] = []
        self._f.write(fmt.header())
        self._f.flush()
        # Identity of the file we created: a closed recording is reopened for
        # late marker edits only if the path still names this very file.
        self._ident = _identity(os.fstat(self._f.fileno()))
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

    def _open_for_append(self):
        """Reopen a closed recording for appending, but only if the path is
        still the regular file this writer created (not a symlink or a replacement)."""
        lst = os.lstat(self.path)          # does not follow a symlink
        if not stat.S_ISREG(lst.st_mode) or _identity(lst) != self._ident:
            raise OSError(f"recording file was replaced; not writing to it: {self.path}")
        flags = os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags)
        try:
            fst = os.fstat(fd)
            if not stat.S_ISREG(fst.st_mode) or _identity(fst) != self._ident:
                raise OSError(f"recording file was replaced; not writing to it: {self.path}")
            return os.fdopen(fd, "a", newline="", encoding="utf-8")
        except BaseException:
            os.close(fd)
            raise

    def marker_deleted(self, marker_id: int) -> None:
        self._pending_lines.append(MarkerList.delete_line(marker_id))

    def _removed(self) -> bool:
        """True if the file was deleted: writes would go to an orphaned inode
        (open file) or silently recreate a file without its header (closed)."""
        if not self.path.exists():
            return True
        return self._f is not None and os.fstat(self._f.fileno()).st_nlink == 0

    def flush(self) -> None:
        """Write buffered samples and marker events. Errors (disk full,
        folder removed, file deleted) are recorded in `error` instead of being raised."""
        if not (self._pending or self._pending_lines) or self.error:
            return
        try:
            if self._removed():
                raise OSError(f"recording file was deleted while recording: {self.path}")
            f = self._f if self._f is not None else self._open_for_append()
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
        except (OSError, ValueError) as e:      # UnicodeError is a ValueError
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
