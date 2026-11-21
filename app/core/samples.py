"""Columnar sample storage.

Measurements are kept as parallel numpy columns instead of one Python object
per sample: at ~1 kHz an hour of data is ~3.6 M samples, which as objects
would cost hundreds of MB and make every statistic an O(n) Python loop.

Columns:
    t     float64  seconds since the start of the recording (device clock when
                   available, so it is immune to USB/GUI scheduling jitter)
    wall  float64  POSIX timestamp (wall-clock time of the sample)
    v,i,p float32  voltage [V], current [A], power [W]
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Tuple

import numpy as np

_FLOAT_COLUMNS = ("v", "i", "p")


@dataclass(frozen=True)
class Samples:
    """An immutable view of a contiguous run of samples."""

    t: np.ndarray
    wall: np.ndarray
    v: np.ndarray
    i: np.ndarray
    p: np.ndarray

    @classmethod
    def empty(cls) -> "Samples":
        f64 = np.empty(0, dtype=np.float64)
        f32 = np.empty(0, dtype=np.float32)
        return cls(f64, f64.copy(), f32, f32.copy(), f32.copy())

    @classmethod
    def from_columns(cls, t, wall, v, i, p) -> "Samples":
        """Build from parallel arrays.

        A row in which a finite value overflows the float32 range (1e308 casts to
        inf) is dropped, so the columns stay aligned; such a value would poison
        every statistic. NaN and inf input is kept as given, as before.
        """
        v64 = np.asarray(v, dtype=np.float64)
        i64 = np.asarray(i, dtype=np.float64)
        p64 = np.asarray(p, dtype=np.float64)
        with np.errstate(over="ignore", invalid="ignore"):   # the overflow is the point
            v32 = v64.astype(np.float32)
            i32 = i64.astype(np.float32)
            p32 = p64.astype(np.float32)
        overflow = (np.isinf(v32) & np.isfinite(v64))
        overflow |= np.isinf(i32) & np.isfinite(i64)
        overflow |= np.isinf(p32) & np.isfinite(p64)
        t64 = np.asarray(t, dtype=np.float64)
        wall64 = np.asarray(wall, dtype=np.float64)
        if overflow.any():
            keep = ~overflow
            t64, wall64, v32, i32, p32 = (a[keep] for a in (t64, wall64, v32, i32, p32))
        return cls(t64, wall64, v32, i32, p32)

    def __len__(self) -> int:
        return int(self.t.shape[0])

    @property
    def duration(self) -> float:
        return float(self.t[-1] - self.t[0]) if len(self) >= 2 else 0.0

    def slice(self, start: int, stop: int) -> "Samples":
        return Samples(self.t[start:stop], self.wall[start:stop],
                       self.v[start:stop], self.i[start:stop], self.p[start:stop])

    def index_range(self, t0: float, t1: float) -> Tuple[int, int]:
        """Indices [start, stop) of samples with t0 <= t <= t1 (t is sorted)."""
        start = int(np.searchsorted(self.t, t0, side="left"))
        stop = int(np.searchsorted(self.t, t1, side="right"))
        return start, stop

    def between(self, t0: float, t1: float) -> "Samples":
        return self.slice(*self.index_range(min(t0, t1), max(t0, t1)))

    def copy(self) -> "Samples":
        """Detached copy (views of a SampleStore are overwritten on clear/reuse)."""
        return Samples(self.t.copy(), self.wall.copy(), self.v.copy(), self.i.copy(), self.p.copy())

    def concat(self, other: "Samples") -> "Samples":
        return Samples(*(np.concatenate((getattr(self, c), getattr(other, c)))
                         for c in ("t", "wall", "v", "i", "p")))

    def column(self, name: str) -> np.ndarray:
        """Return the 'voltage' / 'current' / 'power' (or v/i/p) column."""
        key = {"voltage": "v", "current": "i", "power": "p"}.get(name.lower(), name.lower())
        return getattr(self, key)

    def wall_datetime(self, index: int) -> Optional[datetime]:
        if not len(self):
            return None
        return datetime.fromtimestamp(float(self.wall[index]))


class SampleStore:
    """Append-only growable store with amortized O(1) appends.

    `view()` returns zero-copy views of the filled region; views stay valid
    until the next reallocation, so consumers must not hold them across
    appends for longer than one GUI frame.
    """

    INITIAL_CAPACITY = 65536

    def __init__(self) -> None:
        self._n = 0
        self._alloc(self.INITIAL_CAPACITY)

    def _alloc(self, capacity: int) -> None:
        old = getattr(self, "_cols", None)
        cols = {
            "t": np.empty(capacity, dtype=np.float64),
            "wall": np.empty(capacity, dtype=np.float64),
        }
        for c in _FLOAT_COLUMNS:
            cols[c] = np.empty(capacity, dtype=np.float32)
        if old is not None and self._n:
            for k in cols:
                cols[k][: self._n] = old[k][: self._n]
        self._cols = cols
        self._capacity = capacity

    def __len__(self) -> int:
        return self._n

    @property
    def is_empty(self) -> bool:
        return self._n == 0

    @property
    def nbytes(self) -> int:
        return sum(a.itemsize for a in self._cols.values()) * self._n

    def clear(self) -> None:
        self._n = 0
        if self._capacity > self.INITIAL_CAPACITY * 4:
            self._alloc(self.INITIAL_CAPACITY)

    def append(self, batch: Samples) -> None:
        m = len(batch)
        if not m:
            return
        need = self._n + m
        if need > self._capacity:
            cap = self._capacity
            while cap < need:
                cap *= 2
            self._alloc(cap)
        sl = slice(self._n, need)
        self._cols["t"][sl] = batch.t
        self._cols["wall"][sl] = batch.wall
        self._cols["v"][sl] = batch.v
        self._cols["i"][sl] = batch.i
        self._cols["p"][sl] = batch.p
        self._n = need

    def view(self) -> Samples:
        n = self._n
        c = self._cols
        return Samples(c["t"][:n], c["wall"][:n], c["v"][:n], c["i"][:n], c["p"][:n])

    def replace(self, samples: Samples) -> None:
        self.clear()
        self.append(samples)

    @property
    def last_t(self) -> float:
        return float(self._cols["t"][self._n - 1]) if self._n else 0.0
